Launch the Claude Monitor overlay.

Run this single command immediately without any pre-checks or status messages:

```
MR="$(python -c "import os; print(os.path.join(os.path.expanduser('~'), '.claude', 'monitor').replace(chr(92), '/'))" 2>/dev/null)"
if [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
  powershell.exe -NoProfile -Command "\$needle = 'claude-code-monitor' + '\.py'; Get-CimInstance Win32_Process | Where-Object { \$_.ProcessId -ne \$PID -and \$_.CommandLine -match \$needle } | Select-Object -First 1 -ExpandProperty ProcessId" 2>/dev/null | grep -q '[0-9]' && echo "Monitor already running." || { python "$MR/start-monitor.py" && echo "Monitor launched."; }
else
  pgrep -f '[c]laude-code-monitor.py' > /dev/null && echo "Monitor already running." || { python "$MR/start-monitor.py" && echo "Monitor launched."; }
fi
```

Do NOT check if it's running beforehand with a separate command. Do NOT confirm or explain. Just run the one command above and output the result.
