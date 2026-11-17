# ==============================================================================
# TRILLIONAIRE ALGO ENGINE - LOCAL LAUNCHER & BACKGROUND RUNNER (WINDOWS)
# Idempotently starts FastAPI Bridge (Port 8000) and Next.js Cockpit (Port 3000)
# ==============================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Trillionaire Algo Trading Engine - Local Runner"

Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "  ______ _____  _____ _      _      _____ ____  _   _          _____ _____  ______ " -ForegroundColor Cyan
Write-Host " |__   _|  __ \|_   _| |    | |    |_   _/ __ \| \ | |   /\   |_   _|  __ \|  ____|" -ForegroundColor Cyan
Write-Host "    | | | |__) | | | | |    | |      | || |  | |  \| |  /  \    | | | |__) | |__   " -ForegroundColor Cyan
Write-Host "    | | |  _  /  | | | |    | |      | || |  | | . ` | / /\ \   | | |  _  /|  __|  " -ForegroundColor Cyan
Write-Host "    | | | | \ \ _| |_| |____| |____ _| || |__| | |\  |/ ____ \ _| |_| | \ \| |____ " -ForegroundColor Cyan
Write-Host "    |_| |_|  \_\_____|______|______|_____\____/|_| \_/_/    \_\_____|_|  \_\______|" -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host " [NSE MIS Intraday Quantitative Trading Engine - Background Services]   " -ForegroundColor White
Write-Host "========================================================================" -ForegroundColor Cyan

$RepoRoot = Split-Path -Parent $PSScriptRoot

# -----------------------------------------------------------------------------
# 0. Helper: Port Listening Probe
# -----------------------------------------------------------------------------
function Test-PortListening {
    param([int]$Port)
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($conns) { return $true }
    } catch {}
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect("127.0.0.1", $Port, $null, $null)
        $wait = $iar.AsyncWaitHandle.WaitOne(300, $false)
        if ($wait -and $tcp.Connected) {
            $tcp.EndConnect($iar)
            $tcp.Close()
            return $true
        }
        $tcp.Close()
    } catch {}
    return $false
}

# -----------------------------------------------------------------------------
# 1. Environment Configuration Checks
# -----------------------------------------------------------------------------
Write-Host "`n[1/4] Checking Environment Configurations..." -ForegroundColor Yellow
$BackendEnv = Join-Path $RepoRoot ".env"
$BackendEnvExample = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $BackendEnv)) {
    if (Test-Path $BackendEnvExample) {
        Copy-Item $BackendEnvExample $BackendEnv
        Write-Host "  [OK] Created .env from .env.example" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] Neither .env nor .env.example found at repo root." -ForegroundColor Red
    }
} else {
    Write-Host "  [OK] Found backend .env configuration." -ForegroundColor Green
}

$FrontendEnv = Join-Path $RepoRoot "web_dashboard\.env.local"
$FrontendEnvExample = Join-Path $RepoRoot "web_dashboard\.env.local.example"
if (-not (Test-Path $FrontendEnv)) {
    if (Test-Path $FrontendEnvExample) {
        Copy-Item $FrontendEnvExample $FrontendEnv
        Write-Host "  [OK] Created web_dashboard/.env.local from .env.local.example" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] web_dashboard/.env.local.example not found." -ForegroundColor Yellow
    }
} else {
    Write-Host "  [OK] Found frontend web_dashboard/.env.local configuration." -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# 2. Check Stock Profiles Artifact
# -----------------------------------------------------------------------------
Write-Host "`n[2/4] Checking Universe Microstructure Profiles..." -ForegroundColor Yellow
$StockProfilesPath = Join-Path $RepoRoot "research_lab\artifacts\stock_profiles.json"
if (-not (Test-Path $StockProfilesPath)) {
    Write-Host "  [INFO] stock_profiles.json missing. Generating profiles via stock_profiler..." -ForegroundColor Yellow
    python -m research_lab.profiler.stock_profiler
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] Stock profiles generated successfully." -ForegroundColor Green
    } else {
        Write-Host "  [WARN] Failed to generate stock profiles. Please run manual calibration." -ForegroundColor Red
    }
} else {
    Write-Host "  [OK] Found research_lab/artifacts/stock_profiles.json (100 profiles)." -ForegroundColor Green
}

# Ensure logs directory exists
$LogsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}
$EngineLog = Join-Path $LogsDir "engine_local.log"
$DashboardLog = Join-Path $LogsDir "dashboard_local.log"

# -----------------------------------------------------------------------------
# 3. Launch FastAPI Engine Backend (Port 8000)
# -----------------------------------------------------------------------------
Write-Host "`n[3/4] Checking FastAPI Telemetry Bridge (Port 8000)..." -ForegroundColor Yellow
$backendActive = Test-PortListening -Port 8000

if ($backendActive) {
    Write-Host "  [OK] Port 8000 is already active (FastAPI engine listening). Skipping duplicate launch." -ForegroundColor Green
} else {
    Write-Host "  [INFO] Starting FastAPI server on port 8000 in background (logging to $EngineLog)..." -ForegroundColor Cyan
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c python -m uvicorn api_bridge.main:app --host 0.0.0.0 --port 8000 >> ""$EngineLog"" 2>&1" `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden
    
    # Wait briefly up to 5 seconds for backend port 8000 to listen
    $retries = 0
    while (-not (Test-PortListening -Port 8000) -and ($retries -lt 10)) {
        Start-Sleep -Milliseconds 500
        $retries++
    }
    if (Test-PortListening -Port 8000) {
        Write-Host "  [OK] FastAPI Server online and listening on port 8000." -ForegroundColor Green
    } else {
        Write-Host "  [WARN] FastAPI Server spawned. Check $EngineLog for status." -ForegroundColor Yellow
    }
}

# -----------------------------------------------------------------------------
# 4. Launch Next.js Cockpit Frontend (Port 3000)
# -----------------------------------------------------------------------------
Write-Host "`n[4/4] Checking Next.js Visual Trading Cockpit (Port 3000)..." -ForegroundColor Yellow
$frontendActive = Test-PortListening -Port 3000

if ($frontendActive) {
    Write-Host "  [OK] Port 3000 is already active (Next.js cockpit listening). Skipping duplicate launch." -ForegroundColor Green
} else {
    Write-Host "  [INFO] Starting Next.js cockpit dev server on port 3000 in background (logging to $DashboardLog)..." -ForegroundColor Cyan
    $DashboardDir = Join-Path $RepoRoot "web_dashboard"
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c cd web_dashboard && npm.cmd run dev >> ""$DashboardLog"" 2>&1" `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden

    # Wait briefly up to 10 seconds for frontend port 3000 to listen
    $retries = 0
    while (-not (Test-PortListening -Port 3000) -and ($retries -lt 20)) {
        Start-Sleep -Milliseconds 500
        $retries++
    }
    if (Test-PortListening -Port 3000) {
        Write-Host "  [OK] Next.js Dashboard dev server online on port 3000." -ForegroundColor Green
    } else {
        Write-Host "  [WARN] Next.js dev server spawned. Check $DashboardLog for status." -ForegroundColor Yellow
    }
}

# -----------------------------------------------------------------------------
# 5. Display Access Dashboard
# -----------------------------------------------------------------------------
Write-Host "`n========================================================================" -ForegroundColor Green
Write-Host "  TRILLIONAIRE SERVICES ONLINE" -ForegroundColor Green
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "  * Visual Cockpit Dashboard : http://localhost:3000" -ForegroundColor Cyan
Write-Host "  * FastAPI Swagger Docs     : http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "  * WebSocket Telemetry Feed : ws://localhost:8000/ws/stream" -ForegroundColor Cyan
Write-Host "  * Engine Log               : $EngineLog" -ForegroundColor DarkGray
Write-Host "  * Dashboard Log            : $DashboardLog" -ForegroundColor DarkGray
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "`nTo shut down all services cleanly, run:" -ForegroundColor DarkGray
Write-Host ".\scripts\stop_local.ps1`n" -ForegroundColor Yellow
