#!/usr/bin/env python3
"""Python installer for Session Monitor.

Copies runtime files to ~/.local/share/session-monitor/ and merges hooks into
Claude Code / Codex settings.
No external dependencies — stdlib only.

Usage:
    python install.py           # install
    python install.py --dry-run # preview without changes
"""

import json
import os
import shutil
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
RUNTIME_DIR = os.environ.get("SESSION_MONITOR_ROOT") or os.path.join(
    HOME, ".local", "share", "session-monitor"
)
CLAUDE_DIR = os.path.join(HOME, ".claude")
MONITOR_DIR = RUNTIME_DIR
LEGACY_MONITOR_DIR = os.path.join(CLAUDE_DIR, "session-monitor")
LEGACY_HOOKS_DIR = os.path.join(CLAUDE_DIR, "hooks")
STATE_DIR = os.path.join(MONITOR_DIR, "state")
SESSIONS_DIR = os.path.join(MONITOR_DIR, "sessions")
LOGS_DIR = os.path.join(MONITOR_DIR, "logs")
COMMANDS_DIR = os.path.join(CLAUDE_DIR, "commands")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")
CODEX_DIR = os.path.join(HOME, ".codex")
CODEX_CONFIG = os.path.join(CODEX_DIR, "config.toml")
CODEX_SKILLS_DIR = os.path.join(CODEX_DIR, "skills")

# Files to copy
SRC_DIR = os.path.join(SCRIPT_DIR, "src")
MONITOR_FILES = [
    "session_monitor_paths.py",
    "session-monitor.py",
    "codex_rollout_poller.py",
    "start-session-monitor.py",
    "start.sh",
]
HOOK_FILES = [
    "write-state.py",
]
COMMAND_FILES = [
    ("commands", "session-monitor.md"),
]
MANAGED_COMMAND_MARKER = "<!-- session-monitor:managed-command -->"
CODEX_SKILLS_SRC = os.path.join(SCRIPT_DIR, "codex-skills")
CODEX_SKILLS = [
    "session-monitor",
]

# Codex hooks ─ injected into ~/.codex/config.toml inside marker fences so we
# can detect prior installs and uninstall cleanly. Codex hooks have no
# ${CLAUDE_PLUGIN_ROOT} expansion, so the command is rendered with the
# absolute write-state.py path resolved at install time.
CODEX_HOOK_MARKER_OPEN = "# >>> session-monitor codex hooks (managed) >>>"
CODEX_HOOK_MARKER_CLOSE = "# <<< session-monitor codex hooks (managed) <<<"
CODEX_FEATURE_FLAG_TAG = "added by session-monitor"
CODEX_HOOK_EVENTS = [
    ("SessionStart", "session_start"),
    ("UserPromptSubmit", "working"),
    ("PermissionRequest", "question"),
    ("Stop", "done"),
]


def _shell_hook_path(path: str) -> str:
    path_abs = os.path.abspath(path)
    home_abs = os.path.abspath(HOME)
    try:
        rel = os.path.relpath(path_abs, home_abs)
    except ValueError:
        rel = path_abs
    if not rel.startswith("..") and rel != os.curdir and not os.path.isabs(rel):
        return "$HOME/" + rel.replace("\\", "/")
    return path_abs.replace("\\", "/")


WRITE_STATE_HOOK_PATH = _shell_hook_path(os.path.join(MONITOR_DIR, "write-state.py"))

# Hook definitions to merge into settings.json
HOOKS_CONFIG = {
    "PreToolUse": [
        {
            "matcher": "AskUserQuestion",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "question"',
                    "timeout": 5,
                }
            ],
        },
        {
            "matcher": "ExitPlanMode",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "question"',
                    "timeout": 5,
                }
            ],
        },
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "done"',
                    "timeout": 10,
                }
            ],
        },
    ],
    "StopFailure": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "interrupted"',
                    "timeout": 5,
                }
            ],
        },
    ],
    "PostToolUseFailure": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "tool_failure"',
                    "timeout": 5,
                }
            ],
        },
    ],
    "Notification": [
        {
            "matcher": "idle_prompt",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "idle_prompt"',
                    "timeout": 5,
                }
            ],
        },
        {
            "matcher": "permission_prompt",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "question"',
                    "timeout": 5,
                }
            ],
        },
    ],
    "UserPromptSubmit": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": f'python "{WRITE_STATE_HOOK_PATH}" --provider claude "working"',
                    "timeout": 5,
                }
            ],
        },
    ],
}


def _render_codex_hook_block() -> str:
    """Marker-fenced TOML for Codex's hooks.<Event> tables, ready to append."""
    # Forward slashes — Windows backslashes inside a TOML basic string would
    # be interpreted as escape sequences (`\U`, `\v`, …) and corrupt the
    # path. POSIX paths render identically and Windows accepts them.
    write_state_abs = os.path.join(MONITOR_DIR, "write-state.py").replace("\\", "/")
    lines = [
        CODEX_HOOK_MARKER_OPEN,
        "# Auto-generated by session-monitor install.py.",
        "# Run `python uninstall.py` (or delete this fenced block) to remove.",
        "",
    ]
    for event, state in CODEX_HOOK_EVENTS:
        lines.extend([
            f"[[hooks.{event}]]",
            f"[[hooks.{event}.hooks]]",
            'type = "command"',
            f'command = "python \\"{write_state_abs}\\" --provider codex {state}"',
            "",
        ])
    lines.append(CODEX_HOOK_MARKER_CLOSE)
    return "\n".join(lines)


def _replace_codex_hook_block(text: str, block: str):
    span = _find_codex_managed_block_span(text)
    if span is None:
        return text, False
    start, end = span
    old = text[start:end]
    preserved = _extract_codex_unmanaged_tail(old)
    replacement = block
    if preserved:
        replacement = f"{block}\n\n{preserved}"
    if old == replacement:
        return text, False
    return text[:start] + replacement + text[end:], True


def _find_codex_managed_block_span(text: str):
    """Return the byte span of our Codex hook block, including legacy shapes.

    Older installs accidentally placed the opening marker after
    ``[[hooks.SessionStart.hooks]]``. Replacing from the marker alone leaves an
    empty hook entry that Codex may reject. Users may also manually remove the
    opening marker while keeping the closing marker; treat that as managed too
    when the enclosed hooks clearly point at write-state.py.
    """
    close = text.find(CODEX_HOOK_MARKER_CLOSE)
    if close < 0:
        return None
    end = close + len(CODEX_HOOK_MARKER_CLOSE)
    open_idx = text.find(CODEX_HOOK_MARKER_OPEN)
    if 0 <= open_idx < close:
        start = _include_legacy_session_start_headers(text, open_idx)
        return start, end

    start = _find_codex_hook_block_start_before(text, close)
    if start is None:
        return None
    candidate = text[start:end]
    if "write-state.py" not in candidate:
        return None
    return start, end


def _include_legacy_session_start_headers(text: str, marker_idx: int) -> int:
    start = _find_codex_hook_block_start_before(text, marker_idx)
    if start is None:
        return marker_idx
    between = text[start:marker_idx]
    allowed = {"[[hooks.SessionStart]]", "[[hooks.SessionStart.hooks]]"}
    for line in between.splitlines():
        stripped = line.strip()
        if stripped and stripped not in allowed:
            return marker_idx
    return start


def _find_codex_hook_block_start_before(text: str, end_idx: int):
    token = "[[hooks.SessionStart]]"
    start = text.rfind("\n" + token, 0, end_idx)
    if start >= 0:
        return start + 1
    if text.startswith(token):
        return 0
    return None


def _extract_codex_unmanaged_tail(block: str) -> str:
    """Recover TOML sections Codex may have inserted inside our marker fence.

    Codex stores hook trust under [hooks.state]. If that lands before our
    closing marker, a plain marker-block replacement would delete the user's
    trusted_hash entries and trigger approval prompts again.
    """
    allowed_sections = {
        f"[[hooks.{event}]]" for event, _ in CODEX_HOOK_EVENTS
    } | {
        f"[[hooks.{event}.hooks]]" for event, _ in CODEX_HOOK_EVENTS
    }
    lines = block.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == CODEX_HOOK_MARKER_OPEN:
            continue
        if stripped == CODEX_HOOK_MARKER_CLOSE:
            break
        if stripped.startswith("[") and stripped not in allowed_sections:
            tail = "\n".join(lines[i:])
            close_idx = tail.find(CODEX_HOOK_MARKER_CLOSE)
            if close_idx >= 0:
                tail = tail[:close_idx]
            return tail.strip()
    return ""


def _ensure_codex_hooks_flag(text: str):
    """Ensure `hooks = true` is set inside the `[features]` section, and
    drop the deprecated `codex_hooks = true` line if present (Codex 0.128+
    warns on the old name). Returns (new_text, modified, has_features).

    When `[features]` is missing the caller appends a fresh `[features]`
    block alongside the marker block. We never insert a second `[features]`
    header — TOML treats that as a redefinition error.
    """
    lines = text.splitlines()
    in_features = False
    has_new_flag = False
    deprecated_idxs = []
    feature_header_idx = None
    has_features_section = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s == "[features]":
            in_features = True
            has_features_section = True
            feature_header_idx = i
            continue
        if in_features:
            if s.startswith("[") and not s.startswith("[["):
                in_features = False
                continue
            content = s.split("#", 1)[0].strip()
            if content.startswith("hooks") and "=" in content:
                has_new_flag = True
            elif content.startswith("codex_hooks") and "=" in content:
                deprecated_idxs.append(i)

    if has_new_flag and not deprecated_idxs:
        return text, False, has_features_section
    if not has_features_section:
        return text, False, False

    new_lines = list(lines)
    # Drop deprecated lines first (highest index first so earlier indices
    # remain valid) and adjust feature_header_idx if any precede it.
    for idx in sorted(deprecated_idxs, reverse=True):
        del new_lines[idx]
        if feature_header_idx is not None and idx < feature_header_idx:
            feature_header_idx -= 1
    if not has_new_flag and feature_header_idx is not None:
        new_lines.insert(
            feature_header_idx + 1,
            f"hooks = true  # {CODEX_FEATURE_FLAG_TAG}",
        )
    rebuilt = "\n".join(new_lines)
    if text.endswith("\n"):
        rebuilt += "\n"
    return rebuilt, True, True


def merge_codex_hooks(dry_run=False) -> bool:
    """Install Codex hook marker block + feature flag. Returns True if
    config.toml was modified (or would be, in dry-run)."""
    if not os.path.isdir(CODEX_DIR):
        print("  ~/.codex not found - skipping Codex hooks.")
        return False
    text = ""
    if os.path.exists(CODEX_CONFIG):
        try:
            with open(CODEX_CONFIG, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"  WARNING: failed to read {CODEX_CONFIG}: {e}")
            return False
    if _find_codex_managed_block_span(text) is not None:
        # Hook block already injected. Still migrate the feature-flag line if
        # it's on the deprecated `codex_hooks` name (Codex 0.128+ warns),
        # and refresh the managed block when our default hook set changes.
        migrated, modified, _ = _ensure_codex_hooks_flag(text)
        updated, block_modified = _replace_codex_hook_block(
            migrated, _render_codex_hook_block()
        )
        if not modified and not block_modified:
            print("  Codex hooks marker already present - skipping (idempotent).")
            return False
        if modified and block_modified:
            print(f"  Updating Codex hooks and feature flag in {CODEX_CONFIG}")
        elif modified:
            print(f"  Migrating deprecated codex_hooks flag -> hooks in {CODEX_CONFIG}")
        else:
            print(f"  Updating Codex hooks in {CODEX_CONFIG}")
        if dry_run:
            return True
        ts = int(time.time())
        backup = f"{CODEX_CONFIG}.bak.{ts}"
        shutil.copy2(CODEX_CONFIG, backup)
        print(f"  Backup: {backup}")
        with open(CODEX_CONFIG, "w", encoding="utf-8") as f:
            f.write(updated)
        print("  Codex hooks updated.")
        return True

    text2, in_place_modified, has_features = _ensure_codex_hooks_flag(text)
    # _ensure handles the in-place case when [features] exists; we only need
    # a fresh [features] block when the section is missing entirely.
    needs_features_block = not has_features

    appended = []
    if text2 and not text2.endswith("\n"):
        appended.append("")
    appended.append("")
    if needs_features_block:
        appended.extend(["[features]", f"hooks = true  # {CODEX_FEATURE_FLAG_TAG}", ""])
    appended.append(_render_codex_hook_block())
    appended.append("")
    new_text = text2 + "\n".join(appended)

    print(f"  Adding Codex hooks to {CODEX_CONFIG}")
    if dry_run:
        return True
    if os.path.exists(CODEX_CONFIG):
        ts = int(time.time())
        backup = f"{CODEX_CONFIG}.bak.{ts}"
        shutil.copy2(CODEX_CONFIG, backup)
        print(f"  Backup: {backup}")
    os.makedirs(CODEX_DIR, exist_ok=True)
    with open(CODEX_CONFIG, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("  Codex hooks installed.")
    return True


def _has_write_state_hook(hook_entry: dict) -> bool:
    """Check if a hook entry references write-state.py."""
    for h in hook_entry.get("hooks", []):
        cmd = h.get("command", "")
        if "write-state.py" in cmd:
            return True
    return False


def _is_managed_command_file(path: str) -> bool:
    """Return True only for command files owned by this installer.

    Older installs did not include MANAGED_COMMAND_MARKER, so keep a narrow
    signature to avoid overwriting unrelated user commands.
    """
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    if MANAGED_COMMAND_MARKER in text:
        return True
    return (
        "start-session-monitor.py" in text
        and "Run this single command immediately" in text
        and "Do NOT check if it's running beforehand" in text
        and "Session monitor launched." in text
    )


def _sync_write_state_hook(existing_entry: dict, desired_entry: dict) -> bool:
    """Update our managed hook command in place when installer defaults change."""
    modified = False
    desired_hooks = desired_entry.get("hooks", [])
    if len(desired_hooks) != 1:
        return False
    desired_hook = desired_hooks[0]
    for hook in existing_entry.get("hooks", []):
        if "write-state.py" not in hook.get("command", ""):
            continue
        if hook != desired_hook:
            hook.clear()
            hook.update(desired_hook)
            modified = True
    return modified


def _merge_copytree(src: str, dst: str):
    if not os.path.isdir(src):
        return 0
    copied = 0
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        src_path = os.path.join(src, name)
        dst_path = os.path.join(dst, name)
        if os.path.isdir(src_path):
            copied += _merge_copytree(src_path, dst_path)
        elif not os.path.exists(dst_path):
            shutil.copy2(src_path, dst_path)
            copied += 1
    return copied


def migrate_legacy_runtime(dry_run=False):
    """Copy existing ~/.claude/session-monitor data into the neutral runtime."""
    if not os.path.isdir(LEGACY_MONITOR_DIR):
        return
    if os.path.normcase(os.path.abspath(LEGACY_MONITOR_DIR)) == os.path.normcase(os.path.abspath(MONITOR_DIR)):
        return
    if dry_run:
        print()
        print("Migrating legacy runtime data...")
        print(f"  {LEGACY_MONITOR_DIR} -> {MONITOR_DIR}")
        return
    os.makedirs(MONITOR_DIR, exist_ok=True)
    copied = _merge_copytree(LEGACY_MONITOR_DIR, MONITOR_DIR)
    if copied:
        print()
        print("Migrating legacy runtime data...")
        print(f"  {LEGACY_MONITOR_DIR} -> {MONITOR_DIR} ({copied} files)")


def merge_hooks(settings: dict) -> bool:
    """Merge monitor hooks into settings, skipping duplicates. Returns True if modified."""
    if "hooks" not in settings:
        settings["hooks"] = {}

    modified = False
    # Older installs may have PostToolUse hooks that clear question state by
    # writing "working". Question clearing now happens in the overlay via
    # file-change polling; PostToolUse can be too early for ExitPlanMode
    # approval prompts, so remove only our managed entries.
    for event_name in ("PreToolUse", "PostToolUse"):
        existing = settings["hooks"].get(event_name)
        if not isinstance(existing, list):
            continue
        deprecated_matchers = {""}
        if event_name == "PostToolUse":
            deprecated_matchers.update({"AskUserQuestion", "ExitPlanMode"})
        pruned = [
            entry for entry in existing
            if not (
                entry.get("matcher", "") in deprecated_matchers
                and _has_write_state_hook(entry)
            )
        ]
        if len(pruned) != len(existing):
            settings["hooks"][event_name] = pruned
            modified = True

    for event_name, hook_entries in HOOKS_CONFIG.items():
        if event_name not in settings["hooks"]:
            settings["hooks"][event_name] = []

        existing = settings["hooks"][event_name]

        for new_entry in hook_entries:
            # Check if an equivalent hook already exists
            duplicate = False
            for ex_entry in existing:
                if ex_entry.get("matcher") == new_entry.get("matcher") and _has_write_state_hook(ex_entry):
                    duplicate = True
                    if _sync_write_state_hook(ex_entry, new_entry):
                        modified = True
                    break
            if not duplicate:
                existing.append(new_entry)
                modified = True

    return modified


def install(dry_run=False, skip_codex_hooks=False):
    print("Session Monitor - Python Installer")
    print("=" * 45)
    print()

    # 1. Create directories
    for d in [MONITOR_DIR, STATE_DIR, SESSIONS_DIR, LOGS_DIR, COMMANDS_DIR]:
        if not os.path.isdir(d):
            print(f"  mkdir {d}")
            if not dry_run:
                os.makedirs(d, exist_ok=True)

    migrate_legacy_runtime(dry_run=dry_run)

    # 2. Copy monitor files
    print()
    print("Copying files...")
    for fname in MONITOR_FILES:
        src = os.path.join(SRC_DIR, fname)
        dst = os.path.join(MONITOR_DIR, fname)
        if os.path.exists(src):
            print(f"  {src} -> {dst}")
            if not dry_run:
                shutil.copy2(src, dst)
        else:
            print(f"  WARNING: {src} not found, skipping")

    # 3. Copy hook files into the neutral runtime
    for fname in HOOK_FILES:
        src = os.path.join(SRC_DIR, fname)
        dst = os.path.join(MONITOR_DIR, fname)
        if os.path.exists(src):
            print(f"  {src} -> {dst}")
            if not dry_run:
                shutil.copy2(src, dst)
        else:
            print(f"  WARNING: {src} not found, skipping")

    # 4. Copy command files
    for subdir, fname in COMMAND_FILES:
        src = os.path.join(SCRIPT_DIR, subdir, fname)
        dst = os.path.join(COMMANDS_DIR, fname)
        if os.path.exists(src):
            if os.path.exists(dst) and not _is_managed_command_file(dst):
                print(f"  WARNING: {dst} exists and is not managed by session-monitor; skipping")
                continue
            print(f"  {src} -> {dst}")
            if not dry_run:
                shutil.copy2(src, dst)
        else:
            print(f"  WARNING: {src} not found, skipping")

    # 5. Copy Codex skill, when Codex is present
    print()
    print("Codex CLI skill...")
    if not os.path.isdir(CODEX_DIR):
        print("  ~/.codex not found - skipping Codex skill.")
    else:
        for skill_name in CODEX_SKILLS:
            src_root = os.path.join(CODEX_SKILLS_SRC, skill_name)
            dst_root = os.path.join(CODEX_SKILLS_DIR, skill_name)
            if not os.path.isdir(src_root):
                print(f"  WARNING: {src_root} not found, skipping")
                continue
            for root, _dirs, files in os.walk(src_root):
                rel_dir = os.path.relpath(root, src_root)
                dst_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)
                if not dry_run:
                    os.makedirs(dst_dir, exist_ok=True)
                for fname in files:
                    src = os.path.join(root, fname)
                    dst = os.path.join(dst_dir, fname)
                    print(f"  {src} -> {dst}")
                    if not dry_run:
                        shutil.copy2(src, dst)
    # 6. Merge hooks into settings.json
    print()
    print("Merging hooks into settings.json...")
    settings = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: Failed to read {SETTINGS_FILE}: {e}")
            print("  Creating new settings.json")

    modified = merge_hooks(settings)

    if modified:
        if not dry_run:
            # Backup before writing
            ts = int(time.time())
            backup = f"{SETTINGS_FILE}.bak.{ts}"
            if os.path.exists(SETTINGS_FILE):
                shutil.copy2(SETTINGS_FILE, backup)
                print(f"  Backup: {backup}")
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
        print("  Hooks merged successfully.")
    else:
        print("  Hooks already present, no changes needed.")

    # 7. Codex CLI hooks (optional)
    print()
    print("Codex CLI hooks...")
    if skip_codex_hooks:
        print("  --skip-codex-hooks set - leaving ~/.codex/config.toml alone.")
    else:
        merge_codex_hooks(dry_run=dry_run)

    print()
    if dry_run:
        print("DRY RUN complete - no files were modified.")
    else:
        print("Installation complete!")
        print()
        print("Usage:")
        print("  Launch in Claude Code: /session-monitor  (after restarting Claude Code)")
        print("  Launch in Codex: $session-monitor  (after restarting Codex)")
        print("  Or manually:     python ~/.local/share/session-monitor/start-session-monitor.py")
        print()
        print("Configuration (optional):")
        print("  Create ~/.local/share/session-monitor/config.json to customize:")
        print('  {"language": "ko", "opacity": 0.8, "sound_enabled": true}')


def main():
    dry_run = "--dry-run" in sys.argv
    skip_codex_hooks = "--skip-codex-hooks" in sys.argv
    install(dry_run, skip_codex_hooks)


if __name__ == "__main__":
    main()
