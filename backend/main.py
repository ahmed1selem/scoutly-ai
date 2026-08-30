"""
FastAPI Backend — main application entry point.

Endpoints:
  GET  /                     → Serve frontend HTML
  GET  /static/*             → Serve CSS/JS
  POST /api/scout            → Start a scouting pipeline (SSE stream)
  GET  /api/download/{run_id} → Download the generated PDF
  GET  /api/health           → Health check
  GET  /api/agents           → List available agents & their prompts
"""
import asyncio
import json
import uuid
from pathlib import Path

import aiofiles
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncAzureOpenAI

from .agents.planner import PlannerAgent
from .utils.config import settings, make_azure_client
from .utils.prompt_loader import PromptLoader
from .utils.langsmith_helper import get_trace_url, traceable_pipeline

from pydantic import BaseModel
from typing import Literal

class RunState(BaseModel):
    run_id: str
    status: Literal["scouting", "awaiting_review", "revising", "finalizing", "done", "error"]
    query: str
    financial_decision: dict = {}
    ranked_players: list[dict] = []
    directive_history: list[str] = []
    selection_funnel: dict = {}
    conversation: list[dict] = []   # chat transcript {role, message}
    revision_count: int = 0
    pdf_path: str = ""
    langsmith_trace_url: str = ""

ACTIVE_RUNS: dict[str, RunState] = {}

# ── App Setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Football AI Management System",
    description="Multi-agent AI scouting platform for professional football clubs",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount frontend static files
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")


# ── OpenAI Client (shared) ────────────────────────────────────────────────────
def get_openai_client() -> AsyncAzureOpenAI:
    return make_azure_client()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_frontend():
    index_path = _frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({"status": "Football AI Management System API", "docs": "/docs"})


@app.get("/api/health")
async def health_check():
    """
    Report whether the pipeline's two hard dependencies are actually usable:
    the Azure OpenAI deployment and the pre-built player cache. Both fail in
    ways that only surface deep inside a run otherwise.
    """
    checks = await preflight()
    return {
        "status": "ok" if all(c["ok"] for c in checks.values()) else "degraded",
        "model": settings.OPENAI_MODEL,
        "agents": PromptLoader.list_agents(),
        "checks": checks,
    }


async def preflight() -> dict[str, dict]:
    """Cheap liveness checks for the configured LLM provider and the player cache."""
    import socket
    from urllib.parse import urlparse

    results: dict[str, dict] = {}

    # Check whichever provider is actually configured. Checking Azure while the
    # pipeline runs on Groq reports a fatal-sounding failure that isn't real.
    provider = settings.LLM_PROVIDER
    if provider == "groq":
        key_name, endpoint = "groq", settings.GROQ_BASE_URL
        keys = settings.GROQ_API_KEYS
        remedy = "Set GROQ_API_KEYS in .env (comma-separated to rotate)."
    else:
        key_name, endpoint = "azure_openai", settings.AZURE_OPENAI_ENDPOINT
        keys = [settings.AZURE_OPENAI_API_KEY] if settings.AZURE_OPENAI_API_KEY else []
        remedy = "Point AZURE_OPENAI_ENDPOINT at a live deployment in .env."

    host = urlparse(endpoint).hostname or ""
    if not keys:
        results[key_name] = {
            "ok": False,
            "detail": f"no API key configured for provider '{provider}'. {remedy}",
        }
    else:
        try:
            await asyncio.get_running_loop().getaddrinfo(host, 443)
            results[key_name] = {
                "ok": True,
                "detail": f"{host} resolves, {len(keys)} key(s), model {settings.OPENAI_MODEL}",
            }
        except (socket.gaierror, OSError) as exc:
            results[key_name] = {
                "ok": False,
                "detail": (
                    f"host '{host}' does not resolve ({exc}). Every agent, the "
                    f"intent router and all RAG embeddings will fail. {remedy}"
                ),
            }

    cache_db = settings.ROOT_DIR / "data" / "fbref_cache.db"
    if not cache_db.exists():
        results["player_cache"] = {
            "ok": False,
            "detail": "data/fbref_cache.db missing — run "
                      "`python -m backend.data_pipeline.cache_builder`",
        }
    else:
        try:
            import sqlite3
            conn = sqlite3.connect(str(cache_db))
            count = conn.execute("SELECT COUNT(*) FROM player_cohort").fetchone()[0]
            wages = conn.execute("SELECT COUNT(*) FROM player_wages").fetchone()[0]
            conn.close()
            results["player_cache"] = {
                "ok": count > 0,
                "detail": f"{count} players, {wages} wage rows",
            }
        except Exception as exc:
            results["player_cache"] = {"ok": False, "detail": f"unreadable: {exc}"}

    return results


@app.on_event("startup")
async def log_preflight() -> None:
    import logging

    log = logging.getLogger("scoutly")
    for name, result in (await preflight()).items():
        if result["ok"]:
            log.info("preflight %s: OK — %s", name, result["detail"])
        else:
            log.error("preflight %s: FAILED — %s", name, result["detail"])


@app.get("/api/agents")
async def list_agents():
    """Return all available agents and their system prompt summaries."""
    manifest = PromptLoader.manifest()
    agents = []
    for name, description in manifest.items():
        agents.append({
            "name": name,
            "description": description,
            "prompt_file": f"prompts/{name}.md",
        })
    return {"agents": agents}


@app.post("/api/scout")
async def scout_players(request: Request):
    """
    Start a full agentic scouting pipeline, pausing at the draft stage.
    Streams real-time SSE events.
    """
    body = await request.json()
    query: str = body.get("query", "").strip()

    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    run_id = str(uuid.uuid4())
    ACTIVE_RUNS[run_id] = RunState(run_id=run_id, status="scouting", query=query)
    
    client = get_openai_client()

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()
        yield f"data: {json.dumps({'type': 'connected', 'run_id': run_id, 'query': query})}\n\n"

        async def run_pipeline():
            try:
                planner = PlannerAgent(client=client, sse_queue=queue, run_id=run_id)
                result = await planner.run_draft(query=query)
                
                # Update State
                run_state = ACTIVE_RUNS[run_id]
                run_state.ranked_players = result.get("ranked_players", [])
                run_state.financial_decision = result.get("financial_decision", {})
                run_state.selection_funnel = result.get("selection_funnel", {})
                run_state.status = "awaiting_review"

                await queue.put({
                    "type": "draft_ready",
                    "run_id": run_id,
                    "ranked_players": run_state.ranked_players,
                    "financial_decision": run_state.financial_decision,
                    # Lets the UI explain a thin or empty shortlist instead of
                    # just showing fewer cards with no reason given.
                    "selection_funnel": run_state.selection_funnel,
                })
                await queue.put({
                    "type": "awaiting_review",
                    "run_id": run_id,
                })
            except Exception as exc:
                ACTIVE_RUNS[run_id].status = "error"
                await queue.put({
                    "type": "pipeline_error",
                    "run_id": run_id,
                    "error": str(exc),
                })
            finally:
                await queue.put(None)  # Sentinel → close stream

        task = asyncio.create_task(run_pipeline())

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1200.0)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'timeout', 'message': 'Pipeline timed out'})}\n\n"
                break

            if event is None:
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                break

            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

@app.post("/api/scout/{run_id}/directive")
async def scout_directive(run_id: str, request: Request):
    """
    Provide a directive to revise the current draft, or select a player to finalize.
    Acts as an Intent Router.
    """
    body = await request.json()
    message: str = body.get("message", "").strip()

    if run_id not in ACTIVE_RUNS:
        raise HTTPException(status_code=404, detail="Run not found.")
        
    run_state = ACTIVE_RUNS[run_id]
    if run_state.status != "awaiting_review":
        raise HTTPException(status_code=400, detail=f"Cannot process directive in state: {run_state.status}")

    client = get_openai_client()

    # ── Record user turn in conversation transcript ────────────────────────────
    run_state.conversation.append({"role": "user", "message": message})

    # --- INTENT ROUTER ---
    router_system_prompt = """You are the Intent Router for an AI Football Scouting Assistant. Your job is to read the user's latest chat message and classify their intent. You will be provided with the user's message and the CURRENT_SHORTLIST of players. Output a strictly valid JSON object matching the schema below. Do not include markdown formatting.

AVAILABLE INTENTS:
1. "SELECT_PLAYER": The user explicitly chooses one or more players from the CURRENT_SHORTLIST. Extract the exact player names and add them to the selected_player_ids array.
2. "MODIFY_SEARCH": The user is unsatisfied, wants to change requirements, budget, position, or wants to pivot the search. Leave selected_player_ids empty.
3. "ASK_QUESTION": The user asks a question about the shortlist (e.g. "why is X ranked above Y?", "what are X's weaknesses?", "explain the fit scores") WITHOUT selecting a player or changing the search. Set answer_prompt to a brief answer using the shortlist data. Do NOT re-run any agents.
4. "INVALID_INPUT": The user inputs garbage text, tries to select a player NOT on the shortlist, or goes completely off-topic. Write a polite clarification_message.

OUTPUT JSON SCHEMA:
{
  "intent": "SELECT_PLAYER" | "MODIFY_SEARCH" | "ASK_QUESTION" | "INVALID_INPUT",
  "selected_player_ids": [ "string" ],
  "answer_prompt": "string | null",
  "clarification_message": "string | null"
}"""

    shortlist_summary = [
        {
            "name":            p.get("name"),
            "fit_score":       p.get("measurable_fit") or p.get("tactical_fit_score"),
            "confidence":      p.get("confidence"),
            "scout_note":      (p.get("scout_note", "") or "")[:150],
        }
        for p in run_state.ranked_players
    ]
    router_user_prompt = (
        f"CURRENT_SHORTLIST: {json.dumps(shortlist_summary)}\n\n"
        f"USER_MESSAGE: {message}"
    )

    try:
        router_response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": router_system_prompt},
                {"role": "user",   "content": router_user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            # No cap previously — harmless with a non-reasoning model but
            # unbounded cost/latency risk now that Groq's default is a
            # reasoning model. This is a small classification task; 600 is
            # generous margin over what the four possible intents need.
            max_tokens=600,
            **settings.GROQ_EXTRA_CHAT_KWARGS,
        )
        router_data = json.loads(router_response.choices[0].message.content)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Intent router failed: {exc}")

    intent = router_data.get("intent", "INVALID_INPUT")

    # ── Emit router result for traceability (visible in LangSmith + UI) ────────
    _router_event = json.dumps({
        "type":   "router_result",
        "intent": intent,
        "run_id": run_id,
    })

    # --- INVALID_INPUT ---
    if intent == "INVALID_INPUT":
        clarification = router_data.get("clarification_message", "Invalid input.")
        run_state.conversation.append({"role": "assistant", "message": clarification})

        async def event_stream_invalid():
            yield f"data: {_router_event}\n\n"
            yield f"data: {json.dumps({'type': 'chat_message', 'message': clarification, 'sender': 'system'})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

        return StreamingResponse(
            event_stream_invalid(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # --- ASK_QUESTION (new in M6) ---
    if intent == "ASK_QUESTION":
        answer = router_data.get("answer_prompt") or "I can see your question — let me check the shortlist data."
        run_state.conversation.append({"role": "assistant", "message": answer})

        async def event_stream_qa():
            yield f"data: {_router_event}\n\n"
            yield f"data: {json.dumps({'type': 'chat_message', 'message': answer, 'sender': 'assistant'})}\n\n"
            yield f"data: {json.dumps({'type': 'awaiting_selection', 'run_id': run_id})}\n\n"
            yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"

        return StreamingResponse(
            event_stream_qa(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # --- SELECT_PLAYER ---
    if intent == "SELECT_PLAYER":
        selected_names = router_data.get("selected_player_ids", [])
        selected_players = [p for p in run_state.ranked_players if p.get("name") in selected_names]
        
        if not selected_players:
            async def event_stream_fallback():
                msg = "I understood you wanted to select a player, but I couldn't match the name. Please try again."
                yield f"data: {json.dumps({'type': 'chat_message', 'message': msg, 'sender': 'system'})}\n\n"
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
            return StreamingResponse(event_stream_fallback(), media_type="text/event-stream")
            
        run_state.status = "finalizing"
        
        async def event_stream_finalize():
            queue: asyncio.Queue = asyncio.Queue()
            yield f"data: {_router_event}\n\n"
            yield f"data: {json.dumps({'type': 'finalizing_start', 'run_id': run_id, 'selected': selected_names})}\n\n"

            async def run_pipeline():
                try:
                    trace_url = ""

                    async def _finalize():
                        nonlocal trace_url
                        planner = PlannerAgent(client=client, sse_queue=queue, run_id=run_id)
                        trace_url = get_trace_url(run_id)
                        return await planner.run_finalize(
                            query=run_state.query,
                            ranked_players=selected_players,
                            financial_decision=run_state.financial_decision,
                            langsmith_trace_url=trace_url,
                            selection_funnel=run_state.selection_funnel,
                        )

                    result = await traceable_pipeline(
                        _finalize,
                        run_name=f"scoutly-finalize-{run_id[:8]}",
                        tags=["finalize", "report"],
                        metadata={"scoutly_run_id": run_id, "query": run_state.query},
                    )

                    run_state.pdf_path = result.get("pdf_path", "")
                    run_state.langsmith_trace_url = trace_url
                    run_state.status = "done"

                    await queue.put({
                        "type": "final_ready",
                        "run_id": run_id,
                        "pdf_filename": Path(run_state.pdf_path).name,
                        # The download panel prints this count; without it the
                        # UI has to fall back to a vague "3+".
                        "ranked_count": len(selected_players),
                        "langsmith_trace_url": trace_url,
                    })
                except Exception as exc:
                    run_state.status = "error"
                    await queue.put({
                        "type": "pipeline_error",
                        "run_id": run_id,
                        "error": str(exc),
                    })
                finally:
                    await queue.put(None)

            asyncio.create_task(run_pipeline())

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1200.0)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'timeout', 'message': 'Pipeline timed out'})}\n\n"
                    break
                if event is None:
                    yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                    break
                yield f"data: {json.dumps(event, default=str)}\n\n"

        return StreamingResponse(
            event_stream_finalize(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # --- MODIFY_SEARCH ---
    if run_state.revision_count >= 10:
        raise HTTPException(status_code=400, detail="Maximum revision limit reached.")

    run_state.status = "revising"
    run_state.directive_history.append(message)
    run_state.revision_count += 1

    async def event_stream_revise():
        queue: asyncio.Queue = asyncio.Queue()
        yield f"data: {_router_event}\n\n"
        yield f"data: {json.dumps({'type': 'revising_start', 'run_id': run_id, 'directive': message})}\n\n"

        async def run_pipeline():
            try:
                planner = PlannerAgent(client=client, sse_queue=queue, run_id=run_id)
                result = await planner.run_draft(query=run_state.query, history=run_state.directive_history)
                
                run_state.ranked_players = result.get("ranked_players", [])
                run_state.financial_decision = result.get("financial_decision", run_state.financial_decision)
                run_state.selection_funnel = result.get("selection_funnel", {})
                run_state.status = "awaiting_review"

                await queue.put({
                    "type": "revision_ready",
                    "run_id": run_id,
                    "ranked_players": run_state.ranked_players,
                    "financial_decision": run_state.financial_decision,
                    "selection_funnel": run_state.selection_funnel,
                })
                await queue.put({
                    "type": "awaiting_review",
                    "run_id": run_id,
                })
            except Exception as exc:
                run_state.status = "error"
                await queue.put({
                    "type": "pipeline_error",
                    "run_id": run_id,
                    "error": str(exc),
                })
            finally:
                await queue.put(None)

        asyncio.create_task(run_pipeline())

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1200.0)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'timeout', 'message': 'Pipeline timed out'})}\n\n"
                break
            if event is None:
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream_revise(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

@app.get("/api/scout/{run_id}")
async def get_scout_state(run_id: str):
    """Fetch current state of a scouting run."""
    if run_id not in ACTIVE_RUNS:
        raise HTTPException(status_code=404, detail="Run not found.")
    return ACTIVE_RUNS[run_id].dict()


@app.get("/api/download/{run_id}")
async def download_pdf(run_id: str):
    """
    Download the generated scouting report for a given run ID.

    WeasyPrint needs native GTK libraries that are not present on a stock Windows
    install, so the generator falls back to writing .html. Serve whichever the
    generator actually produced — checking only for .pdf 404s every Windows run.
    """
    # Sanitize run_id
    if not all(c in "0123456789abcdef-" for c in run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID.")

    for suffix, media_type in ((".pdf", "application/pdf"), (".html", "text/html")):
        report_path = settings.REPORTS_DIR / f"{run_id}{suffix}"
        if report_path.exists():
            return FileResponse(
                path=str(report_path),
                media_type=media_type,
                filename=f"scouting_report_{run_id[:8]}{suffix}",
            )

    raise HTTPException(
        status_code=404,
        detail="Report not found. The pipeline may still be running.",
    )


@app.get("/api/prompt/{agent_name}")
async def get_agent_prompt(agent_name: str):
    """Return the raw system prompt for a given agent (for debugging/inspection)."""
    try:
        content = PromptLoader.get(agent_name, use_cache=False)
        return {"agent": agent_name, "prompt": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No prompt found for agent: {agent_name}")
