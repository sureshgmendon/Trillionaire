# ==============================================================================
# TRILLIONAIRE ALGO ENGINE - DEVELOPER ENVIRONMENT INITIALIZATION SCRIPT
# Automated bootstrap for local development and algorithmic testing.
# ==============================================================================

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [TRILLIONAIRE] Initializing Quant Trading Monorepo...    " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Check Python installation
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "[ERROR] Python is not installed or not in PATH."
    Exit 1
}

$pyVersion = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
Write-Host "[OK] Detected Python version: $pyVersion" -ForegroundColor Green

# 2. Setup Virtual Environment
$venvPath = Join-Path $PSScriptRoot "..\.venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "[INFO] Creating virtual environment at .venv..." -ForegroundColor Yellow
    python -m venv $venvPath
    Write-Host "[OK] Virtual environment created successfully." -ForegroundColor Green
} else {
    Write-Host "[OK] Virtual environment already exists at .venv" -ForegroundColor Green
}

# 3. Setup .env file
$envFile = Join-Path $PSScriptRoot "..\.env"
$envExample = Join-Path $PSScriptRoot "..\.env.example"
if (-not (Test-Path $envFile)) {
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Host "[OK] Created .env from .env.example" -ForegroundColor Green
    } else {
        Write-Warning "[WARN] .env.example not found!"
    }
} else {
    Write-Host "[OK] .env configuration file exists." -ForegroundColor Green
}

# 4. Install Dependencies
$requirementsFile = Join-Path $PSScriptRoot "..\requirements.txt"
if (Test-Path $requirementsFile) {
    Write-Host "[INFO] Installing/verifying dependencies from requirements.txt..." -ForegroundColor Yellow
    pip install -r $requirementsFile --quiet
    Write-Host "[OK] Dependencies successfully verified." -ForegroundColor Green
}

# 5. Run Validation Test Suite
Write-Host "[INFO] Executing verification tests..." -ForegroundColor Yellow
python -m pytest tests/unit -q
if ($LASTEXITCODE -eq 0) {
    Write-Host "==========================================================" -ForegroundColor Green
    Write-Host " [SUCCESS] Trillionaire Engine initialized & validated!   " -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Green
} else {
    Write-Warning "[WARN] Tests encountered issues. Review test output above."
}
