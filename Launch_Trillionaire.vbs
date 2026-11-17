' ==============================================================================
' TRILLIONAIRE ALGO ENGINE - ZERO-TERMINAL SILENT LAUNCHER
' Launches backend & dashboard in the background and opens Google Chrome to Cockpit
' ==============================================================================

Option Explicit

Dim WshShell, fso, scriptDir, startScriptPath, psCmd
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve repository root directory
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
If scriptDir = "" Then
    scriptDir = "C:\Users\sures\Documents\Trillionaire"
End If
startScriptPath = scriptDir & "\scripts\start_local.ps1"

' 1. Execute PowerShell launcher silently (WindowStyle Hidden, return immediately)
psCmd = "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & startScriptPath & """"
WshShell.Run psCmd, 0, False

' 2. Poll http://localhost:3000 (up to 15 seconds) until Next.js cockpit is ready
Dim isReady, attempts, maxAttempts, http
isReady = False
attempts = 0
maxAttempts = 30  ' 30 iterations * 500ms = 15 seconds

Do While (attempts < maxAttempts) And (Not isReady)
    attempts = attempts + 1
    WScript.Sleep 500

    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    If http Is Nothing Then
        Set http = CreateObject("MSXML2.ServerXMLHTTP")
    End If
    If http Is Nothing Then
        Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    End If

    If Not http Is Nothing Then
        http.setTimeouts 500, 500, 500, 1000
        http.open "GET", "http://localhost:3000", False
        http.send
        If Err.Number = 0 Then
            If http.status >= 200 And http.status < 500 Then
                isReady = True
            End If
        End If
        Set http = Nothing
    End If
    Err.Clear
    On Error GoTo 0
Loop

' 3. Launch Google Chrome, falling back to default browser if Chrome is in a custom path
Dim chromePaths, chromePath, chromeFound, i, localAppData
localAppData = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
chromePaths = Array( _
    "C:\Program Files\Google\Chrome\Application\chrome.exe", _
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", _
    localAppData & "\Google\Chrome\Application\chrome.exe" _
)

chromeFound = False
For i = 0 To UBound(chromePaths)
    If fso.FileExists(chromePaths(i)) Then
        chromePath = chromePaths(i)
        chromeFound = True
        Exit For
    End If
Next

On Error Resume Next
If chromeFound Then
    WshShell.Run """" & chromePath & """ http://localhost:3000", 1, False
Else
    ' Try running chrome.exe directly from PATH
    WshShell.Run "chrome.exe http://localhost:3000", 1, False
    If Err.Number <> 0 Then
        Err.Clear
        ' Fallback to Windows default browser
        WshShell.Run "cmd /c start http://localhost:3000", 0, False
    End If
End If
On Error GoTo 0
