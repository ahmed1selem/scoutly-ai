"""
M5 — Runtime web search tool (Tavily).

Returns cited snippets: each result includes title, url, and content so the
scouter can attach source citations to the player's scout_note.

Usage (as a LangChain tool):
    from backend.tools.web_search import web_search_tool
    result = web_search_tool.func("Erling Haaland injury 2025")
"""
import os
from langchain_core.tools import tool


@tool
def web_search(query_term: str) -> str:
    """
    Search the web for recent news, form, injuries, or contract updates.
    Returns up to 3 cited results in the format:
      [1] Title — URL
      Snippet text.

    Use this to add live context to player evaluations.
    Always include the citation ([1], [2], …) when referencing a search result.

    Args:
        query_term: the search query, e.g. "Erling Haaland injury 2025"
    """
    try:
        from tavily import TavilyClient
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            return "Web search unavailable: TAVILY_API_KEY not set."

        client  = TavilyClient(api_key=api_key)
        resp    = client.search(query=query_term, search_depth="basic", max_results=3)
        results = resp.get("results", [])

        if not results:
            return "No recent results found."

        lines = []
        for i, r in enumerate(results, start=1):
            title   = r.get("title",   "Untitled")
            url     = r.get("url",     "")
            content = r.get("content", "").strip()[:400]
            lines.append(f"[{i}] {title} — {url}\n{content}")

        return "\n\n".join(lines)

    except ImportError:
        return "Web search unavailable: tavily-python not installed."
    except Exception as exc:
        return f"Web search failed: {exc}"
