"""Unit tests for the M4 fit engine."""
import pytest
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from backend.engine.fit_engine import compute_fit_verdict, TIER_FACTORS, _map_position

# ── position mapping ───────────────────────────────────────────────────────────

def test_fw_maps_to_striker():
    assert "striker" in _map_position("FW")

def test_mf_maps_to_midfielder():
    assert "midfielder" in _map_position("MF")

def test_df_maps_to_centre_back():
    assert "centre_back" in _map_position("DF")

# ── tier factors ───────────────────────────────────────────────────────────────

def test_real_tier_factor_is_1():
    assert TIER_FACTORS["real"] == 1.0

def test_proxy_tier_factor_less_than_real():
    assert TIER_FACTORS["proxy"] < TIER_FACTORS["real"]

def test_game_attr_tier_factor_least():
    assert TIER_FACTORS["game_attribute"] < TIER_FACTORS["proxy"]

def test_qualitative_tier_factor_zero():
    assert TIER_FACTORS["qualitative"] == 0.0

# ── fit verdict ────────────────────────────────────────────────────────────────

@pytest.fixture
def player_fw():
    return {
        "name": "Test Striker",
        "position": "FW",
        "minutes": 2250,
        "coverage_flag": "good",
        "trait_scores": {
            "box_movement":       {"score": 0.90, "source": "real"},
            "link_play":          {"score": 0.70, "source": "proxy"},
            "pressing_intensity": {"score": 0.52, "source": "proxy"},
            "aerial_strength":     {"score": None, "source": "qualitative"},
        },
    }

@pytest.fixture
def club_with_striker_traits():
    return {
        "club": {"club_id": "test_club", "club_name": "Test FC", "league": "Test League"},
        "data_quality": {"confidence_overall": 0.80},
        "team_identity": {"primary_style": {"value": "possession_control"}},
        "recruitment_profile": {
            "ideal_player_traits": {
                "striker": ["box_movement", "link_play", "pressing_intensity",
                            "out_of_possession_work", "aerial_strength"],
            }
        },
    }

def test_measurable_fit_in_range(player_fw, club_with_striker_traits):
    verdict = compute_fit_verdict(player_fw, club_with_striker_traits)
    assert 0 <= verdict["measurable_fit"] <= 100

def test_trait_coverage_correct(player_fw, club_with_striker_traits):
    verdict = compute_fit_verdict(player_fw, club_with_striker_traits)
    # 5 required traits: box_movement(real), link_play(proxy), pressing_intensity(proxy)
    # scored, aerial_strength(qual) → qualitative gap, out_of_possession_work → not in scores
    assert verdict["trait_coverage"] == pytest.approx(3/5, abs=0.01)

def test_tier_factors_applied(player_fw, club_with_striker_traits):
    verdict = compute_fit_verdict(player_fw, club_with_striker_traits)
    breakdown = {t["trait"]: t for t in verdict["per_trait_breakdown"]}
    assert breakdown["box_movement"]["tier_factor"] == 1.0
    assert breakdown["link_play"]["tier_factor"] == 0.7
    assert breakdown["pressing_intensity"]["tier_factor"] == 0.7

def test_qualitative_trait_goes_to_gaps(player_fw, club_with_striker_traits):
    verdict = compute_fit_verdict(player_fw, club_with_striker_traits)
    gap_traits = [g["trait"] for g in verdict["qualitative_gaps"]]
    assert "aerial_strength" in gap_traits
    assert "out_of_possession_work" in gap_traits

def test_confidence_is_0_to_1(player_fw, club_with_striker_traits):
    verdict = compute_fit_verdict(player_fw, club_with_striker_traits)
    assert 0.0 <= verdict["confidence"] <= 1.0

def test_low_minutes_triggers_abstain():
    thin_player = {
        "name": "Thin Sample",
        "position": "FW",
        "minutes": 200,
        "trait_scores": {"box_movement": {"score": 0.80, "source": "real"}},
    }
    club = {
        "data_quality": {"confidence_overall": 0.9},
        "recruitment_profile": {
            "ideal_player_traits": {"striker": ["box_movement"]}
        },
    }
    verdict = compute_fit_verdict(thin_player, club)
    assert verdict["abstain"] is True
    assert verdict["abstain_reason"] is not None

def test_empty_club_profile_abstains(player_fw):
    verdict = compute_fit_verdict(player_fw, {})
    assert verdict["abstain"] is True

def test_per_trait_contribution_math(player_fw, club_with_striker_traits):
    verdict = compute_fit_verdict(player_fw, club_with_striker_traits)
    breakdown = {t["trait"]: t for t in verdict["per_trait_breakdown"]}
    bm = breakdown["box_movement"]
    expected_contribution = bm["score"] * bm["weight"] * bm["tier_factor"]
    assert abs(bm["contribution"] - expected_contribution) < 0.001
