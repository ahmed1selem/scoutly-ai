# Financial Agent — Salary Threshold Analysis Prompt

Scouting query: {query}
Target position: {position}

Financial Plan:
```json
{plan_json}
```

Based on the scouting query and the financial plan above,
determine the appropriate salary thresholds and financial parameters.

Return a JSON object with these exact keys:
- salary_min (int, EUR/week)
- salary_max (int, EUR/week)
- value_max (int, EUR transfer fee)
- adjustment_up_pct (float, max % above market rate for top performers)
- adjustment_down_pct (float, max % below market rate for underperformers)
- position_cap (int, EUR/week cap for this position)
- remaining_wage_capacity (int, EUR/week the club can still add)
- financial_notes (str, plain English summary)
- risk_flags (list of str, any financial risks to flag)
