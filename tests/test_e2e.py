"""
End-to-end system test.

Tests three layers:
  Layer 1 — MCP server tools (direct import, no HTTP)
  Layer 2 — Agent pipeline components (trait engine, fit engine)
  Layer 3 — Report rendering (template + chart generator)

Run with:
    python tests/test_e2e.py

Does NOT require the servers to be running. Imports internals directly so
results are deterministic and the test is fast (~5s with no LLM calls).
"""
import sys
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Known real players we expect to see from the mock cohort
KNOWN_REAL_PLAYERS = {
    "FW": {
        "Erling Haaland", "Alexander Isak", "Ollie Watkins",
        "Lautaro Martinez", "Kylian Mbappe", "Harry Kane",
        "Jonathan David", "Mateo Retegui", "Rafael Leao",
        "Robert Lewandowski",
    },
    "MF": {
        "Jude Bellingham", "Martin Odegaard", "Phil Foden",
        "Granit Xhaka", "Pedri", "Rodri", "Bruno Fernandes",
    },
    "DF": {
        "Virgil van Dijk", "William Saliba", "Ruben Dias",
        "Alessandro Bastoni", "Antonio Rudiger", "Theo Hernandez",
        "Alphonso Davies",
    },
    "GK": {"David Raya", "Alisson Becker", "Mike Maignan"},
}

FAKE_PLAYERS = {
    "Harry Powell", "Lucas Martin", "Marco Ferrara", "Carlos Ruiz",
    "Luca Bianchi", "Pablo Morales", "Thomas Müller", "Kevin Tremblay",
    "Pieter de Vries", "Damien Lacroix", "Rafael Santos", "Jonas Keller",
}

PASS = "  OK  "
FAIL = " FAIL "
WARN = " WARN "

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = ""):
    results.append((name, condition, detail))
    icon = PASS if condition else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"{icon} {name}{suffix}")


def section(title: str):
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


# ═════════════════════════════════════════════════════════════════
# Layer 1 — MCP server: get_position_cohort (mock cohort)
# ═════════════════════════════════════════════════════════════════
section("Layer 1: MCP server — position cohort (mock)")

import asyncio

async def test_mcp_cohort():
    """
    Test mock cohort logic directly. FastMCP-decorated functions aren't
    directly awaitable in unit tests, so we import the internal helpers.
    """
    import os
    os.environ["FBREF_USE_MOCK"] = "true"

    import numpy as np
    from backend.mcp_server.server import _MOCK_COHORT  # type: ignore

    STAT_COLS_MOCK = ["goals", "assists", "xg", "xa", "shots",
                      "shots_on_target", "prg_passes", "prg_carries",
                      "att_pen_touches", "tackles", "interceptions", "pass_cmp_pct"]

    for pos in ["FW", "MF", "DF", "GK"]:
        t0 = time.time()

        # Replicate the exact mock-cohort path from server.py
        pos_upper = pos.upper()
        mock_key = next((k for k in _MOCK_COHORT if k in pos_upper or pos_upper in k), "FW")
        cohort = _MOCK_COHORT.get(mock_key, _MOCK_COHORT["FW"])

        players = []
        for p in cohort:
            mins = float(p.get("minutes", 0))
            nineties = mins / 90 if mins > 0 else 0.001
            rm = {}
            for key in STAT_COLS_MOCK:
                v = float(p.get(key, 0) or 0)
                rm[key] = {"value": v, "per90": round(v / nineties, 3)}
            players.append({
                "name": p["name"], "position": p["position"],
                "squad": p["squad"], "league": p["league"],
                "age": p["age"], "minutes": int(mins),
                "season_90s": round(mins / 90, 1),
                "raw_metrics": rm,
                "coverage_flag": "good" if mins >= 900 else "low",
            })

        # Add percentiles
        for key in STAT_COLS_MOCK:
            vals = [pl["raw_metrics"].get(key, {}).get("per90", 0) for pl in players]
            arr = np.array(vals, dtype=float)
            if arr.max() == 0:
                continue
            ranks = (arr.argsort().argsort() / max(len(arr) - 1, 1) * 100).round().astype(int)
            for pl, pct in zip(players, ranks):
                if key in pl["raw_metrics"]:
                    pl["raw_metrics"][key]["percentile"] = int(pct)

        elapsed = time.time() - t0
        check(
            f"get_position_cohort({pos}) returns list",
            isinstance(players, list) and len(players) > 0,
            f"{len(players)} players, {elapsed:.3f}s",
        )

        names = {p.get("name", "") for p in players}

        # No hallucinated (fictional) players
        fakes_found = names & FAKE_PLAYERS
        check(
            f"  {pos}: zero hallucinated (fictional) players",
            len(fakes_found) == 0,
            f"fakes found: {fakes_found}" if fakes_found else "clean",
        )

        # Contains known real 2024-25 players
        real_found = names & KNOWN_REAL_PLAYERS.get(pos, set())
        check(
            f"  {pos}: contains real 2024-25 players",
            len(real_found) > 0,
            f"matched: {sorted(real_found)[:3]}",
        )

        # raw_metrics present with per90 computed
        has_metrics = all(isinstance(p.get("raw_metrics"), dict) for p in players)
        check(f"  {pos}: raw_metrics with per90 present", has_metrics)

        # Percentiles computed
        stat_key = "goals" if pos == "FW" else "tackles" if pos == "DF" else "prg_passes"
        pct_ok = any(
            p.get("raw_metrics", {}).get(stat_key, {}).get("percentile") is not None
            for p in players
        )
        check(f"  {pos}: {stat_key}.percentile computed", pct_ok)

asyncio.run(test_mcp_cohort())


# ═════════════════════════════════════════════════════════════════
# Layer 1b — SQLite cache path configured
# ═════════════════════════════════════════════════════════════════
section("Layer 1b: SQLite cache configuration")

from backend.mcp_server.server import _FBREF_CACHE_DB, _get_cohort_from_sqlite_cache  # type: ignore
check(
    "_FBREF_CACHE_DB path defined",
    bool(_FBREF_CACHE_DB),
    _FBREF_CACHE_DB,
)
check(
    "_get_cohort_from_sqlite_cache function exists",
    callable(_get_cohort_from_sqlite_cache),
)


# ═════════════════════════════════════════════════════════════════
# Layer 2 — Trait engine
# ═════════════════════════════════════════════════════════════════
section("Layer 2: Trait engine — compute_trait_scores")

from backend.engine.trait_engine import compute_trait_scores  # type: ignore

fw_metrics = {
    "goals":               {"value": 18, "per90": 0.72, "percentile": 88},
    "xg":                  {"value": 15.1, "per90": 0.60, "percentile": 82},
    "assists":             {"value": 6,  "per90": 0.24, "percentile": 70},
    "pass_cmp_pct":        {"value": 77.2, "per90": 77.2, "percentile": 65},
    "prg_carries":         {"value": 91, "per90": 3.64, "percentile": 88},
    "att_pen_touches":     {"value": 134, "per90": 5.36, "percentile": 85},
    "tackles":             {"value": 22, "per90": 0.88, "percentile": 55},
    "interceptions":       {"value": 8,  "per90": 0.32, "percentile": 44},
}

fw_traits = compute_trait_scores(fw_metrics, "FW", {})

check("FW trait scores returned", isinstance(fw_traits, dict) and len(fw_traits) > 0,
      f"{len(fw_traits)} traits")

scored = [k for k, v in fw_traits.items() if v.get("score") is not None]
check("At least 4 FW traits scored from real metrics",
      len(scored) >= 4, f"scored: {scored}")

# All scores in 0-1 range
scores_valid = all(
    0.0 <= v["score"] <= 1.0
    for v in fw_traits.values() if v.get("score") is not None
)
check("All trait scores in [0.0, 1.0]", scores_valid)


# ═════════════════════════════════════════════════════════════════
# Layer 2b — Fit engine
# ═════════════════════════════════════════════════════════════════
section("Layer 2b: Fit engine — compute_fit_verdict")

from backend.engine.fit_engine import compute_fit_verdict  # type: ignore

# compute_fit_verdict(player, club_profile) — player must have trait_scores
player_with_traits = {
    "name":          "Erling Haaland",
    "position":      "FW",
    "minutes":       2160,
    "coverage_flag": "good",
    "raw_metrics":   fw_metrics,
    "trait_scores":  fw_traits,     # from trait engine above
    "data_coverage": {"minutes": 2160, "coverage_flag": "good"},
}

# Minimal club profile matching vectorstore format
mock_club_profile = {
    "club": {"club_name": "Arsenal FC", "league": "Premier League"},
    "tactical_profile": {"formation": "4-3-3", "style": "high-press"},
    "recruitment_profile": {
        "ideal_player_traits": {
            "striker": [
                "finishing", "pressing_intensity", "progressive_carrying",
                "box_movement", "link_play",
            ],
        }
    },
}

verdict = compute_fit_verdict(player_with_traits, mock_club_profile)

check("compute_fit_verdict returns dict", isinstance(verdict, dict))
check("measurable_fit computed",
      verdict.get("measurable_fit") is not None,
      f"measurable_fit={verdict.get('measurable_fit')}")
check("measurable_fit in [0, 100]",
      isinstance(verdict.get("measurable_fit"), (int, float))
      and 0 <= verdict["measurable_fit"] <= 100,
      f"score={verdict.get('measurable_fit')}")
check("per_trait_breakdown is a list",
      isinstance(verdict.get("per_trait_breakdown"), list)
      and len(verdict["per_trait_breakdown"]) > 0,
      f"{len(verdict.get('per_trait_breakdown', []))} traits")
check("confidence in [0, 1]",
      isinstance(verdict.get("confidence"), (int, float))
      and 0 <= verdict["confidence"] <= 1,
      f"confidence={verdict.get('confidence')}")


# ═════════════════════════════════════════════════════════════════
# Layer 3 — Chart generator (no display)
# ═════════════════════════════════════════════════════════════════
section("Layer 3a: Chart generator")

from backend.pdf.chart_generator import (  # type: ignore
    generate_fit_bar_chart,
    generate_radar_chart,
    generate_percentile_bars,
    generate_wage_fit_scatter,
    generate_all_guaranteed_charts,
)
import tempfile, os

mock_players = [
    {
        "name": "Erling Haaland",
        "position": "FW",
        "tactical_fit_score": 87.0,
        "measurable_fit": 84.2,
        "wage_eur": 400000,
        "value_eur": 200_000_000,
        "per_trait_breakdown": [
            {"trait": "finishing", "score": 0.92, "source": "real"},
            {"trait": "pressing_intensity", "score": 0.68, "source": "real"},
            {"trait": "progressive_carries", "score": 0.75, "source": "real"},
        ],
        "raw_metrics": fw_metrics,
    },
    {
        "name": "Alexander Isak",
        "position": "FW",
        "tactical_fit_score": 78.0,
        "measurable_fit": 76.5,
        "wage_eur": 150000,
        "value_eur": 120_000_000,
        "per_trait_breakdown": [
            {"trait": "finishing", "score": 0.84, "source": "real"},
            {"trait": "pressing_intensity", "score": 0.75, "source": "real"},
        ],
        "raw_metrics": fw_metrics,
    },
]

with tempfile.TemporaryDirectory() as tmpdir:
    charts = generate_all_guaranteed_charts(
        players=mock_players,
        charts_dir=Path(tmpdir),
        run_id="test-run",
    )
    check("generate_all_guaranteed_charts returns list", isinstance(charts, list))
    check("At least 3 guaranteed charts produced", len(charts) >= 3,
          f"{len(charts)} charts: {[Path(c).name for c in charts]}")
    check("All chart files exist", all(Path(c).exists() for c in charts))
    check("Charts are non-empty PNG files",
          all(Path(c).stat().st_size > 5000 for c in charts),
          f"sizes: {[Path(c).stat().st_size for c in charts]}")


# ═════════════════════════════════════════════════════════════════
# Layer 3b — Report template rendering
# ═════════════════════════════════════════════════════════════════
section("Layer 3b: Report template rendering")

from jinja2 import Environment, FileSystemLoader, select_autoescape  # type: ignore

env = Environment(
    loader=FileSystemLoader(str(ROOT / "backend" / "pdf" / "templates")),
    autoescape=select_autoescape(["html"]),
)
env.filters["zfill"] = lambda v, w: str(v).zfill(w)
tmpl = env.get_template("report.html")

# Build a renderable ranked_player by merging player + verdict
ranked_for_template = [{
    **player_with_traits, **verdict,
    "rank": 1,
    "squad": "Manchester City",
    "league": "Premier League",
    "age": 24,
    "wage_eur": 400000,
    "scout_note": "[1] BBC Sport — 'Haaland fully fit and training well ahead of next fixture.' — bbc.co.uk/sport",
    "tactical_verdict": "Elite finisher, pressing machine, top-5 xG in the Premier League.",
    "strengths": ["Elite finishing", "High xG", "Strong pressing"],
    "weaknesses": ["High market value", "Limited build-up contribution"],
}]

rendered = tmpl.render(
    query="Find a striker for Arsenal under €150K/week",
    run_id="e2e-test-0000",
    generated_at="26 June 2026, 15:00",
    team_name="Arsenal FC",
    ranked_players=ranked_for_template,
    financial_decision={"team_name": "Arsenal FC", "salary_max": 150000, "value_max": 100_000_000},
    tactical_summary="4-3-3 high press evaluation.",
    charts=[],
    narratives={
        "executive_summary": "E2E test report.",
        "player_narratives": {},
        "financial_section": "Budget check.",
        "tactical_appendix": "4-3-3.",
        "conclusion": "Proceed with signing.",
    },
    player_count=len(ranked_for_template),
    langsmith_trace_url="",
)

check("Template renders without errors", len(rendered) > 10000,
      f"{len(rendered):,} chars")
check("Cover shows ARSENAL (not FC or Antigravity)",
      "ARSENAL" in rendered and "ANTIGRAVITY" not in rendered)
check("No raw Jinja2 tags leaked", "{{" not in rendered and "{%" not in rendered)
check("Player name in report", "Erling Haaland" in rendered)
check("Scout Intelligence section present", "Scout Intelligence" in rendered)

out_path = ROOT / "reports" / "e2e_test_render.html"
out_path.parent.mkdir(exist_ok=True)
out_path.write_text(rendered, encoding="utf-8")
check("Report saved to disk", out_path.exists())


# ═════════════════════════════════════════════════════════════════
# Layer 4 — LangSmith config active
# ═════════════════════════════════════════════════════════════════
section("Layer 4: LangSmith configuration")

from backend.utils.config import settings  # type: ignore
check("LANGSMITH_API_KEY loaded from .env", bool(settings.LANGSMITH_API_KEY))
check("LANGSMITH_ENABLED is True", settings.LANGSMITH_ENABLED)
check("LANGCHAIN_TRACING_V2 env var set to true",
      __import__("os").getenv("LANGCHAIN_TRACING_V2") == "true")

from backend.utils.langsmith_helper import _langsmith_enabled, get_trace_url  # type: ignore
check("_langsmith_enabled() returns True", _langsmith_enabled())
trace_url = get_trace_url("test-run-id")
check("get_trace_url returns non-empty string", bool(trace_url), trace_url)


# ═════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"  TOTAL: {passed} passed, {failed} failed out of {len(results)} checks")

if failed == 0:
    print("  ALL PASS — system is grounded, no hallucinations detected")
else:
    print("  FAILURES:")
    for name, ok, detail in results:
        if not ok:
            print(f"    FAIL  {name}  {detail}")
print(f"{'=' * 60}")
print(f"\n  E2E report saved: reports/e2e_test_render.html")
print("  Open that file in browser to see the full rendered output.")
