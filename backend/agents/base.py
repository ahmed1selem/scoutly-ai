"""Base Agent — shared foundation for all sub-agents."""
import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator

from openai import AsyncAzureOpenAI

# deepagents requires a custom LangChain fork — import is protected so the
# app starts even on environments with standard LangChain (e.g. Fly.io).
try:
    from deepagents import create_deep_agent
    _DEEPAGENTS_OK = True
except Exception as _da_err:
    create_deep_agent = None
    _DEEPAGENTS_OK = False
    import logging as _log
    _log.getLogger(__name__).warning(
        "deepagents unavailable (%s) — agent pipeline will run in fallback mode", _da_err
    )

from ..utils.prompt_loader import PromptLoader
from ..utils.config import settings, get_langchain_azure_llm


class BaseAgent(ABC):
    """
    Abstract base class for all football management agents.

    Each agent:
    - Loads its own system prompt from prompts/<name>.md at runtime
    - Emits SSE events to a shared asyncio.Queue for real-time streaming
    - Communicates with OpenAI GPT-4o using function calling
    """

    #: Override in subclass — must match the .md filename in prompts/
    AGENT_NAME: str = "base"

    def __init__(self, client: AsyncAzureOpenAI, sse_queue: asyncio.Queue):
        self.client = client
        self.queue = sse_queue
        self._system_prompt: str | None = None

    # ─────────────────────────────────────────────────────────────────────────
    # Prompt Loading
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def system_prompt(self) -> str:
        """
        Dynamically load the system prompt from prompts/<AGENT_NAME>.md.
        Reloaded fresh every invocation (no cache) for hot-reload support.
        """
        return PromptLoader.get(self.AGENT_NAME, use_cache=False)

    # ─────────────────────────────────────────────────────────────────────────
    # SSE Event Emission
    # ─────────────────────────────────────────────────────────────────────────

    async def emit(self, event: dict[str, Any]) -> None:
        """Push a structured event to the SSE queue."""
        event.setdefault("agent", self.AGENT_NAME)
        event.setdefault("timestamp", time.time())
        await self.queue.put(event)

    async def emit_start(self, message: str) -> None:
        await self.emit({"type": "agent_start", "message": message})

    async def emit_progress(self, message: str, data: Any = None) -> None:
        payload: dict[str, Any] = {"type": "progress", "message": message}
        if data is not None:
            payload["data"] = data
        await self.emit(payload)

    async def emit_complete(self, message: str, data: Any = None) -> None:
        payload: dict[str, Any] = {"type": "agent_complete", "message": message}
        if data is not None:
            payload["data"] = data
        await self.emit(payload)

    async def emit_error(self, message: str, error: str = "") -> None:
        await self.emit({"type": "error", "message": message, "error": error})

    # ─────────────────────────────────────────────────────────────────────────
    # LLM Helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_deep_agent(self, task_message: str, tools: list | None = None) -> str:
        """
        Run a deep agent loop with streaming SSE emission.
        Returns the final string output of the agent.
        """
        if not _DEEPAGENTS_OK:
            raise RuntimeError(
                "deepagents is not available in this environment. "
                "Ensure the full dependency stack (langchain-anthropic fork) is installed."
            )
        llm = get_langchain_azure_llm()
        agent = create_deep_agent(
            model=llm,
            tools=tools or [],
            system_prompt=self.system_prompt,
        )

        await self.emit_progress(f"[{self.AGENT_NAME}] Deep agent starting...")
        
        final_report = ""
        
        try:
            async for event in agent.astream({"messages": task_message}, stream_mode="updates"):
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
                                await self.emit_progress(f"[{self.AGENT_NAME}] Calling tool: {name}({formatted_args})")
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
                            await self.emit_progress(f"[{self.AGENT_NAME}] Tool result [{tool_name}]: {preview}")
                        elif kind == "AIMessage" and content and isinstance(content, str):
                            final_report = content
                            if len(content) > 50:
                                await self.emit_progress(f"[{self.AGENT_NAME}] Reasoning: {content[:150]}...")
        except Exception as exc:
            await self.emit_error(f"[{self.AGENT_NAME}] Deep agent error", str(exc))
            raise

        return final_report

    # ─────────────────────────────────────────────────────────────────────────
    # Abstract Interface
    # ─────────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def run(self, **kwargs) -> dict[str, Any]:
        """
        Execute this agent's task.
        Must be implemented by each concrete agent.
        Returns a dict with the agent's output.
        """
        ...
