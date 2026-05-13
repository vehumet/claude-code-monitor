# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed

- Drop the private Claude plugin marketplace installation path. The Python installer is now the only supported install method, with root-level `install.py` and `uninstall.py` entry points.
- Update README install/update/uninstall instructions so an LLM can install from the GitHub URL with `git clone` followed by `python install.py`.
- Claude hooks now pass `--provider claude` explicitly, and reinstalling updates older managed hook commands in place.

### Fixed

- Do not classify Claude hook payloads as Codex just because they contain `hook_event_name`.
- Ignore non-interactive `codex exec` rollouts in the fallback poller so summary/background exec sessions do not appear as PID-less monitor rows.
- Improve Codex session summaries by reducing rollouts into a bounded structured digest of recent prompts, progress messages, final answers, tool calls, and file hints before calling the mini summarizer.

### Removed

- Remove marketplace/plugin metadata, marketplace hook definitions, and the marketplace-specific `/update-monitor` command.

## [0.0.20] - 2026-05-08

### Added

- Codex CLI session support. The overlay now monitors Codex (`codex.exe`) sessions alongside Claude Code, with provider markers in the left status column (`●` for Claude, `◆` for Codex; ASCII `[C]`/`[X]` fallback via `CLAUDE_MONITOR_ASCII_GLYPH=1`). Same colour palette, same state vocabulary, same sounds.
- Codex hook auto-installation via `install.py`: when `~/.codex/` is present, the installer appends a marker-fenced block to `~/.codex/config.toml` that enables `[features] hooks = true` and registers `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `PermissionRequest` / `Stop` hooks calling `write-state.py --provider codex <state>`. Idempotent on rerun, backed up to `config.toml.bak.<ts>`. `--skip-codex-hooks` opts out for users who'd rather wire it themselves.
- Rollout JSONL fallback poller (`codex_rollout_poller.py`): when hooks are disabled or hook injection failed, the overlay still discovers user-facing Codex sessions by scanning `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Rollout-discovered sessions appear as PID-less virtual rows (`codex-<sid8>`) with state inferred from in-band `event_msg` markers.

### Changed

- State JSON gains `provider` (`"claude"`/`"codex"`), `lastSignalSource` (`"hook"`/`"rollout"`), and `lastSignalAt`. `provider` is preserved across writes via `_PRESERVED_FIELDS` so a stray fallback can't reclassify an existing row.
- PID-validation helpers (`_pid_is_claude`, `_is_claude_basename`, `is_claude_pid_alive`) generalised to recognise both `claude.exe` (incl. the `claude.exe.old.*` self-update rename) and `codex.exe`. Existing call sites unchanged through thin compatibility wrappers.
- `write-state.py` accepts an optional `--provider claude|codex` CLI prefix (Codex hooks pass `--provider codex` because Codex's runtime doesn't expand `${CLAUDE_PLUGIN_ROOT}`). Provider is otherwise auto-detected from stdin payload (`hook_event_name` only appears in Codex hook payloads) or the ancestor process tree.
- `uninstall.py` now also strips the marker-fenced Codex block and the inserted `hooks = true` (or legacy `codex_hooks = true`) flag from `~/.codex/config.toml`.
- Pinned rows now persist across monitor restarts for live sessions via `~/.claude/monitor/pins.json`.
- Provider identity moved out of the folder label and into the left status marker column, so folder names stay aligned.
- Rollout poller infers state from in-band `task_started` / `task_complete` event counts instead of mtime freshness, so a Codex turn that goes silent during reasoning no longer flips the row between Working and Done. mtime is now only the fallback for the brief window before the first turn marker appears (raised from 5s to 30s).
- PID-less Codex rows no longer attempt terminal focus, because rollout files do not carry process/window ownership and WindowsTerminal fallback can focus unrelated terminals. Hook-registered rows continue to use real PIDs / recorded `hwnd`.
- `install.py` writes the new `[features].hooks = true` flag instead of the deprecated `codex_hooks` (Codex 0.128+ warns on the old name) and migrates existing installations in place — the flag line is rewritten, marker block stays put, and `~/.codex/config.toml` is backed up to `config.toml.bak.<unix_ts>`.
- Codex Stop hook now spawns the same background summarizer Claude does, but shells out to `codex exec --ephemeral --ignore-user-config --color never -m <model> -o <file>` and walks the rollout JSONL for the richest available signal. Single-pass scan picks one of, in priority order: most-recent `agent_message` with `phase: final_answer` (Codex's analogue of Claude's away_summary — usually the turn-completion message) → first `agent_message` with `phase: commentary` (the assistant's plan/intro for the current turn) → first 5 `user_message` events. Default model `gpt-5.4-mini`; override with `CLAUDE_MONITOR_CODEX_SUMMARY_MODEL` env var or `codex_summary_model` in `~/.claude/monitor/config.json`. `--ephemeral` keeps the call from creating its own rollout file, `--ignore-user-config` keeps it from re-firing our hooks, and the captured summary is written to state JSON with `summarySource: "codex_mini"` (refresh policy reuses the 5-minute Haiku cooldown).
- Rollout poller now persists virtual rows to disk (`~/.claude/sessions/codex-<sid8>.json` + `~/.claude/monitor/state/codex-<sid8>.json`) instead of merging in-memory, so PID-less Codex sessions (those running with `[features] hooks` off, or started before the hook block was installed) flow through the standard sessions/state pipeline, get slot numbers, and auto-summary when the overlay sees the row transition to `done`. The summarizer is spawned by the overlay (not a hook) since these sessions have no hook to fire; the same `_should_summarize` cooldown applies.
- Codex rollout ownership is now explicit: hook-seen sessionIds are marked under `~/.claude/monitor/codex-hooked/`, subagent/nested rollouts are ignored, and ignored rollouts only remove monitor-generated virtual row files. Source rollout JSONL files are never deleted.
- Hot-path optimisations after a `simplify`-pass review: poller caches `task_started` / `task_complete` counts in `prev_cache` and skips the per-tick rollout re-scan when mtime is unchanged (large rollouts can be multi-MB and were re-parsed every 500ms); poller skips the state JSON write entirely on cache-hit so the overlay isn't re-reading state every tick for unchanged sessions; `InstanceTracker.poll` reads each `sessions/*.json` once per tick instead of twice (was doing `known_session_ids` collection + main loop separately); `prev_cache` now evicts entries past `_SCAN_MAX_AGE_S` so an always-on monitor doesn't accumulate forever.

## [0.0.19] - 2026-05-05

### Added

- Pin a row by clicking the colored dot at the start of the row. Pinned rows sort above unpinned ones with earliest-pinned first; the dot swaps to a lock glyph (🔒) so the pinned state is visible. Pin state is in-memory only and resets when the overlay restarts.

### Fixed

- Recognize `claude.exe.old.<timestamp>` as a live Claude Code process. When Claude Code self-updates, Windows renames the in-use binary to `claude.exe.old.<ts>` and any already-running session keeps that path until it exits. The previous `endswith("claude.exe")` check rejected every running session after an update, which made the overlay empty itself out and the slot allocator stop reusing slots properly.
- Preserve the home cwd recorded in `state/{pid}.json` instead of overwriting it with the tool cwd of every hook fire. Hooks fire with whatever cwd the current tool is running in (e.g. a PowerShell call inside `.../src/`), and that was leaking into the row label as `src(N)`. Phase 2.5 self-register also now reuses the existing state cwd when rebuilding a `sessions/{pid}.json` that was wiped during a self-update.

## [0.0.18] - 2026-05-04

### Added

- Prioritised signal sources for the summary label, picked one at a time and handed to Haiku for length/language normalisation: jsonl `type:"ai-title"` (wezterm tab parity) → `sessions/{pid}.json` `name` (`/rename` slug) → latest `system/away_summary` recap → first user messages. Includes a regex sweep of the `(disable recaps in /config)` footer and stray markdown markers (`**`, `*`, `_`, `` ` ``, `#`).

### Changed

- Trim fallback (`UserPromptSubmit` first-line snippet) now stamps once when the row is empty and is not refreshed by subsequent prompts, so the label stops jittering with each new message until Haiku takes over.
- Haiku output is no longer truncated with `…` in post-processing — the widget's grid cells already clip overflow, and the trailing ellipsis was misattributing widget clipping to model truncation.
- Hover background now recolours folder column too — the new grid layout's nested folder/summary boxes were missed by the previous hover handler.

## [0.0.17] - 2026-05-03

### Added

- Per-cwd slot number anchor (`①` ~ `⑳`, fallback `[N]` above 20) assigned at first hook invocation and stable for the session lifetime. Replaces the legacy `(1)(2)` numeric suffix that was reassigned whenever sessions came and went.
- Background Haiku 4.5 summarization on `Stop` hook: a detached subprocess re-invokes `write-state.py __summarize__ <pid>`, reads the session JSONL, calls `claude -p ... --model claude-haiku-4-5`, and merges a 12-character Korean noun-phrase label into the state file. Refresh policy: trim/empty → run on next Stop; existing Haiku label → re-run only if 5+ minutes have elapsed.
- `UserPromptSubmit` interim label: when no Haiku label exists yet (or the previous label came from this same trim path), the first non-tag line of the user prompt is captured (≤18 chars, ellipsized) so the new session shows something meaningful before the first Stop fires.
- Empty-session placeholder: brand-new sessions display `New` until either the trim or Haiku path stamps a real label.

### Changed

- Display format is now `folder · ① · subtitle` (subtitle = Haiku summary, trim fallback, or `New`). The slot glyph is always shown — even for a single session in a folder — to keep labels stable when a second session opens later.
- State JSON gains `slot`, `summary`, `summarySource` (`new`/`trim`/`haiku`), `summaryAt`, `summaryMsgCount`, `summarySessionId`. Existing fields are preserved across writes.

## [0.0.16] - 2026-04-30

### Fixed

- `question` state stuck after permission prompt approval: added `PostToolUse` catch-all matcher → `working`, so the overlay returns to `working` immediately when a permission-gated tool finishes (Claude Code does not expose a "user responded to permission_prompt" event, so the tool-completion signal is the earliest reliable hook for the approve case)

## [0.0.15] - 2026-04-29

### Added

- WezTerm click-to-focus: when a Claude Code instance runs inside a wezterm pane, the monitor now switches directly to that pane on click. Pane is identified via `WEZTERM_PANE`/`WEZTERM_UNIX_SOCKET` env vars and `wezterm cli list` query at hook time; click handler runs `wezterm cli activate-tab` with the captured socket and raises the GUI window via Win32. Falls back to the existing process-tree window search outside wezterm. Zero configuration — no wezterm.lua changes required.

## [0.0.14] - 2026-04-26

### Added

- `StopFailure` hook → `interrupted` state for API errors (rate limit, server error, billing, auth, etc.)
- `PostToolUseFailure` hook → resolves to `interrupted` only when the payload carries `is_interrupt: true` (catches ESC during tool execution)

### Changed

- `Notification:idle_prompt` is now a meta-state: resolved to `interrupted` only when the prior state is `working` (likely ESC during text generation); skipped otherwise to stop overwriting `done`/`idle`/`question` with spurious "interrupted" after a session sits idle for ~60s

## [0.0.13] - 2026-04-23

### Fixed

- Catch tool permission prompts (Bash/Write/Edit etc.) via `Notification:permission_prompt` matcher and surface them as the `question` state — previously only `AskUserQuestion`/`ExitPlanMode` PreToolUse matchers fired, so generic permission dialogs went unnoticed

## [0.0.12] - 2026-03-24

### Fixed

- Wrap `_poll_loop()` and `_blink_loop()` in try-except-finally to prevent unhandled exceptions from permanently killing the UI update loops
- Protect ctypes calls (`build_process_tree` / `find_window_for_pid`) in `poll()` with try-except to catch sporadic Win32 API errors
- Re-assert `-topmost` flag every poll cycle to recover from Windows losing the flag after display changes or sleep/wake

## [0.0.11] - 2026-03-19

### Fixed

- Replace time-based done guard (2s) with state-based guard to prevent `interrupted` from overwriting `done` after Windows sleep/standby delays

## [0.0.10] - 2026-03-19

### Added

- Detect Ctrl+C / server error via Notification `idle_prompt` hook and show "interrupted" state
- Orange (peach) color, blink animation, and descending chime for interrupted state
- Done-guard in write-state.py to prevent idle_prompt from overwriting recent "done" state

### Fixed

- Use atomic write (tempfile + os.replace) for state files to prevent partial-read race conditions

## [0.0.9] - 2026-03-17

### Fixed

- Prevent PreToolUse race condition where catch-all "working" hook overwrites "question" state set by specific matcher
- Add 2-second timestamp guard in write-state.py to protect recently written question state

## [0.0.8] - 2026-03-17

### Fixed

- Fix question state not transitioning back to working after user answers
- Add catch-all PreToolUse hook to ensure working state on every tool use
- Add same-state skip optimization to avoid redundant file writes

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
