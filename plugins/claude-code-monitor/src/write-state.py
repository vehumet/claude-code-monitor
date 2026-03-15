#!/usr/bin/env python3
"""Claude Code hook: write instance state for the monitor overlay.

Usage (from hook script):
    echo "$INPUT" | python write-state.py <state>

States: working, done, question
"""
import json
import os
import sys
import time
import glob


def get_state_dir():
    """Return state directory path (env var > default)."""
    return os.environ.get(
        "CLAUDE_MONITOR_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".claude", "monitor", "state"),
    )


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    state = sys.argv[1]  # working / done / question
    home = os.path.expanduser("~")
    state_dir = get_state_dir()
    sessions_dir = os.path.join(home, ".claude", "sessions")

    os.makedirs(state_dir, exist_ok=True)

    # Read stdin JSON (hook event data)
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    session_id = hook_data.get("session_id", "")
    cwd = hook_data.get("cwd", "")

    # Find PID from session files by matching session_id or cwd
    pid = None
    matched_cwd = cwd

    for sf in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                sess = json.load(f)
        except Exception:
            continue

        basename = os.path.splitext(os.path.basename(sf))[0]

        # Match by session_id if available
        if session_id and sess.get("sessionId") == session_id:
            pid = sess.get("pid", int(basename))
            matched_cwd = sess.get("cwd", cwd)
            break

        # Fallback: match by cwd
        if cwd and sess.get("cwd") == cwd:
            pid = sess.get("pid", int(basename))
            matched_cwd = sess.get("cwd", cwd)
            break

    if pid is None:
        # Last resort: use the most recent session file
        session_files = sorted(
            glob.glob(os.path.join(sessions_dir, "*.json")),
            key=os.path.getmtime,
            reverse=True,
        )
        if session_files:
            try:
                basename = os.path.splitext(os.path.basename(session_files[0]))[0]
                pid = int(basename)
                with open(session_files[0], "r", encoding="utf-8") as f:
                    sess = json.load(f)
                    matched_cwd = sess.get("cwd", cwd)
            except Exception:
                sys.exit(0)
        else:
            sys.exit(0)

    # Write state file
    state_file = os.path.join(state_dir, f"{pid}.json")
    now = int(time.time())

    state_data = {
        "pid": pid,
        "state": state,
        "cwd": matched_cwd,
        "updatedAt": now,
    }

    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f)
    except Exception:
        pass


if __name__ == "__main__":
    main()
