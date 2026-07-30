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

Trait names are the schema-locked vocabulary used by the club profiles in the
knowledge base (`recruitment_profile.ideal_player_traits`) and by prompts/tactical.md.
Every trait any club asks for has an entry here, so the fit engine either scores it
or reports it as an explicit qualitative gap — it never silently drops a requirement.

Traits are NOT filtered by position. The club profile is the authority on which
traits matter for which position; computing an unused trait is harmless, whereas
filtering one out that a club actually asked for silently shrinks trait coverage.
"""

from __future__ import annotations

# ── Metrics the cache provides (see backend/data_pipeline/cache_builder.py) ───
# goals assists xg xa shots shots_on_target prg_passes prg_carries
# att_pen_touches tackles interceptions pass_cmp_pct sca gca

# ── Trait recipe table ───────────────────────────────────────────────────────
# Each entry:  trait_name → {method, metrics, source}
#
# method "percentile"     → single metric percentile / 100
# method "avg_percentile" → mean of the available metric percentiles / 100
# method "pct_value"      → metric_value / 100             (e.g. pass_cmp_pct)
# method "game_attr"      → game_attribute_value / 99; falls back to qualitative
# method "qualitative"    → no computation; LLM handles it
#
_TRAIT_RECIPES: dict[str, dict] = {
    # ── Ball progression ─────────────────────────────────────────────────────
    "progressive_passing":     {"method": "percentile",     "metrics": ["prg_passes"],                  "source": "real"},
    "ball_carrying":           {"method": "percentile",     "metrics": ["prg_carries"],                 "source": "real"},
    "line_breaking_passing":   {"method": "percentile",     "metrics": ["prg_passes"],                  "source": "proxy"},
    "vertical_passing":        {"method": "percentile",     "metrics": ["prg_passes"],                  "source": "proxy"},
    "early_forward_passing":   {"method": "percentile",     "metrics": ["prg_passes"],                  "source": "proxy"},

    # ── Attacking output ─────────────────────────────────────────────────────
    "box_movement":                {"method": "percentile",     "metrics": ["att_pen_touches"],             "source": "real"},
    "final_third_decision_making": {"method": "avg_percentile", "metrics": ["xg", "xa", "sca"],             "source": "real"},
    "attacks_crosses":             {"method": "avg_percentile", "metrics": ["att_pen_touches", "aerial_won_pct"], "source": "proxy"},
    "runs_in_behind":              {"method": "avg_percentile", "metrics": ["shots", "att_pen_touches"],    "source": "proxy"},
    "crossing_quality":            {"method": "percentile",     "metrics": ["sca"],                         "source": "proxy"},
    "overlap_underlap_timing":     {"method": "avg_percentile", "metrics": ["prg_carries", "att_pen_touches"], "source": "proxy"},

    # ── Linking / creativity ─────────────────────────────────────────────────
    "link_play":              {"method": "avg_percentile", "metrics": ["assists", "xa", "sca"],       "source": "proxy"},
    "combination_play":       {"method": "avg_percentile", "metrics": ["assists", "sca"],             "source": "proxy"},
    "receives_between_lines": {"method": "avg_percentile", "metrics": ["sca", "att_pen_touches"],     "source": "proxy"},

    # ── Defensive / ball-winning ─────────────────────────────────────────────
    "ball_winning":            {"method": "avg_percentile", "metrics": ["tackles", "interceptions"],  "source": "real"},
    "duel_strength":           {"method": "percentile",     "metrics": ["tackles"],                   "source": "proxy"},
    "1v1_defending":           {"method": "percentile",     "metrics": ["tackles"],                   "source": "proxy"},
    "defensive_concentration": {"method": "percentile",     "metrics": ["interceptions"],             "source": "proxy"},
    "defensive_range":         {"method": "avg_percentile", "metrics": ["tackles", "interceptions"],  "source": "proxy"},
    "defends_large_spaces":    {"method": "avg_percentile", "metrics": ["tackles", "interceptions"],  "source": "proxy"},
    "box_defending":           {"method": "avg_percentile", "metrics": ["tackles", "interceptions"],  "source": "proxy"},
    "out_of_possession_work":  {"method": "avg_percentile", "metrics": ["tackles", "interceptions"],  "source": "proxy"},

    # ── Pressing ─────────────────────────────────────────────────────────────
    "pressing_intensity": {"method": "avg_percentile", "metrics": ["tackles", "interceptions"], "source": "proxy"},
    "pressing_triggers":  {"method": "avg_percentile", "metrics": ["tackles", "interceptions"], "source": "proxy"},
    "counter_pressing":   {"method": "avg_percentile", "metrics": ["tackles", "interceptions"], "source": "proxy"},
    "work_rate":          {"method": "avg_percentile", "metrics": ["tackles", "interceptions", "prg_carries"], "source": "proxy"},

    # ── Technical / on-ball security ─────────────────────────────────────────
    "technical_security":   {"method": "pct_value",     "metrics": ["pass_cmp_pct"],                "source": "real"},
    "comfortable_on_ball":  {"method": "pct_value",     "metrics": ["pass_cmp_pct"],                "source": "real"},
    "secure_first_touch":   {"method": "pct_value",     "metrics": ["pass_cmp_pct"],                "source": "proxy"},
    "plays_under_pressure": {"method": "pct_value",     "metrics": ["pass_cmp_pct"],                "source": "proxy"},
    "press_resistance":     {"method": "avg_percentile","metrics": ["pass_cmp_pct", "prg_passes"],  "source": "proxy"},
    "decision_making":      {"method": "avg_percentile","metrics": ["pass_cmp_pct", "sca"],         "source": "proxy"},
    "distribution":         {"method": "pct_value",     "metrics": ["pass_cmp_pct"],                "source": "proxy"},

    # ── Physical (game attributes; fall back to qualitative when unavailable) ─
    "recovery_pace":  {"method": "game_attr", "metrics": ["sprint_speed"], "source": "game_attribute"},
    "recovery_speed": {"method": "game_attr", "metrics": ["sprint_speed"], "source": "game_attribute"},
    "pace_in_space":  {"method": "game_attr", "metrics": ["sprint_speed"], "source": "game_attribute"},
    "stamina":        {"method": "game_attr", "metrics": ["stamina"],      "source": "game_attribute"},
    "repeat_sprints": {"method": "game_attr", "metrics": ["stamina"],      "source": "game_attribute"},

    # ── Duels and aerials (Sofascore) ────────────────────────────────────────
    # Every club profile asks for duel_strength; nothing could score it while the
    # only defensive number available was interceptions.
    "duel_strength":   {"method": "pct_value",     "metrics": ["duels_won_pct"],     "source": "real"},
    "aerial_strength": {"method": "pct_value",     "metrics": ["aerial_won_pct"],    "source": "real"},
    "aerial_presence": {"method": "pct_value",     "metrics": ["aerial_won_pct"],    "source": "real"},
    "aerial_threat":   {"method": "pct_value",     "metrics": ["aerial_won_pct"],    "source": "proxy"},
    "1v1_defending":   {"method": "avg_percentile","metrics": ["tackles", "ground_duels_won_pct"], "source": "real"},
    "1v1_ability":     {"method": "pct_value",     "metrics": ["dribble_success_pct"], "source": "real"},
    "box_defending":   {"method": "avg_percentile","metrics": ["clearances", "aerial_won_pct"],    "source": "real"},

    # ── Goalkeeping (Sofascore) ──────────────────────────────────────────────
    # These make a goalkeeper brief scorable at all; previously every GK
    # candidate came out at zero trait coverage.
    "reliable_shot_stopping": {"method": "percentile", "metrics": ["saves"],        "source": "real"},
    "long_distribution":      {"method": "pct_value",  "metrics": ["pass_cmp_pct"], "source": "proxy"},
    "sweeper_keeper":         {"method": "avg_percentile", "metrics": ["clearances", "interceptions"], "source": "proxy"},

    # ── Creation (Sofascore key passes / big chances) ────────────────────────
    "crossing_quality":  {"method": "avg_percentile", "metrics": ["crosses", "key_passes"], "source": "proxy"},
    "dead_ball_delivery": {"method": "percentile",    "metrics": ["key_passes"],          "source": "proxy"},
    "set_piece_presence": {"method": "avg_percentile","metrics": ["aerial_won_pct", "goals"], "source": "proxy"},

    # ── Qualitative — genuinely not derivable from any available source ───────
    "hold_up_play":           {"method": "qualitative", "metrics": [], "source": "qualitative"},
    "touchline_width":        {"method": "qualitative", "metrics": [], "source": "qualitative"},
    "positional_flexibility": {"method": "qualitative", "metrics": [], "source": "qualitative"},
    "tactical_discipline":    {"method": "qualitative", "metrics": [], "source": "qualitative"},
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


_QUALITATIVE = {"score": None, "source": "qualitative"}


def _score_recipe(
    recipe: dict,
    raw_metrics: dict,
    game_attributes: dict,
) -> dict | None:
    """
    Compute a single trait score from a recipe.
    Returns {score, source}, or None if the trait's metrics are absent entirely.
    """
    method  = recipe["method"]
    metrics = recipe.get("metrics", [])
    source  = recipe["source"]

    if method == "qualitative":
        return dict(_QUALITATIVE)

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
        # A partial recipe is a weaker signal than a complete one.
        effective_source = source if len(available) == len(metrics) else "proxy"
        return {
            "score":  round(sum(available) / len(available) / 100, 3),
            "source": effective_source,
        }

    if method == "pct_value":
        val = _get_pct_value(raw_metrics, metrics[0])
        if val is None:
            return None
        return {"score": round(min(val / 100, 1.0), 3), "source": source}

    if method == "game_attr":
        val = game_attributes.get(metrics[0])
        if val is None:
            # No synthetic rating available — hand it to the LLM rather than drop it.
            return dict(_QUALITATIVE)
        return {"score": round(float(val) / 99, 3), "source": "game_attribute"}

    return None


# ── Public API ────────────────────────────────────────────────────────────────

def compute_trait_scores(
    raw_metrics: dict,
    position: str = "",
    game_attributes: dict | None = None,
) -> dict[str, dict]:
    """
    Compute trait_scores for a player.

    Args:
        raw_metrics:     {metric: {value, per90, percentile}}  from get_position_cohort
        position:        broad FBref code(s), e.g. "FW" or "FW,MF". Accepted for call-site
                         compatibility; scoring is position-independent (see module docstring).
        game_attributes: optional {sprint_speed, stamina, ...} from the SQLite DB

    Returns:
        {trait_name: {score: 0.0–1.0 | None, source: str}}
        Qualitative traits have score=None (handled by the LLM in M4).
        Traits whose metrics are missing from raw_metrics are omitted, which the
        fit engine surfaces as an explicit coverage gap.
    """
    game_attributes = game_attributes or {}

    trait_scores: dict[str, dict] = {}
    for trait, recipe in _TRAIT_RECIPES.items():
        result = _score_recipe(recipe, raw_metrics, game_attributes)
        if result is not None:
            trait_scores[trait] = result

    return trait_scores


def known_traits() -> set[str]:
    """The full schema-locked trait vocabulary this engine can report on."""
    return set(_TRAIT_RECIPES)
