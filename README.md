# Session Monitor

Always-on-top overlay widget that shows the live status of Claude Code and Codex sessions running on your machine, across supported terminals and desktop app surfaces.

Built with Claude Code and Codex, for personal use. Tested on Windows; other platforms work in part but aren't a focus.

![demo](docs/cm_demo.webp)

## What it shows

Each row is one Claude Code or Codex session: a provider marker (`●` Claude, `◆` Codex), state colour, the project folder + slot number, a short topic summary, and the state label.

| State | Meaning |
|---|---|
| Working | Claude or Codex is generating or running a tool |
| Done | Response finished |
| Waiting | Claude or Codex is asking for user input |
| Interrupted | Stopped via ESC, failure, or a detected interruption |
| Idle | Session open, no activity |

Click a row to focus that terminal or app window. Right-click a row to remove it from the current monitor view without terminating the underlying Claude/Codex session; it reappears when a newer turn or rollout update is detected. Completed app-backed rows are auto-hidden after the configured TTL. On macOS, use a two-finger/secondary click or Ctrl-click. Drag the bar to move the widget; the position is remembered.

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

The topic label in each row is generated automatically when a session goes idle (Stop hook), using a tiny model that runs as a detached background process so it doesn't block your interactive session.

- **Claude rows**: `claude -p --model claude-haiku-4-5` over the session JSONL. Signal priority: ai-title (wezterm tab parity) → `/rename` slug → away_summary recap → first user messages.
- **Codex rows**: the rollout JSONL is reduced locally into a bounded digest of recent user requests, assistant progress comments, final answers, tool calls, touched files, and plan updates. That digest is summarized with `codex exec --ephemeral --ignore-user-config -m gpt-5.4-mini`, keeping input size predictable while producing labels closer to the current task. `--ephemeral` keeps the summary call from writing a new rollout, and `--ignore-user-config` keeps it from re-firing our hooks. Override the model via `SESSION_MONITOR_CODEX_SUMMARY_MODEL` env var or `codex_summary_model` in `~/.local/share/session-monitor/config.json`.

Output language follows the OS locale (Korean → Korean, otherwise English); Codex summaries also try to follow the dominant user request language and preserve natural spacing. Calls are debounced (5-minute cooldown per session). They consume Claude or Codex usage like any other CLI invocation, but at the cheapest tier of each.

## Install

Python installer is the only supported install path. Give an LLM or coding agent this repository URL and ask it to run these commands:

```powershell
git clone https://github.com/vehumet/claude-code-monitor.git session-monitor
cd session-monitor
python install.py
```

The installer is idempotent. It copies Session Monitor into `~/.local/share/session-monitor/`, installs the `/session-monitor` slash command into `~/.claude/commands/`, and merges the required hooks into `~/.claude/settings.json`. When `~/.codex/` exists, it also installs Codex CLI hooks into `~/.codex/config.toml` and the `$session-monitor` skill into `~/.codex/skills/`. Existing runtime data from `~/.claude/session-monitor/` is copied forward on install.

Useful options:

```powershell
python install.py --dry-run
python install.py --skip-codex-hooks
```

Requires Git, Python 3.10+ with tkinter (bundled with the standard Python installer), and at least one supported client: Claude Code CLI, Codex CLI, Claude Desktop, or the Codex desktop app. Hook-based CLI monitoring requires the relevant CLI to be installed and configured.

### Hook and fallback details

For Claude Code, the installer merges the required hooks into `~/.claude/settings.json` and installs `/session-monitor` into `~/.claude/commands/`.

For Codex CLI, when `~/.codex/` is present, the installer appends a marker-fenced block to `~/.codex/config.toml` that enables `[features] hooks = true` and registers `SessionStart` / `UserPromptSubmit` / `PermissionRequest` / `Stop` hooks calling `write-state.py --provider codex <state>`. The block is idempotent on rerun and `~/.codex/config.toml` is backed up to `config.toml.bak.<unix_ts>` before any change. `python uninstall.py` removes the block cleanly.

If you'd rather wire the hooks yourself or run Codex with hooks disabled, the overlay still picks up user-facing Codex sessions through the rollout JSONL fallback poller — sessions are scanned from `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and shown as PID-less rows. Click-to-focus is best-effort for those rows because the fallback has to match a live Codex process by project folder instead of using an exact terminal pane id. Subagent/nested rollouts are ignored, and any session that has ever been seen by a real hook is treated as hook-owned so it won't be resurrected later as a fallback row. Pass `--skip-codex-hooks` to opt out of the auto-injection.

Codex desktop app sessions are detected from the same rollout stream. When the rollout identifies `originator: "Codex Desktop"`, the row is treated as an app-backed Codex session: the monitor uses Codex's local state database for thread titles when available, keeps the app surface distinct from CLI rows internally, and opens `codex://threads/{sessionId}` for click-to-focus before falling back to process/window matching.

Claude Desktop rows are best-effort: Session Monitor can raise the Claude app window, but Claude Desktop does not currently expose a documented desktop deep link for switching to an existing Claude Code session. The documented `claude://` links support regular chats/projects and new Code sessions; `claude://code/{sessionId}` is documented for mobile and did not switch the active Desktop Code chat in local testing.

If you move the monitor's install location after the fact, run `python uninstall.py` then `python install.py` again so the absolute paths in `config.toml` are regenerated.

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

Language is auto-detected from the OS locale (`ko` on Korean systems, `en` otherwise) — set it explicitly to override. `background_opacity` controls overlay opacity (`1.0` is fully opaque; legacy `opacity` still works). `summary_max_chars` controls the summary column width and summary prompt cap. `app_done_ttl_s` auto-hides completed app-backed Codex/Claude Desktop rows after that many seconds (`1800` = 30 minutes); set it to `0` for unlimited. `latest_done_hotkey` is disabled by default; on Windows, set a value like `ctrl+shift+space` to focus the most recently completed visible row. `sound_files` maps monitor events (`done`, `question`, `interrupted`, `status_restored`) to audio files; playback is best-effort using the platform's available audio support and falls back to the built-in beep on Windows.

## Uninstall

```powershell
python uninstall.py
```

## License

[MIT](LICENSE)
