"""
M3 — Trait engine: deterministic bridge from player raw_metrics → trait_scores.

Input:
  raw_metrics     dict  {metric_name: {value, per90, percentile}}  (from get_position_cohort)
  game_attributes dict  {sprint_speed, stamina, ...}               (optional game-attr fallback)
  position        str   broad FBref code: FW | MF | DF | GK

Output:
  trait_scores    dict  {trait_name: {score: 0.0–1.0, source: real|proxy|game_attribute|qualitative}}

Rules (from SCOUTLY_FIT_ENGINE_SCHEMAS.md §4):
  - "real"           → score from FBref per-90 percentile within position cohort
  - "proxy"          → score from a correlated metric; flagged as weaker signal
  - "game_attribute" → score from synthetic game ratings; down-weighted in the fit engine
  - "qualitative"    → no computed score; LLM + RAG handles it at fit-verdict time
"""

from __future__ import annotations

# ── Position mapping ─────────────────────────────────────────────────────────
# FBref broad codes → fine-grained schema positions they cover
_POSITION_EXPANSION = {
    "FW": {"ST", "W", "AM"},
    "MF": {"CM", "DM", "AM"},
    "DF": {"CB", "FB"},
    "GK": {"GK"},
    # Also accept fine-grained codes directly
    "ST": {"ST"}, "W": {"W"}, "AM": {"AM"},
    "CM": {"CM"}, "DM": {"DM"},
    "CB": {"CB"}, "FB": {"FB"},
}


def _expand_position(pos: str) -> set[str]:
    """Return the set of fine-grained codes covered by a player's position string."""
    codes: set[str] = set()
    for token in pos.upper().replace(",", " ").split():
        codes |= _POSITION_EXPANSION.get(token, {token})
    return codes


# ── Trait recipe table ───────────────────────────────────────────────────────
# Each entry:  trait_name → {positions, method, metrics, source}
#
# method "percentile"     → single metric percentile / 100
# method "avg_percentile" → mean of multiple metric percentiles / 100
# method "complement_pct" → 1 - (metric_value / 100)      (low value = high score)
# method "pct_value"      → metric_value / 100             (e.g. pass_cmp_pct)
# method "game_attr"      → game_attribute_value / 99
# method "qualitative"    → no computation; LLM handles it
#
_TRAIT_RECIPES: dict[str, dict] = {
    # ── Ball progression (real) ──────────────────────────────────────────────
    "progressive_passing": {
        "positions": {"CB", "DM", "CM", "FB"},
        "method":    "percentile",
        "metrics":   ["prg_passes"],
        "source":    "real",
    },
    "progressive_carrying": {
        "positions": {"FB", "CM", "W", "CB"},
        "method":    "percentile",
        "metrics":   ["prg_carries"],
        "source":    "real",
    },
    # ── Attacking output (real) ───────────────────────────────────────────────
    "box_movement": {
        "positions": {"ST", "W", "AM"},
        "method":    "percentile",
        "metrics":   ["att_pen_touches"],
        "source":    "real",
    },
    "final_third_decisions": {
        "positions": {"W", "AM", "ST"},
        "method":    "avg_percentile",
        "metrics":   ["xg", "xa"],
        "source":    "real",
    },
    # ── Linking / creativity (proxy) ─────────────────────────────────────────
    "link_play": {
        "positions": {"ST", "AM"},
        "method":    "avg_percentile",
        "metrics":   ["assists", "xa"],
        "source":    "proxy",
    },
    # ── Defensive / ball-winning (real) ──────────────────────────────────────
    "ball_winning": {
        "positions": {"DM", "CM", "CB"},
        "method":    "avg_percentile",
        "metrics":   ["tackles", "interceptions"],
        "source":    "real",
    },
    # ── Technical (real) ─────────────────────────────────────────────────────
    "technical_security": {
        "positions": {"FB", "CM", "AM", "GK"},
        "method":    "pct_value",
        "metrics":   ["pass_cmp_pct"],
        "source":    "real",
    },
    "comfortable_on_ball": {
        "positions": {"GK"},
        "method":    "pct_value",
        "metrics":   ["pass_cmp_pct"],
        "source":    "real",
    },
    # ── Pressing (proxy — total def actions, not att-third specific) ──────────
    "pressing_intensity": {
        "positions": {"ST", "W", "AM"},
        "method":    "avg_percentile",
        "metrics":   ["tackles", "interceptions"],
        "source":    "proxy",
    },
    "counter_pressing": {
        "positions": {"CM", "ST", "W"},
        "method":    "avg_percentile",
        "metrics":   ["tackles", "interceptions"],
        "source":    "proxy",
    },
    "press_resistance": {
        "positions": {"CM", "DM", "AM"},
        "method":    "avg_percentile",
        "metrics":   ["pass_cmp_pct", "prg_passes"],
        "source":    "proxy",
    },
    # ── High-line defending (proxy) ───────────────────────────────────────────
    "defends_high_line": {
        "positions": {"CB", "FB"},
        "method":    "avg_percentile",
        "metrics":   ["tackles", "interceptions"],
        "source":    "proxy",
    },
    # ── Physical (game_attribute) ─────────────────────────────────────────────
    "recovery_pace": {
        "positions": {"CB", "FB"},
        "method":    "game_attr",
        "metrics":   ["sprint_speed"],
        "source":    "game_attribute",
    },
    "stamina": {
        "positions": {"FB", "CM", "W"},
        "method":    "game_attr",
        "metrics":   ["stamina"],
        "source":    "game_attribute",
    },
    # ── Set pieces (qualitative — low sample → no reliable number) ────────────
    "set_piece_threat": {
        "positions": {"CB", "ST"},
        "method":    "qualitative",
        "metrics":   [],
        "source":    "qualitative",
    },
    # ── Positional / role (qualitative — not in stats) ────────────────────────
    "touchline_width": {
        "positions": {"W"},
        "method":    "qualitative",
        "metrics":   [],
        "source":    "qualitative",
    },
    "one_v_one": {
        "positions": {"W", "FB", "AM"},
        "method":    "qualitative",
        "metrics":   [],
        "source":    "qualitative",
    },
    "aerial_ability": {
        "positions": {"CB", "ST", "GK"},
        "method":    "qualitative",
        "metrics":   [],
        "source":    "qualitative",
    },
    "sweeper_keeper": {
        "positions": {"GK"},
        "method":    "qualitative",
        "metrics":   [],
        "source":    "qualitative",
    },
    "quick_passing": {
        "positions": {"CM", "DM"},
        "method":    "pct_value",
        "metrics":   ["pass_cmp_pct"],
        "source":    "proxy",
    },
}


# ── Scoring functions ────────────────────────────────────────────────────────

def _get_percentile(raw_metrics: dict, metric: str) -> float | None:
    """Return percentile (0-100) for a metric, or None if unavailable."""
    m = raw_metrics.get(metric)
    if m and "percentile" in m:
        return float(m["percentile"])
    return None


def _get_pct_value(raw_metrics: dict, metric: str) -> float | None:
    """Return raw value for a percentage-scale metric (e.g. pass_cmp_pct)."""
    m = raw_metrics.get(metric)
    if m and "value" in m:
        return float(m["value"])
    return None


def _score_recipe(
    recipe: dict,
    raw_metrics: dict,
    game_attributes: dict,
) -> dict | None:
    """
    Compute a single trait score from a recipe.
    Returns {score, source} or None if no data available.
    """
    method  = recipe["method"]
    metrics = recipe.get("metrics", [])
    source  = recipe["source"]

    if method == "qualitative":
        return {"score": None, "source": "qualitative"}

    if method == "percentile":
        pct = _get_percentile(raw_metrics, metrics[0])
        if pct is None:
            return None
        return {"score": round(pct / 100, 3), "source": source}

    if method == "avg_percentile":
        pcts = [_get_percentile(raw_metrics, m) for m in metrics]
        available = [p for p in pcts if p is not None]
        if not available:
            return None
        return {"score": round(sum(available) / len(available) / 100, 3), "source": source}

    if method == "pct_value":
        val = _get_pct_value(raw_metrics, metrics[0])
        if val is None:
            return None
        return {"score": round(min(val / 100, 1.0), 3), "source": source}

    if method == "game_attr":
        ga_key = metrics[0]
        val = game_attributes.get(ga_key)
        if val is None:
            return None
        return {"score": round(float(val) / 99, 3), "source": "game_attribute"}

    return None


# ── Public API ────────────────────────────────────────────────────────────────

def compute_trait_scores(
    raw_metrics: dict,
    position: str,
    game_attributes: dict | None = None,
) -> dict[str, dict]:
    """
    Compute trait_scores for a player.

    Args:
        raw_metrics:     {metric: {value, per90, percentile}}  from get_position_cohort
        position:        broad FBref code(s), e.g. "FW" or "FW,MF"
        game_attributes: optional {sprint_speed, stamina, ...} from the SQLite DB

    Returns:
        {trait_name: {score: 0.0–1.0 | None, source: str}}
        Qualitative traits have score=None (handled by LLM in M4).
        Traits that don't apply to the position are omitted.
    """
    game_attributes = game_attributes or {}
    player_pos_codes = _expand_position(position)

    trait_scores: dict[str, dict] = {}

    for trait, recipe in _TRAIT_RECIPES.items():
        required_positions = recipe["positions"]
        # Include the trait only if the player's position overlaps the trait's scope
        if not player_pos_codes.intersection(required_positions):
            continue

        result = _score_recipe(recipe, raw_metrics, game_attributes)
        if result is not None:
            trait_scores[trait] = result

    return trait_scores
