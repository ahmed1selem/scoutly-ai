"""
Visual layout test — renders the report template with realistic mock data
and opens it in the default browser.

Run with:
    python tests/test_report_render.py
"""
import sys
import webbrowser
from pathlib import Path

# Make sure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = ROOT / "backend" / "pdf" / "templates"
OUTPUT_PATH   = ROOT / "reports" / "test_render.html"

# ── Realistic mock data ────────────────────────────────────────────────────────

MOCK_PLAYERS = [
    {
        "name": "Carlos Ruiz",
        "rank": 1,
        "position": "FW",
        "squad": "Atlético Madrid",
        "league": "La Liga",
        "age": 26,
        "minutes": 2430,
        "season_90s": 27.0,
        "wage_eur": 55000,
        "value_eur": 42_000_000,
        "tactical_fit_score": 87.0,
        "measurable_fit": 84.2,
        "confidence": 0.82,
        "trait_coverage": 0.71,
        "coverage_flag": "good",
        "abstain": False,
        "scout_note": (
            "[1] Marca — 'Ruiz en racha: 3 goles en los últimos 5 partidos, "
            "su mejor racha del año.' — marca.com\n"
            "[2] BBC Sport — 'No injuries reported; fully fit for the upcoming "
            "Champions League tie.' — bbc.co.uk/sport"
        ),
        "tactical_verdict": (
            "High-pressing forward with elite penalty-area movement. "
            "Progressive carry numbers rank in the 91st percentile among La Liga forwards."
        ),
        "strengths": ["Elite xG rate", "High press work-rate", "Penalty area presence", "Strong dribbling"],
        "weaknesses": ["Aerial duel win rate below average", "Yellow card risk"],
        "matched_traits": ["pressing_intensity", "finishing", "progressive_carries"],
        "reference_to_tactics": "Fits 4-3-3 high press; vacates wide channels to allow overlaps",
        "per_trait_breakdown": [
            {"trait": "finishing",            "score": 0.88, "source": "real"},
            {"trait": "pressing_intensity",   "score": 0.81, "source": "real"},
            {"trait": "progressive_carries",  "score": 0.91, "source": "real"},
            {"trait": "penalty_area_presence","score": 0.77, "source": "proxy"},
            {"trait": "ball_retention",       "score": 0.65, "source": "proxy"},
            {"trait": "defensive_activity",   "score": 0.59, "source": "game_attr"},
        ],
        "qualitative_gaps": [{"trait": "leadership"}, {"trait": "aerial_duels"}],
        "raw_metrics": {
            "goals":               {"value": 20, "per90": 0.741, "percentile": 89},
            "xg":                  {"value": 18.2, "per90": 0.675, "percentile": 84},
            "assists":             {"value": 8,  "per90": 0.296, "percentile": 72},
            "pass_cmp_pct":        {"value": 80.1, "per90": 80.1, "percentile": 68},
            "tackles":             {"value": 28, "per90": 1.037, "percentile": 55},
            "interceptions":       {"value": 11, "per90": 0.407, "percentile": 49},
            "progressive_carries": {"value": 102,"per90": 3.778, "percentile": 91},
            "progressive_passes":  {"value": 63, "per90": 2.333, "percentile": 77},
        },
    },
    {
        "name": "Luca Bianchi",
        "rank": 2,
        "position": "FW",
        "squad": "Inter Milan",
        "league": "Serie A",
        "age": 28,
        "minutes": 2700,
        "season_90s": 30.0,
        "wage_eur": 72000,
        "value_eur": 38_000_000,
        "tactical_fit_score": 78.0,
        "measurable_fit": 76.5,
        "confidence": 0.74,
        "trait_coverage": 0.62,
        "coverage_flag": "good",
        "abstain": False,
        "scout_note": (
            "[1] Gazzetta dello Sport — 'Bianchi ha segnato in 4 delle ultime 5 "
            "gare, miglior momento della stagione.' — gazzetta.it\n"
            "[2] Sky Sport — 'Nessun problema fisico riportato.' — skysport.it"
        ),
        "tactical_verdict": (
            "Clinical finisher with excellent hold-up play but higher wage demands "
            "may strain the budget at €72K/wk. Strong in final third, less so in build-up."
        ),
        "strengths": ["Clinical finishing", "Strong hold-up play", "Aerial ability"],
        "weaknesses": ["High weekly wage", "Lower progressive carry rate", "Press resistance inconsistent"],
        "per_trait_breakdown": [
            {"trait": "finishing",           "score": 0.84, "source": "real"},
            {"trait": "pressing_intensity",  "score": 0.61, "source": "real"},
            {"trait": "progressive_carries", "score": 0.58, "source": "real"},
            {"trait": "aerial_duels",        "score": 0.79, "source": "proxy"},
            {"trait": "ball_retention",      "score": 0.71, "source": "proxy"},
        ],
        "qualitative_gaps": [{"trait": "press_resistance"}],
        "raw_metrics": {
            "goals":               {"value": 22, "per90": 0.733, "percentile": 87},
            "xg":                  {"value": 19.5,"per90": 0.650,"percentile": 80},
            "assists":             {"value": 5,  "per90": 0.167, "percentile": 51},
            "pass_cmp_pct":        {"value": 75.9,"per90": 75.9, "percentile": 54},
            "tackles":             {"value": 24, "per90": 0.800, "percentile": 45},
            "interceptions":       {"value": 7,  "per90": 0.233, "percentile": 38},
            "progressive_carries": {"value": 88, "per90": 2.933, "percentile": 72},
            "progressive_passes":  {"value": 48, "per90": 1.600, "percentile": 58},
        },
    },
    {
        "name": "Harry Powell",
        "rank": 3,
        "position": "FW",
        "squad": "Manchester City",
        "league": "Premier League",
        "age": 22,
        "minutes": 2250,
        "season_90s": 25.0,
        "wage_eur": 48000,
        "value_eur": 55_000_000,
        "tactical_fit_score": 71.0,
        "measurable_fit": 69.8,
        "confidence": 0.61,
        "trait_coverage": 0.55,
        "coverage_flag": "good",
        "abstain": False,
        "scout_note": (
            "[1] The Guardian — 'Powell missed training Thursday with a minor "
            "hamstring strain; expected back for the weekend fixture.' — theguardian.com\n"
            "[2] Athletic — 'Only 22 but already impressing in the top flight; "
            "City won't sell cheap.' — theathletic.com"
        ),
        "tactical_verdict": (
            "Young English forward with strong potential. "
            "Minor injury concern this week. High market value may exceed budget."
        ),
        "strengths": ["Young age (22)", "PL experience", "High ceiling", "Good passing accuracy"],
        "weaknesses": ["Hamstring strain flag", "Market value near budget cap", "Still developing consistency"],
        "per_trait_breakdown": [
            {"trait": "finishing",           "score": 0.72, "source": "real"},
            {"trait": "pressing_intensity",  "score": 0.78, "source": "real"},
            {"trait": "progressive_carries", "score": 0.81, "source": "real"},
            {"trait": "ball_retention",      "score": 0.68, "source": "proxy"},
        ],
        "qualitative_gaps": [{"trait": "aerial_duels"}, {"trait": "composure"}],
        "raw_metrics": {
            "goals":               {"value": 18, "per90": 0.720, "percentile": 84},
            "xg":                  {"value": 15.1,"per90": 0.604,"percentile": 75},
            "assists":             {"value": 6,  "per90": 0.240, "percentile": 65},
            "pass_cmp_pct":        {"value": 77.2,"per90": 77.2, "percentile": 61},
            "tackles":             {"value": 22, "per90": 0.880, "percentile": 50},
            "interceptions":       {"value": 8,  "per90": 0.320, "percentile": 44},
            "progressive_carries": {"value": 91, "per90": 3.640, "percentile": 88},
            "progressive_passes":  {"value": 52, "per90": 2.080, "percentile": 70},
        },
    },
]

MOCK_FINANCIAL = {
    "team_name":  "Arsenal FC",   # simulates scouting request "scout a winger for Arsenal"
    "salary_max": 80000,
    "salary_min": 10000,
    "value_max":  60_000_000,
    "risk_flags": [
        "Powell's market value (€55M) approaches the transfer budget ceiling.",
        "Bianchi's wage (€72K/wk) exceeds stated salary_max of €80K/wk by 10%.",
    ],
}

MOCK_NARRATIVES = {
    "executive_summary": (
        "Three forwards identified matching the scouting brief for a dynamic, "
        "high-press attacker under €80K/week. Carlos Ruiz emerges as the clear "
        "recommendation: elite xG numbers, La Liga–proven pressing output, and a "
        "wage package well within budget at €55K/week. Bianchi offers a clinical "
        "alternative but stretches salary boundaries. Powell is a high-upside option "
        "worth monitoring if the club prioritises future asset value."
    ),
    "player_narratives": {
        "Carlos Ruiz": (
            "Ruiz has been the standout performer in La Liga's pressing stakes this "
            "term. His 91st-percentile progressive carry rate combined with 20 goals "
            "makes him the most complete profile in this shortlist. At €55K/wk he "
            "represents exceptional value; Atlético are known sellers when a fee north "
            "of €40M is tabled."
        ),
        "Luca Bianchi": (
            "Bianchi's 22-goal tally is the benchmark in this cohort. However, his "
            "limited pressing intensity (61/100) is a concern for a high-press system "
            "and his wages sit above stated parameters. Best deployed as a Plan B "
            "target if Ruiz negotiations stall."
        ),
        "Harry Powell": (
            "An exciting 22-year-old who fits the profile tactically but comes with "
            "two caveats: a reported hamstring strain this week and a market value that "
            "tests the transfer budget. Recommend revisiting in January if available "
            "at reduced terms following a City squad reshuffle."
        ),
    },
    "financial_section": (
        "Ruiz at €55K/wk is the only candidate comfortably within budget parameters. "
        "Bianchi's €72K/wk is 10% above the stated maximum; Powell's €48K/wk wage "
        "is fine but his €55M valuation tests the transfer ceiling. Wage budget "
        "utilisation: Ruiz 69%, Bianchi 90%, Powell 60%."
    ),
    "tactical_appendix": (
        "FC Antigravity plays a 4-3-3 high-press system with a focus on vertical "
        "transitions and pressing triggers in the opponent's half. The target forward "
        "must be comfortable pressing from the front, making runs in behind, and "
        "participating in quick combination play in tight spaces. A minimum pressing "
        "intensity score of 70/100 and progressive carry rate in the 75th+ percentile "
        "are the primary filters for this position."
    ),
    "conclusion": (
        "Recommend opening negotiations with Atlético Madrid for Carlos Ruiz. "
        "His tactical fit (87/100), elite statistics, and wage profile make him the "
        "strongest candidate by a clear margin. Should negotiations fail, Luca "
        "Bianchi is the contingency — budget flexibility will be required. "
        "Powell is a long-term prospect to monitor but not the immediate priority."
    ),
}


def render_report() -> str:
    """Render the Jinja2 template with mock data and return HTML string."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["zfill"] = lambda v, w: str(v).zfill(w)
    template = env.get_template("report.html")

    return template.render(
        query="Find me a striker under €80K/week with 15+ goals who fits a high-press 4-3-3",
        run_id="test-render-00000000-0000",
        generated_at="26 June 2026, 14:30",
        team_name=MOCK_FINANCIAL.get("team_name", "FC Antigravity"),
        ranked_players=MOCK_PLAYERS,
        financial_decision=MOCK_FINANCIAL,
        tactical_summary=(
            "Ruiz best matches the 4-3-3 pressing system; Bianchi fits a target-man "
            "variant; Powell suits a counter-press role. All three evaluated against "
            "the club's 96-profile tactical knowledge base."
        ),
        charts=[],          # no charts in the quick render test
        narratives=MOCK_NARRATIVES,
        player_count=len(MOCK_PLAYERS),
        langsmith_trace_url="",  # empty for test render
    )


def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"Rendering report template with {len(MOCK_PLAYERS)} mock players...")

    html = render_report()
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    print(f"Saved: {OUTPUT_PATH}")
    print("Opening in browser...")
    webbrowser.open(OUTPUT_PATH.as_uri())
    print("Done. Check the browser tab for the visual layout.")


if __name__ == "__main__":
    main()
