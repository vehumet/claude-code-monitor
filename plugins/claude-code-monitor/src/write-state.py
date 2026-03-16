#!/usr/bin/env python3
"""Claude Code hook: write instance state for the monitor overlay.

Usage (from hook script):
    echo "$INPUT" | python write-state.py <state>

States: working, done, question
"""
import json
import logging
import logging.handlers
import os
import sys
import time
import glob
import ctypes
import ctypes.wintypes as wintypes

IS_WINDOWS = sys.platform == "win32"

# ── Windows constants (for process tree) ─────────────────────────
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


# ── Logger ───────────────────────────────────────────────────────

def _setup_logger():
    log_dir = os.path.join(os.path.expanduser("~"), ".claude", "monitor")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("write_state")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "write-state.log"),
            maxBytes=524_288, backupCount=1, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
    return logger


_log = _setup_logger()


# ── Helpers ──────────────────────────────────────────────────────

def _norm_path(p):
    """Normalize path for comparison (case-insensitive on Windows)."""
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


def _build_process_tree():
    """Return dict mapping pid -> parent_pid. Windows only."""
    if not IS_WINDOWS:
        return {}
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return {}
    tree = {}
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        if kernel32.Process32First(snap, ctypes.byref(pe)):
            while True:
                tree[pe.th32ProcessID] = pe.th32ParentProcessID
                if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return tree


def _get_ancestor_pids(my_pid, tree):
    """Return set of ancestor PIDs (including my_pid itself)."""
    ancestors = set()
    pid = my_pid
    visited = set()
    while pid and pid not in visited:
        visited.add(pid)
        ancestors.add(pid)
        pid = tree.get(pid)
    return ancestors


def _capture_foreground_hwnd(my_pid, tree):
    """Capture foreground window HWND if it belongs to an ancestor process."""
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        ancestors = _get_ancestor_pids(my_pid, tree)
        if owner_pid.value in ancestors:
            return hwnd
        return None
    except Exception:
        return None


def get_state_dir():
    """Return state directory path (env var > default)."""
    return os.environ.get(
        "CLAUDE_MONITOR_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".claude", "monitor", "state"),
    )


def _load_sessions(sessions_dir):
    """Load all session files. Returns list of (filepath, basename, data) tuples."""
    sessions = []
    for sf in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            basename = os.path.splitext(os.path.basename(sf))[0]
            sessions.append((sf, basename, data))
        except Exception:
            continue
    return sessions


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    state = sys.argv[1]  # working / done / question
    home = os.path.expanduser("~")
    state_dir = get_state_dir()
    sessions_dir = os.path.join(home, ".claude", "sessions")
    my_pid = os.getpid()

    os.makedirs(state_dir, exist_ok=True)

    # Read stdin JSON (hook event data)
    try:
        raw = sys.stdin.read()
        hook_data = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_data = {}

    # Defensive: accept both session_id and sessionId
    session_id = hook_data.get("session_id") or hook_data.get("sessionId") or ""
    cwd = hook_data.get("cwd", "")
    norm_cwd = _norm_path(cwd)

    _log.debug("=== write-state invoked: state=%s session_id=%s cwd=%s my_pid=%d",
               state, session_id, cwd, my_pid)

    # Phase 1: Load all session files
    sessions = _load_sessions(sessions_dir)
    _log.debug("Phase 1: loaded %d session files", len(sessions))

    pid = None
    matched_cwd = cwd
    tree = _build_process_tree()

    # Phase 2: Match by session_id (full scan)
    if session_id:
        for sf, basename, sess in sessions:
            if sess.get("sessionId") == session_id:
                pid = sess.get("pid", int(basename))
                matched_cwd = sess.get("cwd", cwd)
                _log.debug("Phase 2: session_id match -> pid=%s file=%s", pid, basename)
                break
        if pid is None:
            _log.debug("Phase 2: no session_id match found")

    # Phase 3: Match by cwd (only if session_id didn't match)
    if pid is None and cwd:
        cwd_matches = [
            (sf, basename, sess) for sf, basename, sess in sessions
            if _norm_path(sess.get("cwd", "")) == norm_cwd
        ]
        _log.debug("Phase 3: cwd matches = %d", len(cwd_matches))

        if len(cwd_matches) == 1:
            sf, basename, sess = cwd_matches[0]
            pid = sess.get("pid", int(basename))
            matched_cwd = sess.get("cwd", cwd)
            _log.debug("Phase 3: unique cwd match -> pid=%s", pid)

        elif len(cwd_matches) > 1:
            # Multiple sessions with same cwd — use process tree to disambiguate
            _log.debug("Phase 3: %d sessions share cwd, trying ancestor matching",
                       len(cwd_matches))
            ancestors = _get_ancestor_pids(my_pid, tree)
            _log.debug("Phase 3: my ancestors = %s", ancestors)

            ancestor_match = None
            for sf, basename, sess in cwd_matches:
                sess_pid = sess.get("pid", int(basename))
                if sess_pid in ancestors:
                    ancestor_match = (sf, basename, sess, sess_pid)
                    _log.debug("Phase 3: ancestor match -> sess_pid=%d", sess_pid)
                    break

            if ancestor_match:
                sf, basename, sess, sess_pid = ancestor_match
                pid = sess_pid
                matched_cwd = sess.get("cwd", cwd)
            else:
                # Fallback: pick the session with the most recent startedAt
                _log.debug("Phase 3: no ancestor match, falling back to most recent startedAt")
                cwd_matches.sort(
                    key=lambda x: x[2].get("startedAt", ""),
                    reverse=True,
                )
                sf, basename, sess = cwd_matches[0]
                pid = sess.get("pid", int(basename))
                matched_cwd = sess.get("cwd", cwd)
                _log.debug("Phase 3: startedAt fallback -> pid=%s", pid)

    # Phase 4: Last resort — most recent session file
    if pid is None:
        _log.debug("Phase 4: no match yet, falling back to most recent session file")
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
                _log.debug("Phase 4: fallback -> pid=%s", pid)
            except Exception:
                _log.debug("Phase 4: failed to read fallback session file")
                sys.exit(0)
        else:
            _log.debug("Phase 4: no session files found")
            sys.exit(0)

    # Write state file
    state_file = os.path.join(state_dir, f"{pid}.json")
    now = int(time.time())

    captured_hwnd = _capture_foreground_hwnd(my_pid, tree)
    if captured_hwnd is None:
        # 포커스가 IDE가 아닐 때 기존 HWND 보존
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
            captured_hwnd = existing.get("hwnd")
        except Exception:
            captured_hwnd = None

    state_data = {
        "pid": pid,
        "state": state,
        "cwd": matched_cwd,
        "updatedAt": now,
    }
    if captured_hwnd is not None:
        state_data["hwnd"] = captured_hwnd

    _log.debug("=> Writing state: pid=%s state=%s file=%s", pid, state, state_file)

    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f)
    except Exception:
        _log.error("Failed to write state file %s", state_file, exc_info=True)


if __name__ == "__main__":
    main()
