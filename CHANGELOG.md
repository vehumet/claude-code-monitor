# Changelog

All notable changes to this project will be documented in this file.

## [0.0.3] - 2026-03-17

### Changed

- Fix changelog version history

## [0.0.2] - 2026-03-17

### Fixed

- Resolve monitor not starting in plugin mode
- Resolve plugin path via installed_plugins.json instead of template variable
- Restructure repo to standard marketplace plugin layout
- Disambiguate window activation for multiple Cursor instances
- Improve window matching for multiple Cursor instances with diagnostic logging
- Resolve second Claude Code terminal stuck as idle when sharing cwd

### Added

- Plugin distribution and version management (bump-version script, CI validation)
- Demo screenshot in README

## [0.0.1] - 2026-03-15

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
