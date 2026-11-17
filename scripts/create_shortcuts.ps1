# ==============================================================================
# TRILLIONAIRE ALGO ENGINE - DESKTOP SHORTCUT CREATOR
# Creates one-click zero-terminal Windows desktop shortcuts
# ==============================================================================

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DesktopDir = [Environment]::GetFolderPath("Desktop")
if (-not $DesktopDir -or -not (Test-Path $DesktopDir)) {
    $DesktopDir = Join-Path $env:USERPROFILE "Desktop"
}

$WshShell = New-Object -ComObject WScript.Shell
$WscriptExe = Join-Path $env:SystemRoot "System32\wscript.exe"
$Shell32 = Join-Path $env:SystemRoot "System32\shell32.dll"

# 1. Trillionaire Launcher Shortcut
$LauncherShortcutPath = Join-Path $DesktopDir "Trillionaire.lnk"
$LauncherVbs = Join-Path $RepoRoot "Launch_Trillionaire.vbs"

$Shortcut = $WshShell.CreateShortcut($LauncherShortcutPath)
$Shortcut.TargetPath = $WscriptExe
$Shortcut.Arguments = """$LauncherVbs"""
$Shortcut.WorkingDirectory = $RepoRoot
$Shortcut.Description = "Launch Trillionaire Algorithmic Trading Cockpit"

if (Test-Path $Shell32) {
    $Shortcut.IconLocation = "$Shell32,238"
}
$Shortcut.Save()
Write-Host "[OK] Created desktop shortcut: $LauncherShortcutPath" -ForegroundColor Green

# 2. Stop Trillionaire Shortcut
$StopShortcutPath = Join-Path $DesktopDir "Stop Trillionaire.lnk"
$StopVbs = Join-Path $RepoRoot "Stop_Trillionaire.vbs"

$StopShortcut = $WshShell.CreateShortcut($StopShortcutPath)
$StopShortcut.TargetPath = $WscriptExe
$StopShortcut.Arguments = """$StopVbs"""
$StopShortcut.WorkingDirectory = $RepoRoot
$StopShortcut.Description = "Stop Trillionaire Algorithmic Trading Cockpit"

if (Test-Path $Shell32) {
    $StopShortcut.IconLocation = "$Shell32,131"
}
$StopShortcut.Save()
Write-Host "[OK] Created desktop shortcut: $StopShortcutPath" -ForegroundColor Green
