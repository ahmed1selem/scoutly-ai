# deploy.ps1 - One-shot Fly.io deployment script
# Run from the Scoutly/ directory:  .\deploy.ps1
#
# What it does:
#   1. Checks flyctl is installed
#   2. Opens browser for Fly.io login (one-time)
#   3. Creates the app (first time only)
#   4. Reads all secrets from .env and sets them on Fly.io
#   5. Deploys the container

# Prefer whatever is on PATH; fall back to the default installer location so
# this still works on a machine where flyctl was installed but never added.
$FLY = (Get-Command flyctl -ErrorAction SilentlyContinue).Source
if (-not $FLY) { $FLY = (Get-Command fly -ErrorAction SilentlyContinue).Source }
if (-not $FLY) { $FLY = Join-Path $env:USERPROFILE ".fly\bin\flyctl.exe" }
$ROOT = $PSScriptRoot
$APP  = "scoutly-ai"

Write-Host ""
Write-Host "=== Scoutly AI - Fly.io Deployment ===" -ForegroundColor Cyan
Write-Host ""

# -- 1. Check flyctl ------------------------------------------------------------
if (-not (Test-Path $FLY)) {
    Write-Host "ERROR: flyctl not found at $FLY" -ForegroundColor Red
    Write-Host "Install it: iwr https://fly.io/install.ps1 -useb | iex"
    exit 1
}

# -- 2. Login (opens browser) ---------------------------------------------------
Write-Host "Step 1/4 - Logging in to Fly.io (browser will open)..."
& $FLY auth login
if ($LASTEXITCODE -ne 0) { Write-Host "Login failed." -ForegroundColor Red; exit 1 }

# -- 3. Create app (first time) ------------------------------------------------
Write-Host ""
Write-Host "Step 2/4 - Creating app (skipped if already exists)..."
& $FLY apps create $APP --org personal 2>&1 | Out-Null
# (error is normal if app already exists - continue)

# -- 4. Set secrets from .env -------------------------------------------------
Write-Host ""
Write-Host "Step 3/4 - Setting secrets from .env..."

$envFile = Join-Path $ROOT ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "ERROR: .env not found at $envFile" -ForegroundColor Red
    exit 1
}

# Keys pushed to Fly.io as secrets.
#
# GROQ_* belongs at the top of this list because the app defaults to
# LLM_PROVIDER=groq. This list used to hold only the Azure keys, which meant a
# deploy that appeared to succeed and then failed inside the first agent with
# "no API key configured for provider 'groq'" - the health check reports it,
# but nothing else does until a run is attempted.
$wantedKeys = @(
    "GROQ_API_KEYS",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "GROQ_BASE_URL",
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

# Refuse to ship an app whose configured provider has no key. This failure is
# invisible at deploy time - the build succeeds, the machine starts, the page
# loads - and only appears when someone runs a brief and every agent dies.
$provider = ($secretArgs | Where-Object { $_ -like "LLM_PROVIDER=*" })
$hasGroq  = $secretArgs | Where-Object { $_ -like "GROQ_API_KEY*=*" }
$hasAzure = $secretArgs | Where-Object { $_ -like "AZURE_OPENAI_API_KEY=*" }
$configured = (Get-Content $envFile | Where-Object { $_ -match '^\s*LLM_PROVIDER\s*=' }) -replace '.*=\s*"?([^"]*)"?.*', '$1'
if (-not $configured) { $configured = "groq" }

if ($configured -eq "groq" -and -not $hasGroq) {
    Write-Host ""
    Write-Host "ERROR: LLM_PROVIDER=groq but no GROQ_API_KEYS found in .env." -ForegroundColor Red
    Write-Host "       Every agent would fail on the deployed app. Add the key," -ForegroundColor Yellow
    Write-Host "       or set LLM_PROVIDER=azure if that is what you meant." -ForegroundColor Yellow
    exit 1
}
if ($configured -eq "azure" -and -not $hasAzure) {
    Write-Host ""
    Write-Host "ERROR: LLM_PROVIDER=azure but no AZURE_OPENAI_API_KEY in .env." -ForegroundColor Red
    exit 1
}
Write-Host "  provider=$configured (key present)" -ForegroundColor Green

if ($secretArgs.Count -gt 0) {
    & $FLY secrets set @secretArgs --app $APP
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: Some secrets may not have been set." -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: No secrets found in .env" -ForegroundColor Yellow
}

# -- 5. Deploy -----------------------------------------------------------------
Write-Host ""
Write-Host "Step 4/4 - Building and deploying to Fly.io..."
Write-Host "(This takes 3-5 minutes on first deploy)"
& $FLY deploy --app $APP --remote-only

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "DEPLOYED SUCCESSFULLY" -ForegroundColor Green
    Write-Host ""
    & $FLY status --app $APP
    Write-Host ""
    Write-Host "Your app URL: https://scoutly-ai.fly.dev"
    Write-Host "Health check: https://scoutly-ai.fly.dev/api/health"
    Write-Host "Logs: flyctl logs --app $APP"
    Write-Host ""
    Write-Host "Get FLY_API_TOKEN for GitHub Actions auto-deploy:"
    Write-Host "  flyctl tokens create deploy -x 999999h --app $APP"
    Write-Host "Then add it as a GitHub secret: Settings > Secrets > FLY_API_TOKEN"
} else {
    Write-Host ""
    Write-Host "Deployment FAILED. Check logs:" -ForegroundColor Red
    Write-Host "  flyctl logs --app $APP"
}
