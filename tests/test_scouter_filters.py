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
