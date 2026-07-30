"""
Guards on the derived club profiles.

The profiles are what the fit engine scores against, so a silent regression here
shows up as a plausible-looking number rather than an error. The invariants that
matter:

  * every required trait can actually be scored, or it counts as unmet forever
  * profiles stay distinct — the authored file collapsed 96 clubs into 13 blocks
  * every label traces to a measured indicator
  * club names match the player cohort, or the profile is never found
"""
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.data_pipeline.club_names import fold, match_club  # noqa: E402
from backend.data_pipeline.club_rules import BASELINE, POSITIONS, RULES, ordinal  # noqa: E402
from backend.engine.trait_engine import _TRAIT_RECIPES  # noqa: E402
from backend.schemas.club_profile import ClubProfile  # noqa: E402

PAYLOADS = Path(__file__).resolve().parent.parent / "data" / "vectorstore" / "club_payloads.json"
pytestmark = pytest.mark.skipif(not PAYLOADS.exists(), reason="club profiles not built")


@pytest.fixture(scope="module")
def profiles() -> dict:
    with open(PAYLOADS, encoding="utf-8") as fh:
        return json.load(fh)


def test_every_rule_trait_is_scoreable():
    """A rule may only ask for traits the trait engine has a recipe for."""
    asked = {t for traits in BASELINE.values() for t in traits}
    asked |= {t for rule in RULES for traits in rule.traits.values() for t in traits}
    assert not (asked - set(_TRAIT_RECIPES)), \
        f"rules require traits with no recipe: {sorted(asked - set(_TRAIT_RECIPES))}"


def test_rules_only_target_known_positions():
    for rule in RULES:
        assert set(rule.traits) <= set(POSITIONS), \
            f"rule {rule.key} targets unknown positions: {set(rule.traits) - set(POSITIONS)}"


def test_profiles_validate_against_schema(profiles):
    for club_id, profile in profiles.items():
        ClubProfile(**profile)   # raises with the offending field on failure


def test_required_traits_are_all_scoreable(profiles):
    required = {
        trait
        for profile in profiles.values()
        for traits in profile["recruitment_profile"]["ideal_player_traits"].values()
        for trait in traits
    }
    assert not (required - set(_TRAIT_RECIPES)), \
        f"profiles require unscoreable traits: {sorted(required - set(_TRAIT_RECIPES))}"


def test_every_position_has_a_requirement(profiles):
    for profile in profiles.values():
        traits = profile["recruitment_profile"]["ideal_player_traits"]
        for position in POSITIONS:
            assert traits.get(position), \
                f"{profile['club']['club_name']} has no requirement for {position}"


def test_requirements_are_mostly_distinct(profiles):
    """
    The failure this replaces: 96 clubs sharing 13 requirement blocks, so two
    clubs of the same style produced an identical fit score for the same player.
    """
    blocks = Counter(
        json.dumps(p["recruitment_profile"]["ideal_player_traits"], sort_keys=True)
        for p in profiles.values()
    )
    assert len(blocks) >= len(profiles) * 0.75, \
        f"only {len(blocks)} distinct requirement blocks across {len(profiles)} clubs"
    assert blocks.most_common(1)[0][1] <= 5, \
        f"{blocks.most_common(1)[0][1]} clubs share one requirement block"


def test_labels_cite_measured_indicators(profiles):
    for profile in profiles.values():
        measured = profile.get("measured_indicators", {})
        assert measured, f"{profile['club']['club_name']} has no measured indicators"
        for key, entry in measured.items():
            assert entry.get("percentile") is not None, f"{key} has no percentile"
            assert entry.get("formula"), f"{key} has no formula"


def test_confidence_is_bounded(profiles):
    """Derived labels over proxies must never claim certainty."""
    for profile in profiles.values():
        confidence = profile["data_quality"]["confidence_overall"]
        assert 0.0 < confidence <= 0.85, \
            f"{profile['club']['club_name']} reports confidence {confidence}"


def test_club_names_match_the_player_cohort(profiles):
    """A profile filed under a name the cohort never uses is never found."""
    import sqlite3
    cache = Path(__file__).resolve().parent.parent / "data" / "fbref_cache.db"
    if not cache.exists():
        pytest.skip("player cache not built")
    conn = sqlite3.connect(str(cache))
    try:
        squads = {row[0] for row in conn.execute(
            "SELECT DISTINCT squad FROM player_cohort WHERE squad IS NOT NULL") if row[0]}
    finally:
        conn.close()
    folded = {fold(s) for s in squads}
    missing = [p["club"]["club_name"] for p in profiles.values()
               if fold(p["club"]["club_name"]) not in folded]
    assert not missing, f"profiles whose club name is not a cohort squad: {missing}"


@pytest.mark.parametrize("source,expected", [
    ("FC Bayern München", "Bayern Munich"),
    ("Manchester United", "Manchester Utd"),
    ("Manchester City", "Manchester City"),
    ("Borussia M'gladbach", "Gladbach"),
    ("Wolverhampton", "Wolves"),
    ("Paris Saint-Germain", "PSG"),
    ("Olympique Lyonnais", "Lyon"),
    ("Stade Brestois", "Brest"),
    ("AC Milan", "Milan"),
    ("Inter", "Inter"),
])
def test_club_name_matching(source, expected):
    """Man City and Man Utd must never collapse into each other."""
    candidates = ["Bayern Munich", "Manchester Utd", "Manchester City", "Gladbach",
                  "Wolves", "PSG", "Lyon", "Brest", "Milan", "Inter"]
    assert match_club(source, {fold(c): c for c in candidates}) == expected


@pytest.mark.parametrize("value,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"),
    (21, "21st"), (83, "83rd"), (100, "100th"),
])
def test_ordinal(value, expected):
    assert ordinal(value) == expected
