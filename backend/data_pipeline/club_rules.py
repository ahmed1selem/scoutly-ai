"""
The rule layer: measured indicators to tactical labels and trait requirements.

This is the one part of a club profile that no data source hands you. What
changes from the profiles that shipped before is *what the rule reads*. The old
file assigned a trait list from a style archetype, so 96 clubs shared 13 blocks
and any two possession sides were literally identical. Here each rule fires on a
measured percentile, cites the number that fired it, and the citation travels
into the profile's evidence.

A rule is: "when this indicator sits above (or below) this percentile of the
Big 5, this position needs these traits". Thresholds are deliberately coarse —
percentile 70 rather than 68.4 — because the underlying labels are ordinal.

Every trait named here must exist in trait_engine._TRAIT_RECIPES, or the fit
engine cannot score it and it silently becomes an unmet requirement. There is a
test for that.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: The six position buckets the fit engine maps FBref positions onto.
POSITIONS = ("goalkeeper", "centre_back", "fullback", "midfielder", "winger", "striker")


def ordinal(n: int) -> str:
    """83 -> '83rd'. These strings end up in the report, so 83th will not do."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@dataclass(frozen=True)
class TraitRule:
    """Traits a measured extreme adds to each position."""
    key: str
    indicator: str
    #: "high" fires at or above `percentile`; "low" fires at or below it.
    direction: str
    percentile: int
    #: Human-readable reason, formatted with the value and percentile.
    reason: str
    traits: dict[str, tuple[str, ...]] = field(default_factory=dict)
    poor_fit: tuple[str, ...] = ()

    def describe(self, value: float, pct: int) -> str:
        return self.reason.format(value=value, pct=ordinal(pct))

    def fires(self, pct: int | None) -> bool:
        if pct is None:
            return False
        return pct >= self.percentile if self.direction == "high" else pct <= self.percentile


# ── Baseline: what every club needs from a position, regardless of style ──────
# Four traits, not two. A requirement list is the denominator of the fit score,
# so a position where no style rule fires would otherwise be scored against two
# traits and hand out near-perfect fits for meeting the bare minimum.
BASELINE: dict[str, tuple[str, ...]] = {
    "goalkeeper":  ("reliable_shot_stopping", "distribution",
                    "plays_under_pressure", "aerial_presence"),
    "centre_back": ("duel_strength", "defensive_concentration",
                    "aerial_strength", "box_defending"),
    "fullback":    ("stamina", "1v1_defending", "work_rate", "technical_security"),
    "midfielder":  ("decision_making", "tactical_discipline",
                    "press_resistance", "out_of_possession_work"),
    "winger":      ("1v1_ability", "final_third_decision_making",
                    "work_rate", "secure_first_touch"),
    "striker":     ("box_movement", "link_play",
                    "out_of_possession_work", "secure_first_touch"),
}


RULES: tuple[TraitRule, ...] = (
    TraitRule(
        key="possession_side",
        indicator="possession_pct", direction="high", percentile=65,
        reason="keeps the ball ({value:.1f}%, {pct} percentile of the Big 5)",
        traits={
            "goalkeeper":  ("comfortable_on_ball", "plays_under_pressure"),
            "centre_back": ("progressive_passing", "press_resistance"),
            "fullback":    ("technical_security", "positional_flexibility"),
            "midfielder":  ("press_resistance", "receives_between_lines"),
            "winger":      ("combination_play", "secure_first_touch"),
            "striker":     ("hold_up_play", "link_play"),
        },
        poor_fit=("players_uncomfortable_under_pressure",
                  "centre_backs_with_limited_passing"),
    ),
    TraitRule(
        key="low_possession_side",
        indicator="possession_pct", direction="low", percentile=30,
        reason="concedes the ball by design ({value:.1f}%, {pct} percentile)",
        traits={
            "goalkeeper":  ("long_distribution",),
            "centre_back": ("box_defending", "aerial_strength"),
            "fullback":    ("defensive_concentration",),
            "midfielder":  ("out_of_possession_work", "ball_winning"),
            "winger":      ("work_rate", "repeat_sprints"),
            "striker":     ("hold_up_play", "work_rate"),
        },
    ),
    TraitRule(
        key="high_press",
        indicator="ppda", direction="high", percentile=70,
        reason="presses high ({value:.1f} passes allowed per defensive action, "
               "{pct} percentile)",
        traits={
            "goalkeeper":  ("sweeper_keeper",),
            "centre_back": ("defends_large_spaces",),
            "fullback":    ("counter_pressing", "recovery_speed"),
            "midfielder":  ("counter_pressing", "defensive_range"),
            "winger":      ("pressing_intensity", "work_rate"),
            "striker":     ("pressing_triggers", "out_of_possession_work"),
        },
        poor_fit=("low_work_rate_forwards", "slow_transition_players"),
    ),
    TraitRule(
        key="pins_opponents_back",
        indicator="press_height_pct", direction="high", percentile=70,
        reason="keeps opponents in their own half (only {value:.1f}% of their "
               "passes reach ours, {pct} percentile)",
        traits={
            "centre_back": ("recovery_pace", "defends_large_spaces"),
            "fullback":    ("recovery_speed", "stamina"),
            "midfielder":  ("counter_pressing",),
        },
        poor_fit=("slow_centre_backs",),
    ),
    TraitRule(
        key="direct",
        indicator="directness_pct", direction="high", percentile=70,
        reason="plays direct ({value:.1f}% of passes are long, {pct} percentile)",
        traits={
            "goalkeeper":  ("long_distribution",),
            "centre_back": ("aerial_strength", "early_forward_passing"),
            "midfielder":  ("vertical_passing", "duel_strength"),
            "winger":      ("pace_in_space",),
            "striker":     ("aerial_threat", "runs_in_behind"),
        },
        poor_fit=("weak_aerial_profiles_in_key_positions",),
    ),
    TraitRule(
        key="short_build_up",
        indicator="directness_pct", direction="low", percentile=30,
        reason="builds short ({value:.1f}% of passes are long, {pct} percentile)",
        traits={
            "goalkeeper":  ("comfortable_on_ball",),
            "centre_back": ("progressive_passing", "line_breaking_passing"),
            "midfielder":  ("press_resistance",),
        },
    ),
    TraitRule(
        key="transition_threat",
        indicator="fast_break_shots_pm", direction="high", percentile=70,
        reason="attacks in transition ({value:.2f} fast-break shots a match, "
               "{pct} percentile)",
        traits={
            "centre_back": ("recovery_pace",),
            "midfielder":  ("ball_carrying", "early_forward_passing"),
            "winger":      ("pace_in_space", "repeat_sprints"),
            "striker":     ("runs_in_behind",),
        },
        poor_fit=("slow_transition_players",),
    ),
    TraitRule(
        key="cross_heavy",
        indicator="cross_rate_pm", direction="high", percentile=70,
        reason="creates from wide areas ({value:.1f} crosses a match, "
               "{pct} percentile)",
        traits={
            "fullback":    ("crossing_quality", "overlap_underlap_timing"),
            "winger":      ("crossing_quality", "touchline_width"),
            "striker":     ("attacks_crosses", "aerial_threat"),
        },
        poor_fit=("wide_players_without_1v1_threat",),
    ),
    TraitRule(
        key="set_piece_reliant",
        indicator="set_piece_goal_share_pct", direction="high", percentile=75,
        reason="scores from set pieces ({value:.1f}% of goals headed or from a "
               "free kick, {pct} percentile)",
        traits={
            "centre_back": ("set_piece_presence", "aerial_strength"),
            "fullback":    ("dead_ball_delivery",),
            "midfielder":  ("dead_ball_delivery",),
            "striker":     ("set_piece_presence",),
        },
    ),
    TraitRule(
        key="box_focused",
        indicator="box_shot_share_pct", direction="high", percentile=70,
        reason="works the ball into the box ({value:.1f}% of shots from inside, "
               "{pct} percentile)",
        traits={
            "winger":  ("combination_play",),
            "striker": ("box_movement", "attacks_crosses"),
        },
    ),
    TraitRule(
        key="dribble_led",
        indicator="dribbles_pm", direction="high", percentile=70,
        reason="carries the ball ({value:.1f} successful dribbles a match, "
               "{pct} percentile)",
        traits={
            "midfielder": ("ball_carrying",),
            "winger":     ("ball_carrying", "1v1_ability"),
        },
    ),
    TraitRule(
        key="aerially_dominant",
        indicator="aerial_win_pct", direction="high", percentile=70,
        reason="wins its aerial duels ({value:.1f}%, {pct} percentile)",
        traits={
            "centre_back": ("aerial_strength",),
            "striker":     ("aerial_threat",),
        },
    ),
    TraitRule(
        key="aerially_weak",
        indicator="aerial_win_pct", direction="low", percentile=25,
        reason="loses its aerial duels ({value:.1f}%, {pct} percentile) — a "
               "gap to recruit against",
        traits={
            "centre_back": ("aerial_strength",),
            "goalkeeper":  ("aerial_presence",),
        },
    ),
    TraitRule(
        key="defensively_exposed",
        indicator="shots_against_pm", direction="low", percentile=25,
        reason="faces a high shot volume ({value:.1f} a match, {pct} percentile "
               "for shots faced)",
        traits={
            "goalkeeper":  ("reliable_shot_stopping",),
            "centre_back": ("box_defending",),
            "midfielder":  ("out_of_possession_work",),
        },
    ),
    TraitRule(
        key="high_line",
        indicator="offside_trap_pm", direction="high", percentile=70,
        reason="defends a high line ({value:.2f} opponents caught offside a "
               "match, {pct} percentile)",
        traits={
            "goalkeeper":  ("sweeper_keeper",),
            "centre_back": ("recovery_pace", "defends_large_spaces"),
            "fullback":    ("recovery_speed",),
        },
        poor_fit=("slow_centre_backs",),
    ),
    TraitRule(
        key="deep_line",
        indicator="offside_trap_pm", direction="low", percentile=25,
        reason="defends deep ({value:.2f} opponents caught offside a match, "
               "{pct} percentile)",
        traits={
            "centre_back": ("box_defending", "defensive_concentration"),
            "midfielder":  ("out_of_possession_work",),
        },
    ),
)


# ── Style classification ──────────────────────────────────────────────────────
# Styles are scored, not assigned: each candidate accumulates evidence from the
# indicators that define it, and the strongest becomes primary. A club with no
# strong signal anywhere lands on "balanced", which is a finding, not a default.

@dataclass(frozen=True)
class StyleRule:
    value: str
    #: (indicator, direction, weight) — direction as in TraitRule.
    signals: tuple[tuple[str, str, float], ...]


STYLES: tuple[StyleRule, ...] = (
    StyleRule("possession_control", (
        ("possession_pct", "high", 1.0),
        ("territory_pct", "high", 0.6),
        ("pass_accuracy_pct", "high", 0.6),
        ("directness_pct", "low", 0.5),
    )),
    StyleRule("high_pressing", (
        ("ppda", "high", 1.0),
        ("press_height_pct", "high", 0.7),
        ("offside_trap_pm", "high", 0.5),
        ("recoveries_pm", "high", 0.3),
    )),
    StyleRule("direct_transition", (
        ("directness_pct", "high", 1.0),
        ("fast_break_shots_pm", "high", 0.8),
        ("possession_pct", "low", 0.4),
    )),
    StyleRule("low_block_counter", (
        ("possession_pct", "low", 1.0),
        ("press_height_pct", "low", 0.8),
        ("ppda", "low", 0.6),
        ("fast_break_shots_pm", "high", 0.4),
    )),
    StyleRule("wing_play", (
        ("cross_rate_pm", "high", 1.0),
        ("dribbles_pm", "high", 0.4),
    )),
    StyleRule("set_piece_heavy", (
        ("set_piece_goal_share_pct", "high", 1.0),
        ("corner_rate_pm", "high", 0.4),
        ("aerial_win_pct", "high", 0.4),
    )),
)


def _signal_strength(pct: int | None, direction: str) -> float:
    """
    How far a percentile sits into the tail the signal cares about, 0-1.

    Nothing below the 60th (or above the 40th, inverted) contributes: a club at
    the median is not evidence of anything, and letting it count is how every
    club ends up looking mildly like every style.
    """
    if pct is None:
        return 0.0
    if direction == "high":
        return max(0.0, (pct - 60) / 40)
    return max(0.0, (40 - pct) / 40)


def score_styles(ranks: dict[str, int]) -> list[tuple[str, float]]:
    """Return [(style, 0-1 score)] sorted strongest first."""
    scored: list[tuple[str, float]] = []
    for style in STYLES:
        total = weight_sum = 0.0
        for indicator, direction, weight in style.signals:
            weight_sum += weight
            total += weight * _signal_strength(ranks.get(indicator), direction)
        if weight_sum:
            scored.append((style.value, round(total / weight_sum, 3)))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored
