# ── Scoutly AI — Local Dev Runner (Windows, no Docker) ─────────────────────────
#
# Prerequisites: Python 3.11+ in PATH, .env filled in, vectorstore populated.
# Run once to populate vectorstore:  python -m backend.rag.loader
#
# Usage:  .\start_local.ps1
# Stop:   Ctrl+C in this window, then close the MCP window that opened.

$ErrorActionPreference = "Stop"

# ── 1. Check .env exists ──────────────────────────────────────────────────────
if (-not (Test-Path ".env")) {
    Write-Host "ERROR: .env not found. Copy .env.example and fill in your keys." -ForegroundColor Red
    exit 1
}

# ── 2. Check vectorstore is populated ────────────────────────────────────────
$vsPath = "data\vectorstore\clubs\index.faiss"
if (-not (Test-Path $vsPath)) {
    Write-Host "Vectorstore not found. Building from KB profiles..." -ForegroundColor Yellow
    python -m backend.rag.loader
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Vectorstore build failed." -ForegroundColor Red
        exit 1
    }
    Write-Host "Vectorstore built." -ForegroundColor Green
}

# ── 3. Start MCP server in a new window ───────────────────────────────────────
Write-Host "Starting MCP server on port 8005..." -ForegroundColor Cyan
$mcpJob = Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$PWD'; python -m backend.mcp_server.server"
) -PassThru

Start-Sleep -Seconds 2
Write-Host "MCP server PID: $($mcpJob.Id)" -ForegroundColor Green

# ── 4. Start backend ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Starting backend on http://localhost:8000 ..." -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

# Unset FBREF_USE_MOCK so local runs use real FBref via Chrome
$env:FBREF_USE_MOCK = "false"

python -m uvicorn backend.main:app `
    --host 0.0.0.0 --port 8000 `
    --reload --reload-dir prompts `
    --timeout-keep-alive 1200

# ── 5. Cleanup on exit ────────────────────────────────────────────────────────
if ($mcpJob -and -not $mcpJob.HasExited) {
    Write-Host "Stopping MCP server..." -ForegroundColor Yellow
    Stop-Process -Id $mcpJob.Id -Force
}
