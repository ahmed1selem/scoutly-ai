"""
Tests for budget sanitisation.

Context: the financial agent used to read the target club off a `self.target_club`
attribute that nothing ever set, so every club received an identical budget from
the generic default plan and the progress line always read "the target club".
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agents.financial import sanitise_budget, _MAX_PLAUSIBLE_WEEKLY


# ── weekly-vs-annual confusion ────────────────────────────────────────────────

def test_annual_salary_max_is_reinterpreted_as_weekly():
    """
    An annual figure in salary_max makes every candidate look affordable, so the
    budget filter silently stops filtering.
    """
    out = sanitise_budget({"salary_max": 5_200_000})
    assert out["salary_max"] == 100_000
    assert any("annual" in f for f in out["risk_flags"])


def test_absurd_salary_max_is_capped_and_flagged():
    out = sanitise_budget({"salary_max": 900_000_000})
    assert out["salary_max"] == _MAX_PLAUSIBLE_WEEKLY
    assert any("capped" in f for f in out["risk_flags"])


@pytest.mark.parametrize("value", [10_000, 120_000, 300_000, _MAX_PLAUSIBLE_WEEKLY])
def test_plausible_weekly_wages_pass_through_untouched(value):
    out = sanitise_budget({"salary_max": value})
    assert out["salary_max"] == value
    assert out["risk_flags"] == []


# ── an inverted range excludes everyone ───────────────────────────────────────

def test_salary_min_above_max_is_removed():
    out = sanitise_budget({"salary_min": 250_000, "salary_max": 120_000})
    assert out["salary_min"] == 0
    assert any("salary_min" in f for f in out["risk_flags"])


def test_valid_range_is_preserved():
    out = sanitise_budget({"salary_min": 40_000, "salary_max": 120_000})
    assert (out["salary_min"], out["salary_max"]) == (40_000, 120_000)


# ── provenance of the numbers ─────────────────────────────────────────────────

def test_club_is_recorded_on_the_budget():
    out = sanitise_budget({"salary_max": 100_000}, target_club="Liverpool")
    assert out["team_name"] == "Liverpool"


def test_missing_club_does_not_produce_an_empty_name():
    assert sanitise_budget({"salary_max": 100_000})["team_name"] == "Target Club"


@pytest.mark.parametrize("query", [
    "Find a centre-back on a budget of 40m",
    "Find a striker under 100k a week",
    "Cheap left-back please",
    "a modest budget signing",
])
def test_stated_constraints_are_credited_to_the_brief(query):
    out = sanitise_budget({"salary_max": 100_000}, query=query)
    assert out["budget_basis"] == "stated in the brief"


def test_unstated_budget_is_labelled_as_a_default_not_the_club_s_finances():
    """
    The generic plan is not any club's real budget. Saying so keeps the report
    from presenting a default as an audited figure.
    """
    out = sanitise_budget({"salary_max": 100_000}, query="Find a strong centre-back")
    assert "not the club's audited budget" in out["budget_basis"]


# ── robustness ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {}, {"salary_max": None}, {"salary_max": "lots"}, {"salary_max": True},
    {"salary_min": None, "salary_max": 100_000},
])
def test_malformed_payloads_do_not_raise(payload):
    out = sanitise_budget(dict(payload))
    assert "budget_basis" in out and "team_name" in out


def test_existing_risk_flags_are_kept():
    out = sanitise_budget({"salary_max": 5_200_000, "risk_flags": ["wage bill near cap"]})
    assert "wage bill near cap" in out["risk_flags"]
    assert len(out["risk_flags"]) == 2


# ── deal economics: fees costed against the budget ────────────────────────────

from datetime import datetime, timedelta            # noqa: E402
from backend.agents.financial import assess_deal_economics, _years_until  # noqa: E402

BUDGET = {"salary_max": 100_000, "value_max": 60_000_000}


def _player(**kw):
    base = {"name": "P", "wage_eur": 50_000, "value_eur": 40_000_000}
    base.update(kw)
    return base


def test_affordable_candidate_is_marked_within_budget():
    p = _player()
    assess_deal_economics([p], BUDGET)
    assert p["within_fee_budget"] and p["within_wage_budget"]
    assert p["economic_verdict"] == "Within budget on both fee and wage"


def test_fee_over_ceiling_is_identified_separately_from_wage():
    p = _player(value_eur=220_000_000)
    assess_deal_economics([p], BUDGET)
    assert p["within_fee_budget"] is False
    assert p["within_wage_budget"] is True
    assert "fee above ceiling" in p["economic_verdict"]


def test_wage_over_ceiling_is_identified_separately_from_fee():
    p = _player(wage_eur=250_000)
    assess_deal_economics([p], BUDGET)
    assert p["within_wage_budget"] is False
    assert "wage above ceiling" in p["economic_verdict"]


def test_both_over_ceiling():
    p = _player(wage_eur=250_000, value_eur=220_000_000)
    assess_deal_economics([p], BUDGET)
    assert p["economic_verdict"] == "Outside budget on both fee and wage"


def test_expiring_contract_is_noted_as_negotiating_leverage():
    """A fee above the ceiling matters less when the contract is nearly up."""
    soon = (datetime.now() + timedelta(days=300)).strftime("%d/%m/%Y")
    p = _player(value_eur=220_000_000, contract_expires=soon)
    assess_deal_economics([p], BUDGET)
    assert "negotiable" in p["economic_verdict"]


def test_annual_wage_is_the_weekly_figure_times_52():
    p = _player(wage_eur=50_000)
    assess_deal_economics([p], BUDGET)
    assert p["wage_annual_eur"] == 2_600_000


def test_fee_is_amortised_over_the_assumed_contract():
    p = _player(value_eur=40_000_000)
    assess_deal_economics([p], BUDGET)
    assert p["amortised_fee_annual_eur"] == 40_000_000 // p["deal_years_assumed"]


def test_both_cash_and_book_views_are_produced():
    p = _player(wage_eur=50_000, value_eur=40_000_000)
    assess_deal_economics([p], BUDGET)
    assert p["first_year_cash_eur"] == 40_000_000 + 2_600_000
    assert p["annual_book_cost_eur"] == 10_000_000 + 2_600_000
    assert p["annual_book_cost_eur"] < p["first_year_cash_eur"]


def test_unvalued_player_is_not_costed_as_free():
    """No published valuation must not read as a EUR 0 fee."""
    p = _player(value_eur=None)
    assess_deal_economics([p], BUDGET)
    assert p["fee_eur"] is None
    assert p["within_fee_budget"] is None
    assert "no published valuation" in p["fee_basis"]


def test_fee_is_labelled_as_a_valuation_not_an_asking_price():
    p = _player()
    assess_deal_economics([p], BUDGET)
    assert "not a quoted asking price" in p["fee_basis"]


def test_summary_counts_only_affordable_candidates():
    players = [_player(name="cheap", value_eur=10_000_000),
               _player(name="dear", value_eur=200_000_000)]
    summary = assess_deal_economics(players, BUDGET)
    assert summary["candidates_within_budget"] == 1
    assert summary["cheapest_affordable"] == "cheap"
    assert summary["total_fee_if_all_signed_eur"] == 210_000_000


def test_empty_shortlist_does_not_raise():
    assert assess_deal_economics([], BUDGET)["candidates_costed"] == 0


@pytest.mark.parametrize("raw,expected_none", [
    (None, True), ("", True), ("not a date", True), ("30/06/2030", False),
])
def test_contract_date_parsing(raw, expected_none):
    assert (_years_until(raw) is None) is expected_none


# ── contract-aware fee ────────────────────────────────────────────────────────
# A Transfermarkt valuation is what a player is worth, not what a club will
# accept. Gating on the raw figure hid exactly the deals clubs do: Jonathan Tah
# was valued at EUR 28M and joined Bayern for nothing when his contract expired,
# and Barcelona paid Joan Garcia's EUR 25M release clause against a EUR 45M
# valuation.

from datetime import datetime, timedelta

from backend.agents.financial import (
    effective_fee_eur, assess_deal_economics, _FEE_DISCOUNT_HORIZON_YEARS,
)


def _expiry_in(years: float) -> str:
    return (datetime.now() + timedelta(days=int(years * 365.25))).strftime("%d/%m/%Y")


def test_expired_contract_costs_nothing():
    fee, note = effective_fee_eur(28_000_000, _expiry_in(-0.1))
    assert fee == 0
    assert "free transfer" in note


def test_long_contract_pays_the_full_valuation():
    fee, _ = effective_fee_eur(28_000_000, _expiry_in(4))
    assert fee == 28_000_000


def test_fee_decays_toward_expiry():
    far = effective_fee_eur(30_000_000, _expiry_in(2.5))[0]
    near = effective_fee_eur(30_000_000, _expiry_in(0.5))[0]
    assert 0 < near < far < 30_000_000


def test_one_year_left_is_a_third_of_the_horizon():
    fee, _ = effective_fee_eur(30_000_000, _expiry_in(1))
    expected = 30_000_000 / _FEE_DISCOUNT_HORIZON_YEARS
    assert abs(fee - expected) / expected < 0.02


def test_unknown_expiry_is_not_treated_as_a_bargain():
    """An unknown contract is not evidence of a cheap one."""
    fee, note = effective_fee_eur(28_000_000, None)
    assert fee == 28_000_000
    assert "unknown" in note


def test_no_valuation_returns_none():
    assert effective_fee_eur(None, _expiry_in(1))[0] is None
    assert effective_fee_eur(0, _expiry_in(1))[0] is None


def test_a_free_agent_is_within_any_fee_budget():
    """
    fee == 0 and fee is None are different states. Testing truthiness reported
    the cheapest possible signing as impossible to cost.
    """
    players = [{"name": "Tah", "value_eur": 28_000_000,
                "contract_expires": _expiry_in(-0.1), "wage_eur": 150_000}]
    assess_deal_economics(players, {"value_max": 25_000_000, "salary_max": 200_000})
    p = players[0]
    assert p["fee_eur"] == 0
    assert p["within_fee_budget"] is True
    assert p["economic_verdict"] == "Within budget on both fee and wage"


def test_costing_agrees_with_the_gate_for_a_running_down_contract():
    """
    The scouter filters on the discounted fee, so costing the raw valuation
    reported a candidate as over budget immediately after admitting them.
    """
    players = [{"name": "Nearly out", "value_eur": 45_000_000,
                "contract_expires": _expiry_in(1), "wage_eur": 80_000}]
    summary = assess_deal_economics(players, {"value_max": 20_000_000,
                                              "salary_max": 100_000})
    assert players[0]["within_fee_budget"] is True
    assert summary["candidates_within_budget"] == 1
    assert players[0]["market_value_eur"] == 45_000_000, "raw valuation still reported"


def test_a_player_on_a_long_deal_is_still_gated():
    players = [{"name": "Expensive", "value_eur": 90_000_000,
                "contract_expires": _expiry_in(4), "wage_eur": 80_000}]
    assess_deal_economics(players, {"value_max": 20_000_000, "salary_max": 100_000})
    assert players[0]["within_fee_budget"] is False


# ── the wage ceiling allows for the raise a signing is given ─────────────────

from backend.agents.financial import _WAGE_CEILING_MULTIPLIER


def test_ceiling_sits_above_the_clubs_current_top_earner():
    """
    A club that could only ever match its existing top earner could never sign
    anyone better paid, which is not how recruitment works.
    """
    assert _WAGE_CEILING_MULTIPLIER > 1.0


def test_multiplier_stays_within_the_measured_range():
    """
    Measured against 18 genuine summer-2025 signings, ceilings from 1.0x to 1.5x
    admit exactly the same 16. The multiplier corrects for those wages being the
    player's OLD ones; it is not licence to keep widening the gate. Covering the
    real structure-breaks (Modric 1.63x, Grealish 2.31x) needs 2.5x, at which
    point the ceiling means nothing.
    """
    assert 1.2 <= _WAGE_CEILING_MULTIPLIER <= 1.5
