"""
Tests for the wage and market-value sources.

The bug these guard against: a figure lifted out of search-result prose was
accepted as fact whenever it exceeded EUR 10,000, which put two different
players on an identical EUR 295,000/wk taken from a "highest paid" listicle and
labelled it wage_estimated=False.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend" / "mcp_server"))

from backend.agents.scouter import _apply_web_wage
from transfermarkt_lookup import _parse_market_value, _fold, _clean_field
from server import _estimate_wage


# ── web-sourced wages must be validated and never claimed as fact ─────────────

TRUSTED = "https://www.capology.com/player/x/"


def test_annual_figure_is_converted_to_weekly():
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": 5_200_000, "wage_basis": "annual",
                        "source_url": TRUSTED})
    assert p["wage_eur"] == 100_000


def test_weekly_figure_is_taken_as_is():
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": 120_000, "wage_basis": "weekly",
                        "source_url": TRUSTED})
    assert p["wage_eur"] == 120_000


def test_web_wage_is_always_marked_estimated():
    """Only the offline Capology cache is authoritative; a page is a cross-check."""
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": 90_000, "wage_basis": "weekly",
                        "source_url": TRUSTED})
    assert p["wage_estimated"] is True
    assert p["wage_source"] == "capology.com"


@pytest.mark.parametrize("url", [
    "https://www.bbc.co.uk/sport/football/12345",
    "https://en.wikipedia.org/wiki/Player",
    "https://somerandomblog.example/top-10-highest-paid",
])
def test_wage_from_an_untrusted_site_is_rejected(url):
    """
    General news prose produced plausible-but-wrong figures that displaced the
    honest modelled band, so only wage databases are accepted.
    """
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": 90_000, "wage_basis": "weekly",
                        "source_url": url})
    assert "wage_eur" not in p


def test_missing_basis_is_rejected():
    """Without a stated basis a yearly figure is indistinguishable from a weekly one."""
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": 200_000, "wage_basis": None,
                        "source_url": "https://example.com/x"})
    assert "wage_eur" not in p


def test_uncited_figure_is_rejected():
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": 90_000, "wage_basis": "weekly", "source_url": None})
    assert "wage_eur" not in p


@pytest.mark.parametrize("amount", [500, 1_500, 900_000, 50_000_000])
def test_implausible_weekly_wages_are_rejected(amount):
    """A transfer fee or squad total parsed as a wage must not reach the report."""
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": amount, "wage_basis": "weekly",
                        "source_url": TRUSTED})
    assert "wage_eur" not in p


@pytest.mark.parametrize("bad", [None, 0, -5, "lots"])
def test_non_numeric_amounts_are_rejected(bad):
    p = {}
    _apply_web_wage(p, {"wage_amount_eur": bad, "wage_basis": "weekly",
                        "source_url": TRUSTED})
    assert "wage_eur" not in p


# ── estimated wages must differentiate between players ────────────────────────

def test_estimates_differentiate_by_age_and_minutes():
    """
    A flat three-bucket tier gave every Premier League starter the identical
    figure, which read as a precise fact repeated across the shortlist.
    """
    peak = _estimate_wage("Premier League", 3420, 27)
    youth = _estimate_wage("Premier League", 3420, 20)
    veteran = _estimate_wage("Premier League", 3420, 35)
    assert youth < peak
    assert veteran < peak
    assert len({peak, youth, veteran}) == 3


def test_estimates_scale_with_minutes():
    assert _estimate_wage("Premier League", 3420, 27) > _estimate_wage("Premier League", 1850, 27)


def test_estimates_respect_league_tier():
    assert _estimate_wage("Premier League", 3000, 27) > _estimate_wage("Ligue 1", 3000, 27)


def test_estimates_are_rounded_so_they_do_not_look_exact():
    for mins in (1900, 2400, 3000, 3420):
        assert _estimate_wage("Premier League", mins, 27) % 500 == 0


def test_estimate_never_returns_zero():
    assert _estimate_wage("", 0, 0) > 0


# ── Transfermarkt value parsing ───────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("€70.00m", 70_000_000),
    ("€850k", 850_000),
    ("€1.20bn", 1_200_000_000),
    ("€45.00m Last update: 03/06/2026", 45_000_000),
])
def test_market_value_parsing(text, expected):
    assert _parse_market_value(text) == expected


@pytest.mark.parametrize("text", ["-", "", None, "n/a"])
def test_unpublished_market_value_is_none_not_zero(text):
    """A player with no published value must not be rendered as worthless."""
    assert _parse_market_value(text) is None


def test_name_folding_ignores_accents():
    assert _fold("Joško Gvardiol") == _fold("Josko Gvardiol")


# ── placeholder fields must not masquerade as data ────────────────────────────

@pytest.mark.parametrize("raw", ["-", "–", "—", "?", "N/A", "na", "unknown", "", "   "])
def test_placeholder_contract_dates_become_none(raw):
    """
    A bare "-" in Transfermarkt's Contract row read as an expiry date in the
    report, so a player with no known contract looked contracted.
    """
    assert _clean_field(raw) is None


@pytest.mark.parametrize("raw", ["30/06/2030", "Manchester City", "left"])
def test_real_field_values_survive_cleaning(raw):
    assert _clean_field(raw) == raw


# ── wage modelled from market value ───────────────────────────────────────────
# The league-tier model put every Premier League starter near EUR 55,000/wk.
# Joško Gvardiol's reported range is GBP 200,000-250,000, so the tier model was
# out by roughly a factor of five for a top-valued player.

from server import estimate_wage_from_value  # noqa: E402


@pytest.mark.parametrize("value_eur,league,low_target,high_target", [
    # (valuation, league, published weekly range in EUR, roughly)
    (70_000_000,  "Premier League", 235_000, 295_000),   # Gvardiol ~GBP 200-250k
    (220_000_000, "Premier League", 400_000, 480_000),   # Haaland ~GBP 375k
    (13_000_000,  "Premier League",  60_000, 100_000),   # Kilman ~GBP 70k
])
def test_estimate_brackets_published_wage(value_eur, league, low_target, high_target):
    """The modelled band must overlap the published range, not sit beside it."""
    low, mid, high = estimate_wage_from_value(value_eur, league)
    assert low <= high_target and high >= low_target, (
        f"band {low:,}-{high:,} does not overlap {low_target:,}-{high_target:,}"
    )


def test_wage_rises_with_value_but_sub_linearly():
    """Trebling a fee does not treble the wage."""
    _, small, _ = estimate_wage_from_value(30_000_000, "Premier League")
    _, large, _ = estimate_wage_from_value(90_000_000, "Premier League")
    assert large > small
    assert large < small * 3


@pytest.mark.parametrize("league", ["La Liga", "Serie A", "Bundesliga", "Ligue 1"])
def test_other_leagues_pay_less_than_the_premier_league(league):
    _, pl, _ = estimate_wage_from_value(70_000_000, "Premier League")
    _, other, _ = estimate_wage_from_value(70_000_000, league)
    assert other < pl


def test_band_straddles_the_midpoint():
    low, mid, high = estimate_wage_from_value(50_000_000, "Premier League")
    assert low < mid < high


def test_no_valuation_yields_zeros_so_callers_can_fall_back():
    """Must not invent a wage when there is nothing to model from."""
    assert estimate_wage_from_value(0, "Premier League") == (0, 0, 0)
    assert estimate_wage_from_value(None, "Premier League") == (0, 0, 0)


def test_estimates_are_rounded_so_they_do_not_look_contractual():
    for value in (12_000_000, 45_000_000, 150_000_000):
        low, mid, high = estimate_wage_from_value(value, "Premier League")
        for x in (low, mid, high):
            assert x % 1_000 == 0


def test_unknown_league_still_produces_a_usable_band():
    low, mid, high = estimate_wage_from_value(40_000_000, "Eredivisie")
    assert 0 < low < mid < high


# ── identity verification for market-value lookups ────────────────────────────
# A valuation for the wrong player is worse than none: it flows into the wage
# estimate and then passes the budget filter as "cheap". Three gates guard it.

from transfermarkt_lookup import _verify_profile, _MIN_PLAUSIBLE_VALUE_EUR  # noqa: E402

GOOD = {"name": "Gvardiol", "current_club": "Manchester City",
        "age": 24, "market_value_eur": 70_000_000}


def test_matching_profile_verifies():
    assert _verify_profile(GOOD, "Joško Gvardiol", "Manchester City", 22) is None


def test_wrong_surname_is_rejected():
    profile = dict(GOOD, name="Haaland")
    assert _verify_profile(profile, "Joško Gvardiol", "Manchester City", 22)


def test_namesake_in_a_lower_division_is_rejected_on_value():
    """
    "Luis Díaz" resolved to one at LD Alajuelense worth €550k. Name and age both
    passed, so the value floor is what catches it.
    """
    profile = {"name": "Díaz", "current_club": "LD Alajuelense",
               "age": 27, "market_value_eur": 550_000}
    reason = _verify_profile(profile, "Luis Díaz", "Liverpool", 28)
    assert reason and "too low" in reason


@pytest.mark.parametrize("value", [0, 1, 500_000, _MIN_PLAUSIBLE_VALUE_EUR - 1])
def test_implausibly_low_valuations_are_rejected(value):
    profile = dict(GOOD, market_value_eur=value)
    assert _verify_profile(profile, "Joško Gvardiol", "Manchester City", 22)


def test_transferred_player_is_accepted_when_the_age_agrees():
    """
    The cohort is a past season but Transfermarkt shows the club today, so anyone
    who moved would be wrongly rejected on club alone — Marc Guéhi reads as
    Manchester City now, not Crystal Palace.
    """
    profile = {"name": "Guéhi", "current_club": "Manchester City",
               "age": 26, "market_value_eur": 70_000_000}
    assert _verify_profile(profile, "Marc Guéhi", "Crystal Palace", 24) is None


def test_club_mismatch_with_a_wildly_different_age_is_rejected():
    profile = {"name": "Guéhi", "current_club": "Some Other Club",
               "age": 41, "market_value_eur": 70_000_000}
    reason = _verify_profile(profile, "Marc Guéhi", "Crystal Palace", 24)
    assert reason and "age" in reason


def test_club_mismatch_with_no_age_available_is_rejected():
    """Without an age there is nothing left to confirm identity with."""
    profile = {"name": "Guéhi", "current_club": "Some Other Club",
               "market_value_eur": 70_000_000}
    assert _verify_profile(profile, "Marc Guéhi", "Crystal Palace", 0)


def test_profile_with_no_valuation_yet_still_passes_the_value_gate():
    """A missing value is not an implausible one — that is handled downstream."""
    profile = {"name": "Gvardiol", "current_club": "Manchester City", "age": 24}
    assert _verify_profile(profile, "Joško Gvardiol", "Manchester City", 22) is None
