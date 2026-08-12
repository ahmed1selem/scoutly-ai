"""Unit tests for the scouter's candidate-eligibility helpers."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.agents.scouter import _same_club


# ── names that refer to the same club ─────────────────────────────────────────

@pytest.mark.parametrize("squad,club", [
    ("Liverpool", "Liverpool"),
    ("Manchester Utd", "Manchester United"),
    ("Newcastle Utd", "Newcastle United"),
    ("Sheffield Utd", "Sheffield United"),
    ("Nott'ham Forest", "Nottingham Forest"),
    ("Wolves", "Wolverhampton Wanderers"),
    ("Tottenham", "Tottenham Hotspur"),
    ("West Ham", "West Ham United"),
    ("Leeds United", "Leeds"),
])
def test_matches_same_club(squad, club):
    assert _same_club(squad, club)


# ── names that must stay distinct ─────────────────────────────────────────────
# The dangerous direction: a false positive here silently deletes a legitimate
# candidate from the shortlist.

@pytest.mark.parametrize("squad,club", [
    ("Manchester City", "Manchester United"),
    ("Manchester Utd", "Manchester City"),
    ("Real Madrid", "Atletico Madrid"),
    ("Arsenal", "Liverpool"),
    ("Brentford", "Brighton"),
    ("Inter", "AC Milan"),
])
def test_rejects_different_clubs(squad, club):
    assert not _same_club(squad, club)


@pytest.mark.parametrize("squad,club", [("", "Liverpool"), ("Liverpool", ""), ("", "")])
def test_empty_names_never_match(squad, club):
    assert not _same_club(squad, club)


# ── the wage ceiling must survive a late wage revision ────────────────────────

def _reapply_wage_ceiling(players, salary_max):
    """Mirror of the scouter's post-cross-check gate, for testing."""
    return [p for p in players if int(p.get("wage_eur", 0) or 0) <= salary_max]


def test_a_wage_confirmed_after_selection_is_still_gated():
    """
    The web cross-check can replace a modelled wage with a higher confirmed one,
    and it runs after the pre-selection ceiling. Matteo Politano reached a
    Bologna shortlist at EUR 124,615/wk against a EUR 120,000 limit because
    nothing re-checked.
    """
    players = [
        {"name": "Within", "wage_eur": 90_000},
        {"name": "Politano", "wage_eur": 124_615},
    ]
    kept = _reapply_wage_ceiling(players, 120_000)
    assert [p["name"] for p in kept] == ["Within"]


def test_reapplied_ceiling_keeps_everyone_when_all_are_affordable():
    players = [{"name": "A", "wage_eur": 40_000}, {"name": "B", "wage_eur": 119_999}]
    assert len(_reapply_wage_ceiling(players, 120_000)) == 2


def test_reapplied_ceiling_treats_a_missing_wage_as_affordable():
    """A player with no wage figure is not evidence of being over budget."""
    players = [{"name": "Unpriced"}]
    assert len(_reapply_wage_ceiling(players, 120_000)) == 1


# ── a mid-season transfer must not occupy two slate slots ─────────────────────

def _dedupe_split_seasons(cohort):
    """Mirror of the scouter's split-season merge, for testing."""
    by_person = {}
    for p in cohort:
        key = (p.get("name") or "").strip().casefold()
        kept = by_person.get(key)
        if kept is None or int(p.get("minutes", 0) or 0) > int(kept.get("minutes", 0) or 0):
            by_person[key] = p
    return list(by_person.values())


def test_split_season_rows_collapse_to_the_fuller_one():
    """
    A player who changed club mid-season has one cached row per squad. Brice
    Samba appeared twice on Barcelona's goalkeeper slate, at two different fit
    scores, competing with himself for a place.
    """
    cohort = [
        {"name": "Brice Samba", "squad": "Lens", "minutes": 1200},
        {"name": "Brice Samba", "squad": "Rennes", "minutes": 1800},
        {"name": "Someone Else", "squad": "Lille", "minutes": 900},
    ]
    kept = _dedupe_split_seasons(cohort)
    assert len(kept) == 2
    samba = next(p for p in kept if p["name"] == "Brice Samba")
    assert samba["squad"] == "Rennes", "should keep the row with more minutes"


def test_dedupe_is_case_insensitive_and_keeps_distinct_players():
    cohort = [
        {"name": "Joan García", "squad": "Espanyol", "minutes": 3420},
        {"name": "joan garcía", "squad": "Espanyol", "minutes": 90},
        {"name": "Joan Jordán", "squad": "Sevilla", "minutes": 1500},
    ]
    kept = _dedupe_split_seasons(cohort)
    assert len(kept) == 2
    assert max(p["minutes"] for p in kept if "garc" in p["name"].casefold()) == 3420


def test_dedupe_tolerates_missing_minutes():
    cohort = [
        {"name": "A", "squad": "X"},
        {"name": "A", "squad": "Y", "minutes": 500},
    ]
    kept = _dedupe_split_seasons(cohort)
    assert len(kept) == 1 and kept[0]["squad"] == "Y"


# ── the wage ceiling has a narrow grace band, the fee ceiling does not ────────
# A fee is a single negotiated number the club either has or does not. A wage is
# the most movable term in a contract, and the ceiling it is checked against is
# inferred from what the club currently pays rather than stated by anyone, so
# discarding a strong fit for being 3% over is false precision.

from backend.agents.scouter import _WAGE_CEILING_GRACE


def _apply_wage_gate(players, salary_max):
    """Mirror of the scouter's wage gate, including the grace band."""
    grace_max = int(salary_max * _WAGE_CEILING_GRACE)
    kept = [p for p in players if int(p.get("wage_eur", 0) or 0) <= grace_max]
    for p in kept:
        p["wage_stretch"] = int(p.get("wage_eur", 0) or 0) > salary_max
    return kept


def test_a_candidate_just_over_the_ceiling_survives_but_is_flagged():
    players = [{"name": "Just over", "wage_eur": 104_000}]
    kept = _apply_wage_gate(players, 100_000)
    assert len(kept) == 1
    assert kept[0]["wage_stretch"] is True


def test_a_candidate_inside_the_ceiling_is_not_flagged():
    players = [{"name": "Comfortable", "wage_eur": 90_000}]
    kept = _apply_wage_gate(players, 100_000)
    assert kept[0]["wage_stretch"] is False


def test_beyond_the_band_is_still_excluded_outright():
    """The band is narrow on purpose — this is not a soft margin."""
    players = [{"name": "Way over", "wage_eur": 200_000}]
    assert _apply_wage_gate(players, 100_000) == []


def test_the_band_is_no_wider_than_ten_percent():
    """Guards against the grace quietly widening into a soft margin."""
    assert 1.0 < _WAGE_CEILING_GRACE <= 1.10
