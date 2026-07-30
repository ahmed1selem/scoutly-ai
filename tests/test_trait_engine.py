"""Unit tests for the M3 trait engine."""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.engine.trait_engine import compute_trait_scores, known_traits

# ── vocabulary ────────────────────────────────────────────────────────────────

def _kb_traits() -> set[str]:
    """Every trait any club profile actually asks for."""
    payloads_file = (
        Path(__file__).resolve().parent.parent / "data" / "vectorstore" / "club_payloads.json"
    )
    if not payloads_file.exists():
        pytest.skip("club_payloads.json not built")
    payloads = json.loads(payloads_file.read_text(encoding="utf-8"))
    wanted: set[str] = set()
    for profile in payloads.values():
        by_pos = profile.get("recruitment_profile", {}).get("ideal_player_traits", {})
        for traits in by_pos.values():
            for t in traits:
                wanted.add(t.get("trait") if isinstance(t, dict) else str(t))
    return wanted


def test_engine_covers_every_trait_the_knowledge_base_requests():
    """
    A club asking for a trait the engine has never heard of becomes an
    unexplained coverage gap, which is how "measurable_fit" ended up being
    computed from a single metric.
    """
    missing = _kb_traits() - known_traits()
    assert not missing, f"club profiles request traits with no recipe: {sorted(missing)}"


def test_engine_emits_no_traits_outside_the_schema_vocabulary():
    """Trait names must match the tokens in prompts/tactical.md exactly."""
    prompt = (
        Path(__file__).resolve().parent.parent / "prompts" / "tactical.md"
    ).read_text(encoding="utf-8")
    unknown = [t for t in known_traits() if f"`{t}`" not in prompt]
    assert not unknown, f"traits absent from the schema-locked token list: {sorted(unknown)}"


# ── trait computation ─────────────────────────────────────────────────────────

@pytest.fixture
def fw_metrics():
    return {
        "att_pen_touches": {"value": 134, "per90": 5.36, "percentile": 90},
        "xg":              {"value": 15,  "per90": 0.60, "percentile": 85},
        "xa":              {"value": 5,   "per90": 0.20, "percentile": 68},
        "sca":             {"value": 90,  "per90": 3.60, "percentile": 75},
        "shots":           {"value": 88,  "per90": 3.52, "percentile": 92},
        "tackles":         {"value": 22,  "per90": 0.88, "percentile": 55},
        "interceptions":   {"value": 8,   "per90": 0.32, "percentile": 48},
        "prg_carries":     {"value": 91,  "per90": 3.64, "percentile": 78},
        "pass_cmp_pct":    {"value": 77.2,"per90": 77.2, "percentile": 70},
    }


def test_box_movement_from_att_pen_touches(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["box_movement"]["source"] == "real"
    assert abs(scores["box_movement"]["score"] - 0.90) < 0.01


def test_final_third_decision_making_averages_xg_xa_sca(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["final_third_decision_making"]["source"] == "real"
    assert abs(scores["final_third_decision_making"]["score"] - (85 + 68 + 75) / 300) < 0.01


def test_pressing_traits_are_proxy(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["pressing_intensity"]["source"] == "proxy"
    assert scores["counter_pressing"]["source"] == "proxy"


def test_ball_winning_is_real_when_both_metrics_present(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["ball_winning"]["source"] == "real"


def test_partial_recipe_is_downgraded_to_proxy():
    """A 'real' trait scored from half its inputs is a weaker signal, and says so."""
    scores = compute_trait_scores({"tackles": {"value": 5, "per90": 1.0, "percentile": 70}}, "DF")
    assert scores["ball_winning"]["source"] == "proxy"


def test_qualitative_traits_have_none_score(fw_metrics):
    """
    Some traits genuinely have no measurable proxy in any source. Aerials and
    duels are no longer among them — Sofascore supplies those — so this asserts
    on one that really is a judgement call.
    """
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["tactical_discipline"]["score"] is None
    assert scores["tactical_discipline"]["source"] == "qualitative"


def test_stamina_from_game_attributes(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW", {"stamina": 80})
    assert scores["stamina"]["source"] == "game_attribute"
    assert abs(scores["stamina"]["score"] - 80 / 99) < 0.01


def test_missing_game_attribute_degrades_to_qualitative(fw_metrics):
    """
    Without a synthetic rating the trait is handed to the LLM rather than
    dropped, so the club's requirement still shows up in qualitative_gaps.
    """
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["stamina"]["source"] == "qualitative"
    assert scores["stamina"]["score"] is None


def test_traits_are_not_filtered_by_position(fw_metrics):
    """
    The club profile decides which traits matter per position. Filtering here
    silently dropped traits clubs had asked for — e.g. a fullback needing
    box_movement scored zero coverage for it.
    """
    scores = compute_trait_scores(fw_metrics, "DF")
    assert "box_movement" in scores
    assert "ball_winning" in scores


def test_defensive_traits_scored_for_a_defender():
    df_metrics = {
        "tackles":       {"value": 78,  "per90": 2.60, "percentile": 88},
        "interceptions": {"value": 52,  "per90": 1.73, "percentile": 82},
        "prg_passes":    {"value": 148, "per90": 4.93, "percentile": 75},
        "pass_cmp_pct":  {"value": 91.2,"per90": 91.2, "percentile": 92},
        "duels_won_pct": {"value": 61.4,"per90": 61.4, "percentile": 79},
        "aerial_won_pct": {"value": 72.0,"per90": 72.0, "percentile": 90},
    }
    scores = compute_trait_scores(df_metrics, "DF")
    assert scores["ball_winning"]["source"] == "real"
    assert scores["defensive_concentration"]["score"] == 0.82
    assert abs(scores["technical_security"]["score"] - 0.912) < 0.001
    # Duels and aerials come from Sofascore; without them these were unscorable
    # even though every club profile asks for duel_strength.
    assert abs(scores["duel_strength"]["score"] - 0.614) < 0.001
    assert abs(scores["aerial_strength"]["score"] - 0.720) < 0.001


def test_duel_strength_is_unscorable_without_duel_data():
    """The trait drops out rather than being faked from an unrelated metric."""
    scores = compute_trait_scores(
        {"tackles": {"value": 78, "per90": 2.6, "percentile": 88}}, "DF")
    assert "duel_strength" not in scores


def test_goalkeeper_traits_are_scorable():
    """
    A goalkeeper brief previously produced zero trait coverage for every
    candidate, because nothing in FBref's output touched shot-stopping.
    """
    gk_metrics = {
        "saves":         {"value": 112, "per90": 3.2, "percentile": 84},
        "pass_cmp_pct":  {"value": 71.5,"per90": 71.5, "percentile": 60},
        "clearances":    {"value": 24,  "per90": 0.7, "percentile": 55},
        "interceptions": {"value": 8,   "per90": 0.2, "percentile": 40},
    }
    scores = compute_trait_scores(gk_metrics, "GK")
    assert scores["reliable_shot_stopping"]["score"] == 0.84
    assert scores["distribution"]["score"] is not None
    assert scores["sweeper_keeper"]["score"] is not None


def test_score_range_0_to_1(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW", {"stamina": 78, "sprint_speed": 90})
    for trait, result in scores.items():
        if result["score"] is not None:
            assert 0.0 <= result["score"] <= 1.0, f"{trait} out of range: {result['score']}"


def test_no_metrics_leaves_only_qualitative_traits():
    scores = compute_trait_scores({}, "FW")
    assert scores, "qualitative traits should still be reported"
    assert all(r["source"] == "qualitative" and r["score"] is None for r in scores.values())
