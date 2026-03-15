# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-03-15

### Added

- Initial public release
- Real-time overlay showing all active Claude Code instances
- 4 states: Working, Done, Waiting (question), Idle
- Sound notifications for Done and Question events (Windows)
- Click-to-focus: click an instance to bring its terminal to the foreground
- Draggable overlay with position persistence
- i18n support (English, Korean)
- Configurable via `~/.claude/monitor/config.json`
- `CLAUDE_MONITOR_STATE_DIR` environment variable for custom state directory
- Claude Code Plugin support (hooks.json + slash command)
- Standalone install/uninstall scripts with settings.json merge
- `--version` flag
