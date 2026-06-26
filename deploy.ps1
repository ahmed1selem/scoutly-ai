# deploy.ps1 — One-shot Fly.io deployment script
# Run from the Scoutly/ directory:  .\deploy.ps1
#
# What it does:
#   1. Checks flyctl is installed
#   2. Opens browser for Fly.io login (one-time)
#   3. Creates the app (first time only)
#   4. Reads all secrets from .env and sets them on Fly.io
#   5. Deploys the container

$FLY = "C:\Users\ahmed\.fly\bin\flyctl.exe"
$ROOT = $PSScriptRoot

Write-Host ""
Write-Host "=== Scoutly AI — Fly.io Deployment ===" -ForegroundColor Cyan
Write-Host ""

# ── 1. Check flyctl ────────────────────────────────────────────────────────────
if (-not (Test-Path $FLY)) {
    Write-Host "ERROR: flyctl not found at $FLY" -ForegroundColor Red
    Write-Host "Install it: iwr https://fly.io/install.ps1 -useb | iex"
    exit 1
}

# ── 2. Login (opens browser) ───────────────────────────────────────────────────
Write-Host "Step 1/4 — Logging in to Fly.io (browser will open)..."
& $FLY auth login
if ($LASTEXITCODE -ne 0) { Write-Host "Login failed." -ForegroundColor Red; exit 1 }

# ── 3. Create app (first time) ────────────────────────────────────────────────
Write-Host ""
Write-Host "Step 2/4 — Creating app (skipped if already exists)..."
& $FLY apps create scoutly-ai --org personal 2>&1 | Out-Null
# (error is normal if app already exists — continue)

# ── 4. Set secrets from .env ─────────────────────────────────────────────────
Write-Host ""
Write-Host "Step 3/4 — Setting secrets from .env..."

$envFile = Join-Path $ROOT ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "ERROR: .env not found at $envFile" -ForegroundColor Red
    exit 1
}

# Keys we want to push to Fly.io as secrets
$wantedKeys = @(
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "AZURE_OPENAI_API_VERSION",
    "TAVILY_API_KEY",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_ENDPOINT"
)

$secretArgs = @()
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*#' -or $line.Trim() -eq '') { continue }
    if ($line -match '^([^=]+)="?([^"]*)"?$') {
        $key   = $Matches[1].Trim()
        $value = $Matches[2].Trim()
        if ($key -in $wantedKeys -and $value -ne '') {
            $secretArgs += "$key=$value"
            Write-Host "  + $key" -ForegroundColor Green
        }
    }
}

if ($secretArgs.Count -gt 0) {
    & $FLY secrets set @secretArgs --app scoutly-ai
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: Some secrets may not have been set." -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: No secrets found in .env" -ForegroundColor Yellow
}

# ── 5. Deploy ─────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Step 4/4 — Building and deploying to Fly.io..."
Write-Host "(This takes 3-5 minutes on first deploy)"
& $FLY deploy --app scoutly-ai --remote-only

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "DEPLOYED SUCCESSFULLY" -ForegroundColor Green
    Write-Host ""
    & $FLY status --app scoutly-ai
    Write-Host ""
    Write-Host "Your app URL: https://scoutly-ai.fly.dev"
    Write-Host "Health check: https://scoutly-ai.fly.dev/api/health"
    Write-Host "Logs: flyctl logs --app scoutly-ai"
    Write-Host ""
    Write-Host "Get FLY_API_TOKEN for GitHub Actions auto-deploy:"
    Write-Host "  flyctl tokens create deploy -x 999999h --app scoutly-ai"
    Write-Host "Then add it as a GitHub secret: Settings > Secrets > FLY_API_TOKEN"
} else {
    Write-Host ""
    Write-Host "Deployment FAILED. Check logs:" -ForegroundColor Red
    Write-Host "  flyctl logs --app scoutly-ai"
}
