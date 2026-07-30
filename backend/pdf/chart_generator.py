"""
Guaranteed programmatic charts — always generated, independent of the LLM analysis agent.

These run inside the PDF generator (not the LLM agent) so they are deterministic,
consistently styled, and always present in the report.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ── Design tokens (mirrors report.html) ──────────────────────────────────────
# A light palette: the report is a document that gets printed, and a dark chart
# on white paper both wastes ink and breaks the page.
DARK    = "#FFFFFF"   # figure background
SURFACE = "#F6F7F9"   # plot area
BORDER  = "#D8DCE2"
TEXT    = "#14181F"
MUTED   = "#5C6673"
ACCENT  = "#1F6FEB"
ACCENT2 = "#B45309"
GOLD    = "#B45309"
GREEN   = "#15803D"
RED     = "#B91C1C"
PURPLE  = "#6D28D9"

PLAYER_COLORS = [ACCENT, ACCENT2, GOLD, PURPLE, "#4ECDC4"]


def _style() -> None:
    plt.rcParams.update({
        "figure.facecolor":  DARK,
        "axes.facecolor":    SURFACE,
        "axes.edgecolor":    BORDER,
        "text.color":        TEXT,
        "axes.labelcolor":   MUTED,
        "xtick.color":       MUTED,
        "ytick.color":       MUTED,
        "grid.color":        BORDER,
        "grid.alpha":        0.4,
        "legend.facecolor":  SURFACE,
        "legend.edgecolor":  BORDER,
        "font.family":       "DejaVu Sans",
        "font.size":         9,
        "axes.titlepad":     14,
    })


def _score_color(score: float) -> str:
    """
    Colour by demonstrated fit. The thresholds are lower than they were for the
    old coverage-normalised score: demonstrated fit counts unmeasured
    requirements as unmet, so 55 is a strong result, not a mediocre one.
    """
    if score >= 55:
        return GREEN
    if score >= 35:
        return GOLD
    return RED


def _save(fig: plt.Figure, path: str) -> str:
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=DARK, edgecolor="none")
    plt.close(fig)
    return path


# ── Chart 1: Fit Score Comparison ────────────────────────────────────────────

def generate_fit_bar_chart(players: list[dict], output_path: str) -> str:
    """Horizontal bar chart — all candidates ranked by tactical fit score."""
    _style()

    pairs = []
    for p in players:
        # demonstrated_fit is the ranking number: it counts requirements we could
        # not measure as unmet, so thin evidence cannot inflate a bar.
        raw = p.get("demonstrated_fit")
        if raw is None:
            raw = p.get("measurable_fit")
        try:
            score = float(raw)
        except (TypeError, ValueError):
            score = 0.0
        pairs.append((score, p.get("name", "Unknown")[:22]))

    # barh draws the first item at the bottom, so ascending order puts the best
    # candidate at the top and matches the table's ordering.
    pairs.sort()
    scores  = [s for s, _ in pairs]
    names   = [n for _, n in pairs]
    colors  = [_score_color(s) for s in scores]

    fig, ax = plt.subplots(figsize=(8, max(3.0, len(players) * 0.75 + 1.2)))
    fig.patch.set_facecolor(DARK)
    ax.set_facecolor(SURFACE)

    # Background track
    ax.barh(names, [100] * len(names), color=BORDER, height=0.45, alpha=0.25)
    bars = ax.barh(names, scores, color=colors, height=0.45, edgecolor="none")

    for bar, score in zip(bars, scores):
        ax.text(
            bar.get_width() + 1.5,
            bar.get_y() + bar.get_height() / 2,
            f"{score:.0f}",
            va="center", ha="left",
            color=TEXT, fontsize=11, fontweight="bold",
        )

    ax.set_xlim(0, 118)
    ax.set_xlabel("Demonstrated fit (0-100)", fontsize=9)
    ax.set_title("Demonstrated fit — share of the club’s requirement met",
                 color=TEXT, fontsize=12, fontweight="bold")
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(BORDER)
    ax.tick_params(axis="y", labelsize=10, labelcolor=TEXT)

    for threshold, color, label in [(80, GREEN, "Strong"), (60, GOLD, "Decent")]:
        ax.axvline(threshold, color=color, linestyle="--", alpha=0.3, linewidth=0.8)
        ax.text(threshold + 0.5, len(names) - 0.55, label, color=color, fontsize=7, alpha=0.7)

    plt.tight_layout()
    return _save(fig, output_path)


# ── Chart 2: Trait Radar Overlay ──────────────────────────────────────────────

_RADAR_CATS = {
    "Attack":    ["finishing", "goals_per_90", "shots_on_target", "penalty_area_presence",
                  "shot_volume", "goal_involvement"],
    "Pressing":  ["pressing_intensity", "work_rate", "counter_pressing", "press_resist",
                  "defensive_activity", "high_press"],
    "Retention": ["ball_retention", "dribbling", "progressive_carries", "touch_quality",
                  "carry_progression", "hold_up_play"],
    "Creativity":["progressive_passing", "chance_creation", "through_balls", "key_passes",
                  "technical_security", "pass_range"],
    "Defense":   ["tackling", "interceptions", "aerial_duels", "defensive_positioning",
                  "recovery_runs", "press_resistance"],
    "Athleticism":["sprint_speed", "stamina", "acceleration", "agility",
                   "physical_presence", "dynamic_movement"],
}
# ── Radar chart removed ──────────────────────────────────────────────────────
# It filled every axis it had no data for with a hardcoded 40.0, so four of six
# axes were fabricated for every player. There is no honest way to draw a
# six-axis profile from the metrics actually available.



# ── Chart 3: Per-Player Percentile Bars ──────────────────────────────────────

def _percentile_metrics(player: dict) -> list[tuple[str, float]]:
    """Extract (label, percentile 0-100) pairs from a player dict."""
    items: list[tuple[str, float]] = []

    # From FBref raw_metrics: use percentile field when available
    rm = player.get("raw_metrics") or {}
    METRIC_LABELS = {
        "goals":               "Goals /90",
        "xg":                  "xG /90",
        "assists":             "Assists /90",
        "pass_cmp_pct":        "Pass Acc %",
        "tackles":             "Tackles /90",
        "interceptions":       "Interceptions /90",
        "prg_carries":         "Prog Carries /90",
        "prg_passes":          "Prog Passes /90",
        "sca":                 "Shot-Creating Actions",
        "gca":                 "Goal-Creating Actions",
    }
    for key, label in METRIC_LABELS.items():
        block = rm.get(key, {})
        if not isinstance(block, dict):
            continue
        pct = block.get("percentile")
        if pct is not None:
            try:
                items.append((label, float(pct)))
                if len(items) >= 8:
                    break
            except (TypeError, ValueError):
                pass

    # Deliberately NOT supplemented with per_trait_breakdown scores: those are
    # 0-1 trait scores, not cohort percentiles, and plotting them on the same
    # "th percentile" axis mislabels them. Nor is a placeholder invented when a
    # player has no percentiles — an empty list tells the caller to skip the
    # chart rather than draw a fabricated 50th.
    return items[:8]


def generate_percentile_bars(player: dict, output_path: str) -> str:
    """Horizontal percentile bars for a single player's key metrics."""
    _style()

    metrics = _percentile_metrics(player)
    if not metrics:
        raise ValueError(
            f"no percentile data for {player.get('name', 'player')} — chart skipped"
        )
    labels  = [m[0] for m in metrics]
    values  = [m[1] for m in metrics]
    colors  = [GREEN if v >= 75 else ACCENT if v >= 50 else GOLD if v >= 30 else RED
               for v in values]

    fig, ax = plt.subplots(figsize=(7.5, max(3.0, len(metrics) * 0.52 + 1.5)))
    fig.patch.set_facecolor(DARK)
    ax.set_facecolor(SURFACE)

    y = list(range(len(labels)))
    ax.barh(y, [100] * len(labels), color=BORDER, height=0.5, alpha=0.2)
    ax.barh(y, values, color=colors, height=0.5, edgecolor="none")

    for j, (val, label) in enumerate(zip(values, labels)):
        ax.text(-1.5, j, label, va="center", ha="right", color=TEXT, fontsize=8.5)
        pct_label = f"{val:.0f}th" if val < 100 else "100"
        ax.text(val + 1, j, pct_label, va="center", ha="left",
                color=TEXT, fontsize=8.5, fontweight="bold")

    ax.set_xlim(-38, 120)
    ax.set_ylim(-0.7, len(labels) - 0.3)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0", "25th", "50th", "75th", "Top"])
    ax.set_yticks([])
    ax.spines[["top", "left", "right"]].set_visible(False)
    ax.spines["bottom"].set_color(BORDER)
    ax.axvline(75, color=GREEN, linestyle="--", alpha=0.2, linewidth=0.8)
    ax.axvline(50, color=MUTED, linestyle="--", alpha=0.15, linewidth=0.8)

    name = player.get("name", "Player")
    pos  = player.get("position", "")
    ax.set_title(f"{name}  ·  {pos}  — Performance Percentiles",
                 color=TEXT, fontsize=10, fontweight="bold")

    plt.tight_layout()
    return _save(fig, output_path)


# ── Chart 4: Market value vs demonstrated fit ────────────────────────────────

def generate_wage_fit_scatter(players: list[dict], output_path: str) -> str:
    """
    Scatter: Transfermarkt market value (x) against demonstrated fit (y).

    Previously plotted weekly wage, which was a single league-tier constant for
    every candidate — the chart came out as a vertical line and its "bubble size
    = market value" caption described data the report did not have. Market value
    is real, per-player and dated, so it carries actual variance.
    """
    _style()

    priced = [p for p in players if float(p.get("value_eur", 0) or 0) > 0]
    if len(priced) < 2:
        raise ValueError("fewer than two candidates have a published market value")

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor(DARK)
    ax.set_facecolor(SURFACE)

    values, scores = [], []
    for i, player in enumerate(priced):
        value = float(player.get("value_eur", 0) or 0) / 1_000_000
        raw = player.get("demonstrated_fit")
        if raw is None:
            raw = player.get("measurable_fit")
        score = float(raw or 0)
        name  = player.get("name", f"P{i + 1}")[:16]
        color = PLAYER_COLORS[i % len(PLAYER_COLORS)]

        ax.scatter(value, score, s=200, color=color, alpha=0.85, edgecolors="white",
                   linewidth=1.0, zorder=3)
        ax.annotate(name, (value, score), xytext=(7, 5),
                    textcoords="offset points", color=TEXT, fontsize=8.5, fontweight="600")
        values.append(value)
        scores.append(score)

    mv, ms = float(np.mean(values)), float(np.mean(scores))
    ax.axvline(mv, color=BORDER, linestyle="--", alpha=0.5, linewidth=0.8)
    ax.axhline(ms, color=BORDER, linestyle="--", alpha=0.5, linewidth=0.8)
    ax.text(0.02, 0.97, "Better fit,\nlower fee", transform=ax.transAxes,
            color=GREEN, fontsize=8, va="top")
    ax.text(0.98, 0.03, "Weaker fit,\nhigher fee", transform=ax.transAxes,
            color=RED, fontsize=8, va="bottom", ha="right")

    ax.set_xlabel("Transfermarkt market value (€M)", fontsize=9)
    ax.set_ylabel("Demonstrated fit (0-100)", fontsize=9)
    ax.set_title("Fee against demonstrated fit", color=TEXT, fontsize=12, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(BORDER)
    ax.grid(True, alpha=0.25, color=BORDER)

    if len(priced) < len(players):
        ax.text(0.99, 0.99,
                f"{len(players) - len(priced)} candidate(s) omitted — no published value",
                transform=ax.transAxes, color=MUTED, fontsize=7, ha="right", va="top")

    plt.tight_layout()
    return _save(fig, output_path)


# ── Public entry point ────────────────────────────────────────────────────────

def generate_all_guaranteed_charts(
    players: list[dict[str, Any]],
    charts_dir: Path,
    run_id: str,
) -> list[str]:
    """
    Generate all guaranteed charts and return a list of absolute file paths.
    Saved under charts_dir / run_id / guaranteed/.
    Errors per chart are swallowed so one bad chart never breaks the report.
    """
    if not players:
        return []

    out = charts_dir / run_id / "guaranteed"
    out.mkdir(parents=True, exist_ok=True)
    generated: list[str] = []

    _attempts: list[tuple[str, Any, list]] = [
        ("00_fit_ranking.png",    generate_fit_bar_chart,     [players]),
        ("04_value_vs_fit.png",   generate_wage_fit_scatter,  [players]),
    ]

    for filename, fn, args in _attempts:
        path = str(out / filename)
        try:
            fn(*args, path)
            generated.append(path)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"[chart_generator] Skipped {filename}: {exc}")

    # Per-player percentile bars
    for i, player in enumerate(players[:5]):
        safe_name = re.sub(r"[^\w]", "_", player.get("name", f"player_{i}"))[:24]
        filename  = f"02_percentile_{i:02d}_{safe_name}.png"
        path      = str(out / filename)
        try:
            generate_percentile_bars(player, path)
            generated.append(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[chart_generator] Skipped {filename}: {exc}")

    return generated
