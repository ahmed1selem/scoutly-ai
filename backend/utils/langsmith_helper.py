"""
LangSmith helper utilities.

Provides:
  - get_trace_url(run_id)  → build a LangSmith project URL for the current run
  - traceable_pipeline()   → async wrapper that applies @traceable and captures metadata
"""
from __future__ import annotations

import os
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


def _langsmith_enabled() -> bool:
    return bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))


def get_trace_url(run_id: str = "") -> str:
    """
    Return the LangSmith project URL.  When LangSmith is enabled we attempt
    to get the per-trace URL; otherwise returns "" (no link shown in report).

    We build the URL from env vars rather than making an API call so there is
    zero added latency on the hot path.
    """
    if not _langsmith_enabled():
        return ""

    project = os.getenv("LANGCHAIN_PROJECT", "scoutly-ai")
    endpoint = os.getenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain.com")

    # Derive the UI base from the API endpoint
    if "api.smith.langchain.com" in endpoint:
        ui_base = "https://smith.langchain.com"
    else:
        # Self-hosted LangSmith — UI likely same host without /api
        ui_base = endpoint.replace("api.", "").rstrip("/")

    # Try to get the run-level URL using get_current_run_tree()
    try:
        from langsmith.run_helpers import get_current_run_tree
        tree = get_current_run_tree()
        if tree and tree.id:
            # LangSmith trace URL format:
            # https://smith.langchain.com/o/{tenant}/projects/p/{project_id}/runs/{run_id}
            # We can't know tenant/project_id without an API call, so we use the
            # search URL which filters by our run_id tag.
            return (
                f"{ui_base}/projects/{project}/runs"
                f"?filter=%7B%22run_id%22%3A%22{tree.id}%22%7D"
            )
    except Exception:
        pass

    # Fallback: link to the project dashboard (still useful)
    return f"{ui_base}/projects/{project}"


async def traceable_pipeline(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    run_name: str = "scoutly_pipeline",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """
    Wrap an async callable with LangSmith tracing if enabled.
    Falls back to a plain call when LangSmith is not configured.
    """
    if not _langsmith_enabled():
        return await coro_fn()

    try:
        from langsmith import traceable

        @traceable(
            name=run_name,
            tags=tags or [],
            metadata=metadata or {},
        )
        async def _traced():
            return await coro_fn()

        return await _traced()

    except ImportError:
        logger.debug("langsmith not installed — running pipeline without tracing")
        return await coro_fn()
    except Exception as exc:
        logger.warning(f"LangSmith tracing setup failed ({exc}) — running without tracing")
        return await coro_fn()
