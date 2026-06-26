"""Unit tests for the M3 trait engine."""
import pytest
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from backend.engine.trait_engine import compute_trait_scores, _expand_position

# ── position expansion ─────────────────────────────────────────────────────────

def test_fw_expands_to_attacking_roles():
    codes = _expand_position("FW")
    assert "ST" in codes
    assert "W" in codes
    assert "AM" in codes

def test_df_expands_to_cb_fb():
    codes = _expand_position("DF")
    assert "CB" in codes
    assert "FB" in codes

def test_combined_position():
    codes = _expand_position("FW,MF")
    assert "ST" in codes
    assert "CM" in codes

# ── trait computation ──────────────────────────────────────────────────────────

@pytest.fixture
def fw_metrics():
    return {
        "att_pen_touches": {"value": 134, "per90": 5.36, "percentile": 90},
        "xg":              {"value": 15,  "per90": 0.60, "percentile": 85},
        "xa":              {"value": 5,   "per90": 0.20, "percentile": 68},
        "tackles":         {"value": 22,  "per90": 0.88, "percentile": 55},
        "interceptions":   {"value": 8,   "per90": 0.32, "percentile": 48},
        "prg_carries":     {"value": 91,  "per90": 3.64, "percentile": 78},
        "pass_cmp_pct":    {"value": 77.2,"per90": 77.2, "percentile": 70},
    }

def test_fw_gets_box_movement(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert "box_movement" in scores
    assert scores["box_movement"]["source"] == "real"
    assert abs(scores["box_movement"]["score"] - 0.90) < 0.01

def test_fw_gets_final_third_decisions(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert "final_third_decisions" in scores
    assert scores["final_third_decisions"]["source"] == "real"

def test_fw_pressing_traits_are_proxy(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert scores["pressing_intensity"]["source"] == "proxy"
    assert scores["counter_pressing"]["source"] == "proxy"

def test_fw_qualitative_traits_have_none_score(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW")
    assert "aerial_ability" in scores
    assert scores["aerial_ability"]["score"] is None
    assert scores["aerial_ability"]["source"] == "qualitative"

def test_stamina_from_game_attributes(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW", {"stamina": 80})
    assert "stamina" in scores
    assert scores["stamina"]["source"] == "game_attribute"
    assert abs(scores["stamina"]["score"] - 80/99) < 0.01

def test_gk_does_not_get_fw_traits(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "GK")
    # GK should not get FW traits like box_movement
    assert "box_movement" not in scores

def test_df_does_not_get_fw_traits():
    df_metrics = {
        "tackles":       {"value": 78, "per90": 2.60, "percentile": 88},
        "interceptions": {"value": 52, "per90": 1.73, "percentile": 82},
        "prg_passes":    {"value": 148,"per90": 4.93, "percentile": 75},
        "pass_cmp_pct":  {"value": 91.2,"per90": 91.2,"percentile": 92},
    }
    scores = compute_trait_scores(df_metrics, "DF")
    assert "box_movement" not in scores        # ST/W/AM only
    assert "ball_winning" in scores            # CB/DM/CM
    assert scores["ball_winning"]["source"] == "real"

def test_score_range_0_to_1(fw_metrics):
    scores = compute_trait_scores(fw_metrics, "FW", {"stamina": 78})
    for trait, result in scores.items():
        if result["score"] is not None:
            assert 0.0 <= result["score"] <= 1.0, f"{trait} score out of range: {result['score']}"

def test_missing_metric_skips_trait():
    # No metrics at all → no measurable traits
    scores = compute_trait_scores({}, "FW")
    for trait, result in scores.items():
        # Qualitative traits are fine with None score
        if result["source"] != "qualitative":
            assert result["score"] is None or result.get("score") == 0
