"""Scouter Agent — queries player database via MCP server."""
import asyncio
import json
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
        mcp_url = settings.MCP_SERVER_URL
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
        Scout players from the database using MCP tools via Deep Agent.
        """
        await self.emit_start("Connecting to player database via MCP server...")

        salary_max = financial_decision.get("salary_max", 100_000)
        salary_min = financial_decision.get("salary_min", 0)
        value_max = financial_decision.get("value_max", 100_000_000)

        from langchain_core.tools import tool

        @tool
        async def get_position_cohort(
            position: str,
            season: str = "2425",
            league: str = "",
        ) -> list[dict]:
            """
            Fetch the FULL cohort for a position from FBref with per-90 stats and
            percentile ranks already computed within the cohort.

            Use this as the PRIMARY search tool.  It returns ALL players at the
            given position with:
              raw_metrics.<metric>.value      — raw season total
              raw_metrics.<metric>.per90      — per-90-minutes value
              raw_metrics.<metric>.percentile — rank within position cohort (0–100)
              wage_eur                        — estimated weekly wage in EUR (league-tier)

            position: FW | MF | DF | GK  (broad FBref codes)
            league:   Optional league filter e.g. "Premier League", "La Liga".
                      Leave empty to search all Big 5 leagues.
            """
            # Wage estimates by league tier — used when live wage data unavailable.
            WAGE_ESTIMATES = {
                "premier league": {"starter": 55000, "rotation": 28000, "fringe": 12000},
                "la liga":        {"starter": 45000, "rotation": 22000, "fringe": 10000},
                "serie a":        {"starter": 38000, "rotation": 18000, "fringe": 8000},
                "bundesliga":     {"starter": 38000, "rotation": 18000, "fringe": 8000},
                "ligue 1":        {"starter": 30000, "rotation": 15000, "fringe": 7000},
            }
            try:
                res = await self._call_mcp_tool(
                    "get_position_cohort",
                    {"position": position, "season": season, "league": league},
                )
                players = res or []
                # Inject estimated wages so the agent can apply budget filtering
                # using data from this tool — not its own training knowledge.
                for p in players:
                    if not p.get("wage_eur"):
                        league_key = p.get("league", league or "").lower()
                        tier_map = next(
                            (v for k, v in WAGE_ESTIMATES.items() if k in league_key),
                            {"starter": 30000, "rotation": 15000, "fringe": 7000},
                        )
                        mins = int(p.get("minutes", 0))
                        tier = "starter" if mins >= 1800 else "rotation" if mins >= 900 else "fringe"
                        p["wage_eur"] = tier_map[tier]
                        p["wage_estimated"] = True
                return players
            except Exception as e:
                return [{"error": str(e)}]

        @tool
        async def get_enriched_player_stats(player_name: str, season: str = "2425") -> dict:
            """
            Fetch deep per-player stats for ONE candidate by merging passing + possession +
            goal-and-shot-creation FBref tables.  Provides pass_cmp_pct, att_pen_touches,
            sca, gca — metrics needed for technical_security and box_movement traits.

            Call this on EACH selected candidate AFTER get_position_cohort.
            Returns {"enriched_metrics": {...}} to be merged into the player's raw_metrics.
            """
            try:
                res = await self._call_mcp_tool("get_enriched_player_stats", {
                    "player_name": player_name,
                    "season": season,
                })
                return res or {"enriched_metrics": {}}
            except Exception as e:
                return {"error": str(e), "enriched_metrics": {}}

        @tool
        def compute_player_traits(
            raw_metrics: dict,
            position: str,
            game_attributes: dict = None,
        ) -> dict:
            """
            Compute trait_scores for a candidate player from their per-90 metrics.
            Returns {trait_name: {score: 0.0–1.0 or null, source: real|proxy|game_attribute|qualitative}}.
            Call this on each selected candidate AFTER get_position_cohort.

            Args:
                raw_metrics:     the player's raw_metrics dict from get_position_cohort
                position:        the player's position code (FW / MF / DF / GK)
                game_attributes: optional {sprint_speed, stamina} from a legacy DB
            """
            from ..engine.trait_engine import compute_trait_scores
            return compute_trait_scores(raw_metrics, position, game_attributes or {})

        from ..tools.web_search import web_search

        # ── Smart localized search: look up club's league from the vector DB ──
        club_league = ""
        club_traits: dict = {}
        if target_club:
            try:
                from ..rag.vectorstore import get_vectorstore
                vs = get_vectorstore()
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

        task_template = PromptLoader.get("scouter_task")
        task_prompt   = task_template.format(
            query=query,
            salary_min=salary_min,
            salary_max=salary_max,
            value_max=value_max,
            target_club=target_club or "not specified",
            club_league=club_league or "all Big 5 leagues",
        )

        if club_traits:
            task_prompt += (
                f"\n\nTarget Club ({target_club}) ideal player traits (from Knowledge Base):\n"
                + json.dumps(club_traits, indent=2)
                + "\nEnsure the candidates you select match these traits as closely as possible."
            )

        response_text = await self._run_deep_agent(
            task_prompt,
            tools=[get_position_cohort, get_enriched_player_stats,
                   compute_player_traits, web_search],
        )

        cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
        try:
            result  = json.loads(cleaned_text)
            players = result.get("players", [])
        except json.JSONDecodeError:
            with open("scouter_failed_output.txt", "w", encoding="utf-8") as f:
                f.write(cleaned_text)
            await self.emit_error(
                "Failed to parse JSON from scouter agent. See scouter_failed_output.txt",
                cleaned_text,
            )
            raise

        # ── Problem 3: fill missing wages with league-tier estimates ───────────
        LEAGUE_WAGE_ESTIMATES = {
            "premier league": {"starter": 55000, "rotation": 28000, "fringe": 12000},
            "la liga":        {"starter": 45000, "rotation": 22000, "fringe": 10000},
            "serie a":        {"starter": 38000, "rotation": 18000, "fringe": 8000},
            "bundesliga":     {"starter": 38000, "rotation": 18000, "fringe": 8000},
            "ligue 1":        {"starter": 30000, "rotation": 15000, "fringe": 7000},
        }
        for p in players:
            if not p.get("wage_eur") or p.get("wage_eur") == 0:
                league_key = (p.get("league") or club_league or "").lower()
                tier_map   = next(
                    (v for k, v in LEAGUE_WAGE_ESTIMATES.items() if k in league_key),
                    {"starter": 30000, "rotation": 15000, "fringe": 7000},
                )
                mins = p.get("minutes", 0)
                tier = "starter" if mins >= 1800 else "rotation" if mins >= 900 else "fringe"
                p["wage_eur"]        = tier_map[tier]
                p["wage_estimated"]  = True  # flag so the financial agent knows it's an estimate

        await self.emit_complete(f"Shortlisted {len(players)} verified players from database")
        return {"players": players}
