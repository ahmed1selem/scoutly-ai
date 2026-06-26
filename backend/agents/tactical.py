"""
Tactical Agent — evaluates candidates against the club's tactical profile and ranks them.

M4: deterministic trait-match (fit_engine) runs first; the LLM writes the
qualitative verdict and justification over those numbers.
"""
import asyncio
import json
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .base import BaseAgent
from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader
from ..engine.fit_engine import compute_fit_verdict

try:
    import fitz
    _PDF_BACKEND = "pymupdf"
except ImportError:
    _PDF_BACKEND = None


def _extract_pdf_text(pdf_path: Path) -> str:
    if not pdf_path.exists():
        return (
            "Tactical document not found. Using default tactical profile:\n"
            "Formation: 4-3-3 (high press)\nStyle: Possession-based with vertical transitions\n"
        )
    if _PDF_BACKEND == "pymupdf":
        doc = fitz.open(str(pdf_path))
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        return text
    try:
        return pdf_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "Unable to read tactical document."


class TacticalAgent(BaseAgent):
    AGENT_NAME = "tactical"

    def __init__(self, client: AsyncOpenAI, sse_queue: asyncio.Queue):
        super().__init__(client, sse_queue)

    async def run(
        self,
        players: list[dict],
        financial_decision: dict,
        target_club: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        await self.emit_start("Loading club tactical profile from Knowledge Base...")

        # ── 1. Fetch club profile from vector DB ───────────────────────────────
        club_profile: dict = {}
        rag_context: str   = ""

        try:
            from ..rag.vectorstore import get_vectorstore
            vs = get_vectorstore()

            search_term = target_club if target_club else "football tactics pressing possession"
            clubs = vs.search_clubs(search_term, n_results=1)

            if clubs:
                club_profile = clubs[0]
                club_name    = club_profile.get("club", {}).get("club_name", "Unknown")
                primary_style = (
                    club_profile.get("team_identity", {})
                    .get("primary_style", {})
                    .get("value", "")
                )
                await self.emit_progress(
                    f"Club profile found: {club_name} (style: {primary_style})"
                )

                # Retrieve generic tactical-knowledge docs for this style
                knowledge_docs = vs.get_tactical_knowledge(
                    primary_style or "football tactics", n_results=3
                )
                rag_context = "\n\n".join(knowledge_docs)
            else:
                await self.emit_progress("No club profile found in KB; using tactics PDF fallback.")

        except Exception as e:
            await self.emit_progress(f"Vector DB error ({e}); using tactics PDF fallback.")

        if not club_profile:
            tactics_text = _extract_pdf_text(settings.TACTICS_PDF_PATH)
            rag_context  = tactics_text

        # ── 2. Deterministic trait-match for each player (M4 core) ─────────────
        await self.emit_progress(
            f"Computing deterministic fit scores for {len(players)} candidates..."
        )

        fit_verdicts: list[dict] = []
        for p in players:
            if not p.get("trait_scores"):
                # Player came in without trait_scores — compute them now if raw_metrics exist
                raw_metrics = p.get("raw_metrics", {})
                position    = p.get("position", "FW")
                game_attrs  = {
                    k: p.get(k)
                    for k in ("sprint_speed", "stamina")
                    if p.get(k) is not None
                }
                if raw_metrics:
                    from ..engine.trait_engine import compute_trait_scores
                    p["trait_scores"] = compute_trait_scores(
                        raw_metrics, position, game_attrs
                    )

            verdict = compute_fit_verdict(p, club_profile) if club_profile else {
                "measurable_fit":      0.0,
                "trait_coverage":      0.0,
                "per_trait_breakdown": [],
                "qualitative_gaps":    [],
                "confidence":          0.1,
                "abstain":             True,
                "abstain_reason":      "No club profile available",
                "used_position_key":   None,
            }
            fit_verdicts.append({"player": p, "verdict": verdict})
            name = p.get("name", "?")
            fit  = verdict["measurable_fit"]
            conf = verdict["confidence"]
            await self.emit_progress(
                f"  {name}: measurable_fit={fit:.1f}  confidence={conf:.2f}"
                + (f"  [ABSTAIN: {verdict['abstain_reason']}]" if verdict["abstain"] else "")
            )

        # ── 3. LLM qualitative verdict ─────────────────────────────────────────
        await self.emit_progress("LLM writing qualitative verdict over deterministic scores...")

        task_prompt = PromptLoader.get("tactical_evaluate").format(
            player_count=len(players),
            club_profile_json=json.dumps(club_profile, indent=2)[:3000],
            rag_context=rag_context[:2000],
            fit_verdicts_json=json.dumps(
                [
                    {
                        "name":            fv["player"].get("name"),
                        "position":        fv["player"].get("position"),
                        "measurable_fit":  fv["verdict"]["measurable_fit"],
                        "trait_coverage":  fv["verdict"]["trait_coverage"],
                        "per_trait_breakdown": fv["verdict"]["per_trait_breakdown"],
                        "qualitative_gaps":    fv["verdict"]["qualitative_gaps"],
                        "confidence":      fv["verdict"]["confidence"],
                        "abstain":         fv["verdict"]["abstain"],
                        "abstain_reason":  fv["verdict"].get("abstain_reason"),
                        "trait_scores":    fv["player"].get("trait_scores", {}),
                        "scout_note":      fv["player"].get("scout_note", ""),
                    }
                    for fv in fit_verdicts
                ],
                indent=2,
            )[:4000],
            financial_json=json.dumps(financial_decision, indent=2),
        )
        task_prompt += (
            "\n\nCRITICAL: Output ONLY valid JSON. "
            "Do not include markdown code blocks or extra text."
        )

        response_text = await self._run_deep_agent(task_prompt)
        cleaned_text  = response_text.replace("```json", "").replace("```", "").strip()

        try:
            result = json.loads(cleaned_text)
        except json.JSONDecodeError:
            await self.emit_error("Failed to parse JSON from tactical agent.", cleaned_text)
            raise

        ranked  = result.get("ranked_players", [])
        summary = result.get("tactical_summary", "")

        # Merge deterministic fit data back into ranked player dicts
        name_to_verdict = {
            fv["player"].get("name", ""): fv["verdict"] for fv in fit_verdicts
        }
        name_to_player  = {p.get("name", ""): p for p in players}

        for rp in ranked:
            name = rp.get("name", "")
            if name in name_to_player:
                rp.update(name_to_player[name])
            if name in name_to_verdict:
                v = name_to_verdict[name]
                rp["measurable_fit"]      = v["measurable_fit"]
                rp["trait_coverage"]      = v["trait_coverage"]
                rp["per_trait_breakdown"] = v["per_trait_breakdown"]
                rp["qualitative_gaps"]    = v["qualitative_gaps"]
                rp["confidence"]          = v["confidence"]
                rp["abstain"]             = v["abstain"]
                rp["abstain_reason"]      = v.get("abstain_reason")

        await self.emit_complete(
            f"Tactical ranking complete — top pick: {ranked[0]['name'] if ranked else 'N/A'}"
        )
        return {"ranked_players": ranked, "tactical_summary": summary}
