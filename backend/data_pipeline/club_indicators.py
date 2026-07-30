"""
Team indicators — the measured layer under the club tactical profiles.

Every value here is computed from a fetched number, never asserted. Each one
carries the arithmetic that produced it so the profile can cite it:

    possession_pct   58.6   averageBallPossession
    ppda              9.14  totalPassesAgainst / (tackles + interceptions + fouls)
    press_height     47.7   share of opponent passes played in our half

Indicators are then percentile-ranked across the 96 Big-5 clubs, because a raw
PPDA of 9.1 means nothing on its own — "top quintile presser in the Big 5" does.

Every indicator here comes from Sofascore. The FBref squad tables were meant to
add goalkeeper sweeper distance, tackles by pitch third, touches in the box and
set-piece share of chance creation — but FBref serves those columns empty at
squad level, exactly as it does for players. Of 27 columns in the possession
squad table, 7 arrive filled and none of the ones wanted here are among them;
the advanced goalkeeping page would not load at all. cache_builder still stages
whatever the squad tables return, so if FBref starts serving them the data is
already on disk — but nothing in this module depends on it.

Defensive line height therefore comes from opponents caught offside per match.
It is a proxy, not a measurement of distance, but it holds up: Barcelona's
2024-25 offside trap tops the Big 5 at 4.76 a match, 52% clear of second, and
the deep blocks sit at the bottom.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Indicator:
    """One measurable team property."""
    key: str
    label: str
    formula: str
    #: Computes the value from a Sofascore stats dict, or None if unmeasurable.
    compute: Callable[[dict], float | None]
    #: True when a LOW value means "more of the thing" (PPDA, goals conceded).
    inverted: bool = False
    unit: str = ""
    source: str = "sofascore"


def _num(stats: dict, key: str) -> float | None:
    value = stats.get(key)
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _ratio(stats: dict, numerator: str, *denominators: str,
           scale: float = 1.0) -> float | None:
    top = _num(stats, numerator)
    if top is None:
        return None
    bottom = 0.0
    for name in denominators:
        part = _num(stats, name)
        if part is None:
            return None
        bottom += part
    if bottom <= 0:
        return None
    return top / bottom * scale


def _per_match(stats: dict, key: str) -> float | None:
    total = _num(stats, key)
    matches = _num(stats, "matches")
    if total is None or not matches:
        return None
    return total / matches


# ── Sofascore-derived indicators ──────────────────────────────────────────────

def _ppda(stats: dict) -> float | None:
    """
    Passes allowed per defensive action — the standard pressing measure.

    Sofascore gives opponent passes and our defensive actions as season totals,
    so this is the plain definition rather than an approximation. Lower presses
    harder, hence inverted=True downstream.
    """
    opp_passes = _num(stats, "totalPassesAgainst")
    actions = sum(
        v for v in (_num(stats, k) for k in ("tackles", "interceptions", "fouls"))
        if v is not None
    )
    if opp_passes is None or actions <= 0:
        return None
    return opp_passes / actions


def _press_height(stats: dict) -> float | None:
    """
    Share of the opponent's passes that they play inside our half.

    Sofascore's *Against fields mirror the for-fields from the opponent's point
    of view, so oppositionHalfPassesTotalAgainst is opponent passes in THEIR
    opposition half — which is ours. A team that pins opponents back scores low.
    """
    return _ratio(stats, "oppositionHalfPassesTotalAgainst",
                  "oppositionHalfPassesTotalAgainst", "ownHalfPassesTotalAgainst",
                  scale=100.0)


def _territory(stats: dict) -> float | None:
    """Share of our own passes played in the opposition half."""
    return _ratio(stats, "totalOppositionHalfPasses",
                  "totalOppositionHalfPasses", "totalOwnHalfPasses", scale=100.0)


def _set_piece_goal_share(stats: dict) -> float | None:
    """
    Headed and free-kick goals as a share of goals scored.

    Penalties are excluded: they are a set piece in the laws but say nothing
    about whether a team is built to attack dead balls.
    """
    headed = _num(stats, "headedGoals")
    free_kick = _num(stats, "freeKickGoals")
    goals = _num(stats, "goalsScored")
    if headed is None or free_kick is None or not goals:
        return None
    return (headed + free_kick) / goals * 100.0


INDICATORS: list[Indicator] = [
    Indicator("possession_pct", "possession", "averageBallPossession",
              lambda s: _num(s, "averageBallPossession"), unit="%"),
    Indicator("pass_accuracy_pct", "pass accuracy", "accuratePassesPercentage",
              lambda s: _num(s, "accuratePassesPercentage"), unit="%"),
    Indicator("ppda", "PPDA",
              "totalPassesAgainst / (tackles + interceptions + fouls)",
              _ppda, inverted=True),
    Indicator("press_height_pct", "opponent passes in our half",
              "oppositionHalfPassesTotalAgainst / all opponent passes",
              _press_height, inverted=True, unit="%"),
    Indicator("territory_pct", "our passes in their half",
              "totalOppositionHalfPasses / all our passes", _territory, unit="%"),
    Indicator("directness_pct", "long balls",
              "totalLongBalls / totalPasses",
              lambda s: _ratio(s, "totalLongBalls", "totalPasses", scale=100.0),
              unit="%"),
    Indicator("fast_break_shots_pm", "fast-break shots",
              "fastBreakShots / matches",
              lambda s: _per_match(s, "fastBreakShots"), unit="/match"),
    Indicator("cross_rate_pm", "crosses", "totalCrosses / matches",
              lambda s: _per_match(s, "totalCrosses"), unit="/match"),
    Indicator("corner_rate_pm", "corners", "corners / matches",
              lambda s: _per_match(s, "corners"), unit="/match"),
    Indicator("set_piece_goal_share_pct", "set-piece goal share",
              "(headedGoals + freeKickGoals) / goalsScored",
              _set_piece_goal_share, unit="%"),
    Indicator("big_chances_pm", "big chances", "bigChances / matches",
              lambda s: _per_match(s, "bigChances"), unit="/match"),
    Indicator("box_shot_share_pct", "shots from inside the box",
              "shotsFromInsideTheBox / shots",
              lambda s: _ratio(s, "shotsFromInsideTheBox", "shots", scale=100.0),
              unit="%"),
    Indicator("dribbles_pm", "successful dribbles", "successfulDribbles / matches",
              lambda s: _per_match(s, "successfulDribbles"), unit="/match"),
    Indicator("aerial_win_pct", "aerial duels won", "aerialDuelsWonPercentage",
              lambda s: _num(s, "aerialDuelsWonPercentage"), unit="%"),
    Indicator("ground_duel_win_pct", "ground duels won", "groundDuelsWonPercentage",
              lambda s: _num(s, "groundDuelsWonPercentage"), unit="%"),
    Indicator("recoveries_pm", "ball recoveries", "ballRecovery / matches",
              lambda s: _per_match(s, "ballRecovery"), unit="/match"),
    Indicator("shots_against_pm", "shots faced", "shotsAgainst / matches",
              lambda s: _per_match(s, "shotsAgainst"), inverted=True, unit="/match"),
    Indicator("errors_pm", "errors leading to a shot",
              "errorsLeadingToShotAgainst / matches",
              lambda s: _per_match(s, "errorsLeadingToShotAgainst"),
              inverted=True, unit="/match"),
    Indicator("offside_trap_pm", "opponents caught offside",
              "offsidesAgainst / matches",
              lambda s: _per_match(s, "offsidesAgainst"), unit="/match"),
]

ALL_INDICATORS = {ind.key: ind for ind in INDICATORS}


def compute_indicators(stats: dict) -> dict[str, float]:
    """Run every Sofascore indicator over one club's stats block."""
    out: dict[str, float] = {}
    for ind in INDICATORS:
        try:
            value = ind.compute(stats)
        except Exception as exc:  # a malformed field must not kill the build
            logger.debug(f"indicator {ind.key} failed: {exc}")
            value = None
        if value is not None:
            out[ind.key] = round(float(value), 3)
    return out


def percentile_rank(values: dict[str, float], inverted: bool = False) -> dict[str, int]:
    """
    Percentile of each club within the cohort, 0-100.

    Inverted indicators are flipped so that a high percentile always means "more
    of the thing the label describes" — a PPDA of 8 presses harder than 14, so
    it must rank higher, not lower.
    """
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=inverted)
    n = len(ordered)
    return {
        club: int(round(idx / max(n - 1, 1) * 100))
        for idx, (club, _) in enumerate(ordered)
    }


def rank_all(per_club: dict[str, dict[str, float]]) -> dict[str, dict[str, int]]:
    """{club: {indicator: percentile}} across every club that has the indicator."""
    ranks: dict[str, dict[str, int]] = {club: {} for club in per_club}
    for key, ind in ALL_INDICATORS.items():
        column = {club: vals[key] for club, vals in per_club.items() if key in vals}
        if len(column) < 2:
            continue
        for club, pct in percentile_rank(column, ind.inverted).items():
            ranks[club][key] = pct
    return ranks
