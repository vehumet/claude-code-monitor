#!/usr/bin/env python3
"""Uninstaller for Claude Code Monitor.

Removes installed files and cleans up hooks from settings.json.
No external dependencies — stdlib only.

Usage:
    python uninstall.py           # uninstall
    python uninstall.py --dry-run # preview without changes
    python uninstall.py --keep-config  # preserve config.json and position.json
"""

import json
import os
import shutil
import sys
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
MONITOR_DIR = os.path.join(CLAUDE_DIR, "monitor")
HOOKS_DIR = os.path.join(CLAUDE_DIR, "hooks")
COMMANDS_DIR = os.path.join(CLAUDE_DIR, "commands")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")
CODEX_CONFIG = os.path.join(HOME, ".codex", "config.toml")
CODEX_HOOK_MARKER_OPEN = "# >>> claude-code-monitor codex hooks (managed) >>>"
CODEX_HOOK_MARKER_CLOSE = "# <<< claude-code-monitor codex hooks (managed) <<<"
CODEX_FEATURE_FLAG_TAG = "added by claude-code-monitor"

# Files that may be preserved
CONFIG_FILES = {"config.json", "position.json"}

# Files to remove
HOOK_FILES = [os.path.join(HOOKS_DIR, "write-state.py")]
COMMAND_FILES = [os.path.join(COMMANDS_DIR, "monitor.md")]


def _has_write_state_hook(hook_entry: dict) -> bool:
    """Check if a hook entry references write-state.py."""
    for h in hook_entry.get("hooks", []):
        cmd = h.get("command", "")
        if "write-state.py" in cmd:
            return True
    return False


def remove_codex_hooks(dry_run=False) -> bool:
    """Strip the marker-fenced Codex hook block + the feature flag line we
    inserted. Other content in config.toml is left untouched. Returns True
    if any change was made (or would be, in dry-run)."""
    if not os.path.exists(CODEX_CONFIG):
        print("  (~/.codex/config.toml not found)")
        return False
    try:
        with open(CODEX_CONFIG, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"  WARNING: failed to read {CODEX_CONFIG}: {e}")
        return False

    has_marker = CODEX_HOOK_MARKER_OPEN in text
    lines = text.splitlines(keepends=True)

    out = []
    skip = False
    removed_marker = False
    for ln in lines:
        s = ln.strip()
        if s == CODEX_HOOK_MARKER_OPEN:
            skip = True
            removed_marker = True
            continue
        if s == CODEX_HOOK_MARKER_CLOSE:
            skip = False
            continue
        if skip:
            continue
        # Drop the feature-flag line we inserted into [features]; we tag it
        # with a comment marker precisely so this line-match is unambiguous.
        # Both the new key (`hooks`) and the deprecated one (`codex_hooks`)
        # are caught here so users on either name uninstall cleanly.
        pre_comment = ln.split("#", 1)[0]
        flag_key = pre_comment.split("=", 1)[0].strip() if "=" in pre_comment else ""
        if flag_key in ("hooks", "codex_hooks") and CODEX_FEATURE_FLAG_TAG in ln:
            removed_marker = True
            continue
        out.append(ln)

    if not removed_marker and not has_marker:
        print("  No Codex monitor hooks found in config.toml.")
        return False

    new_text = "".join(out)
    # Collapse any triple-blank-line gaps left behind by the removed block.
    while "\n\n\n" in new_text:
        new_text = new_text.replace("\n\n\n", "\n\n")

    print(f"  Removing Codex hooks from {CODEX_CONFIG}")
    if dry_run:
        return True
    ts = int(time.time())
    backup = f"{CODEX_CONFIG}.bak.{ts}"
    shutil.copy2(CODEX_CONFIG, backup)
    print(f"  Backup: {backup}")
    with open(CODEX_CONFIG, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def remove_hooks(settings: dict) -> bool:
    """Remove monitor hooks from settings. Returns True if modified."""
    hooks = settings.get("hooks", {})
    if not hooks:
        return False

    modified = False
    for event_name in list(hooks.keys()):
        original_len = len(hooks[event_name])
        hooks[event_name] = [
            entry for entry in hooks[event_name]
            if not _has_write_state_hook(entry)
        ]
        if len(hooks[event_name]) != original_len:
            modified = True
        # Remove empty event arrays
        if not hooks[event_name]:
            del hooks[event_name]
            modified = True

    # Remove empty hooks object
    if not hooks and "hooks" in settings:
        del settings["hooks"]
        modified = True

    return modified


def uninstall(dry_run=False, keep_config=False):
    print("Claude Code Monitor — Uninstaller")
    print("=" * 35)
    print()

    # 1. Remove hook files
    print("Removing hook files...")
    for fpath in HOOK_FILES:
        if os.path.exists(fpath):
            print(f"  rm {fpath}")
            if not dry_run:
                os.remove(fpath)
        else:
            print(f"  (not found) {fpath}")

    # 2. Remove command files
    print()
    print("Removing command files...")
    for fpath in COMMAND_FILES:
        if os.path.exists(fpath):
            print(f"  rm {fpath}")
            if not dry_run:
                os.remove(fpath)
        else:
            print(f"  (not found) {fpath}")

    # 3. Remove monitor directory
    print()
    print("Removing monitor directory...")
    if os.path.isdir(MONITOR_DIR):
        if keep_config:
            # Remove everything except config files
            for item in os.listdir(MONITOR_DIR):
                item_path = os.path.join(MONITOR_DIR, item)
                if item in CONFIG_FILES:
                    print(f"  (keeping) {item_path}")
                    continue
                if os.path.isdir(item_path):
                    print(f"  rmdir {item_path}")
                    if not dry_run:
                        shutil.rmtree(item_path)
                else:
                    print(f"  rm {item_path}")
                    if not dry_run:
                        os.remove(item_path)
        else:
            print(f"  rmdir {MONITOR_DIR}")
            if not dry_run:
                shutil.rmtree(MONITOR_DIR)
    else:
        print(f"  (not found) {MONITOR_DIR}")

    # 4. Clean hooks from settings.json
    print()
    print("Cleaning hooks from settings.json...")
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARNING: Failed to read {SETTINGS_FILE}: {e}")
            settings = None

        if settings is not None:
            modified = remove_hooks(settings)
            if modified:
                if not dry_run:
                    ts = int(time.time())
                    backup = f"{SETTINGS_FILE}.bak.{ts}"
                    shutil.copy2(SETTINGS_FILE, backup)
                    print(f"  Backup: {backup}")
                    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                        json.dump(settings, f, indent=2, ensure_ascii=False)
                        f.write("\n")
                print("  Hooks removed from settings.json.")
            else:
                print("  No monitor hooks found in settings.json.")
    else:
        print("  (not found) settings.json")

    # 5. Codex hooks (best-effort)
    print()
    print("Cleaning Codex hooks from ~/.codex/config.toml...")
    remove_codex_hooks(dry_run=dry_run)

    print()
    if dry_run:
        print("DRY RUN complete — no files were modified.")
    else:
        print("Uninstall complete!")
        if keep_config:
            print(f"  Config preserved in {MONITOR_DIR}")


def main():
    dry_run = "--dry-run" in sys.argv
    keep_config = "--keep-config" in sys.argv
    uninstall(dry_run, keep_config)


if __name__ == "__main__":
    main()
