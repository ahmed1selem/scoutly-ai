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

# Wait for MCP to bind before starting the backend
sleep 5

echo "[start.sh] Starting FastAPI backend on :8000..."
python -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --timeout-keep-alive 1200 &
BACKEND_PID=$!

echo "[start.sh] Both processes running — MCP PID=${MCP_PID}, BACKEND PID=${BACKEND_PID}"

# Block until either process exits, then clean up both
wait "${MCP_PID}" "${BACKEND_PID}"
