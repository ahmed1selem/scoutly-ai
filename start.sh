#!/bin/bash
# Single-container startup — runs MCP server (port 8001) + FastAPI backend (port 8000).
# Both processes share the same container so they communicate via localhost.
# FBref data comes from data/fbref_cache.db (no Chrome needed).

set -euo pipefail

cleanup() {
    echo "[start.sh] Shutting down..."
    kill "${MCP_PID:-}" "${BACKEND_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[start.sh] Starting FastMCP server on :8001..."
FBREF_USE_MOCK=false \
  python backend/mcp_server/server.py &
MCP_PID=$!

# Wait for MCP to actually bind, not just for a fixed delay to pass. A blind
# `sleep 5` let the container report "both processes running" and come up
# healthy with a DEAD MCP server inside it: mcp 2.x renamed FastMCP with no
# compatible import path, the process exited on import within milliseconds,
# and every real scouting request came back with an empty cohort — silently,
# because /api/health checks Groq and SQLite directly and never touches the
# MCP server at all. Uses bash's /dev/tcp rather than curl, which this image
# does not install.
mcp_up=0
for _ in $(seq 1 15); do
    if ! kill -0 "${MCP_PID}" 2>/dev/null; then
        echo "[start.sh] FATAL: MCP server exited during startup — see the traceback above." >&2
        exit 1
    fi
    if (exec 3<>/dev/tcp/127.0.0.1/8001) 2>/dev/null; then
        exec 3<&- 3>&-
        mcp_up=1
        break
    fi
    sleep 1
done
if [ "${mcp_up}" != "1" ]; then
    echo "[start.sh] FATAL: MCP server did not bind :8001 within 15s." >&2
    exit 1
fi
echo "[start.sh] MCP server is up."

echo "[start.sh] Starting FastAPI backend on :8000..."
python -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --timeout-keep-alive 1200 &
BACKEND_PID=$!

echo "[start.sh] Both processes running — MCP PID=${MCP_PID}, BACKEND PID=${BACKEND_PID}"

# wait -n returns as soon as EITHER process exits (bash >= 4.3), so a
# mid-run MCP crash takes the whole container down — and Fly restarts it —
# instead of silently leaving uvicorn serving alone against a dead MCP
# server, which is exactly what happened before this script would notice.
wait -n "${MCP_PID}" "${BACKEND_PID}"
exit_code=$?
echo "[start.sh] A process exited (code ${exit_code}) — shutting down."
exit "${exit_code}"
