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

# ── Design tokens (mirrors CSS vars) ─────────────────────────────────────────
DARK    = "#0D1117"
SURFACE = "#161B22"
BORDER  = "#30363D"
TEXT    = "#C9D1D9"
MUTED   = "#8B949E"
ACCENT  = "#00D4AA"
ACCENT2 = "#FF6B35"
GOLD    = "#F59E0B"
GREEN   = "#22C55E"
RED     = "#EF4444"
PURPLE  = "#A78BFA"

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
    if score >= 80:
        return GREEN
    if score >= 60:
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
        raw = p.get("tactical_fit_score") or p.get("measurable_fit")
        try:
            score = float(raw)
        except (TypeError, ValueError):
            score = 0.0
        pairs.append((score, p.get("name", "Unknown")[:22]))

    pairs.sort(reverse=True)
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
    ax.set_xlabel("Tactical Fit Score", fontsize=9)
    ax.set_title("Candidate Fit Score Ranking", color=TEXT, fontsize=12, fontweight="bold")
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


def _radar_values(player: dict) -> list[float]:
    breakdown  = player.get("per_trait_breakdown", [])
    trait_dict = {}
    for t in breakdown:
        if t.get("score") is not None:
            key = re.sub(r"[^a-z0-9]+", "_", str(t["trait"]).lower())
            trait_dict[key] = float(t["score"]) * 100

    values = []
    for keywords in _RADAR_CATS.values():
        hits = [trait_dict[k] for k in keywords if k in trait_dict]
        values.append(float(np.mean(hits)) if hits else 40.0)
    return values


def generate_radar_chart(players: list[dict], output_path: str) -> str:
    """Spider/radar chart overlaying all players on shared axes."""
    _style()

    categories = list(_RADAR_CATS.keys())
    N      = len(categories)
    angles = np.linspace(0, 2 * np.pi, N, endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(7, 7), subplot_kw={"polar": True})
    fig.patch.set_facecolor(DARK)
    ax.set_facecolor(SURFACE)

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories, size=10, color=TEXT, fontweight="600")
    ax.set_ylim(0, 100)
    ax.set_yticks([25, 50, 75, 100])
    ax.set_yticklabels(["25", "50", "75", "100"], size=7, color=MUTED)
    ax.grid(color=BORDER, linewidth=0.6, alpha=0.6)
    ax.spines["polar"].set_color(BORDER)

    legend_patches: list[mpatches.Patch] = []
    for i, player in enumerate(players[:5]):
        vals   = _radar_values(player)
        data   = vals + vals[:1]
        color  = PLAYER_COLORS[i % len(PLAYER_COLORS)]
        name   = player.get("name", f"Player {i + 1}")[:20]

        ax.plot(angles, data, "o-", linewidth=2, color=color, alpha=0.9, markersize=4)
        ax.fill(angles, data, alpha=0.09, color=color)
        legend_patches.append(mpatches.Patch(color=color, label=name))

    ax.legend(
        handles=legend_patches,
        loc="upper right",
        bbox_to_anchor=(1.38, 1.18),
        fontsize=9,
        framealpha=0.25,
    )
    ax.set_title("Player Trait Radar — All Candidates", color=TEXT, fontsize=12,
                 fontweight="bold", pad=22)

    plt.tight_layout()
    return _save(fig, output_path)


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
        "progressive_carries": "Prog Carries /90",
        "progressive_passes":  "Prog Passes /90",
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
                if len(items) >= 6:
                    break
            except (TypeError, ValueError):
                pass

    # Supplement / fallback with trait breakdown scores (as percentiles)
    breakdown = player.get("per_trait_breakdown") or []
    for t in breakdown:
        if t.get("score") is None:
            continue
        label = str(t["trait"]).replace("_", " ").title()
        val   = float(t["score"]) * 100
        if label not in {i[0] for i in items}:
            items.append((label, val))
        if len(items) >= 10:
            break

    return items[:10] if items else [("Fit Score", float(player.get("measurable_fit") or 50))]


def generate_percentile_bars(player: dict, output_path: str) -> str:
    """Horizontal percentile bars for a single player's key metrics."""
    _style()

    metrics = _percentile_metrics(player)
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


# ── Chart 4: Wage vs Fit Scatter ─────────────────────────────────────────────

def generate_wage_fit_scatter(players: list[dict], output_path: str) -> str:
    """Scatter: weekly wage (x) vs tactical fit score (y) — bubble size = market value."""
    _style()

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor(DARK)
    ax.set_facecolor(SURFACE)

    wages, scores = [], []
    for i, player in enumerate(players):
        wage  = float(player.get("wage_eur", 0) or 0) / 1_000
        score = float(player.get("tactical_fit_score") or player.get("measurable_fit") or 0)
        value = float(player.get("value_eur", 0) or 0) / 1_000_000
        name  = player.get("name", f"P{i + 1}")[:16]
        color = PLAYER_COLORS[i % len(PLAYER_COLORS)]
        size  = max(80, min(700, value * 15)) if value else 180

        ax.scatter(wage, score, s=size, color=color, alpha=0.85, edgecolors=BORDER,
                   linewidth=0.5, zorder=3)
        ax.annotate(name, (wage, score), xytext=(5, 5),
                    textcoords="offset points", color=color, fontsize=8.5, fontweight="600")
        wages.append(wage)
        scores.append(score)

    if wages:
        mw, ms = float(np.mean(wages)), float(np.mean(scores))
        ax.axvline(mw, color=BORDER, linestyle="--", alpha=0.35, linewidth=0.8)
        ax.axhline(ms, color=BORDER, linestyle="--", alpha=0.35, linewidth=0.8)
        ax.text(0.02, 0.97, "High Fit\nLow Cost", transform=ax.transAxes,
                color=GREEN, fontsize=8, alpha=0.75, va="top")
        ax.text(0.75, 0.97, "High Fit\nHigher Cost", transform=ax.transAxes,
                color=GOLD, fontsize=8, alpha=0.75, va="top")

    ax.set_xlabel("Weekly Wage (€K)", fontsize=9)
    ax.set_ylabel("Tactical Fit Score", fontsize=9)
    ax.set_title("Value-for-Money Positioning", color=TEXT, fontsize=12, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(BORDER)
    ax.grid(True, alpha=0.15, color=BORDER)

    note = "Bubble size ∝ market value"
    ax.text(0.99, 0.01, note, transform=ax.transAxes,
            color=MUTED, fontsize=7, ha="right", va="bottom")

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
        ("00_fit_ranking.png",    generate_fit_bar_chart,    [players]),
        ("01_radar_overlay.png",  generate_radar_chart,      [players]),
        ("04_wage_vs_fit.png",    generate_wage_fit_scatter,  [players]),
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
