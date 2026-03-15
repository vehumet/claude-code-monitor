#!/usr/bin/env python3
"""Standalone installer for Claude Code Monitor.

Copies files to ~/.claude/monitor/ and merges hooks into settings.json.
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
CLAUDE_DIR = os.path.join(HOME, ".claude")
MONITOR_DIR = os.path.join(CLAUDE_DIR, "monitor")
HOOKS_DIR = os.path.join(CLAUDE_DIR, "hooks")
STATE_DIR = os.path.join(MONITOR_DIR, "state")
COMMANDS_DIR = os.path.join(CLAUDE_DIR, "commands")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")

# Files to copy
SRC_DIR = os.path.join(SCRIPT_DIR, "src")
MONITOR_FILES = [
    "claude-code-monitor.py",
    "start-monitor.vbs",
    "start.sh",
]
HOOK_FILES = [
    "write-state.py",
]
COMMAND_FILES = [
    ("commands", "monitor.md"),
]

# Hook definitions to merge into settings.json
HOOKS_CONFIG = {
    "PreToolUse": [
        {
            "matcher": "AskUserQuestion",
            "hooks": [
                {
                    "type": "command",
                    "command": 'python "$HOME/.claude/hooks/write-state.py" "question"',
                    "timeout": 5,
                }
            ],
        },
        {
            "matcher": "ExitPlanMode",
            "hooks": [
                {
                    "type": "command",
                    "command": 'python "$HOME/.claude/hooks/write-state.py" "question"',
                    "timeout": 5,
                }
            ],
        },
    ],
    "PostToolUse": [
        {
            "matcher": "AskUserQuestion",
            "hooks": [
                {
                    "type": "command",
                    "command": 'python "$HOME/.claude/hooks/write-state.py" "working"',
                    "timeout": 5,
                }
            ],
        },
        {
            "matcher": "ExitPlanMode",
            "hooks": [
                {
                    "type": "command",
                    "command": 'python "$HOME/.claude/hooks/write-state.py" "working"',
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
                    "command": 'python "$HOME/.claude/hooks/write-state.py" "done"',
                    "timeout": 10,
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
                    "command": 'python "$HOME/.claude/hooks/write-state.py" "working"',
                    "timeout": 5,
                }
            ],
        },
    ],
}


def _has_write_state_hook(hook_entry: dict) -> bool:
    """Check if a hook entry references write-state.py."""
    for h in hook_entry.get("hooks", []):
        cmd = h.get("command", "")
        if "write-state.py" in cmd:
            return True
    return False


def merge_hooks(settings: dict) -> bool:
    """Merge monitor hooks into settings, skipping duplicates. Returns True if modified."""
    if "hooks" not in settings:
        settings["hooks"] = {}

    modified = False
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
                    break
            if not duplicate:
                existing.append(new_entry)
                modified = True

    return modified


def install(dry_run=False):
    print("Claude Code Monitor — Standalone Installer")
    print("=" * 45)
    print()

    # 1. Create directories
    for d in [MONITOR_DIR, HOOKS_DIR, STATE_DIR, COMMANDS_DIR]:
        if not os.path.isdir(d):
            print(f"  mkdir {d}")
            if not dry_run:
                os.makedirs(d, exist_ok=True)

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

    # 3. Copy hook files
    for fname in HOOK_FILES:
        src = os.path.join(SRC_DIR, fname)
        dst = os.path.join(HOOKS_DIR, fname)
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
            print(f"  {src} -> {dst}")
            if not dry_run:
                shutil.copy2(src, dst)
        else:
            print(f"  WARNING: {src} not found, skipping")

    # 5. Merge hooks into settings.json
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

    print()
    if dry_run:
        print("DRY RUN complete — no files were modified.")
    else:
        print("Installation complete!")
        print()
        print("Usage:")
        print("  Launch monitor:  /monitor  (in Claude Code)")
        print("  Or manually:     pythonw ~/.claude/monitor/claude-code-monitor.py")
        print()
        print("Configuration (optional):")
        print("  Create ~/.claude/monitor/config.json to customize:")
        print('  {"language": "ko", "opacity": 0.8, "sound_enabled": true}')


def main():
    dry_run = "--dry-run" in sys.argv
    install(dry_run)


if __name__ == "__main__":
    main()
