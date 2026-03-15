Set WshShell = CreateObject("WScript.Shell")
If WScript.Arguments.Count > 0 Then
    scriptPath = WScript.Arguments(0)
Else
    scriptPath = WshShell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.claude\monitor\claude-code-monitor.py"
End If
WshShell.Run "pythonw """ & scriptPath & """", 0, False
