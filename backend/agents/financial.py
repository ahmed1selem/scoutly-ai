"""Financial Agent — reads YAML financial plan and determines player salary thresholds."""
import asyncio
import json
import re
from datetime import datetime
from typing import Any

import yaml
from openai import AsyncOpenAI

from .base import BaseAgent
from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader


#: No Big-5 first-team wage plausibly exceeds this per week, in EUR. Used to
#: catch an annual figure returned where a weekly one was asked for.
_MAX_PLAUSIBLE_WEEKLY = 400_000

#: Words that indicate the brief itself constrained the budget.
_BUDGET_TOKENS = ("budget", "wage", "salary", "fee", "£", "€", "$", "million", "cheap",
                  "modest", "free transfer", "afford", "per week", "a week")

#: A bare amount with a magnitude suffix — "under 100k a week", "40m", "£25 m".
_BUDGET_AMOUNT = re.compile(r"\d+\s*(?:k|m|bn)\b", re.IGNORECASE)


def _budget_stated_in(query: str) -> bool:
    text = (query or "").lower()
    return any(tok in text for tok in _BUDGET_TOKENS) or bool(_BUDGET_AMOUNT.search(text))


def sanitise_budget(result: dict, query: str = "", target_club: str = "") -> dict:
    """
    Make a budget safe to scout against, and say what it is based on.

    Two failure modes matter downstream. An annual figure returned where a weekly
    wage was asked for makes every candidate look affordable, so the budget filter
    silently stops filtering. A `salary_min` above `salary_max` excludes everyone.
    Both are clamped rather than raised, and recorded in `risk_flags` so the
    correction is visible instead of quietly applied.

    `budget_basis` exists because the generic plan is not any specific club's
    finances. Unless the brief stated a constraint, these are scaled defaults and
    the report should not imply otherwise.
    """
    flags = list(result.get("risk_flags") or [])

    smax = result.get("salary_max")
    if isinstance(smax, (int, float)) and not isinstance(smax, bool) and smax > _MAX_PLAUSIBLE_WEEKLY:
        if smax / 52 <= _MAX_PLAUSIBLE_WEEKLY:
            result["salary_max"] = int(smax / 52)
            flags.append(
                f"salary_max looked annual ({smax:,.0f}); read as "
                f"{result['salary_max']:,}/week"
            )
        else:
            result["salary_max"] = _MAX_PLAUSIBLE_WEEKLY
            flags.append(
                f"salary_max {smax:,.0f}/wk exceeds any plausible wage; "
                f"capped at {_MAX_PLAUSIBLE_WEEKLY:,}"
            )

    smin = result.get("salary_min")
    if (isinstance(smin, (int, float)) and not isinstance(smin, bool)
            and isinstance(result.get("salary_max"), (int, float))
            and smin > result["salary_max"]):
        flags.append(f"salary_min {smin:,.0f} exceeded salary_max; floor removed")
        result["salary_min"] = 0

    result["risk_flags"] = flags
    result["team_name"] = target_club or result.get("team_name") or "Target Club"
    result["budget_basis"] = (
        "stated in the brief" if _budget_stated_in(query)
        else "scaled from generic default rules — not the club's audited budget"
    )
    return result


#: Fee amortisation window, in years. Clubs spread a transfer fee across the
#: contract; five years is the usual accounting cap.
_MAX_AMORTISATION_YEARS = 5
#: Assumed contract length for a new signing when nothing better is known.
_DEFAULT_DEAL_YEARS = 4


def _years_until(date_str: str | None) -> float | None:
    """Years from today to a Transfermarkt-style dd/mm/yyyy date, or None."""
    if not date_str:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            expiry = datetime.strptime(str(date_str).strip(), fmt)
        except ValueError:
            continue
        return max((expiry - datetime.now()).days / 365.25, 0.0)
    return None


def assess_deal_economics(players: list[dict], budget: dict) -> dict:
    """
    Cost each candidate against the budget, deterministically.

    Kept as arithmetic rather than an LLM call: these are the numbers a director
    acts on, and the model has no more information than the fields here. The fee
    is a market-value *proxy*, not an asking price, and every output says so.

    Writes onto each player:
      fee_eur, fee_basis, wage_annual_eur, contract_years_left,
      amortised_fee_annual_eur, first_year_cash_eur, annual_book_cost_eur,
      within_fee_budget, within_wage_budget, economic_verdict

    Returns a squad-level summary for the report.
    """
    fee_cap = budget.get("value_max")
    wage_cap = budget.get("salary_max")
    assessed = 0

    for p in players:
        fee = p.get("value_eur")
        wage = p.get("wage_eur")
        wage_annual = int(wage * 52) if wage else None

        years_left = _years_until(p.get("contract_expires"))
        deal_years = min(_DEFAULT_DEAL_YEARS, _MAX_AMORTISATION_YEARS)
        amortised = int(fee / deal_years) if fee else None

        p["fee_eur"] = fee
        p["fee_basis"] = (
            "Transfermarkt market value — a valuation, not a quoted asking price"
            if fee else "no published valuation"
        )
        p["wage_annual_eur"] = wage_annual
        p["contract_years_left"] = round(years_left, 1) if years_left is not None else None
        p["amortised_fee_annual_eur"] = amortised
        p["deal_years_assumed"] = deal_years
        # Two views because clubs use both: cash out of the door in year one,
        # and the annual charge that actually hits the accounts.
        p["first_year_cash_eur"] = (fee or 0) + (wage_annual or 0) if (fee or wage_annual) else None
        p["annual_book_cost_eur"] = (
            (amortised or 0) + (wage_annual or 0) if (amortised or wage_annual) else None
        )

        within_fee = None if not (fee and fee_cap) else fee <= fee_cap
        within_wage = None if not (wage and wage_cap) else wage <= wage_cap
        p["within_fee_budget"] = within_fee
        p["within_wage_budget"] = within_wage

        # A contract close to expiry is leverage: the fee falls even if the
        # valuation does not, so it belongs in the verdict.
        if within_fee is False and within_wage is False:
            verdict = "Outside budget on both fee and wage"
        elif within_fee is False:
            verdict = "Wage affordable, fee above ceiling"
            if years_left is not None and years_left <= 1.5:
                verdict += f" — but only {years_left:.1f} years left, so the fee is negotiable"
        elif within_wage is False:
            verdict = "Fee affordable, wage above ceiling"
        elif within_fee and within_wage:
            verdict = "Within budget on both fee and wage"
        else:
            verdict = "Cannot be costed — missing fee or wage data"
        p["economic_verdict"] = verdict

        if fee or wage:
            assessed += 1

    priced = [p for p in players if p.get("fee_eur")]
    affordable = [p for p in players
                  if p.get("within_fee_budget") and p.get("within_wage_budget")]
    return {
        "fee_ceiling_eur": fee_cap,
        "wage_ceiling_eur": wage_cap,
        "candidates_costed": assessed,
        "candidates_with_a_valuation": len(priced),
        "candidates_within_budget": len(affordable),
        "cheapest_affordable": min(
            (p for p in affordable if p.get("fee_eur")),
            key=lambda p: p["fee_eur"], default={},
        ).get("name"),
        "total_fee_if_all_signed_eur": sum(p["fee_eur"] for p in priced) or None,
        "fee_basis_note": (
            "Fees are Transfermarkt valuations, not asking prices. Amortisation "
            f"assumes a {_DEFAULT_DEAL_YEARS}-year contract."
        ),
    }


class FinancialAgent(BaseAgent):
    AGENT_NAME = "financial"

    def __init__(self, client: AsyncOpenAI, sse_queue: asyncio.Queue):
        super().__init__(client, sse_queue)
        self._plan_cache: dict | None = None

    def _load_financial_plan(self) -> dict:
        """Load and cache the financial plan YAML."""
        if self._plan_cache is None:
            with open(settings.FINANCIAL_PLAN_PATH, encoding="utf-8") as f:
                self._plan_cache = yaml.safe_load(f)
        return self._plan_cache

    async def run(
        self,
        query: str,
        position: str | None = None,
        target_club: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        """
        Return the salary and fee thresholds to scout against.

        `target_club` matters: without it every club got an identical budget from
        the generic default plan. It used to be read off a `self.target_club`
        attribute that nothing ever set, so the progress line always said
        "the target club" and the numbers were club-agnostic.
        """
        await self.emit_start("Reading club financial plan and calculating salary thresholds...")

        plan = self._load_financial_plan()
        club = target_club or "the target club"
        await self.emit_progress(f"Financial plan loaded — sizing a budget for {club}")

        plan_json = json.dumps(plan, indent=2)

        task_prompt = PromptLoader.get("financial_analyze").format(
            club=club,
            query=query,
            position=position or 'to be determined from query',
            plan_json=plan_json
        )
        task_prompt += "\n\nCRITICAL: You must output ONLY valid JSON format in your final response containing the salary parameters. Do not include markdown codeblocks or other text in your final message."

        response_text = await self._run_json_completion(task_prompt, max_tokens=800)


        # Clean the response to ensure it's valid JSON
        cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
        try:
            result = json.loads(cleaned_text)
        except json.JSONDecodeError:
            await self.emit_error("Failed to parse JSON from financial agent.", cleaned_text)
            raise

        result = sanitise_budget(result, query=query, target_club=target_club)

        await self.emit_complete(
            f"Budget for {result['team_name']}: €{result.get('salary_min', 0):,} – "
            f"€{result.get('salary_max', 0):,}/week, fee up to "
            f"€{(result.get('value_max') or 0) / 1e6:.0f}M ({result['budget_basis']})"
        )
        return result

    async def assess_deals(self, players: list[dict], budget: dict) -> dict:
        """
        Cost the shortlist against the budget once market values are known.

        Runs after the scouter, because a fee only exists once Transfermarkt has
        been consulted. Previously the agent set a fee ceiling and nothing ever
        checked a real fee against it.
        """
        if not players:
            return {}
        await self.emit_start(f"Costing {len(players)} candidates against the budget...")
        summary = assess_deal_economics(players, budget)
        for p in players:
            fee = p.get("fee_eur")
            await self.emit_progress(
                f"  {p.get('name','?')}: "
                f"fee {'EUR %.0fM' % (fee / 1e6) if fee else 'unvalued'}"
                f" + wage EUR {(p.get('wage_annual_eur') or 0) / 1e6:.1f}M/yr"
                f" -> {p.get('economic_verdict')}"
            )
        await self.emit_complete(
            f"{summary.get('candidates_within_budget', 0)} of {len(players)} candidates "
            f"sit inside both ceilings"
        )
        return summary
