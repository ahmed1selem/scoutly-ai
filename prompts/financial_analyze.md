# Financial Agent — Salary Threshold Analysis Prompt

Buying club: {club}
Scouting query: {query}
Target position: {position}

Financial Plan (generic default rules — NOT this club's audited accounts):
```json
{plan_json}
```

Determine the salary thresholds and financial parameters for **{club}** signing a
**{position}**.

Rules:
- If the query states a budget, a wage ceiling or a fee limit, those take
  precedence over every default below.
- Otherwise scale the generic defaults to what a club of {club}'s standing in its
  league would plausibly commit to this position. A perennial Champions League
  side and a promoted club do not share a wage ceiling.
- `salary_max` is a **weekly** wage in EUR, not annual. Sanity-check it: a Big-5
  first-team regular sits between roughly EUR 10,000 and EUR 400,000 per week.
- `value_max` is a one-off transfer fee in EUR, so it is orders of magnitude
  larger than the weekly wage. Never return the two on the same scale.
- Put anything you inferred rather than were told into `risk_flags`, so the
  reader knows which numbers are assumptions.

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
