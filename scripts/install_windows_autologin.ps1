<#
.SYNOPSIS
    Installs the Windows Scheduled Task for Trillionaire Daily Zerodha Kite Auto-Login.

.DESCRIPTION
    Registers a Windows Scheduled Task named "TrillionaireKiteAutoLogin" that executes
    scripts\daily_kite_autologin.py every weekday (Monday through Friday) at 08:45 AM IST.
    This logs in from your local residential broadband IP and pushes the fresh access_token
    to the AWS EC2 Trillionaire Engine before pre-market opens at 09:00 AM.

.PARAMETER Action
    Register (default), Unregister, RunNow, or Status.

.EXAMPLE
    .\scripts\install_windows_autologin.ps1
    .\scripts\install_windows_autologin.ps1 -Action RunNow
    .\scripts\install_windows_autologin.ps1 -Action Status
    .\scripts\install_windows_autologin.ps1 -Action Unregister
#>

[CmdletBinding()]
param (
    [ValidateSet("Register", "Unregister", "RunNow", "Status")]
    [string]$Action = "Register"
)

$TaskName = "TrillionaireKiteAutoLogin"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$ScriptPath = Join-Path $RepoRoot "scripts\daily_kite_autologin.py"
$LogPath = Join-Path $RepoRoot "logs\autologin.log"

# Ensure logs directory exists
$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Determine Python Executable
$PythonExe = $null
if (Test-Path (Join-Path $RepoRoot "venv\Scripts\python.exe")) {
    $PythonExe = (Join-Path $RepoRoot "venv\Scripts\python.exe")
} elseif (Test-Path (Join-Path $RepoRoot ".venv\Scripts\python.exe")) {
    $PythonExe = (Join-Path $RepoRoot ".venv\Scripts\python.exe")
} else {
    $PythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCmd) {
        $PythonExe = $PythonCmd.Source
    }
}

if (-not $PythonExe) {
    Write-Error "Python executable not found in PATH or venv! Please ensure Python is installed."
    exit 1
}

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "   TRILLIONAIRE ZERO-TOUCH WINDOWS AUTOLOGIN SCHEDULER" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "Action           : $Action"
Write-Host "Repository Root  : $RepoRoot"
Write-Host "Python Path      : $PythonExe"
Write-Host "Target Script    : $ScriptPath"
Write-Host "Log Output       : $LogPath"
Write-Host ""

if ($Action -eq "Unregister") {
    Write-Host "Unregistering Scheduled Task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed successfully." -ForegroundColor Green
    exit 0
}

if ($Action -eq "Status") {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Task) {
        Write-Host "Task '$TaskName' is currently registered." -ForegroundColor Green
        Write-Host "State: $($Task.State)"
        Get-ScheduledTaskInfo -TaskName $TaskName | Format-List
    } else {
        Write-Host "Task '$TaskName' is NOT registered." -ForegroundColor Yellow
    }
    exit 0
}

if ($Action -eq "RunNow") {
    Write-Host "Executing '$ScriptPath' manually now for verification..." -ForegroundColor Cyan
    & $PythonExe $ScriptPath
    exit $LASTEXITCODE
}

# Default: Register Scheduled Task
Write-Host "Configuring Scheduled Task '$TaskName'..." -ForegroundColor Yellow

# Unregister existing if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Trigger: Monday through Friday at 08:45 AM
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 08:45AM

# Action: python.exe scripts\daily_kite_autologin.py
$Argument = "`"$ScriptPath`""
$TaskAction = New-ScheduledTaskAction -Execute $PythonExe -Argument $Argument -WorkingDirectory $RepoRoot

# Settings: wake to run, allow start on battery, run as soon as possible if missed
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -StartWhenAvailable

# Register task under current user
try {
    Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger $Trigger -Settings $Settings -Description "Trillionaire Algo Engine - Daily Automated Zerodha Kite TOTP Login & Remote Token Dispatcher" | Out-Null
    Write-Host "SUCCESS: Windows Scheduled Task '$TaskName' successfully registered!" -ForegroundColor Green
    Write-Host "Schedule: Every Mon-Fri at 08:45 AM IST" -ForegroundColor Green
    Write-Host "Next Step: You can test the task anytime by running:" -ForegroundColor Gray
    Write-Host "  .\scripts\install_windows_autologin.ps1 -Action RunNow" -ForegroundColor White
} catch {
    Write-Error "Failed to register scheduled task: $_"
    exit 1
}
