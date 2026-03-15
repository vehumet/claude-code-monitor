Launch the Claude Monitor overlay.

Run this single command immediately without any pre-checks or status messages:

```
MR="$(python -c "
import json,os,sys
f=os.path.join(os.path.expanduser('~'),'.claude','plugins','installed_plugins.json')
try:
 d=json.load(open(f))
 p=[v[0]['installPath'] for k,v in d['plugins'].items() if k.startswith('claude-code-monitor@')]
 if p:print(p[0].replace(chr(92),'/'));sys.exit(0)
except Exception:pass
print(os.path.join(os.path.expanduser('~'),'.claude','monitor').replace(chr(92),'/'))
" 2>/dev/null)"
if [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* ]]; then
  tasklist 2>/dev/null | grep -qi pythonw && echo "Monitor already running." || { cscript //nologo "$(cygpath -w "$MR/src/start-monitor.vbs")" && echo "Monitor launched."; }
else
  pgrep -f claude-code-monitor.py > /dev/null && echo "Monitor already running." || { pythonw "$MR/src/claude-code-monitor.py" 2>/dev/null & echo "Monitor launched."; }
fi
```

Do NOT check if it's running beforehand with a separate command. Do NOT confirm or explain. Just run the one command above and output the result.
