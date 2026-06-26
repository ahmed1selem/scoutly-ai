"""
Analysis Agent — deep statistical analysis using the deepagents framework.

Flow (agent-driven, not hard-coded):
  1. read_player_data   → understand the candidates
  2. run_python_script  → generate matplotlib charts as real .png files
  3. describe_image     → GPT-4 Vision reads each chart and extracts insights
  4. Agent writes the final consolidated report referencing chart insights
"""
import asyncio
import base64
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from openai import AzureOpenAI
from langchain_openai import AzureChatOpenAI
from langchain_core.tools import tool
from deepagents import create_deep_agent

from ..utils.config import settings
from ..utils.prompt_loader import PromptLoader


# ── Azure clients (shared) ────────────────────────────────────────────────────

def _azure_llm() -> AzureChatOpenAI:
    return AzureChatOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=0,
    )

def _azure_raw() -> AzureOpenAI:
    return AzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )


# ── Tools ─────────────────────────────────────────────────────────────────────

_PREAMBLE = """\
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
import seaborn as sns
import json, os, sys, warnings
warnings.filterwarnings('ignore')

plt.rcParams.update({
    'figure.facecolor': '#0D1117', 'axes.facecolor': '#161B22',
    'axes.edgecolor': '#30363D', 'text.color': '#C9D1D9',
    'axes.labelcolor': '#C9D1D9', 'xtick.color': '#8B949E',
    'ytick.color': '#8B949E', 'grid.color': '#21262D',
    'grid.alpha': 0.5, 'legend.facecolor': '#161B22',
    'font.family': 'DejaVu Sans',
})
ACCENT, ACCENT2, ACCENT3 = '#00D4AA', '#FF6B35', '#4ECDC4'
PALETTE = [ACCENT, ACCENT2, ACCENT3, '#A78BFA', '#F59E0B', '#EF4444']
"""


@tool
def run_python_script(script_content: str, output_dir: str) -> str:
    """Write and run a Python visualization script. Returns full stdout+stderr.
    If it fails, read the error carefully and fix the script, then retry.

    The following are already imported — DO NOT re-import them:
    matplotlib (Agg backend set), plt, mpatches, np, pd, sns, json, os, sys
    Also available: ACCENT, ACCENT2, ACCENT3, PALETTE (color list)

    Save every chart with the full path:
        plt.savefig(os.path.join(output_dir, 'chart_name.png'), dpi=150, bbox_inches='tight')
        plt.close()

    Args:
        script_content: Python code (do NOT re-import the pre-imported modules).
        output_dir: Absolute path where chart PNGs should be saved.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Inject the safe preamble then the LLM code
    full_script = _PREAMBLE + f"\noutput_dir = r'{output_dir}'\n\n" + script_content

    script_path = out / "visualize.py"
    script_path.write_text(full_script, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += "\nSTDERR:\n" + result.stderr

    if result.returncode != 0:
        # Keep the script file so the agent can diagnose it
        return (
            f"FAILED (exit {result.returncode}):\n{output}\n\n"
            f"Script saved at: {script_path}\n"
            "Fix the error above and call run_python_script again."
        )[:6000]
    return f"SUCCESS:\n{output}"[:6000]


@tool
def describe_image(image_path: str) -> str:
    """Use GPT-4 Vision to describe a chart PNG and extract key insights.
    Call this on every chart after generating it to understand what it shows.

    Args:
        image_path: Absolute path to a .png chart file.
    """
    path = Path(image_path)
    if not path.exists():
        return f"File not found: {image_path}"
    with open(path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    client = _azure_raw()
    response = client.chat.completions.create(
        model=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{img_b64}",
                        "detail": "high",
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "You are a football data analyst. Describe this chart in detail: "
                        "what type of chart is it, what data does it show, what are "
                        "the key trends, standout players, or insights visible? Be specific."
                    ),
                },
            ],
        }],
        max_tokens=600,
    )
    return response.choices[0].message.content


@tool
def list_files(directory: str) -> str:
    """List all files in a directory.

    Args:
        directory: Absolute path to directory.
    """
    try:
        files = sorted(Path(directory).iterdir())
        return "\n".join(str(f) for f in files) if files else "Empty directory."
    except Exception as e:
        return f"Error: {e}"




# ── AnalysisAgent class (keeps the same interface as before) ───────────────────

class AnalysisAgent:
    AGENT_NAME = "analysis"

    def __init__(self, client, sse_queue: asyncio.Queue):
        self._sse_queue = sse_queue

    async def emit(self, event: dict):
        await self._sse_queue.put({"agent": self.AGENT_NAME, **event})

    async def emit_start(self, msg: str):
        await self.emit({"type": "agent_start", "message": msg})

    async def emit_progress(self, msg: str, data: dict | None = None):
        await self.emit({"type": "agent_progress", "message": msg, **({"data": data} if data else {})})

    async def emit_complete(self, msg: str, data: dict | None = None):
        await self.emit({"type": "agent_complete", "message": msg, **({"data": data} if data else {})})

    async def emit_error(self, msg: str, detail: str = ""):
        await self.emit({"type": "agent_error", "message": msg, "detail": detail})

    async def run(
        self,
        players: list[dict],
        run_id: str,
        **kwargs,
    ) -> dict[str, Any]:
        await self.emit_start(f"Starting deep analysis of {len(players)} candidates...")

        chart_dir = str(settings.CHARTS_DIR / run_id)

        # Build the deep agent
        llm = _azure_llm()
        agent = create_deep_agent(
            model=llm,
            tools=[run_python_script, describe_image, list_files],
            system_prompt=PromptLoader.get("analysis"),
        )

        task = (
            f"Analyse these {len(players)} football candidates.\n\n"
            f"player_data = {json.dumps(players, indent=2)}\n\n"
            f"Save ALL charts to this exact directory: {chart_dir}\n\n"
            "Follow the 5 steps in your instructions."
        )

        await self.emit_progress("Deep agent running — generating charts and analysis...")

        # Stream agent events to the SSE queue
        report = "Analysis complete."
        all_charts: list[str] = []

        try:
            for event in agent.stream({"messages": task}, stream_mode="updates"):
                if not isinstance(event, dict):
                    continue
                for node, state in event.items():
                    if not isinstance(state, dict):
                        continue
                    for msg in (state.get("messages") or []):
                        if msg is None:
                            continue
                        kind = type(msg).__name__
                        content = getattr(msg, "content", "") or ""
                        tool_calls = getattr(msg, "tool_calls", []) or []

                        if tool_calls:
                            for tc in tool_calls:
                                if not isinstance(tc, dict):
                                    continue
                                name = tc.get("name", "")
                                args = tc.get("args", {})
                                formatted_args = ", ".join(f"{k}={v}" for k, v in args.items())
                                await self.emit_progress(f"Calling: {name}({formatted_args})")
                        elif kind == "ToolMessage":
                            tool_name = getattr(msg, "name", "tool")
                            content_str = str(content)
                            try:
                                parsed = json.loads(content_str)
                                if isinstance(parsed, list):
                                    preview = f"Returned {len(parsed)} items."
                                    if parsed and isinstance(parsed[0], dict) and "name" in parsed[0]:
                                        names = [p.get('name', 'Unknown') for p in parsed[:3]]
                                        preview += f" Top: {', '.join(names)}"
                                elif isinstance(parsed, dict):
                                    if "players" in parsed:
                                        preview = f"Returned {len(parsed['players'])} players."
                                    elif "ranked_players" in parsed:
                                        preview = f"Ranked {len(parsed['ranked_players'])} players."
                                    else:
                                        preview = f"Returned keys: {', '.join(parsed.keys())}"
                                else:
                                    preview = str(parsed)[:100]
                            except Exception:
                                preview = content_str[:100] + "..." if len(content_str) > 100 else content_str
                            await self.emit_progress(f"Tool result [{tool_name}]: {preview}")
                        elif kind == "AIMessage" and content and isinstance(content, str):
                            report = content
                            if len(content) > 100:
                                await self.emit_progress(f"Agent reasoning: {content[:150]}...")

        except Exception as exc:
            await self.emit_error("Deep agent error", str(exc))
            raise

        # Collect charts produced
        chart_path = Path(chart_dir)
        if chart_path.exists():
            all_charts = [
                str(f) for f in sorted(chart_path.iterdir())
                if f.suffix.lower() == ".png"
            ]

        await self.emit_complete(
            f"Analysis complete — {len(all_charts)} charts generated"
        )

        return {
            "charts": all_charts,
            "player_reports": {},
            "unified_report": report,
        }
