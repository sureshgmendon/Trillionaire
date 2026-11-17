' ==============================================================================
' TRILLIONAIRE ALGO ENGINE - ZERO-TERMINAL SILENT STOP
' Gracefully terminates background FastAPI and Next.js processes
' ==============================================================================

Option Explicit

Dim WshShell, fso, scriptDir, stopScriptPath, psCmd
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve repository root directory
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
If scriptDir = "" Then
    scriptDir = "C:\Users\sures\Documents\Trillionaire"
End If
stopScriptPath = scriptDir & "\scripts\stop_local.ps1"

psCmd = "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & stopScriptPath & """"
WshShell.Run psCmd, 0, True
