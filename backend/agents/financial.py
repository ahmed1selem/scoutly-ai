"""Financial Agent — reads YAML financial plan and determines player salary thresholds."""
import asyncio
import json
from typing import Any

import yaml
from openai import AsyncOpenAI

from .base import BaseAgent
from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader


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

    async def run(self, query: str, position: str | None = None, **kwargs) -> dict[str, Any]:
        """
        Analyze the club's financial plan and return salary thresholds for scouting.
        """
        await self.emit_start("Reading club financial plan and calculating salary thresholds...")

        plan = self._load_financial_plan()
        await self.emit_progress(f"Financial plan loaded for {plan.get('team', {}).get('name', 'Unknown')}")

        plan_json = json.dumps(plan, indent=2)

        task_prompt = PromptLoader.get("financial_analyze").format(
            query=query,
            position=position or 'to be determined from query',
            plan_json=plan_json
        )
        task_prompt += "\n\nCRITICAL: You must output ONLY valid JSON format in your final response containing the salary parameters. Do not include markdown codeblocks or other text in your final message."

        response_text = await self._run_deep_agent(task_prompt)
        
        # Clean the response to ensure it's valid JSON
        cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
        try:
            result = json.loads(cleaned_text)
        except json.JSONDecodeError:
            await self.emit_error("Failed to parse JSON from financial agent.", cleaned_text)
            raise

        await self.emit_complete(
            f"Financial analysis complete — salary range: €{result.get('salary_min', 0):,} – €{result.get('salary_max', 0):,}/week"
        )
        return result
