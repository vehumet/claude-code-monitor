# Claude Code Monitor

Always-on-top overlay widget that shows the live status of every Claude Code session running on your machine.

Built with Claude Code, for personal use. Tested on Windows; other platforms work in part but aren't a focus.

![demo](docs/cm_demo.webp)

## What it shows

Each row is one Claude Code session: a colour dot for state, the project folder + slot number, a short Haiku-generated topic summary, and the state label.

| State | Meaning |
|---|---|
| Working | Claude is generating or running a tool |
| Done | Response finished |
| Waiting | Claude is asking you a question |
| Interrupted | Stopped via ESC or error |
| Idle | Session open, no activity |

Click a row to focus that terminal. Drag the bar to move the widget; the position is remembered.

## How the summary is generated

The topic label in each row is generated automatically by **Claude Haiku 4.5** (`claude -p --model claude-haiku-4-5`) on the Stop hook, using whichever signal is richest — in priority order: the session's ai-title (mirrors the wezterm tab), a `/rename` slug, the most recent recap, or the first user messages. Output language follows the OS locale (Korean → Korean, otherwise English). Calls are debounced (5-minute cooldown per session) and run as a detached subprocess so they don't block your interactive session, but they do consume your Claude usage like any other `claude` invocation.

## Install

Plugin install (recommended):

```bash
claude plugin marketplace add vehumet/claude-code-monitor
claude plugin install claude-code-monitor
```

Standalone install:

```bash
git clone https://github.com/vehumet/claude-code-monitor.git
cd claude-code-monitor
python plugins/claude-code-monitor/install.py
```

Requires Python 3.10+ with tkinter (bundled with the standard installer) and the Claude Code CLI.

## Run

In a Claude Code session:

```
/claude-code-monitor:monitor
```

Or directly:

```bash
# Windows — survives shell exit
cscript //nologo "%USERPROFILE%\.claude\monitor\start-monitor.vbs"

# Any platform
pythonw ~/.claude/monitor/claude-code-monitor.py
```

## Config

Optional `~/.claude/monitor/config.json`:

```json
{
  "language": "ko",
  "opacity": 0.65,
  "summary_max_chars": 12,
  "sound_enabled": true
}
```

Language is auto-detected from the OS locale (`ko` on Korean systems, `en` otherwise) — set it explicitly to override. `summary_max_chars` controls both the summary column width and the cap passed to Haiku.

## Uninstall

```bash
claude plugin uninstall claude-code-monitor
# or, for standalone:
python plugins/claude-code-monitor/uninstall.py
```

## License

[MIT](LICENSE)
