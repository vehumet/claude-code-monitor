# Claude Code Monitor

> Real-time overlay widget showing the status of all active Claude Code instances.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)

## Features

- **4 states** displayed in real-time: Working (green), Done (blue), Waiting for input (yellow), Idle (grey)
- **Sound notifications** when tasks complete or questions arise (Windows)
- **Click to focus** — click any instance row to bring its terminal window to the foreground
- **Drag to reposition** — position is saved between sessions
- **Zero dependencies** — Python stdlib only (tkinter + ctypes)
- **Multi-instance** — monitors all active Claude Code sessions simultaneously

## Requirements

- **Python 3.10+** with tkinter (included in standard Python installers)
- **Windows 10/11** (primary) | macOS/Linux (partial — no sound or window focus)
- **Claude Code CLI**

## Installation

### Option A: Claude Code Plugin (Recommended)

> Note: Plugin support depends on Claude Code's plugin system availability.

```bash
# Add the marketplace
claude plugin marketplace add vehumet/claude-code-monitor

# Install the plugin
claude plugin install claude-code-monitor
```

Or test locally:

```bash
git clone https://github.com/vehumet/claude-code-monitor.git
claude --plugin-dir ./claude-code-monitor
```

### Option B: Standalone Install

```bash
git clone https://github.com/vehumet/claude-code-monitor.git
cd claude-code-monitor
python install.py
```

This will:
1. Copy monitor files to `~/.claude/monitor/`
2. Copy the hook script to `~/.claude/hooks/`
3. Merge hook entries into `~/.claude/settings.json` (with backup)
4. Install the `/monitor` slash command

Preview changes without modifying anything:

```bash
python install.py --dry-run
```

## Usage

### Launch the monitor

In Claude Code:

```
/monitor
```

Or manually:

```bash
# Windows (recommended — runs as independent process)
cscript //nologo "%USERPROFILE%\.claude\monitor\start-monitor.vbs"

# Any platform
pythonw ~/.claude/monitor/claude-code-monitor.py
```

### How it works

```
Claude Code Instance          Hooks (write-state.py)         Monitor Overlay
┌──────────────────┐         ┌────────────────────┐         ┌──────────────┐
│ User sends prompt│────────>│ UserPromptSubmit   │────────>│ ● project1   │
│                  │         │   -> "working"     │         │   Working    │
│ AI is working... │         │                    │         │              │
│                  │         │ Stop               │         │ ● project2   │
│ Task complete    │────────>│   -> "done"        │────────>│   Done       │
│                  │         │                    │         │              │
│ AI asks question │         │ PreToolUse         │         │              │
│ (AskUserQuestion)│────────>│   -> "question"    │────────>│              │
└──────────────────┘         └────────────────────┘         └──────────────┘
                                    │                              ▲
                                    │  ~/.claude/monitor/state/    │
                                    │     {pid}.json               │
                                    └──────────────────────────────┘
                                          (polled every 500ms)
```

State files in `~/.claude/monitor/state/` are JSON:

```json
{"pid": 12345, "state": "working", "cwd": "/path/to/project", "updatedAt": 1710500000}
```

The overlay reads Claude Code session files (`~/.claude/sessions/*.json`) to discover instances, and state files to display their current status.

## Configuration

Create `~/.claude/monitor/config.json` to customize behavior:

```json
{
  "language": "en",
  "opacity": 0.65,
  "width": 260,
  "poll_interval_ms": 500,
  "blink_interval_ms": 600,
  "sound_enabled": true
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `language` | `"en"` | UI language: `"en"` or `"ko"` |
| `opacity` | `0.65` | Window transparency (0.0 - 1.0) |
| `width` | `260` | Overlay width in pixels |
| `poll_interval_ms` | `500` | How often to check for state changes |
| `blink_interval_ms` | `600` | Blink speed for new "Done" notifications |
| `sound_enabled` | `true` | Play sound on Done/Question events (Windows only) |

All fields are optional — omitted fields use defaults.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MONITOR_STATE_DIR` | `~/.claude/monitor/state/` | Override state file directory |

## Troubleshooting

### Monitor doesn't show any instances

- Ensure Claude Code is running and has active sessions
- Check that `~/.claude/sessions/` contains session JSON files
- Verify Python has tkinter: `python -c "import tkinter"`

### Hooks not triggering

- Check `~/.claude/settings.json` contains the hook entries (search for `write-state.py`)
- Verify `~/.claude/hooks/write-state.py` exists and is readable

### Monitor closes when terminal closes (Windows)

- Use the VBS launcher instead of running Python directly:
  ```
  cscript //nologo "%USERPROFILE%\.claude\monitor\start-monitor.vbs"
  ```
- The VBS script spawns an independent process that survives shell exit

### Sound not working

- Sound notifications use Windows `winsound.Beep` — not available on macOS/Linux
- Set `"sound_enabled": false` in config.json to disable

## Uninstall

### Plugin mode

```bash
claude plugin uninstall claude-code-monitor
```

### Standalone mode

```bash
cd claude-code-monitor
python uninstall.py
```

Options:
- `--dry-run` — preview without modifying files
- `--keep-config` — preserve `config.json` and `position.json`

## License

[MIT](LICENSE)
