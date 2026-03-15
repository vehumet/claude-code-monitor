Launch the Claude Monitor overlay.

Run this single command immediately without any pre-checks or status messages:

```
if [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
  tasklist 2>/dev/null | grep -qi pythonw && echo "Monitor already running." || { cscript //nologo "$(cygpath -w "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/monitor}/src/start-monitor.vbs")" && echo "Monitor launched."; }
else
  pgrep -f claude-code-monitor.py > /dev/null && echo "Monitor already running." || { pythonw "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/monitor}/src/claude-code-monitor.py" 2>/dev/null & echo "Monitor launched."; }
fi
```

Do NOT check if it's running beforehand with a separate command. Do NOT confirm or explain. Just run the one command above and output the result.
