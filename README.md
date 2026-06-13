# Session Monitor

Languages: **English** | [한국어](README.ko.md)

Always-on-top overlay widget for Claude Code and Codex sessions.

- Shows live status across supported terminals and desktop app surfaces.
- Built with Claude Code and Codex for personal use.
- Tested primarily on Windows; other platforms work in part but are not the focus.

![demo](docs/cm_demo.webp)

## What it shows

Each row is one Claude Code or Codex session.

- Provider marker: `●` Claude, `◆` Codex
- State colour and state label
- Project folder and slot number
- Short topic summary

| State | Meaning |
|---|---|
| Working | Claude or Codex is generating or running a tool |
| Done | Response finished |
| Waiting | Claude or Codex is asking for user input |
| Interrupted | Stopped via ESC, failure, or a detected interruption |
| Idle | Session open, no activity |

- Click a row to focus that terminal or app window.
- Right-click a row to remove it from the current monitor view without terminating the underlying Claude/Codex session.
- Removed rows reappear when a newer turn or rollout update is detected.
- Completed app-backed rows are auto-hidden after the configured TTL.
- On macOS, use a two-finger/secondary click or Ctrl-click.
- Drag the bar to move the widget; the position is remembered.

## Client and surface support

| Surface | Status | Detection | Focus behavior | Notes |
|---|---|---|---|---|
| Claude Code CLI | Supported | Claude hooks in `~/.claude/settings.json` | Focuses the owning terminal/window when possible | Exactness depends on the terminal exposing a useful process/window relationship. |
| Codex CLI with hooks | Supported | Codex hooks in `~/.codex/config.toml` | Focuses the owning terminal/window when possible | Best path for active CLI sessions because rows have a real PID. |
| Codex CLI without hooks | Supported fallback | Rollout JSONL files under `~/.codex/sessions/` | Best-effort by WezTerm pane, session id, cwd, or live `codex.exe` process | Rows are PID-less until a real hook sees the session. Nested/subagent rollouts are ignored. |
| Claude Desktop / app-backed Code sessions | Best-effort | Native Claude session files when available | Raises the Claude app window | Claude Desktop does not currently expose a documented desktop deep link that switches to an existing Code chat. |
| Codex desktop app | Supported | Codex rollout stream plus local Codex state DB titles when available | Opens `codex://threads/{sessionId}`, then raises the Codex app window | Windows foreground rules can still flash the taskbar instead of focusing in some environments. |
| WezTerm | Best terminal focus path | `wezterm cli list` pane metadata | Activates the matching pane/tab, then raises the WezTerm window | Requires `wezterm` on `PATH`. |
| Windows Terminal / PowerShell / cmd | Supported fallback | Process tree, PID, cwd, and window matching | Raises the matched terminal window | Can be less precise when several panes share the same cwd. |
| Other terminals | Best-effort | Generic process/window matching | Raises a likely matching window | Precision depends on window titles and process ownership. |
| macOS/Linux | Partial | Hooks and rollout files work | Window focus, hotkey, and sound are limited | The project is developed and tested primarily on Windows. |

## How the summary is generated

The topic label in each row is generated automatically.

- Trigger: session goes idle through a Stop hook.
- Execution: detached background process, so it does not block your interactive session.
- Cost: consumes Claude or Codex usage like any other CLI invocation, using the cheapest configured tier for each provider.
- Language: follows the OS locale, Korean on Korean systems and English otherwise. Codex summaries also try to follow the dominant user request language.
- Debounce: 5-minute cooldown per session.

| Provider | Source | Summarizer | Notes |
|---|---|---|---|
| Claude | Session JSONL | `claude -p --model claude-haiku-4-5` | Signal priority: ai-title, `/rename` slug, away_summary recap, first user messages. |
| Codex | Bounded digest from rollout JSONL | `codex exec --ephemeral --ignore-user-config -m gpt-5.4-mini` | Digest includes recent user requests, assistant progress, final answers, tool calls, touched files, and plan updates. |

Codex summary safeguards:

- `--ephemeral`: prevents the summary call from writing a new rollout.
- `--ignore-user-config`: prevents monitor hooks from firing recursively.
- Model override: `SESSION_MONITOR_CODEX_SUMMARY_MODEL` or `codex_summary_model` in `~/.local/share/session-monitor/config.json`.

## Install

Python installer is the only supported install path.

```powershell
git clone https://github.com/vehumet/claude-code-monitor.git session-monitor
cd session-monitor
python install.py
```

Installer behavior:

- Safe to rerun.
- Copies Session Monitor into `~/.local/share/session-monitor/`.
- Installs `/session-monitor` into `~/.claude/commands/`.
- Merges required Claude hooks into `~/.claude/settings.json`.
- When `~/.codex/` exists, installs Codex CLI hooks into `~/.codex/config.toml`.
- Installs the `$session-monitor` skill into `~/.codex/skills/`.
- Copies existing runtime data forward from `~/.claude/session-monitor/`.

Useful options:

```powershell
python install.py --dry-run
python install.py --skip-codex-hooks
```

Requirements:

- Git
- Python 3.10+ with tkinter, bundled with the standard Python installer
- At least one supported client: Claude Code CLI, Codex CLI, Claude Desktop, or the Codex desktop app
- For hook-based CLI monitoring: the relevant CLI installed and configured

### Detection notes

- The installer configures Claude and Codex hooks automatically where possible.
- Codex still appears when hooks are disabled, using rollout files as a fallback. Those rows are PID-less, so click-to-focus is less precise.
- Claude Desktop can be raised, but it cannot reliably switch to a specific existing Code chat.
- Codex desktop rows open `codex://threads/{sessionId}` before falling back to ordinary window focus.
- If you move the install directory, run `python uninstall.py` and then `python install.py` again so hook paths are regenerated.

### Update

```powershell
cd session-monitor
git pull
python install.py
```

## Run

After installing or updating, restart the CLI session so it reloads newly installed commands/skills.

In Claude Code:

```
/session-monitor
```

In Codex CLI, custom slash commands are not currently supported. Use the installed skill:

```
$session-monitor
```

Or directly:

```powershell
# Windows
python "$env:USERPROFILE\.local\share\session-monitor\start-session-monitor.py"
```

```bash
# macOS/Linux
python ~/.local/share/session-monitor/start-session-monitor.py
```

## Config

Optional `~/.local/share/session-monitor/config.json`:

```json
{
  "language": "ko",
  "background_opacity": 0.85,
  "summary_max_chars": 12,
  "blink_seconds": 10,
  "app_done_ttl_s": 1800,
  "latest_done_hotkey": "",
  "sound_enabled": true,
  "sound_files": {
    "done": "~/Sounds/done.mp3",
    "question": "~/Sounds/question.mp3",
    "interrupted": "~/Sounds/interrupted.wav",
    "status_restored": "~/Sounds/restored.mp3"
  }
}
```

Config keys:

- `language`: auto-detected from OS locale, `ko` on Korean systems and `en` otherwise; set explicitly to override.
- `background_opacity`: overlay opacity. `1.0` is fully opaque. Legacy `opacity` still works.
- `summary_max_chars`: summary column width and summary prompt cap.
- `app_done_ttl_s`: auto-hide completed app-backed Codex/Claude Desktop rows after this many seconds. `1800` is 30 minutes. `0` means unlimited.
- `latest_done_hotkey`: disabled by default. On Windows, use a value like `ctrl+shift+space` to focus the most recently completed visible row.
- `sound_files`: maps monitor events to audio files. Supported events: `done`, `question`, `interrupted`, `status_restored`.
- Sound playback: best-effort using platform audio support; falls back to the built-in beep on Windows.

## Uninstall

```powershell
python uninstall.py
```

## License

[MIT](LICENSE)
