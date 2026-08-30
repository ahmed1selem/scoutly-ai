"""Scouter Agent — queries player database via MCP server."""
import asyncio
import json
import os
from typing import Any

from openai import AsyncOpenAI
from mcp import ClientSession
from mcp.client.sse import sse_client

from .base import BaseAgent
from ..engine.fit_engine import compute_fit_verdict
from .financial import effective_fee_eur
from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader

#: Fit points added to a candidate already playing in the buying club's league.
#:
#: Deliberately tiny, and the number is measured rather than chosen. Sweeping it
#: against the 2025-26 outcomes of 220 real transfers, every increase costs
#: predictive accuracy — Spearman rho against match rating falls monotonically
#: from +0.300 at zero to +0.262 at 5 and +0.198 at 12. Cross-league signings
#: also outperformed same-league ones that season (6.865 against 6.806, t=-2.0),
#: so there is no performance premium here to encode.
#:
#: It stays because a same-league signing carries less adaptation risk and an
#: easier deal, which is a real preference — but at 1.0 point on a scale where
#: fits span roughly 20-70 it only ever breaks a tie, at a cost of 0.005 rho.
#: Raising it trades measurable accuracy for a preference the data contradicts.
_SAME_LEAGUE_BONUS = 1.0

#: How far over the wage ceiling a candidate may sit and still be shown, flagged.
#:
#: The wage gate is the one place where absolute enforcement is wrong. A transfer
#: fee is a single negotiated number the club either has or does not; a weekly
#: wage is the most movable term in a contract, and the ceiling it is checked
#: against is itself inferred from what the club currently pays rather than
#: stated by anyone. Discarding a strong fit for being 3% over an inferred figure
#: is false precision.
#:
#: 10% and no more. This is not a soft margin: beyond the band candidates are
#: still excluded outright, and everyone inside it is marked `wage_stretch` so
#: the report never presents them as comfortably affordable.
_WAGE_CEILING_GRACE = 1.10


# FBref abbreviates squad names; club profiles spell them out.
_CLUB_WORD_ALIASES = {
    "utd": "united", "nottham": "nottingham", "wolves": "wolverhampton",
    "spurs": "tottenham", "boro": "middlesbrough", "wanderers": "wolverhampton",
    "atletico": "atlético", "internazionale": "inter",
}
# Generic corporate decoration only. "City" and "United" are NOT noise — dropping
# them collapses Manchester City and Manchester United onto the same key.
_CLUB_NOISE_WORDS = {"fc", "afc", "cf", "sc", "ac", "as", "ss", "club", "de", "the"}


def _club_tokens(value: str) -> set[str]:
    text = str(value).lower()
    # Apostrophes are elisions, not separators: "Nott'ham" is one word.
    text = text.replace("'", "").replace("’", "")
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    words = [_CLUB_WORD_ALIASES.get(w, w) for w in cleaned.split()]
    return {w for w in words if w and w not in _CLUB_NOISE_WORDS}


def _same_club(squad: str, club: str) -> bool:
    """
    Whether an FBref squad name and a club-profile name refer to the same club.

    Every word of the shorter name must appear in the longer one, so
    "Tottenham" matches "Tottenham Hotspur" while "Manchester City" and
    "Manchester United" stay distinct.
    """
    a, b = _club_tokens(squad), _club_tokens(club)
    if not a or not b:
        return False
    return a <= b or b <= a


# Plausible weekly wage band for a Big-5 starter, in EUR. Anything outside it is
# a parsing error (an annual figure, a transfer fee, a squad total) rather than a
# wage, and must not reach the report.
_WAGE_MIN_WEEKLY = 2_000
_WAGE_MAX_WEEKLY = 600_000

# Only sites that actually maintain wage databases are trusted. A figure pulled
# from general news prose looked authoritative and was consistently wrong —
# Gvardiol came back at EUR 45,385/wk against a real figure near EUR 120,000 —
# so a plausible-but-wrong number was displacing an honest modelled band.
_TRUSTED_WAGE_DOMAINS = (
    "capology.com",
    "salarysport.com",
    "spotrac.com",
    "footballbenchmark.com",
)


def _apply_web_wage(player: dict, extracted: dict) -> None:
    """
    Adopt a web-sourced wage only if it is cited to a wage database, survives
    basis normalisation, and lands in a plausible weekly band.

    It is still marked as an estimate: only the Capology cache built offline is
    authoritative enough for wage_estimated=False. When any check fails the
    player keeps the modelled league/minutes/age band, which is a stated
    estimate rather than a wrong specific.
    """
    amount = extracted.get("wage_amount_eur")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        return

    basis = (extracted.get("wage_basis") or "").lower()
    if basis == "annual":
        weekly = amount / 52
    elif basis == "weekly":
        weekly = amount
    else:
        return  # no stated basis — cannot tell a weekly wage from a yearly one

    if not (_WAGE_MIN_WEEKLY <= weekly <= _WAGE_MAX_WEEKLY):
        return

    source = str(extracted.get("source_url") or "").lower()
    if not source:
        return  # uncited figures are not usable
    domain = next((d for d in _TRUSTED_WAGE_DOMAINS if d in source), None)
    if not domain:
        return  # cited, but not to anyone who publishes wages

    player["wage_eur"] = int(round(weekly))
    player["wage_estimated"] = True
    player["wage_source"] = domain
    # The modelled band was computed around the wage this figure just replaced,
    # so it no longer brackets anything. Leaving it there is how a player with a
    # cited EUR 19,808/wk came out on the report as "EUR 37,000-EUR 61,000".
    player.pop("wage_low", None)
    player.pop("wage_high", None)


class ScouterAgent(BaseAgent):
    AGENT_NAME = "scouter"

    def __init__(self, client: AsyncOpenAI, sse_queue: asyncio.Queue):
        super().__init__(client, sse_queue)

    # ── MCP Communication ─────────────────────────────────────────────────────

    async def _call_mcp_tool(self, tool_name: str, params: dict) -> Any:
        """Connect to the MCP server and call a single tool."""
        # Derive the URL from the same MCP_SERVER_HOST/PORT the server binds to,
        # so one setting moves both ends. Hardcoding a default of 8005 here while
        # the server read MCP_SERVER_PORT meant any deployment that changed the
        # port broke silently: fly.toml sets 8001, the server listened on 8001,
        # and the scouter went on dialling 8005. Every tool call failed and the
        # only symptom was an empty shortlist on every run.
        mcp_url = os.getenv("MCP_SERVER_URL") or settings.MCP_SERVER_URL
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

        # How many players the LLM ranks in one call. Bounded on purpose: the
        # cohort tool can return hundreds, and the selection prompt has to fit
        # not just the context window but the provider's per-minute token budget
        # (Groq's free tier allows 12k tokens/minute across all requests).
        # The cohort is drawn across the whole Big 5, not the buying club's
        # league. A backtest of 327 real summer-2025 transfers found 51% of the
        # successful ones moved between leagues — Zubimendi to Arsenal, Huijsen
        # to Real Madrid, Cherki to Manchester City — and a league gate made
        # every one of them unreachable. Huijsen scores in the 96th percentile
        # of the Big-5 centre-back cohort for Real Madrid.
        # Large enough to cover every position's full eligible set (890 MF is the
        # biggest). It must be, because get_position_cohort trims by MINUTES, so
        # any smaller number picks who the fit ranking below is even allowed to
        # consider on the basis of playing time — the one ordering measured to
        # have no predictive power at all (rho +0.016 against 2025-26 rating).
        #
        # At 300 the scouter saw 34% of midfielders and 45% of defenders. Valentin
        # Rongier, the third-best fit of 878 midfielders for Rennes and a player
        # Rennes actually signed, played 1,562 minutes and so was never in the
        # cohort to be ranked. Ranking the full set is local, deterministic and
        # costs about half a second; the model still only ever sees SELECTION_LIMIT.
        COHORT_LIMIT = 1200
        #: Candidates worth paying for a valuation lookup, taken by fit.
        VALUATION_LIMIT = 30
        SELECTION_LIMIT = 20

        async def _resolve_wage(player: dict, fallback_league: str = "") -> None:
            """
            Fill in wage_eur from the wage cache (real → league median → tier).

            The builder stamps a wage on every cached player, so this is normally
            a no-op; it only makes a round trip for players that came from the
            mock cohort. Each MCP call opens its own SSE connection, so the
            early return matters.
            """
            # The cached wage is a coarse league/minutes stamp. Once a market value
            # is known, re-price off it — so don't short-circuit on a stamped value.
            if player.get("_wage_priced"):
                return
            player["_wage_priced"] = True
            try:
                salary = await self._call_mcp_tool("get_player_salary", {
                    "player_name":      player.get("name", ""),
                    "league":           player.get("league") or fallback_league,
                    "minutes":          int(player.get("minutes", 0)),
                    "age":              int(player.get("age", 0) or 0),
                    "market_value_eur": float(player.get("value_eur") or 0),
                    "season":           "2425",
                })
                if isinstance(salary, dict) and salary.get("wage_eur_per_week"):
                    player["wage_eur"]       = int(salary["wage_eur_per_week"])
                    player["wage_estimated"] = bool(salary.get("estimated", True))
                    player["wage_source"]    = salary.get("source", "cache")
                    if salary.get("wage_low") and salary.get("wage_high"):
                        player["wage_low"]  = int(salary["wage_low"])
                        player["wage_high"] = int(salary["wage_high"])
            except Exception as exc:
                await self.emit_progress(f"Wage lookup failed for {player.get('name')}: {exc}")

        async def _resolve_market_value(player: dict) -> None:
            """Attach the Transfermarkt valuation and contract expiry, once."""
            if player.get("value_eur") or player.get("_value_checked"):
                return
            player["_value_checked"] = True
            try:
                mv = await self._call_mcp_tool("get_player_market_value", {
                    "player_name": player.get("name", ""),
                    "squad":       player.get("squad", ""),
                    "age":         int(player.get("age", 0) or 0),
                })
                if isinstance(mv, dict) and mv.get("market_value_eur"):
                    player["value_eur"]        = int(mv["market_value_eur"])
                    player["value_source"]     = mv.get("profile_url") or "transfermarkt"
                    player["value_as_of"]      = mv.get("market_value_updated")
                    player["value_estimated"]  = True   # a market value is an estimate
                    player["contract_expires"] = mv.get("contract_expires")
            except Exception as exc:
                await self.emit_progress(
                    f"  {player.get('name','?')}: valuation unavailable ({type(exc).__name__})"
                )

        # ── Step 1: Club profile from vector DB ───────────────────────────────
        club_league = ""
        club_data: dict = {}
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

        # ── Step 2: Extract position and age bounds from query ────────────────
        # Age travels with the position because the cohort query needs both and
        # a second LLM call to fetch one integer is not worth the tokens. Until
        # this carried age, "a centre-back under 27" returned a 33-year-old: the
        # bound was extracted elsewhere, for a search path this agent never uses.
        await self.emit_progress("Extracting target position from query...")
        target_position, min_age, max_age = "FW", None, None
        try:
            pos_resp = await self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[{"role": "user", "content": (
                    "Reply ONLY with JSON "
                    "{\"position\": \"FW\"|\"MF\"|\"DF\"|\"GK\", "
                    "\"max_age\": int|null, \"min_age\": int|null}. "
                    "ST/CF/LW/RW → FW.  CM/DM/AM → MF.  CB/LB/RB → DF.\n"
                    "Age bounds are inclusive: \"under 27\" means max_age 26, "
                    "\"27 or younger\" means max_age 27, \"over 24\" means "
                    "min_age 25. Use null when the query says nothing about age.\n\n"
                    f"Query: {query}"
                )}],
                response_format={"type": "json_object"},
                temperature=0.0,
                # 48 was enough for a non-reasoning model to emit four short
                # fields and nothing else. Groq's current default model spends
                # tokens on internal reasoning before writing the answer — 48
                # is smaller than that overhead alone, so every call here was
                # failing outright, not just running thin. reasoning_effort=low
                # (below) cuts the overhead; this is the margin on top of it.
                max_tokens=300,
                **settings.GROQ_EXTRA_CHAT_KWARGS,
            )
            parsed = json.loads(pos_resp.choices[0].message.content)
            target_position = parsed.get("position") or "FW"
            for key, name in ((parsed.get("min_age"), "min"), (parsed.get("max_age"), "max")):
                if isinstance(key, int) and 15 <= key <= 45:
                    if name == "min":
                        min_age = key
                    else:
                        max_age = key
        except Exception:
            pass

        age_note = ""
        if min_age and max_age:
            age_note = f", age {min_age}-{max_age}"
        elif max_age:
            age_note = f", age up to {max_age}"
        elif min_age:
            age_note = f", age from {min_age}"
        await self.emit_progress(
            f"Target position: {target_position}, searching all Big 5"
            f"{f' (preferring {club_league})' if club_league else ''}{age_note}"
        )

        # ── Step 3: Fetch cohort directly via MCP ─────────────────────────────
        await self.emit_progress(f"Fetching {target_position} cohort from player database...")
        try:
            cohort = await self._call_mcp_tool(
                "get_position_cohort",
                {
                    "position":    target_position,
                    "season":      "2425",
                    # No league filter: the buyer's own league is a preference
                    # applied when ranking, not a wall around the market.
                    "league":      "",
                    "limit":       COHORT_LIMIT,
                    "min_minutes": 450,
                    "max_age":     max_age,
                    "min_age":     min_age,
                },
            )
            cohort = cohort if isinstance(cohort, list) else []
            cohort = [p for p in cohort if isinstance(p, dict) and "name" in p]
        except Exception as e:
            await self.emit_progress(f"Cohort fetch error: {e} — returning empty")
            cohort = []

        if not cohort:
            await self.emit_complete("No cohort data available — returning empty shortlist")
            return {"players": []}

        await self.emit_progress(f"Got {len(cohort)} players from the cache")

        # A player who changed club mid-season has one cached row per squad, so
        # the same person competes with themselves for a slate slot — Barcelona's
        # goalkeeper slate carried Brice Samba twice, at two different fit scores.
        # Keep the row with the most minutes.
        #
        # This stops the duplicate reaching a shortlist; it does NOT reunite the
        # split season. Nicolò Fagioli's 2024-25 sits in two rows (Juventus 571 +
        # Fiorentina 962) and the surviving row still under-reports him, which
        # needs the cache builder to aggregate across squads before per-90s and
        # percentiles are derived.
        by_person: dict[str, dict] = {}
        for p in cohort:
            key = (p.get("name") or "").strip().casefold()
            kept = by_person.get(key)
            if kept is None or int(p.get("minutes", 0) or 0) > int(kept.get("minutes", 0) or 0):
                by_person[key] = p
        if len(by_person) < len(cohort):
            await self.emit_progress(
                f"Merged {len(cohort) - len(by_person)} duplicate row(s) from "
                "mid-season transfers, keeping the fuller season for each player"
            )
            cohort = list(by_person.values())

        # A thin shortlist is a legitimate answer to a tight brief, but only if
        # the reader can see which constraint did the cutting. Brentford asked for
        # a EUR 15M centre-back on EUR 60k/wk and got exactly one candidate, with
        # nothing in the report explaining that those two ceilings barely overlap.
        funnel: dict = {"cohort": len(cohort)}

        # ── Step 3a: Drop candidates that make no sense as signings ───────────
        # A player already at the target club is not a transfer target. FBref's
        # squad names match the club profile's, so a direct comparison is enough.
        if target_club:
            own_club = [p for p in cohort if _same_club(p.get("squad", ""), target_club)]
            if own_club:
                cohort = [p for p in cohort if p not in own_club]
                funnel["already_at_club"] = len(own_club)
                await self.emit_progress(
                    f"Excluded {len(own_club)} player(s) already at {target_club}: "
                    + ", ".join(p.get("name", "?") for p in own_club)
                )

        # FBref reports a combined position string ("MF,DF"). Ranking a midfielder
        # who occasionally covers at the back against centre-back requirements
        # produces a fit score that answers a different question, so keep only
        # players whose PRIMARY position is the one being scouted.
        primary = [
            p for p in cohort
            if p.get("position", "").split(",")[0].strip().upper() == target_position
        ]
        if primary:
            if len(primary) < len(cohort):
                funnel["wrong_primary_position"] = len(cohort) - len(primary)
                await self.emit_progress(
                    f"Dropped {len(cohort) - len(primary)} player(s) whose primary "
                    f"position is not {target_position}"
                )
            cohort = primary
        else:
            await self.emit_progress(
                f"No player has {target_position} as a primary position — "
                "keeping secondary-position candidates"
            )

        if not cohort:
            await self.emit_complete("No eligible candidates after filtering")
            return {"players": []}

        # ── Step 4: Trait scores for the whole cohort ─────────────────────────
        # Metrics come from the cache the builder populated offline; nothing here
        # touches the network. Traits whose metrics that build could not obtain
        # are simply absent, and the fit engine reports them as coverage gaps for
        # the tactical LLM to judge qualitatively.
        from ..engine.trait_engine import compute_trait_scores
        for p in cohort:
            try:
                p["trait_scores"] = compute_trait_scores(p.get("raw_metrics", {}), target_position, {})
            except Exception:
                p["trait_scores"] = {}

        # ── Step 6: LLM selects best 3-5 players ─────────────────────────────
        await self.emit_progress("LLM selecting best candidates from cohort...")

        # Send a compact projection, not the raw cohort. Percentiles are what the
        # model reasons over; value/per90 triple the payload for no benefit, and
        # trait scores are derived from those same percentiles deterministically,
        # so sending both is pure duplication. This is a structural trim — the
        # JSON stays well-formed, unlike a string slice.
        def _digest(p: dict) -> dict:
            return {
                "name":    p.get("name"),
                "squad":   p.get("squad"),
                "age":     p.get("age"),
                "mins":    p.get("minutes"),
                "wage":    p.get("wage_eur"),
                "pct": {
                    k: v.get("percentile")
                    for k, v in (p.get("raw_metrics") or {}).items()
                    if v.get("percentile") is not None
                },
            }

        # Rank by how well each player meets THIS club's requirement, then hand
        # the model a bounded slate.
        #
        # This used to sort by minutes played, which selected the most durable
        # players in the cohort rather than the best-suited ones — and since the
        # cohort was capped, a strong fit outside the top 20 by minutes never
        # reached the model at all. The fit engine is deterministic and free, so
        # there is no reason to defer it until after the shortlist is fixed.
        _fit_cache: dict[int, float] = {}

        def _ranking_score(player: dict) -> float:
            key = id(player)
            if key not in _fit_cache:
                score = 0.0
                if club_data:
                    try:
                        verdict = compute_fit_verdict(player, club_data)
                        score = float(verdict.get("demonstrated_fit") or 0.0)
                    except Exception:
                        score = 0.0
                if not score:
                    # No club profile, or nothing measurable: fall back to the
                    # old ordering so the slate is never arbitrary.
                    score = min(float(player.get("minutes", 0) or 0) / 100.0, 34.0)
                # Same-league players are proven against the same opposition and
                # carry no adaptation risk, which is worth a nudge — but not
                # enough to keep a clearly better fit abroad out of the slate.
                if club_league and club_league.lower() in (player.get("league", "") or "").lower():
                    score += _SAME_LEAGUE_BONUS
                _fit_cache[key] = score
            return _fit_cache[key]

        ranked = sorted(cohort, key=_ranking_score, reverse=True)
        home = sum(1 for p in ranked[:VALUATION_LIMIT]
                   if club_league and club_league.lower() in (p.get("league", "") or "").lower())
        await self.emit_progress(
            f"Ranked {len(cohort)} Big-5 candidates by fit to {target_club or 'the club'}; "
            f"pricing the top {min(VALUATION_LIMIT, len(ranked))} "
            f"({home} from {club_league or 'the same league'}, "
            f"{min(VALUATION_LIMIT, len(ranked)) - home} from abroad)"
        )
        slate = ranked[:VALUATION_LIMIT]

        # ── Fee ceiling: a hard filter, applied BEFORE the model sees anyone ──
        # A transfer fee is as much a constraint as the position. Recommending a
        # €220M striker to a club with a €40M ceiling is not a useful option to
        # weigh, it is noise — so the model is never shown one. This costs one
        # Transfermarkt lookup per candidate on a cold cache and nothing after.
        #
        # The figure compared against the ceiling is the contract-aware fee, not
        # the raw valuation. Gating on the valuation threw away the deals clubs
        # most want: Jonathan Tah's €28M valuation put him over a €25M ceiling in
        # the same window he joined Bayern for nothing.
        if value_max:
            await self.emit_progress(
                f"Valuing {len(slate)} candidates against the €{value_max:,.0f} fee ceiling..."
            )
            for p in slate:
                await _resolve_market_value(p)
                fee, basis = effective_fee_eur(
                    p.get("value_eur"), p.get("contract_expires")
                )
                p["effective_fee_eur"] = fee
                p["fee_basis"] = basis

            affordable_fee = [
                p for p in slate
                if p.get("effective_fee_eur") is None
                or p["effective_fee_eur"] <= value_max
            ]
            priced_out = [p for p in slate if p not in affordable_fee]
            if priced_out:
                funnel["over_fee_ceiling"] = len(priced_out)
                funnel["cheapest_excluded_fee_eur"] = min(
                    (p["effective_fee_eur"] for p in priced_out), default=None
                )
                # Report the effective fee, since that is what the ceiling was
                # applied to; showing the raw valuation makes the arithmetic in
                # the funnel look wrong for anyone on a running-down contract.
                await self.emit_progress(
                    f"Excluded {len(priced_out)} candidate(s) above the fee ceiling: "
                    + ", ".join(
                        f"{p['name']} (€{p['effective_fee_eur'] / 1e6:.0f}M)"
                        for p in priced_out
                    )
                )
            slate = affordable_fee

            if not slate:
                await self.emit_complete(
                    f"No candidate is available within a €{value_max:,.0f} fee ceiling. "
                    "Raise the transfer budget or widen the brief."
                )
                return {"players": [], "reason": "no candidate within the fee budget"}

        # ── Wage ceiling: a filter, with a narrow grace band ─────────────────
        # Deliberately after the valuations: a wage modelled from a player's market
        # value is far closer to reality than a league/minutes tier, which put every
        # Premier League starter near €55,000/wk when Gvardiol's reported range is
        # £200,000-250,000.
        #
        # Unlike the fee gate this one is not absolute. A fee is negotiated once
        # and a club either has the cash or does not; a wage is the number most
        # likely to move in talks, and the ceiling itself is inferred from what
        # the club currently pays rather than stated by anyone. Losing a good fit
        # to a few per cent on an inferred number is the wrong kind of precision.
        #
        # The band is narrow on purpose — this is not a soft margin. Anything
        # beyond it is still excluded outright, and everything inside it is
        # carried forward flagged, so the report can say a candidate is a stretch
        # rather than quietly presenting them as comfortably affordable.
        await self.emit_progress(f"Pricing wages for {len(slate)} candidates...")
        for p in slate:
            await _resolve_wage(p, fallback_league=club_league)

        grace_max = int(salary_max * _WAGE_CEILING_GRACE) if salary_max else 0
        affordable_wage = [p for p in slate if int(p.get("wage_eur", 0) or 0) <= grace_max]
        priced_out_wage = [p for p in slate if p not in affordable_wage]

        stretched = [
            p for p in affordable_wage
            if int(p.get("wage_eur", 0) or 0) > salary_max
        ]
        for p in stretched:
            p["wage_stretch"] = True
            p["wage_stretch_note"] = (
                f"€{p.get('wage_eur', 0):,}/wk against a €{salary_max:,} ceiling — "
                f"within the {(_WAGE_CEILING_GRACE - 1) * 100:.0f}% negotiating band, "
                "not comfortably inside budget"
            )
        if stretched:
            funnel["wage_stretch"] = len(stretched)
            await self.emit_progress(
                f"Kept {len(stretched)} candidate(s) just above the "
                f"€{salary_max:,}/wk ceiling, flagged as a stretch: "
                + ", ".join(
                    f"{p['name']} (€{p.get('wage_eur', 0):,}/wk)" for p in stretched
                )
            )
        if priced_out_wage:
            funnel["over_wage_ceiling"] = len(priced_out_wage)
            funnel["lowest_excluded_wage_eur"] = min(
                (p.get("wage_eur", 0) or 0 for p in priced_out_wage), default=None
            )
            await self.emit_progress(
                f"Excluded {len(priced_out_wage)} candidate(s) above the "
                f"€{salary_max:,}/wk wage ceiling: "
                + ", ".join(
                    f"{p['name']} (€{p.get('wage_eur', 0):,}/wk)" for p in priced_out_wage
                )
            )
        slate = affordable_wage

        if not slate:
            await self.emit_complete(
                f"No candidate fits a €{salary_max:,}/wk wage ceiling. "
                "Raise the wage budget or widen the brief."
            )
            return {"players": [], "reason": "no candidate within the wage budget"}

        # More candidates are valued than are shown: valuing 30 costs seconds,
        # while every extra player in the prompt costs tokens against a daily
        # cap. Those that survived both ceilings are already ordered by fit.
        if len(slate) > SELECTION_LIMIT:
            slate = slate[:SELECTION_LIMIT]

        if len(slate) < len(cohort):
            await self.emit_progress(
                f"Presenting the {len(slate)} strongest affordable candidates to the model "
                f"(of {len(cohort)} in the cohort)"
            )
        cohort_summary = json.dumps([_digest(p) for p in slate], default=str, separators=(",", ":"))

        # Only the traits for the position being scouted — sending all six
        # position profiles invites the model to justify a centre-back with a
        # winger's requirements.
        _POS_KEYS = {
            "GK": ["goalkeeper"], "DF": ["centre_back", "fullback"],
            "MF": ["midfielder"], "FW": ["striker", "winger"],
        }
        relevant_traits = {
            k: club_traits[k]
            for k in _POS_KEYS.get(target_position, [])
            if k in club_traits
        }
        traits_ctx = (
            f"\nIdeal traits for this position: {json.dumps(relevant_traits)}"
            if relevant_traits else ""
        )
        club_line = target_club or "not specified"
        sel_prompt = (
            f"Scouting request: {query}\n"
            f"Budget: salary_max={salary_max} EUR/wk, value_max={value_max} EUR\n"
            f"Target club: {club_line}{traits_ctx}\n\n"
            f"You are scouting FOR {club_line} and no other club. Never justify a "
            f"player by reference to a different club's system or philosophy.\n\n"
            f"Player cohort ({len(slate)} players; percentiles are ranks within this "
            f"position cohort, traits are 0-1 scores):\n"
            f"{cohort_summary}\n\n"
            "Select 3-5 players that best match the request. "
            "Use the wage_eur field from the data for budget checks — DO NOT use your own knowledge of real wages. "
            "Copy names EXACTLY as given. "
            "Return ONLY valid JSON: "
            '{"players": [{"name": str, '
            '"scout_note": "2 sentences: best metric percentile + one strength"}]}'
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
                max_tokens=2200,  # margin for reasoning overhead — see pos_resp above
                **settings.GROQ_EXTRA_CHAT_KWARGS,
            )
            result    = json.loads(sel_resp.choices[0].message.content)
            selected  = result.get("players", [])
        except Exception as exc:
            await self.emit_error("LLM selection failed", str(exc))
            # Fall back to the deterministic ranking rather than returning
            # nothing. `slate` is already ordered by fit to this club and has
            # passed both ceilings, so the top of it is a defensible shortlist —
            # and the model's only contribution beyond that ordering is the
            # scout note. A whole run was being thrown away over a rate limit.
            selected = [{"name": p.get("name", "")} for p in slate[:5]]
            # Recorded either way: without it the funnel reports an empty or
            # unexplained shortlist with the budget exclusions as the only
            # visible cause, and a rate-limited run reads exactly like a brief
            # nobody could satisfy. Rayo Vallecano's benchmark run lost 25
            # affordable goalkeepers here and the report said only that 6 were
            # over the fee ceiling.
            funnel["selection_failed"] = True
            funnel["selection_error"] = type(exc).__name__
            funnel["selection_fallback"] = len(selected)
            await self.emit_progress(
                f"Falling back to the deterministic fit ranking: "
                f"{len(selected)} candidate(s) carried forward without a model note"
            )

        # Rehydrate from the cohort so stats are the cache's, not the model's.
        # The LLM only chooses names and writes the note; it cannot invent a
        # player or restate a metric.
        by_name = {p.get("name", ""): p for p in cohort}
        players: list[dict] = []
        for choice in selected:
            name = (choice.get("name") or "").strip()
            source = by_name.get(name)
            if source is None:
                # Tolerate minor spelling drift before discarding a pick.
                source = next(
                    (p for n, p in by_name.items() if n.lower() == name.lower()), None
                )
            if source is None:
                await self.emit_progress(f"Discarding '{name}' — not in the cohort")
                continue
            player = dict(source)
            player["scout_note"] = choice.get("scout_note", "")
            players.append(player)

        # ── Step 7: Web search for form/injuries ──────────────────────────────
        if players:
            from ..tools.web_search import web_search as _ws

            # ── 7a: Form & injury news ────────────────────────────────────────
            # Kept out of scout_note so the analytical note stays readable on the
            # player card; the raw citations live in their own field.
            await self.emit_progress(f"Web search: form & injury news for {len(players)} candidates...")
            for p in players:
                try:
                    note = await _ws.ainvoke(f"{p.get('name','')} injury form 2025 latest news")
                    p["news"] = str(note)[:600]
                except Exception:
                    pass

            # ── 7b: Wage and market value from the web ────────────────────────
            # Treated as a weak signal, never as authority. An earlier version
            # accepted any figure above €10k and marked it wage_estimated=False,
            # which put two different players on an identical €295,000/wk lifted
            # from a "highest paid" listicle and presented it as fact.
            await self.emit_progress("Cross-checking wages and market values on the web...")
            for p in players:
                name = p.get("name", "")
                squad = p.get("squad", "")
                try:
                    wage_text = str(await _ws.ainvoke(
                        f"{name} {squad} weekly wage salary 2024 2025"
                    ))
                    val_text = "(market value taken from Transfermarkt, not search results)"

                    extract_resp = await self.client.chat.completions.create(
                        model=settings.OPENAI_MODEL,
                        messages=[{"role": "user", "content": (
                            f"Extract financial figures for the footballer {name} ({squad}) "
                            f"from these search results.\n\n"
                            f"WAGE RESULTS:\n{wage_text[:900]}\n\n"
                            f"MARKET VALUE RESULTS:\n{val_text[:900]}\n\n"
                            "Rules:\n"
                            "- Only report a figure explicitly stated for THIS player. If the "
                            "results give a squad-wide or league-wide figure, a different "
                            "player's wage, or nothing specific, return null.\n"
                            "- wage_basis must say whether the source quoted it per week or "
                            "per year. Do not convert; report the number as published.\n"
                            "- Convert GBP to EUR by multiplying by 1.18.\n"
                            "- source_url must be the URL the figure came from, or null.\n\n"
                            'Return ONLY JSON: {"wage_amount_eur": int|null, '
                            '"wage_basis": "weekly"|"annual"|null, '
                            '"market_value_eur": int|null, "source_url": str|null}'
                        )}],
                        response_format={"type": "json_object"},
                        temperature=0.0,
                        max_tokens=350,  # margin for reasoning overhead — see pos_resp above
                        **settings.GROQ_EXTRA_CHAT_KWARGS,
                    )
                    fin = json.loads(extract_resp.choices[0].message.content)
                    _apply_web_wage(p, fin)
                except Exception as exc:
                    await self.emit_progress(f"  {name}: wage cross-check skipped ({type(exc).__name__})")

                # Valuations were resolved before selection so the fee ceiling
                # could be enforced; this only fills gaps for anyone that missed.
                await _resolve_market_value(p)

                # Belt and braces. The pre-selection filter should mean this never
                # fires; if it ever does, the report must say so rather than
                # present an unaffordable player as a live option. Recomputed
                # rather than reused, because this runs after a late valuation
                # may have filled in for a candidate the gate could not price.
                fee, basis = effective_fee_eur(
                    p.get("value_eur"), p.get("contract_expires")
                )
                p.setdefault("effective_fee_eur", fee)
                p.setdefault("fee_basis", basis)
                if fee is not None and value_max and fee > value_max:
                    p["over_fee_budget"] = True
                    p["fee_budget_note"] = (
                        f"€{fee / 1e6:.0f}M effective fee against a "
                        f"€{value_max / 1e6:.0f}M ceiling ({basis})"
                    )
                    await self.emit_progress(
                        f"  {name}: OVER FEE BUDGET (should have been filtered) — "
                        f"{p['fee_budget_note']}"
                    )

                value_note = (
                    f" | value≈€{p['value_eur'] / 1_000_000:.0f}M" if p.get("value_eur") else ""
                )
                contract_note = (
                    f" | contract to {p['contract_expires']}" if p.get("contract_expires") else ""
                )
                await self.emit_progress(
                    f"  {name}: €{p.get('wage_eur', 0):,}/wk "
                    f"[{p.get('wage_source', 'unknown')}]{value_note}{contract_note}"
                )

            # ── Re-apply the wage ceiling ────────────────────────────────────
            # The cross-check above can replace a modelled wage with a higher
            # figure from a wage database, which arrives AFTER the pre-selection
            # gate has run. Without re-checking, a late number walks straight
            # past the ceiling — Matteo Politano reached a Bologna shortlist at
            # €124,615/wk against a €120,000 limit.
            # Applied against the same grace band as the gate above, or this
            # would silently undo it — a candidate admitted as a flagged stretch
            # would be dropped here for the very reason they were flagged.
            if salary_max:
                confirm_max = int(salary_max * _WAGE_CEILING_GRACE)
                still_affordable = [
                    p for p in players if int(p.get("wage_eur", 0) or 0) <= confirm_max
                ]
                priced_out = [p for p in players if p not in still_affordable]
                if priced_out:
                    await self.emit_progress(
                        f"Dropped {len(priced_out)} candidate(s) whose confirmed wage "
                        f"exceeds €{confirm_max:,}/wk: "
                        + ", ".join(
                            f"{p['name']} (€{p.get('wage_eur', 0):,})" for p in priced_out
                        )
                    )
                players = still_affordable
                funnel["over_wage_on_confirmation"] = len(priced_out)

                # A confirmed wage can also push someone who looked comfortable
                # into the band, so the flag is recomputed rather than trusted.
                for p in players:
                    if int(p.get("wage_eur", 0) or 0) > salary_max:
                        p["wage_stretch"] = True
                        p["wage_stretch_note"] = (
                            f"€{p.get('wage_eur', 0):,}/wk against a "
                            f"€{salary_max:,} ceiling — within the negotiating band"
                        )
                    else:
                        p.pop("wage_stretch", None)
                        p.pop("wage_stretch_note", None)

        funnel["shortlisted"] = len(players)
        await self.emit_complete(f"Shortlisted {len(players)} verified players from database")
        return {"players": players, "funnel": funnel}
