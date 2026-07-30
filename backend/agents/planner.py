"""
Planner Agent — top-level orchestrator that coordinates all sub-agents.

The Planner:
1. Reads its system prompt from prompts/planner.md (dynamically built with agent manifest)
2. Uses OpenAI function calling to decide which agents to invoke
3. Executes the chosen agents in the correct order
4. Triggers PDF report generation
5. Returns the final pipeline result
"""
import asyncio
import json
from typing import Any

from openai import AsyncAzureOpenAI

from .base import BaseAgent
from .financial import FinancialAgent
from .scouter import ScouterAgent
from .analysis import AnalysisAgent
from .tactical import TacticalAgent
from ..pdf.generator import PDFGenerator
from ..utils.prompt_loader import PromptLoader
from ..utils.config import settings


class PlannerAgent(BaseAgent):
    AGENT_NAME = "planner"

    def __init__(self, client: AsyncAzureOpenAI, sse_queue: asyncio.Queue, run_id: str):
        super().__init__(client, sse_queue)
        self.run_id = run_id

        # Instantiate all sub-agents (they share the same SSE queue)
        self.financial = FinancialAgent(client, sse_queue)
        self.scouter = ScouterAgent(client, sse_queue)
        self.analysis = AnalysisAgent(client, sse_queue)
        self.tactical = TacticalAgent(client, sse_queue)
        self.pdf_gen = PDFGenerator(sse_queue)

    @property
    def system_prompt(self) -> str:
        """
        Build the Planner's system prompt dynamically by injecting the agent manifest
        (list of available .md prompt files) into the base planner.md content.
        """
        base_prompt = PromptLoader.get("planner", use_cache=False)
        manifest = PromptLoader.manifest()
        manifest_text = "\n".join(
            f"- **{name}**: {desc}" for name, desc in manifest.items()
        )
        return (
            base_prompt
            + f"\n\n## Currently Available Agent Prompts (loaded at runtime)\n{manifest_text}"
        )

    # ── DeepAgents Implementation ────────────────────────────────────────────────

    async def _extract_scouting_context(self, query: str) -> dict:
        """
        Use a fast LLM call to extract target club and position from the user's query.
        Returns {"club": str, "position": str} — either may be "" if not found.
        """
        from ..utils.config import get_langchain_azure_llm
        llm = get_langchain_azure_llm()
        prompt = (
            "Extract the TARGET CLUB and TARGET POSITION from this football scouting query. "
            "Reply with ONLY a JSON object: {\"club\": \"...\", \"position\": \"...\"}. "
            "If a field is not mentioned, use an empty string. "
            "Position should be one of: CB, FB, DM, CM, AM, W, ST, GK.\n\n"
            f"Query: {query}"
        )
        try:
            resp = await llm.ainvoke(prompt)
            text = resp.content.strip().strip("```json").strip("```").strip()
            data = json.loads(text)
            return {
                "club":     str(data.get("club", "")).strip(),
                "position": str(data.get("position", "")).strip(),
            }
        except Exception as e:
            await self.emit_progress(f"Context extraction failed ({e}) — proceeding without club binding")
            return {"club": "", "position": ""}

    async def run(self, query: str, **kwargs) -> dict[str, Any]:
        """Backward compatibility for abstract base."""
        return await self.run_draft(query, **kwargs)

    async def run_draft(self, query: str, history: list[str] = None, **kwargs) -> dict[str, Any]:
        """
        Run the first half of the agentic pipeline sequentially: Financial -> Scouter -> Tactical.
        Generates a draft recommendation.
        """
        await self.emit_start(f"Planner drafting recommendation for: '{query}'")

        # 0. Extract scouting context (club + position) from the query
        ctx = await self._extract_scouting_context(query)
        target_club     = ctx["club"]
        target_position = ctx["position"]
        if target_club:
            await self.emit_progress(f"Scouting context: club='{target_club}', position='{target_position}'")

        # 1. Financial Agent
        await self.emit_progress("Step 1: Running Financial Agent...")
        financial_res = await self.financial.run(
            query=query, position=target_position or None, target_club=target_club
        )

        # 2. Scouter Agent  (receives target_club for smart localised search)
        await self.emit_progress("Step 2: Running Scouter Agent...")
        scouter_res = await self.scouter.run(
            query=query, financial_decision=financial_res, target_club=target_club
        )
        players = scouter_res.get("players", [])
        # Carried into the report so a short shortlist explains itself.
        selection_funnel = scouter_res.get("funnel", {})

        # 3. Tactical Agent  (receives target_club so it retrieves the RIGHT club profile)
        await self.emit_progress("Step 3: Running Tactical Agent...")
        if players:
            tactical_res = await self.tactical.run(
                players=players, financial_decision=financial_res, target_club=target_club
            )
            ranked_players = tactical_res.get("ranked_players", players)
        else:
            tactical_res = {}
            ranked_players = []
            await self.emit_progress("No players found to evaluate tactically.")

        # 3b. Financial Agent, second pass: cost the shortlist. This has to run
        # after the scouter, because a transfer fee only exists once Transfermarkt
        # has been consulted — the first pass sets the ceilings, this one checks
        # real fees and wages against them.
        if ranked_players:
            await self.emit_progress("Step 3b: Costing the shortlist against the budget...")
            financial_res["deal_economics"] = await self.financial.assess_deals(
                ranked_players, financial_res
            )

        final_summary = "Draft pipeline completed successfully."
        if history:
            final_summary += f" (Applied {len(history)} revisions)"

        # Inject the club being scouted for into financial_decision so the
        # PDF cover, team_name field, and narrative all reflect the real club.
        if target_club:
            financial_res["team_name"] = target_club
        elif "team_name" not in financial_res:
            financial_res["team_name"] = "Target Club"

        final = {
            "run_id": self.run_id,
            "summary": final_summary,
            "ranked_players": ranked_players,
            "financial_decision": financial_res,
            "selection_funnel": selection_funnel,
        }

        await self.emit_progress("Draft generated and ready for human review.")
        return final

    async def run_finalize(
        self,
        query: str,
        ranked_players: list[dict],
        financial_decision: dict,
        langsmith_trace_url: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        """
        Run the second half of the pipeline sequentially: Analysis -> PDF.
        """
        await self.emit_start("Planner finalizing recommendation and generating report...")

        # 1. Analysis Agent
        await self.emit_progress("Step 4: Running Analysis Agent...")
        if ranked_players:
            analysis_res = await self.analysis.run(players=ranked_players, run_id=self.run_id)
        else:
            analysis_res = {"unified_report": "No players to analyze."}

        # 2. PDF Generator
        await self.emit_progress("Step 5: Generating PDF Report...")
        pdf_res = await self.pdf_gen.generate(
            run_id=self.run_id,
            query=query,
            ranked_players=ranked_players,
            financial_decision=financial_decision,
            tactical_summary="Tactical evaluation applied.",
            charts=analysis_res.get("charts", []),
            unified_analysis=analysis_res.get("unified_report", ""),
            langsmith_trace_url=langsmith_trace_url,
            selection_funnel=kwargs.get("selection_funnel") or {},
        )

        final = {
            "run_id": self.run_id,
            "pdf_path": pdf_res.get("pdf_path", ""),
            # Pass the generator's own report on how it rendered. Callers need to
            # know whether they got a real PDF or the HTML fallback, and it was
            # only reaching the SSE stream, not the return value.
            "pdf_backend": pdf_res.get("pdf_backend", ""),
            "html_path": pdf_res.get("html_path", ""),
            "summary": "Final report generated.",
        }

        await self.emit_complete("Pipeline complete — PDF report ready!")
        return final
