# Scouter — Task Prompt

Original user query: {query}

Scouting context:
- Target Club:  {target_club}
- Search League: {club_league}

Financial context:
- Min Wage: {salary_min} EUR/week
- Max Wage: {salary_max} EUR/week
- Max Transfer Value: {value_max} EUR

## Instructions

**Step 1 — Fetch position cohort.**
Call `get_position_cohort` with:
- `position`: FBref broad code — DF (defenders/CBs/FBs), MF (midfielders), FW (forwards/wingers/strikers), GK
- `league`: "{club_league}" — this restricts the search to the correct league, dramatically improving result quality.
  If the league string is "all Big 5 leagues", omit the league parameter.

The tool returns the top-15 players (by minutes) with per-90 stats and percentile ranks already computed.
It merges standard + defensive tables, so tackles and interceptions are populated for defenders.

ANTI-LOOPING RULE: Call `get_position_cohort` at most twice per position. It already returns the best 15.

**Step 2 — Select 3–5 candidates.**
From the cohort, pick players whose `raw_metrics` percentiles best match the query.
Prioritize `coverage_flag = "good"` (≥ 900 minutes) for data reliability.
Apply the financial constraint: only include players where wage estimate is within the budget.

**Step 3 — Enrich each selected player.**
For each selected player, call `get_enriched_player_stats(player_name)`.
This fetches passing + possession + goal-and-shot-creation tables and returns `enriched_metrics`
(pass_cmp_pct, att_pen_touches, sca, gca). Merge the `enriched_metrics` into the player's `raw_metrics`.

**Step 4 — Compute trait scores.**
Call `compute_player_traits(raw_metrics=<merged_metrics>, position=<position>)` for each player.
Include the returned `trait_scores` in the player dict. Do NOT invent or modify these numbers.

**Step 5 — Web search.**
Call `web_search` once per candidate for latest form, injury, and contract news.
Always include citation references [1], [2], [3] when quoting a result.

**Step 6 — Write scout_note.**
Each `scout_note` must:
- Name the 2 strongest metrics WITH percentile (e.g. "tackles per 90 in the 88th percentile among defenders")
- Include one cited web-search fact ([1] source)
- Note any coverage warnings (low minutes → provisional stats)

ANTI-HALLUCINATION RULE: NEVER invent or include players not returned by `get_position_cohort`.
If the cohort is empty, output `{{"players": []}}`.

## Output Format

Your final response MUST be ONLY valid JSON — no markdown, no extra text:

{{"players": [
  {{
    "name": "...",
    "position": "DF",
    "squad": "...",
    "league": "...",
    "age": 26,
    "minutes": 2700,
    "season_90s": 30.0,
    "raw_metrics": {{
      "tackles":     {{"value": 78, "per90": 2.60, "percentile": 88}},
      "interceptions": {{"value": 52, "per90": 1.73, "percentile": 82}},
      "pass_cmp_pct": {{"value": 91.2, "per90": 91.2, "percentile": 90}},
      "prg_passes":  {{"value": 148, "per90": 4.93, "percentile": 75}}
    }},
    "coverage_flag": "good",
    "trait_scores": {{
      "ball_winning":         {{"score": 0.85, "source": "real"}},
      "progressive_passing":  {{"score": 0.75, "source": "real"}},
      "technical_security":   {{"score": 0.91, "source": "real"}},
      "recovery_pace":        {{"score": null, "source": "qualitative"}}
    }},
    "wage_eur": 55000,
    "wage_estimated": true,
    "scout_note": "Elite ball-winner — tackles per 90 in the 88th percentile. [1] No injury concerns, contract until 2027.",
    "sources": ["https://example.com/player-news"]
  }}
]}}
