# ── Scoutly AI — Demo Runner ──────────────────────────────────────────────────
#
# start_local.ps1 gets the app running. This gets it running *in front of an
# audience*, which is a different problem: everything that can fail should fail
# now, on your terms, rather than thirty seconds into the presentation.
#
# It refuses to start unless the demo can actually complete — the check that
# matters most is the daily token budget, because the pipeline dies mid-run when
# it runs out and the only visible symptom is an empty shortlist.
#
# Usage:  .\demo.ps1              full preflight, then serve
#         .\demo.ps1 -SkipChecks  straight to serving (only if you just ran it)
#         .\demo.ps1 -NoBrowser   do not open a browser window

param(
    [switch]$SkipChecks,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$script:mcpProc = $null

function Say($msg, $colour = "Gray")  { Write-Host $msg -ForegroundColor $colour }
function Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
}

function Stop-Demo {
    if ($script:mcpProc -and -not $script:mcpProc.HasExited) {
        Say ""
        Say "Stopping MCP server (PID $($script:mcpProc.Id))..." "Yellow"
        Stop-Process -Id $script:mcpProc.Id -Force -ErrorAction SilentlyContinue
    }
}

Say ""
Say "==================================================================" "Cyan"
Say "  Scoutly AI - demo preflight" "Cyan"
Say "==================================================================" "Cyan"
Say ""

if (-not $SkipChecks) {

    # Config, data and token budget all live in demo_preflight.py, which exits
    # non-zero when the demo cannot proceed. Keeping the checks in Python rather
    # than inline here is deliberate: PowerShell strips double quotes when
    # passing an argument to a native executable, so embedded Python loses its
    # string literals in ways that only surface at run time.
    python demo_preflight.py
    if ($LASTEXITCODE -ne 0) {
        Say ""
        Say "Preflight failed - fix the above before demoing." "Red"
        exit 1
    }

    # ── Ports ─────────────────────────────────────────────────────────────────
    # A server left running from an earlier attempt binds the port and the new
    # one dies on startup, which is a confusing thing to debug on stage.
    Say ""
    Say "Ports" "White"
    foreach ($port in 8000, 8005) {
        $held = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($held) {
            $procId = $held[0].OwningProcess
            Warn "port $port already held by PID $procId - stopping it"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    }
    Ok "8000 and 8005 free"
}

# ── 5. Start the MCP server ───────────────────────────────────────────────────
Say ""
Say "Starting services" "White"
$env:FBREF_USE_MOCK    = "false"
$env:FBREF_LIVE_SCRAPE = "false"   # no Chrome, no FBref traffic per request

$script:mcpProc = Start-Process powershell -PassThru -WindowStyle Minimized -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$PWD'; `$env:FBREF_LIVE_SCRAPE='false'; python -m backend.mcp_server.server"
)

$ready = $false
foreach ($attempt in 1..30) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:8005/sse" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # SSE holds the connection open, so a timeout here still means it bound
        if ($_.Exception.Message -match "timed out|timeout") { $ready = $true; break }
    }
}
if (-not $ready) {
    Fail "MCP server did not come up on 8005"
    Stop-Demo
    exit 1
}
Ok "MCP server on 8005 (PID $($script:mcpProc.Id))"

# ── 6. Brief the presenter ────────────────────────────────────────────────────
Say ""
Say "==================================================================" "Cyan"
Say "  Briefs that are known to produce a full shortlist" "Cyan"
Say "==================================================================" "Cyan"
Say ""
Say "  1. Find a technical central midfielder for Fiorentina who is secure" "White"
Say "     in possession and progresses the ball, fee up to 20m and 90k a week" "White"
Say "     -> returns Modric and Vitinha, both of whom really did move in" "DarkGray"
Say "        summer 2025 and posted 7.5+ seasons afterwards." "DarkGray"
Say ""
Say "  2. Find a goalkeeper for Barcelona who is a reliable shot-stopper and" "White"
Say "     comfortable sweeping behind a high line, fee up to 30m and 150k a week" "White"
Say "     -> Brice Samba top of a 151-keeper cohort." "DarkGray"
Say ""
Say "  3. Find a hard-working central midfielder for Rennes who can control" "White"
Say "     tempo and break up play, fee under 10m and up to 80k per week" "White"
Say "     -> ranks all 868 Big-5 midfielders; good one for showing the funnel." "DarkGray"
Say ""
Say "  Then, at the review step, try asking a question rather than picking:" "White"
Say "     why is the first one ranked above the second?" "White"
Say "  It routes to ASK_QUESTION and answers without re-running the pipeline." "DarkGray"
Say ""
Say "  A run takes 90-120 seconds. The stream is the demo - let it play." "Yellow"
Say ""
Say "==================================================================" "Cyan"
Say ""

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        Start-Sleep -Seconds 6
        Start-Process "http://localhost:8000"
    } | Out-Null
}

# ── 7. Serve ──────────────────────────────────────────────────────────────────
# No --reload: in-flight runs live in an in-memory dict, and a reload drops
# them, so a debate in progress would 404 on the next directive.
try {
    python -m uvicorn backend.main:app `
        --host 0.0.0.0 --port 8000 `
        --timeout-keep-alive 1200
} finally {
    Stop-Demo
}
