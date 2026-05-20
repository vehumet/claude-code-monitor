<!-- session-monitor:managed-command -->

Launch the session monitor overlay.

Run this single command immediately without any pre-checks or status messages:

```
MR="$(python -c "import os; print(os.path.join(os.path.expanduser('~'), '.local', 'share', 'session-monitor').replace(chr(92), '/'))" 2>/dev/null)"
if [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
  powershell.exe -NoProfile -Command "\$needle = '[\\\\/]session-monitor\\.py(\"|\\s|$)'; Get-CimInstance Win32_Process | Where-Object { \$_.ProcessId -ne \$PID -and \$_.Name -match '^pythonw?\.exe$' -and \$_.CommandLine -match \$needle } | Select-Object -First 1 -ExpandProperty ProcessId" 2>/dev/null | grep -q '[0-9]' && echo "Session monitor already running." || { python "$MR/start-session-monitor.py" && echo "Session monitor launched."; }
else
  pgrep -f '[/\\][s]ession-monitor.py([[:space:]]|$)' > /dev/null && echo "Session monitor already running." || { python "$MR/start-session-monitor.py" && echo "Session monitor launched."; }
fi
```

Do NOT check if it's running beforehand with a separate command. Do NOT confirm or explain. Just run the one command above and output the result.
