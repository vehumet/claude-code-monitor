# Claude Code Monitor

Always-on-top overlay widget that shows the live status of every Claude Code **and Codex CLI** session running on your machine.

Built with Claude Code and Codex CLI, for personal use. Tested on Windows; other platforms work in part but aren't a focus.

![demo](docs/cm_demo.webp)

## What it shows

Each row is one Claude Code or Codex session: a provider marker (`●` Claude, `◆` Codex), state colour, the project folder + slot number, a short topic summary, and the state label.

| State | Meaning |
|---|---|
| Working | Claude is generating or running a tool |
| Done | Response finished |
| Waiting | Claude is asking you a question |
| Interrupted | Stopped via ESC or error |
| Idle | Session open, no activity |

Click a row to focus that terminal. Drag the bar to move the widget; the position is remembered.

## How the summary is generated

The topic label in each row is generated automatically when a session goes idle (Stop hook), using a tiny model that runs as a detached background process so it doesn't block your interactive session.

- **Claude rows**: `claude -p --model claude-haiku-4-5` over the session JSONL. Signal priority: ai-title (wezterm tab parity) → `/rename` slug → away_summary recap → first user messages.
- **Codex rows**: the rollout JSONL is reduced locally into a bounded digest of recent user requests, assistant progress comments, final answers, tool calls, touched files, and plan updates. That digest is summarized with `codex exec --ephemeral --ignore-user-config -m gpt-5.4-mini`, keeping input size predictable while producing labels closer to the current task. `--ephemeral` keeps the summary call from writing a new rollout, and `--ignore-user-config` keeps it from re-firing our hooks. Override the model via `CLAUDE_MONITOR_CODEX_SUMMARY_MODEL` env var or `codex_summary_model` in `~/.claude/monitor/config.json`.

Output language follows the OS locale (Korean → Korean, otherwise English); Codex summaries also try to follow the dominant user request language and preserve natural spacing. Calls are debounced (5-minute cooldown per session). They consume Claude or Codex usage like any other CLI invocation, but at the cheapest tier of each.

## Install

Python installer is the only supported install path. Give an LLM or coding agent this repository URL and ask it to run these commands:

```powershell
git clone https://github.com/vehumet/claude-code-monitor.git
cd claude-code-monitor
python install.py
```

The installer is idempotent. It copies the monitor into `~/.claude/monitor/`, installs the `/monitor` slash command into `~/.claude/commands/`, and merges the required hooks into `~/.claude/settings.json`. When `~/.codex/` exists, it also installs Codex CLI hooks into `~/.codex/config.toml`.

Useful options:

```powershell
python install.py --dry-run
python install.py --skip-codex-hooks
```

Requires Git, Python 3.10+ with tkinter (bundled with the standard Python installer), and the Claude Code CLI.

### Codex CLI support

When `~/.codex/` is present, the installer appends a marker-fenced block to `~/.codex/config.toml` that enables `[features] hooks = true` and registers `SessionStart` / `UserPromptSubmit` / `PermissionRequest` / `Stop` hooks calling `write-state.py --provider codex <state>`. The block is idempotent on rerun and `~/.codex/config.toml` is backed up to `config.toml.bak.<unix_ts>` before any change. `python uninstall.py` removes the block cleanly.

If you'd rather wire the hooks yourself or run Codex with hooks disabled, the overlay still picks up user-facing Codex sessions through the rollout JSONL fallback poller — sessions are scanned from `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` and shown as PID-less rows (clicks won't focus the terminal, but state and folder are visible). Subagent/nested rollouts are ignored, and any session that has ever been seen by a real hook is treated as hook-owned so it won't be resurrected later as a fallback row. Pass `--skip-codex-hooks` to opt out of the auto-injection.

If you move the monitor's install location after the fact, run `python uninstall.py` then `python install.py` again so the absolute paths in `config.toml` are regenerated.

### Update

```powershell
cd claude-code-monitor
git pull
python install.py
```

## Run

In a Claude Code session:

```
/monitor
```

In Codex CLI, custom slash commands are not currently supported. The installer adds a small Codex skill instead; restart Codex after installing or updating, then type:

```
$session-monitor
```

Or directly:

```powershell
# Windows
python "$env:USERPROFILE\.claude\monitor\start-monitor.py"
```

```bash
# macOS/Linux
python ~/.claude/monitor/start-monitor.py
```

## Config

Optional `~/.claude/monitor/config.json`:

```json
{
  "language": "ko",
  "opacity": 0.65,
  "summary_max_chars": 12,
  "blink_seconds": 10,
  "sound_enabled": true
}
```

Language is auto-detected from the OS locale (`ko` on Korean systems, `en` otherwise) — set it explicitly to override. `summary_max_chars` controls both the summary column width and the cap passed to Haiku.

## Uninstall

```powershell
python uninstall.py
```

## License

[MIT](LICENSE)
