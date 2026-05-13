Launch the Claude Monitor overlay.

Run this single command immediately without any pre-checks or status messages:

```
MR="$(python -c "import os; print(os.path.join(os.path.expanduser('~'), '.claude', 'monitor').replace(chr(92), '/'))" 2>/dev/null)"
if [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
  tasklist 2>/dev/null | grep -qi pythonw && echo "Monitor already running." || { cscript //nologo "$(cygpath -w "$MR/start-monitor.vbs")" && echo "Monitor launched."; }
else
  pgrep -f claude-code-monitor.py > /dev/null && echo "Monitor already running." || { pythonw "$MR/claude-code-monitor.py" 2>/dev/null & echo "Monitor launched."; }
fi
```

Do NOT check if it's running beforehand with a separate command. Do NOT confirm or explain. Just run the one command above and output the result.
