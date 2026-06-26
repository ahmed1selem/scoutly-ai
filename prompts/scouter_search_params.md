# Scouter — Search Parameter Extraction Prompt

Scouting query: {query}
Financial constraints: salary_min={salary_min}, salary_max={salary_max}, value_max={value_max}

Extract structured search parameters from this query.
Return a JSON object with these optional keys:
- position (str, e.g. ST/CM/CB/LW/RW/CAM/CDM/LB/RB/GK)
- max_age (int)
- min_age (int)
- nationality (str)
- min_goals (float)
- min_rating (int, default 78)
- limit (int, default 10)

Always include salary constraints from the financial data.
