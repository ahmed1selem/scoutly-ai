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
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
try:
    from weasyprint import HTML as WeasyHTML
except Exception:
    WeasyHTML = None

from ..utils.config import settings, make_llm_client
from ..utils.prompt_loader import PromptLoader
from .chart_generator import generate_all_guaranteed_charts


def _find_chromium() -> str | None:
    """Locate an installed Chrome/Edge binary for headless PDF printing."""
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        shutil.which("chrome"),
        shutil.which("msedge"),
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def _html_to_pdf_via_chromium(html: str, pdf_path: Path) -> bool:
    """
    Render HTML to PDF with headless Chrome.

    WeasyPrint needs native GTK libraries that a stock Windows install does not
    have, which is why reports were silently downgraded to .html. Chrome is
    already present on any machine running the scraper and prints the same CSS
    the browser does, so this produces a real PDF with no extra dependencies.
    """
    browser = _find_chromium()
    if not browser:
        return False

    # Chrome resolves --print-to-pdf against its own working directory, so the
    # destination has to be absolute or the file lands somewhere unexpected and
    # the caller sees a silent failure.
    target = pdf_path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = None

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.html"
        src.write_text(html, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--no-pdf-header-footer",
                    "--run-all-compositor-stages-before-draw",
                    "--virtual-time-budget=10000",
                    f"--print-to-pdf={target}",
                    src.resolve().as_uri(),
                ],
                capture_output=True,
                timeout=180,
            )
        except Exception as exc:
            logging.getLogger(__name__).warning("Chromium print-to-pdf failed: %s", exc)
            return False

    # Chrome signals success by writing the file; its exit code is unreliable.
    if target.exists() and target.stat().st_size > 1024:
        return True
    logging.getLogger(__name__).warning(
        "Chromium print-to-pdf produced no usable file (exit %s): %s",
        getattr(proc, "returncode", "?"),
        (proc.stderr.decode("utf-8", "replace")[:300] if proc and proc.stderr else ""),
    )
    return False


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
        self._client = None

    async def emit(self, event: dict) -> None:
        event.setdefault("agent", "pdf_generator")
        await self.queue.put(event)

    def _get_client(self):
        if self._client is None:
            self._client = make_llm_client()
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
        selection_funnel: dict | None = None,
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
        # `match` is an Ansible test, not a Jinja2 one. The template uses it to
        # pick charts by filename prefix, so without this the whole render raises
        # TemplateRuntimeError as soon as there is at least one chart.
        jinja_env.tests["match"] = lambda value, pattern: bool(re.match(pattern, str(value)))
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
            selection_funnel=selection_funnel or {},
        )

        # ── Step 4: HTML → PDF ────────────────────────────────────────────
        # Always keep the HTML alongside the PDF: it is the source of truth for
        # the layout and stays readable if PDF conversion is unavailable.
        html_path = settings.REPORTS_DIR / f"{run_id}.html"
        html_path.write_text(html_content, encoding="utf-8")

        pdf_target = settings.REPORTS_DIR / f"{run_id}.pdf"
        pdf_path = html_path
        backend = "none"

        if WeasyHTML:
            await self.emit({"type": "progress", "message": "Converting to PDF via WeasyPrint..."})
            try:
                WeasyHTML(string=html_content, base_url=str(settings.TEMPLATES_DIR)).write_pdf(str(pdf_target))
                pdf_path, backend = pdf_target, "weasyprint"
            except Exception as exc:
                await self.emit({"type": "progress", "message": f"WeasyPrint failed ({exc}) — trying headless Chrome"})

        if backend == "none":
            await self.emit({"type": "progress", "message": "Converting to PDF via headless Chrome..."})
            if await asyncio.to_thread(_html_to_pdf_via_chromium, html_content, pdf_target):
                pdf_path, backend = pdf_target, "chromium"
            else:
                await self.emit({
                    "type": "progress",
                    "message": "No PDF backend available — delivering the HTML report "
                               "(install GTK for WeasyPrint, or Chrome for print-to-PDF)",
                })

        await self.emit({
            "type": "agent_complete",
            "message": f"Report saved: {pdf_path.name} (via {backend})",
            "data": {
                "pdf_path": str(pdf_path),
                "filename": pdf_path.name,
                "html_path": str(html_path),
                "pdf_backend": backend,
            },
        })

        return {
            "pdf_path": str(pdf_path),
            "filename": pdf_path.name,
            "html_path": str(html_path),
            "pdf_backend": backend,
        }
