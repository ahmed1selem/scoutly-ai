"""
PDF Generator — assembles the final scouting report using WeasyPrint.

Flow:
  1. Call GPT-4o (using pdf_report.md prompt) to generate narrative sections
  2. Encode all chart images as base64
  3. Render the Jinja2 HTML template with all data
  4. Convert HTML → PDF using WeasyPrint
  5. Save to reports/<run_id>.pdf
"""
import asyncio
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from openai import AsyncAzureOpenAI
try:
    from weasyprint import HTML as WeasyHTML
except Exception:
    WeasyHTML = None

from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader
from .chart_generator import generate_all_guaranteed_charts


def _encode_chart(path: str) -> str:
    """Base64-encode a chart image for embedding in HTML."""
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return ""


_CHART_TITLE_MAP = {
    "00_fit_ranking":   "Candidate Fit Score Ranking",
    "01_radar_overlay": "Player Trait Radar — All Candidates",
    "04_wage_vs_fit":   "Value-for-Money Positioning",
}

def _chart_name(path: str) -> str:
    stem = Path(path).stem
    # Guaranteed charts: strip leading "NN_" prefix
    clean = re.sub(r"^\d+_", "", stem)
    if stem in _CHART_TITLE_MAP:
        return _CHART_TITLE_MAP[stem]
    # Per-player percentile charts
    if stem.startswith("02_percentile_"):
        raw = re.sub(r"^02_percentile_\d+_", "", stem).replace("_", " ").title()
        return f"{raw} — Performance Percentiles"
    return clean.replace("_", " ").title()


class PDFGenerator:
    """Generates the final WeasyPrint PDF scouting report."""

    def __init__(self, sse_queue: asyncio.Queue):
        self.queue = sse_queue
        self._client: AsyncAzureOpenAI | None = None

    async def emit(self, event: dict) -> None:
        event.setdefault("agent", "pdf_generator")
        await self.queue.put(event)

    def _get_client(self) -> AsyncAzureOpenAI:
        if self._client is None:
            self._client = AsyncAzureOpenAI(
                api_key=settings.AZURE_OPENAI_API_KEY,
                api_version=settings.AZURE_OPENAI_API_VERSION,
                azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            )
        return self._client

    async def _generate_narratives(
        self,
        query: str,
        ranked_players: list[dict],
        financial_decision: dict,
        tactical_summary: str,
        unified_analysis: str,
    ) -> dict[str, Any]:
        """Use GPT-4o with pdf_report.md prompt to write all narrative sections."""
        system_prompt = PromptLoader.get("pdf_report", use_cache=False)
        client = self._get_client()

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Scouting mission: {query}\n\n"
                        f"Ranked candidates: {json.dumps(ranked_players, indent=2)[:4000]}\n\n"
                        f"Financial decision: {json.dumps(financial_decision, indent=2)}\n\n"
                        f"Tactical summary: {tactical_summary}\n\n"
                        f"Analysis highlights: {unified_analysis[:2000]}\n\n"
                        "Generate all narrative sections for the PDF report. "
                        "Return JSON with keys: executive_summary, player_narratives (dict name→text), "
                        "financial_section, tactical_appendix, conclusion."
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
        )
        return json.loads(response.choices[0].message.content)

    async def generate(
        self,
        run_id: str,
        query: str,
        ranked_players: list[dict] | None = None,
        financial_decision: dict | None = None,
        tactical_summary: str = "",
        charts: list[str] | None = None,
        unified_analysis: str = "",
        langsmith_trace_url: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        """
        Generate the full PDF scouting report.

        Returns:
            {"pdf_path": str, "page_count": int}
        """
        await self.emit({"type": "agent_start", "message": "Generating PDF scouting report..."})

        ranked_players = ranked_players or []
        financial_decision = financial_decision or {}
        charts = charts or []

        # ── Step 1: Generate narrative sections ───────────────────────────
        await self.emit({"type": "progress", "message": "Writing report narratives..."})
        try:
            narratives = await self._generate_narratives(
                query=query,
                ranked_players=ranked_players,
                financial_decision=financial_decision,
                tactical_summary=tactical_summary,
                unified_analysis=unified_analysis,
            )
        except Exception as exc:
            narratives = {
                "executive_summary": f"Report generated for: {query}",
                "player_narratives": {},
                "financial_section": str(financial_decision),
                "tactical_appendix": tactical_summary,
                "conclusion": "See ranked players list for recommendations.",
            }

        # ── Step 2a: Guaranteed programmatic charts ───────────────────────
        await self.emit({"type": "progress", "message": "Generating analytics charts..."})
        try:
            guaranteed = generate_all_guaranteed_charts(
                players=ranked_players,
                charts_dir=settings.CHARTS_DIR,
                run_id=run_id,
            )
        except Exception as exc:
            guaranteed = []
            await self.emit({"type": "progress", "message": f"Chart generation warning: {exc}"})

        # Guaranteed charts come first in the report
        all_chart_paths = guaranteed + list(charts)

        # ── Step 2b: Encode charts as base64 ─────────────────────────────
        await self.emit({"type": "progress", "message": f"Embedding {len(all_chart_paths)} charts..."})
        encoded_charts = [
            {
                "name":       _chart_name(c),
                "filename":   Path(c).name,
                "b64":        _encode_chart(c),
                "guaranteed": Path(c).parent.name == "guaranteed",
            }
            for c in all_chart_paths
        ]

        # ── Step 3: Render HTML template ──────────────────────────────────
        await self.emit({"type": "progress", "message": "Rendering HTML report template..."})

        jinja_env = Environment(
            loader=FileSystemLoader(str(settings.TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        # zfill is a Python str method, not a built-in Jinja2 filter
        jinja_env.filters["zfill"] = lambda value, width: str(value).zfill(width)
        template = jinja_env.get_template("report.html")

        html_content = template.render(
            query=query,
            run_id=run_id,
            generated_at=datetime.now().strftime("%d %B %Y, %H:%M"),
            team_name=financial_decision.get("team_name", "FC Antigravity"),
            ranked_players=ranked_players,
            financial_decision=financial_decision,
            tactical_summary=tactical_summary,
            charts=encoded_charts,
            narratives=narratives,
            player_count=len(ranked_players),
            langsmith_trace_url=langsmith_trace_url,
        )

        # ── Step 4: WeasyPrint → PDF ──────────────────────────────────────
        await self.emit({"type": "progress", "message": "Converting HTML to PDF via WeasyPrint..."})

        if WeasyHTML:
            pdf_path = settings.REPORTS_DIR / f"{run_id}.pdf"
            try:
                WeasyHTML(string=html_content, base_url=str(settings.TEMPLATES_DIR)).write_pdf(str(pdf_path))
            except Exception as e:
                # If WeasyPrint fails at runtime, save as HTML
                pdf_path = settings.REPORTS_DIR / f"{run_id}.html"
                with open(pdf_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
        else:
            # If WeasyPrint failed to import, save as HTML
            pdf_path = settings.REPORTS_DIR / f"{run_id}.html"
            with open(pdf_path, "w", encoding="utf-8") as f:
                f.write(html_content)

        await self.emit({
            "type": "agent_complete",
            "message": f"Report saved: {pdf_path.name}",
            "data": {"pdf_path": str(pdf_path), "filename": pdf_path.name},
        })

        return {"pdf_path": str(pdf_path), "filename": pdf_path.name}
