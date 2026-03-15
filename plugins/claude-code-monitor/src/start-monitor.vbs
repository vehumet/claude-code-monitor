Set WshShell = CreateObject("WScript.Shell")
If WScript.Arguments.Count > 0 Then
    scriptPath = WScript.Arguments(0)
Else
    scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
    scriptPath = scriptDir & "claude-code-monitor.py"
End If
WshShell.Run "pythonw """ & scriptPath & """", 0, False
