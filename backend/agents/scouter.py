"""Scouter Agent — queries player database via MCP server."""
import asyncio
import json
import os
from typing import Any

from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client

from .base import BaseAgent
from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader


class ScouterAgent(BaseAgent):
    AGENT_NAME = "scouter"

    def __init__(self, client: AsyncOpenAI, sse_queue: asyncio.Queue):
        super().__init__(client, sse_queue)

    # ── MCP Communication ─────────────────────────────────────────────────────

    async def _call_mcp_tool(self, tool_name: str, params: dict) -> Any:
        """Connect to the MCP server and call a single tool."""
        mcp_url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8005/sse")
        result_data: Any = None

        try:
            async with sse_client(mcp_url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, params)
                    if result.content:
                        if len(result.content) == 1:
                            # Single-item result (dict, str, int, …)
                            raw = result.content[0].text
                            try:
                                result_data = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                try:
                                    import ast
                                    result_data = ast.literal_eval(raw)
                                except Exception:
                                    result_data = raw
                        else:
                            # mcp >= 1.10 serialises list results as one block per item
                            result_data = []
                            for block in result.content:
                                try:
                                    result_data.append(json.loads(block.text))
                                except (json.JSONDecodeError, TypeError):
                                    try:
                                        import ast
                                        result_data.append(ast.literal_eval(block.text))
                                    except Exception:
                                        result_data.append(block.text)
        except BaseException as exc:
            # mcp >= 1.10 raises an ExceptionGroup during SSE connection cleanup,
            # even when the tool call succeeded. If we already received data,
            # the error is a benign teardown race — ignore it.
            if result_data is not None:
                return result_data
            # Real failure: unwrap and surface the inner exception
            inner: BaseException = exc
            if hasattr(exc, "exceptions") and exc.exceptions:
                inner = exc.exceptions[0]
            raise RuntimeError(
                f"MCP call '{tool_name}' to {mcp_url} failed — "
                f"{type(inner).__name__}: {inner}"
            ) from inner

        return result_data

    # ── Main Run ──────────────────────────────────────────────────────────────

    async def run(
        self,
        query: str,
        financial_decision: dict,
        target_club: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        """
        Scout players via direct Python MCP calls + one LLM selection call.
        No deepagents — deterministic tool calls, reliable JSON output.
        """
        await self.emit_start("Connecting to player database via MCP server...")

        salary_max = financial_decision.get("salary_max", 300_000)
        salary_min = financial_decision.get("salary_min", 0)
        value_max  = financial_decision.get("value_max", 200_000_000)

        WAGE_ESTIMATES = {
            "premier league": {"starter": 55000, "rotation": 28000, "fringe": 12000},
            "la liga":        {"starter": 45000, "rotation": 22000, "fringe": 10000},
            "serie a":        {"starter": 38000, "rotation": 18000, "fringe": 8000},
            "bundesliga":     {"starter": 38000, "rotation": 18000, "fringe": 8000},
            "ligue 1":        {"starter": 30000, "rotation": 15000, "fringe": 7000},
        }

        def _inject_wages(players: list, fallback_league: str = "") -> list:
            for p in players:
                if isinstance(p, dict) and not p.get("wage_eur"):
                    lk = p.get("league", fallback_league).lower()
                    tm = next(
                        (v for k, v in WAGE_ESTIMATES.items() if k in lk),
                        {"starter": 30000, "rotation": 15000, "fringe": 7000},
                    )
                    mins = int(p.get("minutes", 0))
                    tier = "starter" if mins >= 1800 else "rotation" if mins >= 900 else "fringe"
                    p["wage_eur"]       = tm[tier]
                    p["wage_estimated"] = True
            return players

        # ── Step 1: Club profile from vector DB ───────────────────────────────
        club_league = ""
        club_traits: dict = {}
        if target_club:
            try:
                from ..rag.vectorstore import get_vectorstore
                vs    = get_vectorstore()
                clubs = vs.search_clubs(target_club, n_results=1)
                if clubs:
                    club_data   = clubs[0]
                    club_league = club_data.get("club", {}).get("league", "")
                    club_traits = club_data.get("recruitment_profile", {}).get("ideal_player_traits", {})
                    await self.emit_progress(
                        f"Club profile: {club_data['club']['club_name']} — league={club_league}"
                    )
            except Exception as e:
                await self.emit_progress(f"Vector DB lookup failed ({e}) — searching full Big 5")

        # ── Step 2: Extract position from query ───────────────────────────────
        await self.emit_progress("Extracting target position from query...")
        try:
            pos_resp = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[{"role": "user", "content": (
                    "Reply ONLY with JSON {\"position\": \"FW\"|\"MF\"|\"DF\"|\"GK\"}. "
                    "ST/CF/LW/RW → FW.  CM/DM/AM → MF.  CB/LB/RB → DF.\n\n"
                    f"Query: {query}"
                )}],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=20,
            )
            target_position = json.loads(pos_resp.choices[0].message.content).get("position", "FW")
        except Exception:
            target_position = "FW"
        await self.emit_progress(f"Target position: {target_position}, league filter: {club_league or 'all Big 5'}")

        # ── Step 3: Fetch cohort directly via MCP ─────────────────────────────
        await self.emit_progress(f"Fetching {target_position} cohort from player database...")
        try:
            cohort = await self._call_mcp_tool(
                "get_position_cohort",
                {"position": target_position, "season": "2425", "league": club_league},
            )
            cohort = cohort if isinstance(cohort, list) else []
            cohort = [p for p in cohort if isinstance(p, dict) and "name" in p]
        except Exception as e:
            await self.emit_progress(f"Cohort fetch error: {e} — returning empty")
            cohort = []

        if not cohort:
            await self.emit_complete("No cohort data available — returning empty shortlist")
            return {"players": []}

        cohort = _inject_wages(cohort, fallback_league=club_league)
        await self.emit_progress(f"Got {len(cohort)} players — wages injected from league estimates")

        # ── Step 4: Enrich TOP 3 candidates with deeper stats ─────────────────
        await self.emit_progress("Enriching top 3 candidates with passing/possession/GCA stats...")
        top3_by_minutes = sorted(cohort, key=lambda p: p.get("minutes", 0), reverse=True)[:3]
        for p in top3_by_minutes:
            try:
                enriched = await self._call_mcp_tool(
                    "get_enriched_player_stats",
                    {"player_name": p["name"], "season": "2425"},
                )
                if isinstance(enriched, dict) and enriched.get("enriched_metrics"):
                    p["raw_metrics"].update(enriched["enriched_metrics"])
            except Exception:
                pass

        # ── Step 5: Compute trait scores for all players ──────────────────────
        from ..engine.trait_engine import compute_trait_scores
        for p in cohort:
            try:
                p["trait_scores"] = compute_trait_scores(p.get("raw_metrics", {}), target_position, {})
            except Exception:
                p["trait_scores"] = {}

        # ── Step 6: LLM selects best 3-5 players ─────────────────────────────
        await self.emit_progress("LLM selecting best candidates from cohort...")

        cohort_summary = json.dumps(cohort, default=str)[:7000]
        traits_ctx = f"\nTarget club ideal traits: {json.dumps(club_traits)}" if club_traits else ""
        sel_prompt = (
            f"Scouting request: {query}\n"
            f"Budget: salary_max={salary_max} EUR/wk, value_max={value_max} EUR\n"
            f"Target club: {target_club or 'not specified'}{traits_ctx}\n\n"
            f"Player cohort ({len(cohort)} players, wages are ESTIMATED league-tier values):\n"
            f"{cohort_summary}\n\n"
            "Select 3-5 players that best match the request. "
            "Use the wage_eur field from the data for budget checks — DO NOT use your own knowledge of real wages. "
            "Return ONLY valid JSON: "
            '{{"players": [{{"name": str, "position": str, "squad": str, "league": str, '
            '"age": int, "minutes": int, "raw_metrics": dict, "trait_scores": dict, '
            '"wage_eur": int, "wage_estimated": true, '
            '"scout_note": "2 sentences: best metric percentile + one strength"}}]}}'
        )

        try:
            sel_resp = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": PromptLoader.get("scouter")},
                    {"role": "user",   "content": sel_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=4000,
            )
            result  = json.loads(sel_resp.choices[0].message.content)
            players = result.get("players", [])
        except Exception as exc:
            await self.emit_error("LLM selection failed", str(exc))
            players = []

        # ── Step 7: Web search for form/injuries ──────────────────────────────
        if players:
            from ..tools.web_search import web_search as _ws
            await self.emit_progress(f"Web search: form & injury news for {len(players)} candidates...")
            for p in players:
                try:
                    note = await _ws.ainvoke(f"{p.get('name','')} injury form 2025 latest news")
                    p["scout_note"] = (p.get("scout_note", "") + "\n" + str(note))[:600]
                except Exception:
                    pass

        await self.emit_complete(f"Shortlisted {len(players)} verified players from database")
        return {"players": players}
