"""
M4 — Fit engine: deterministic trait-match between a player and a club's profile.

Numbers only come from here.  The LLM in TacticalAgent explains and judges the
qualitative gaps; it does not compute or modify these scores.

Tier factors (from SCOUTLY_FIT_ENGINE_SCHEMAS.md §6):
  real           → 1.0  (FBref per-90 percentile)
  proxy          → 0.7  (correlated metric)
  game_attribute → 0.5  (synthetic rating, down-weighted)
  qualitative    → 0.0  (no deterministic score; LLM handles)
"""

from __future__ import annotations

TIER_FACTORS: dict[str, float] = {
    "real":           1.0,
    "proxy":          0.7,
    "game_attribute": 0.5,
    "qualitative":    0.0,
}

# ── FBref broad position → club profile position keys, in preference order ───
# FBref's Big-5 tables only give broad codes, so "DF" covers both centre-backs
# and fullbacks. Listing both means a club profile that defines only one of them
# still resolves instead of abstaining for want of a position key.
_FBREF_TO_CLUB_POS = {
    "GK":  ["goalkeeper"],
    "DF":  ["centre_back", "fullback"],
    "MF":  ["midfielder"],
    "FW":  ["striker", "winger"],
    # Fine-grained codes, when a caller has them
    "CB":  ["centre_back"],
    "FB":  ["fullback"],
    "CM":  ["midfielder"],
    "DM":  ["midfielder"],
    "AM":  ["midfielder"],
    "W":   ["winger"],
    "ST":  ["striker"],
}


def _map_position(fbref_pos: str) -> list[str]:
    """
    Map a FBref position string (e.g. "FW,MF") to a ranked list of club-profile
    position keys to try.  Returns candidates in preference order.
    """
    tokens = [t.strip() for t in fbref_pos.replace(",", " ").upper().split()]
    ordered: list[str] = []
    for t in tokens:
        for key in _FBREF_TO_CLUB_POS.get(t, []):
            if key not in ordered:
                ordered.append(key)
    return ordered or ["midfielder"]


# ── Core computation ──────────────────────────────────────────────────────────

def compute_fit_verdict(
    player: dict,
    club_profile: dict,
) -> dict:
    """
    Compute the full fit verdict for one player against one club.

    Args:
        player:       player dict with at minimum:
                        position (str), trait_scores (dict), data_coverage (dict)
        club_profile: `club_tactical_profile_v1` payload from the vector DB

    Returns:
        {
          "measurable_fit":    0–100  (deterministic),
          "trait_coverage":    0.0–1.0,  (fraction of required traits that are measurable)
          "per_trait_breakdown": [...],
          "qualitative_gaps":  [...],   (required traits with no deterministic score)
          "confidence":        0.0–1.0,
          "abstain":           bool,
          "abstain_reason":    str | None,
        }
    """
    trait_scores    = player.get("trait_scores", {})
    data_coverage   = player.get("data_coverage", {})
    position        = player.get("position", "FW")
    minutes         = int(player.get("minutes", data_coverage.get("minutes", 0)))
    coverage_flag   = player.get("coverage_flag", data_coverage.get("coverage_flag", "unknown"))

    # Resolve the club-profile position key to use
    pos_candidates  = _map_position(position)
    ideal_traits_map: dict[str, list] = (
        club_profile.get("recruitment_profile", {}).get("ideal_player_traits", {})
    )
    required_traits: list[str] = []
    used_pos_key = None
    for pos_key in pos_candidates:
        traits = ideal_traits_map.get(pos_key, [])
        if traits:
            required_traits = traits
            used_pos_key = pos_key
            break
    if not required_traits:
        # Fall back to the first non-empty position
        for pos_key, traits in ideal_traits_map.items():
            if traits:
                required_traits = traits
                used_pos_key = pos_key
                break

    per_trait_breakdown = []
    qualitative_gaps    = []
    weighted_score_sum  = 0.0
    max_possible        = 0.0
    # Denominator over EVERY required trait, measured or not. Without it a player
    # whose only scorable trait happens to be elite normalises to ~100 while a
    # fully-measured, genuinely better player scores lower — missing data would
    # raise the score.
    required_weight     = 0.0

    for trait_entry in required_traits:
        if isinstance(trait_entry, dict):
            trait_name = trait_entry.get("trait", trait_entry.get("name", ""))
            weight     = float(trait_entry.get("weight", 1.0))
        else:
            trait_name = str(trait_entry)
            weight     = 1.0

        required_weight += weight
        ts = trait_scores.get(trait_name)

        if ts is None:
            # Trait exists in club profile but player has no entry at all
            qualitative_gaps.append({
                "trait":  trait_name,
                "reason": "not_in_player_trait_scores",
                "weight": weight,
            })
            continue

        source = ts.get("source", "qualitative")
        score  = ts.get("score")

        if source == "qualitative" or score is None:
            qualitative_gaps.append({
                "trait":  trait_name,
                "reason": "qualitative — LLM assessment needed",
                "weight": weight,
            })
            continue

        tier_factor = TIER_FACTORS.get(source, 0.5)
        contribution = float(score) * weight * tier_factor

        weighted_score_sum += contribution
        max_possible       += weight * tier_factor  # max score = 1.0 × weight × tier_factor

        per_trait_breakdown.append({
            "trait":        trait_name,
            "score":        round(float(score), 3),
            "source":       source,
            "tier_factor":  tier_factor,
            "weight":       weight,
            "contribution": round(contribution, 3),
        })

    # If no required traits were found at all, abstain immediately
    if not required_traits:
        return {
            "measurable_fit":      0.0,
            "demonstrated_fit":    0.0,
            "trait_coverage":      0.0,
            "used_position_key":   None,
            "per_trait_breakdown": [],
            "qualitative_gaps":    [],
            "confidence":          0.0,
            "abstain":             True,
            "abstain_reason":      "No ideal_player_traits defined in club profile for this position",
        }

    # Two different questions, kept separate on purpose:
    #   measurable_fit  — how good is the player on the traits we could measure?
    #   demonstrated_fit — how much of the club's full requirement did they
    #                      demonstrably meet? Unmeasured traits contribute 0.
    # Ranking must use demonstrated_fit; measurable_fit alone rewards thin data.
    measurable_fit = round(weighted_score_sum / max_possible * 100, 1) if max_possible > 0 else 0.0
    demonstrated_fit = (
        round(weighted_score_sum / required_weight * 100, 1) if required_weight > 0 else 0.0
    )

    # Trait coverage: fraction of required traits that we could score deterministically
    n_required   = len(required_traits)
    n_measurable = len(per_trait_breakdown)
    trait_coverage = n_measurable / n_required if n_required > 0 else 0.0

    # Confidence: penalise thin coverage (by minutes and by trait_coverage)
    minutes_conf = min(minutes / 1800, 1.0)          # full confidence at 1800 min
    club_conf    = float(
        club_profile.get("data_quality", {}).get("confidence_overall", 0.7)
    )
    # Coverage is a multiplier, not one weighted term among three. Weighted in,
    # minutes played alone put a floor of ~0.65 under confidence, so a verdict
    # resting on 2 of 5 traits still reported "high confidence". Nothing about
    # playing time or a well-documented club profile can substitute for having
    # measured what the club actually asked for.
    confidence = round(
        (minutes_conf * 0.6 + club_conf * 0.4) * max(trait_coverage, 0.05), 3
    )

    # Abstain if evidence is too thin
    abstain        = confidence < 0.3 or (minutes > 0 and minutes < 270)
    abstain_reason = None
    if confidence < 0.3:
        abstain_reason = f"Overall confidence too low ({confidence:.2f})"
    elif minutes < 270 and minutes > 0:
        abstain_reason = f"Insufficient minutes ({minutes} < 270)"

    return {
        "measurable_fit":      measurable_fit,
        "demonstrated_fit":    demonstrated_fit,
        "trait_coverage":      round(trait_coverage, 3),
        "used_position_key":   used_pos_key,
        "per_trait_breakdown": per_trait_breakdown,
        "qualitative_gaps":    qualitative_gaps,
        "confidence":          confidence,
        "abstain":             abstain,
        "abstain_reason":      abstain_reason,
    }
