# ── Scoutly AI — Podman Deployment ─────────────────────────────────────────────
#
# Builds and runs two rootless Podman containers:
#   football-mcp      Port 8001 — MCP server (FBref tools, SQLite)
#   football-backend  Port 8000 — FastAPI backend + agents + FAISS vectorstore
#
# Usage:  .\start_podman.ps1              # first run (builds images)
#         .\start_podman.ps1 -NoBuild     # skip rebuild, just restart
#         .\start_podman.ps1 -Stop        # stop and remove containers
# ───────────────────────────────────────────────────────────────────────────────
param(
    [switch]$NoBuild,
    [switch]$Stop
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Find podman executable ────────────────────────────────────────────────────
$PODMAN = Get-Command podman -ErrorAction SilentlyContinue
if (-not $PODMAN) {
    $candidates = @(
        "$env:ProgramFiles\RedHat\Podman\podman.exe",
        "$env:LOCALAPPDATA\Programs\RedHat\Podman\podman.exe"
    )
    $PODMAN = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $PODMAN) { Write-Err "podman not found. Install via: winget install -e --id RedHat.Podman" }
} else {
    $PODMAN = $PODMAN.Source
}
# Alias so the rest of the script can call `podman` as a function
function podman { & $PODMAN @args }

$NETWORK   = "football-net"
$MCP_IMG   = "scoutly-mcp"
$BACK_IMG  = "scoutly-backend"
$MCP_CTR   = "football-mcp"
$BACK_CTR  = "football-backend"

# ── Helpers ───────────────────────────────────────────────────────────────────
function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg"   -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "   ERROR: $msg" -ForegroundColor Red; exit 1 }

function Remove-Container($name) {
    $exists = podman ps -a --format "{{.Names}}" 2>$null | Select-String -SimpleMatch $name
    if ($exists) {
        Write-Warn "Removing existing container: $name"
        podman rm -f $name 2>$null | Out-Null
    }
}

# ── Stop mode ─────────────────────────────────────────────────────────────────
if ($Stop) {
    Write-Step "Stopping Scoutly containers"
    podman stop $MCP_CTR  2>$null; podman rm $MCP_CTR  2>$null
    podman stop $BACK_CTR 2>$null; podman rm $BACK_CTR 2>$null
    Write-OK "Containers stopped."
    exit 0
}

# ── Prerequisite checks ───────────────────────────────────────────────────────
Write-Step "Checking prerequisites"

Write-OK "Podman found: $(podman --version)"

# On Windows, Podman needs a Linux VM (podman machine). Initialize + start it if needed.
$machineList = podman machine list --format "{{.Name}} {{.Running}}" 2>$null
if (-not ($machineList -match "podman-machine-default")) {
    Write-Host "   Initializing Podman machine (first-time setup, ~2 min)..." -ForegroundColor Yellow
    podman machine init 2>&1 | Out-Host
}
if (-not ($machineList -match "true")) {
    Write-Host "   Starting Podman machine..." -ForegroundColor Yellow
    podman machine start 2>&1 | Out-Host
    Write-OK "Podman machine started"
} else {
    Write-OK "Podman machine already running"
}

if (-not (Test-Path ".env")) {
    Write-Err ".env not found. Copy .env.example and fill in your Azure OpenAI / Tavily keys."
}
Write-OK ".env present"

# Check vectorstore is pre-built (it's mounted as a volume — must exist locally)
$vsIndex = "data\vectorstore\clubs\index.faiss"
if (-not (Test-Path $vsIndex)) {
    Write-Warn "Vectorstore not found at $vsIndex"
    Write-Host "   Building vectorstore from KB profiles (run once)..." -ForegroundColor Yellow
    python -m backend.rag.loader
    if ($LASTEXITCODE -ne 0) { Write-Err "Vectorstore build failed. Check KB/profiles_enriched.json exists." }
    Write-OK "Vectorstore built"
} else {
    Write-OK "Vectorstore found"
}

# Ensure runtime dirs exist (mounted as volumes)
foreach ($d in @("charts", "reports", "data")) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}

# ── Network ───────────────────────────────────────────────────────────────────
Write-Step "Creating Podman network '$NETWORK'"
podman network exists $NETWORK 2>$null
if ($LASTEXITCODE -ne 0) {
    podman network create $NETWORK --driver bridge | Out-Null
    Write-OK "Network created"
} else {
    Write-OK "Network already exists"
}

# ── Build images ──────────────────────────────────────────────────────────────
if (-not $NoBuild) {
    Write-Step "Building MCP image ($MCP_IMG)"
    podman build -t $MCP_IMG -f Containerfile.mcp .
    if ($LASTEXITCODE -ne 0) { Write-Err "MCP image build failed" }
    Write-OK "MCP image built"

    Write-Step "Building backend image ($BACK_IMG)"
    podman build -t $BACK_IMG -f Containerfile.backend .
    if ($LASTEXITCODE -ne 0) { Write-Err "Backend image build failed" }
    Write-OK "Backend image built"
} else {
    Write-Warn "-NoBuild set — skipping image build"
}

# ── Remove old containers ─────────────────────────────────────────────────────
Write-Step "Removing old containers (if any)"
Remove-Container $MCP_CTR
Remove-Container $BACK_CTR

# ── Start MCP server ─────────────────────────────────────────────────────────
Write-Step "Starting MCP server (port 8001)"
podman run -d `
    --name $MCP_CTR `
    --network $NETWORK `
    -p 8001:8001 `
    --dns 8.8.8.8 `
    -v "${PWD}\data:/app/data" `
    --env-file .env `
    -e FBREF_USE_MOCK=true `
    $MCP_IMG

if ($LASTEXITCODE -ne 0) { Write-Err "Failed to start MCP container" }
Write-OK "MCP container started"

# Wait for MCP to be ready
Write-Host "   Waiting for MCP to initialize..." -NoNewline
$retries = 0
while ($retries -lt 15) {
    Start-Sleep -Seconds 2
    $logs = podman logs $MCP_CTR 2>&1
    if ($logs -match "Application startup complete") { break }
    Write-Host "." -NoNewline
    $retries++
}
Write-Host ""
if ($retries -eq 15) { Write-Warn "MCP did not confirm startup — continuing anyway" }
else { Write-OK "MCP ready" }

# ── Start backend ─────────────────────────────────────────────────────────────
Write-Step "Starting backend (port 8000)"
podman run -d `
    --name $BACK_CTR `
    --network $NETWORK `
    -p 8000:8000 `
    --dns 8.8.8.8 `
    -v "${PWD}\data:/app/data" `
    -v "${PWD}\prompts:/app/prompts" `
    -v "${PWD}\charts:/app/charts" `
    -v "${PWD}\reports:/app/reports" `
    -e MCP_SERVER_HOST=$MCP_CTR `
    -e MCP_SERVER_PORT=8001 `
    --env-file .env `
    $BACK_IMG

if ($LASTEXITCODE -ne 0) { Write-Err "Failed to start backend container" }
Write-OK "Backend container started"

# Wait for backend to be ready
Write-Host "   Waiting for backend to initialize..." -NoNewline
$retries = 0
while ($retries -lt 20) {
    Start-Sleep -Seconds 2
    try {
        $resp = Invoke-WebRequest "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { break }
    } catch {}
    Write-Host "." -NoNewline
    $retries++
}
Write-Host ""
if ($retries -eq 20) { Write-Warn "Backend health check timed out — check logs" }
else { Write-OK "Backend healthy" }

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host " Scoutly AI is running!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host " Frontend : http://localhost:8000"
Write-Host " API docs : http://localhost:8000/docs"
Write-Host " MCP      : http://localhost:8001"
Write-Host ""
Write-Host " Logs:"
Write-Host "   podman logs -f $BACK_CTR"
Write-Host "   podman logs -f $MCP_CTR"
Write-Host ""
Write-Host " Stop:"
Write-Host "   .\start_podman.ps1 -Stop"
Write-Host "==========================================" -ForegroundColor Green
