# ==============================================================================
# TRILLIONAIRE ALGO ENGINE - LOCAL SERVICES SHUTDOWN (WINDOWS)
# Gracefully terminates FastAPI Bridge (Port 8000) and Next.js Cockpit (Port 3000)
# ==============================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Trillionaire Algo Trading Engine - Stop Services"

Write-Host "========================================================================" -ForegroundColor Yellow
Write-Host " [SHUTTING DOWN TRILLIONAIRE LOCAL SERVICES]                           " -ForegroundColor White
Write-Host "========================================================================" -ForegroundColor Yellow

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Stop-PortListener {
    param(
        [int]$Port,
        [string]$ServiceName
    )
    Write-Host "`nChecking for $ServiceName listening on port $Port..." -ForegroundColor Cyan
    $stopped = $false

    # 1. Probe via Get-NetTCPConnection
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($conn in $conns) {
            $procId = $conn.OwningProcess
            if ($procId -and $procId -gt 0) {
                $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                $procName = if ($proc) { $proc.ProcessName } else { "PID $procId" }
                Write-Host "  Stopping $ServiceName ($procName, PID: $procId)..." -ForegroundColor Yellow
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        }
    } catch {}

    # 2. Fallback via netstat in case Get-NetTCPConnection is restricted
    try {
        $netstatLines = netstat -ano | Select-String ":$Port\s+.*LISTENING\s+(\d+)"
        foreach ($line in $netstatLines) {
            if ($line -match ":$Port\s+.*LISTENING\s+(\d+)") {
                $procId = [int]$matches[1]
                if ($procId -gt 0) {
                    Write-Host "  Stopping $ServiceName (PID: $procId via netstat)..." -ForegroundColor Yellow
                    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                    $stopped = $true
                }
            }
        }
    } catch {}

    if ($stopped) {
        Write-Host "  [OK] $ServiceName on port $Port successfully stopped." -ForegroundColor Green
    } else {
        Write-Host "  [INFO] No active listener found on port $Port." -ForegroundColor DarkGray
    }
}

Stop-PortListener -Port 8000 -ServiceName "FastAPI Telemetry Bridge"
Stop-PortListener -Port 3000 -ServiceName "Next.js Cockpit Dashboard"

Write-Host "`n========================================================================" -ForegroundColor Green
Write-Host "  ALL TRILLIONAIRE SERVICES HAVE BEEN SHUT DOWN" -ForegroundColor Green
Write-Host "========================================================================" -ForegroundColor Green
