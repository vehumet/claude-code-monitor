# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [0.0.31] - 2026-08-04

### Changed

- Parse only newly appended Codex rollout records and reuse recent rollout discovery and thread metadata caches.

### Fixed

- Prevent large, long-running Codex rollouts from blocking the overlay event loop with repeated full-file scans.
- Avoid duplicate question-state scans for PID-less Codex rows and restore current rollout state without a full startup rescan.

## [0.0.30] - 2026-06-14

### Added

- Add configurable sound files for monitor events, including custom `done`, `question`, `interrupted`, and `status_restored` sounds.
- Add a disabled-by-default Windows hotkey that focuses the most recently completed visible session.
- Add Korean README documentation alongside the English default README.

### Changed

- Auto-hide completed app-backed Claude/Codex rows after a configurable TTL while keeping recent rows visible on startup.
- Use bold `C`/`G` letter markers for Claude/Codex provider identity instead of small geometric glyphs.
- Generate Codex row labels as short chat-list titles with JSON `title` output for more task-name-like labels.
- Refresh README structure and support tables around Claude and Codex as first-class monitor targets.

### Fixed

- Persist each session's completion time so the latest-completed hotkey does not prefer old rows just because their state file refreshed later.
- Keep dismissed Codex rollout rows hidden until a genuinely newer turn or rollout update arrives.

## [0.0.29] - 2026-06-13

### Fixed

- Prevent dismissed Codex rollout rows from immediately reappearing just because the poller repaired their runtime files.

## [0.0.28] - 2026-06-11

### Fixed

- Keep resumed Codex sessions visible when the Codex thread database is newer than an old hook-owned marker.
- Scope Codex fork rollout scanning to the active session segment so parent transcript markers and questions do not leak into child rows or summaries.
- Treat Codex rollouts whose latest marker is `task_started` as Working even when start and complete counts are tied.
- Open PID-less Codex rows by thread deep link whenever a session id is available, reducing slow fallback focus scans.
- Reduce unnecessary overlay redraws and topmost/geometry updates so timestamp-only refreshes do not flicker the widget.

## [0.0.27] - 2026-06-11

### Added

- Allow monitor rows to be dismissed with right-click, secondary click, or Ctrl-click without terminating the underlying Claude/Codex session.

### Changed

- Dismissed rows reappear automatically when a newer hook signal or Codex rollout update is detected.

## [0.0.26] - 2026-06-11

### Added

- Detect Codex desktop app sessions from rollout metadata, use local Codex thread titles when available, and open `codex://threads/{sessionId}` for app-backed Codex rows.
- Restore live Claude Desktop Code sessions from `~/.claude/sessions/*.json`, distinguish them with a desktop marker, and focus the Claude app window for those rows.

### Fixed

- Keep Claude Desktop `AskUserQuestion` rows in the Waiting state while the transcript still has an unanswered question, including sessions that were previously reset to Working by file-change detection.

## [0.0.25] - 2026-06-02

### Changed

- Make the overlay background opacity configurable via `background_opacity`, keep legacy `opacity` support, and raise the default opacity to `0.85` for a darker monitor background.

### Fixed

- Prevent Codex `session_start` hooks with a different sessionId from overwriting an existing active PID session before a real state update arrives.

## [0.0.24] - 2026-05-21

### Fixed

- Handle Codex CLI sessionId rollover on an existing PID so active sessions keep updating the monitor instead of being skipped as stale PID reuse.
- Clear stale Codex summary fields when a PID rolls over to a new sessionId so the row label belongs to the current session.

## [0.0.23] - 2026-05-21

### Changed

- Drop the private Claude plugin marketplace installation path. The Python installer is now the only supported install method, with root-level `install.py` and `uninstall.py` entry points.
- Replace the Windows VBS monitor launcher with a Python detached launcher (`start-session-monitor.py`), used by both `/session-monitor` and manual launch instructions.
- Rename the Claude Code slash command to `/session-monitor` and install a matching Codex `$session-monitor` skill.
- Update README install/update/uninstall instructions so an LLM can install from the GitHub URL with `git clone` followed by `python install.py`.
- Clarify that Claude Code and Codex CLI sessions should be restarted after install/update before using `/session-monitor` or `$session-monitor`.
- Rename user-facing branding, install path, source paths, and environment variables to Session Monitor names.
- Claude hooks now pass `--provider claude` explicitly, and reinstalling updates older managed hook commands in place.
- Move the shared runtime from `~/.claude/session-monitor` to `~/.local/share/session-monitor`, with installer migration for existing runtime data.

### Fixed

- Do not classify Claude hook payloads as Codex just because they contain `hook_event_name`.
- Ignore non-interactive `codex exec` rollouts in the fallback poller so summary/background exec sessions do not appear as PID-less monitor rows.
- Improve Codex session summaries by reducing rollouts into a bounded structured digest of recent prompts, progress messages, final answers, tool calls, and file hints before calling the mini summarizer.

### Removed

- Remove marketplace/plugin metadata, marketplace hook definitions, and the marketplace-specific `/update-monitor` command.

## [0.0.22] - 2026-05-17

### Fixed

- Restore Claude Code `StopFailure`, `PostToolUseFailure`, and `Notification` hook installation so API errors, tool interrupts, idle-prompt interrupts, and permission prompts are reflected in the monitor after reinstall.
- Dismiss the recent state-change highlight when the highlighted row is clicked.

## [0.0.21] - 2026-05-16

### Fixed

- Preserve Codex hook trust state when refreshing the managed hook block so reinstall/update does not delete `trusted_hash` entries.
- Ignore Codex desktop app-server helper hooks and rows so non-interactive desktop background processes do not appear as sticky monitor sessions.

## [0.0.20] - 2026-05-08

### Added

- Codex CLI session support. The overlay now monitors Codex (`codex.exe`) sessions alongside Claude Code, with provider markers in the left status column (`●` for Claude, `◆` for Codex; ASCII `[C]`/`[X]` fallback via `SESSION_MONITOR_ASCII_GLYPH=1`). Same colour palette, same state vocabulary, same sounds.
- Codex hook auto-installation via `install.py`: when `~/.codex/` is present, the installer appends a marker-fenced block to `~/.codex/config.toml` that enables `[features] hooks = true` and registers `SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `PermissionRequest` / `Stop` hooks calling `write-state.py --provider codex <state>`. Idempotent on rerun, backed up to `config.toml.bak.<ts>`. `--skip-codex-hooks` opts out for users who'd rather wire it themselves.
- Rollout JSONL fallback poller (`codex_rollout_poller.py`): when hooks are disabled or hook injection failed, the overlay still discovers user-facing Codex sessions by scanning `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. Rollout-discovered sessions appear as PID-less virtual rows (`codex-<sid8>`) with state inferred from in-band `event_msg` markers.

### Changed

- State JSON gains `provider` (`"claude"`/`"codex"`), `lastSignalSource` (`"hook"`/`"rollout"`), and `lastSignalAt`. `provider` is preserved across writes via `_PRESERVED_FIELDS` so a stray fallback can't reclassify an existing row.
- PID-validation helpers (`_pid_is_claude`, `_is_claude_basename`, `is_claude_pid_alive`) generalised to recognise both `claude.exe` (incl. the `claude.exe.old.*` self-update rename) and `codex.exe`. Existing call sites unchanged through thin compatibility wrappers.
- `write-state.py` accepts an optional `--provider claude|codex` CLI prefix (Codex hooks pass `--provider codex` because Codex's runtime doesn't expand `${CLAUDE_PLUGIN_ROOT}`). Provider is otherwise auto-detected from stdin payload (`hook_event_name` only appears in Codex hook payloads) or the ancestor process tree.
- `uninstall.py` now also strips the marker-fenced Codex block and the inserted `hooks = true` (or legacy `codex_hooks = true`) flag from `~/.codex/config.toml`.
- Pinned rows now persist across monitor restarts for live sessions via `~/.claude/session-monitor/pins.json`.
- Provider identity moved out of the folder label and into the left status marker column, so folder names stay aligned.
- Rollout poller infers state from in-band `task_started` / `task_complete` event counts instead of mtime freshness, so a Codex turn that goes silent during reasoning no longer flips the row between Working and Done. mtime is now only the fallback for the brief window before the first turn marker appears (raised from 5s to 30s).
- PID-less Codex rows no longer attempt terminal focus, because rollout files do not carry process/window ownership and WindowsTerminal fallback can focus unrelated terminals. Hook-registered rows continue to use real PIDs / recorded `hwnd`.
- `install.py` writes the new `[features].hooks = true` flag instead of the deprecated `codex_hooks` (Codex 0.128+ warns on the old name) and migrates existing installations in place — the flag line is rewritten, marker block stays put, and `~/.codex/config.toml` is backed up to `config.toml.bak.<unix_ts>`.
- Codex Stop hook now spawns the same background summarizer Claude does, but shells out to `codex exec --ephemeral --ignore-user-config --color never -m <model> -o <file>` and walks the rollout JSONL for the richest available signal. Single-pass scan picks one of, in priority order: most-recent `agent_message` with `phase: final_answer` (Codex's analogue of Claude's away_summary — usually the turn-completion message) → first `agent_message` with `phase: commentary` (the assistant's plan/intro for the current turn) → first 5 `user_message` events. Default model `gpt-5.4-mini`; override with `SESSION_MONITOR_CODEX_SUMMARY_MODEL` env var or `codex_summary_model` in `~/.claude/session-monitor/config.json`. `--ephemeral` keeps the call from creating its own rollout file, `--ignore-user-config` keeps it from re-firing our hooks, and the captured summary is written to state JSON with `summarySource: "codex_mini"` (refresh policy reuses the 5-minute Haiku cooldown).
- Rollout poller now persists virtual rows to disk (`~/.claude/sessions/codex-<sid8>.json` + `~/.claude/session-monitor/state/codex-<sid8>.json`) instead of merging in-memory, so PID-less Codex sessions (those running with `[features] hooks` off, or started before the hook block was installed) flow through the standard sessions/state pipeline, get slot numbers, and auto-summary when the overlay sees the row transition to `done`. The summarizer is spawned by the overlay (not a hook) since these sessions have no hook to fire; the same `_should_summarize` cooldown applies.
- Codex rollout ownership is now explicit: hook-seen sessionIds are marked under `~/.claude/session-monitor/codex-hooked/`, subagent/nested rollouts are ignored, and ignored rollouts only remove monitor-generated virtual row files. Source rollout JSONL files are never deleted.
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
- `session-monitor.py`: add Phase 1c global WindowsTerminal scan in `find_window_for_pid()`
- `session-monitor.py`: filter out "Program Manager" from window candidates

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
- Configurable via `~/.claude/session-monitor/config.json`
- `SESSION_MONITOR_STATE_DIR` environment variable for custom state directory
- Claude Code Plugin support (hooks.json + slash command)
- Standalone install/uninstall scripts with settings.json merge
- `--version` flag
