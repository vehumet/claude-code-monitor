# Changelog

All notable changes to this project will be documented in this file.

## [0.0.7] - 2026-03-17

### Fixed

- Self-register session file when `--resume` causes sessionId mismatch
- Add Phase 2.5: walk ancestor chain to find claude.exe PID, then update or create session file
- Remove Phase 3 startedAt fallback which could pick an unrelated session

## [0.0.6] - 2026-03-17

### Fixed

- Remove Phase 4 blind fallback that overwrites unrelated sessions — skip state write if no match found by Phase 2/3/3.5

## [0.0.5] - 2026-03-17

### Fixed

- Fix PowerShell (Windows Terminal) focus failure — WT delegation model not detected by ancestor chain scan
- Fix desktop app state leaking into PowerShell slot via blind Phase 4 fallback

### Changed

- `write-state.py`: process tree now stores exe name (`pid -> (parent_pid, exe)`)
- `write-state.py`: accept terminal host HWND on UserPromptSubmit hook
- `write-state.py`: add Phase 3.5 ancestor PID matching before Phase 4 fallback
- `claude-code-monitor.py`: add Phase 1c global WindowsTerminal scan in `find_window_for_pid()`
- `claude-code-monitor.py`: filter out "Program Manager" from window candidates

## [0.0.4] - 2026-03-17

### Fixed

- Find WindowsTerminal window via terminal host descendant scan
- Activate correct Cursor window when multiple instances share same PID

### Added

- Proactively resolve HWND for new instances in poll()

### Changed

- Update slash command and plugin update instructions to match plugin mode format

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
