# ==============================================================================
# TRILLIONAIRE ALGO ENGINE - LOCAL LAUNCHER & REPLAY RUNNER (WINDOWS)
# Launches FastAPI Bridge (Port 8000) and Next.js Cockpit (Port 3000)
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
Write-Host " [NSE MIS Intraday Quantitative Trading Engine - Local Architecture]    " -ForegroundColor White
Write-Host "========================================================================" -ForegroundColor Cyan

$RepoRoot = Split-Path -Parent $PSScriptRoot

# 1. Environment Configuration Checks
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

# 2. Check Stock Profiles Artifact
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
    Write-Host "  [OK] Found research_lab/artifacts/stock_profiles.json." -ForegroundColor Green
}

# 3. Launch FastAPI Engine Backend
Write-Host "`n[3/4] Launching FastAPI REST & Telemetry Bridge (Port 8000)..." -ForegroundColor Yellow
$backendProcess = Start-Process -FilePath "python" `
    -ArgumentList "-m uvicorn api_bridge.main:app --host 0.0.0.0 --port 8000 --reload" `
    -WorkingDirectory $RepoRoot `
    -PassThru

Write-Host "  [OK] FastAPI Server launched (PID: $($backendProcess.Id))." -ForegroundColor Green

# 4. Launch Next.js Cockpit Frontend
Write-Host "`n[4/4] Launching Next.js Visual Trading Cockpit (Port 3000)..." -ForegroundColor Yellow
$dashboardDir = Join-Path $RepoRoot "web_dashboard"
$frontendProcess = Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/c cd web_dashboard && npm.cmd run dev" `
    -WorkingDirectory $RepoRoot `
    -PassThru

Write-Host "  [OK] Next.js Dashboard dev server launched (PID: $($frontendProcess.Id))." -ForegroundColor Green

# 5. Display Access Dashboard
Write-Host "`n========================================================================" -ForegroundColor Green
Write-Host "  TRILLIONAIRE SERVICES ONLINE" -ForegroundColor Green
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "  * Visual Cockpit Dashboard : http://localhost:3000" -ForegroundColor Cyan
Write-Host "  * FastAPI Swagger Docs     : http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "  * WebSocket Telemetry Feed : ws://localhost:8000/ws/stream" -ForegroundColor Cyan
Write-Host "  * Replay & Simulation API  : http://localhost:8000/api/v1/control/replay/start" -ForegroundColor Cyan
Write-Host "  * Telegram Emergency Stop : Send /kill in your registered Telegram bot" -ForegroundColor Yellow
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "`nTo shut down all services, close the spawned terminal windows or run:" -ForegroundColor DarkGray
Write-Host "Stop-Process -Id $($backendProcess.Id), $($frontendProcess.Id) -Force`n" -ForegroundColor DarkGray
