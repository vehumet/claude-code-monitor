#!/usr/bin/env python3
"""Session Monitor overlay — always-on-top widget showing Claude Code and Codex sessions.

Usage:
    python  session-monitor.py      # with console
    pythonw session-monitor.py      # no console window (Windows)

No external dependencies — stdlib + tkinter + ctypes only.
"""

__version__ = "0.0.23"

import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import glob
import time
import threading
import tkinter as tk
import urllib.parse
import urllib.error
import urllib.request
from tkinter import font as tkfont

# codex_rollout_poller lives next to this file; src/ isn't always on
# sys.path when launched via the .vbs wrapper.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_monitor_paths import (
    config_file,
    logs_dir,
    pins_file,
    position_file,
    sessions_dir,
    state_dir as default_state_dir,
    write_state_file,
)
try:
    from codex_rollout_poller import poll_codex_rollouts, is_virtual_id
except ImportError:
    poll_codex_rollouts = None  # standalone fallback: degrade gracefully
    def is_virtual_id(_pid):  # type: ignore[no-redef]
        return False

IS_WINDOWS = sys.platform == "win32"

# ── Windows-only imports ───────────────────────────────────────────
if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wintypes
    import winsound

    # ── Windows constants ──────────────────────────────────────────
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    TH32CS_SNAPPROCESS = 0x00000002
    SW_RESTORE = 9

    # ── DPI awareness ──────────────────────────────────────────────
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # ── ctypes structures ──────────────────────────────────────────

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


# ── Diagnostic logger ─────────────────────────────────────────────

def _setup_logger():
    log_dir = logs_dir()
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("session_monitor")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "debug.log"),
            maxBytes=1_048_576, backupCount=1, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
    return logger

_log = _setup_logger()


# ── i18n ──────────────────────────────────────────────────────────
LABELS = {
    "en": {
        "working": "Working",
        "done": "Done",
        "question": "Waiting",
        "interrupted": "Interrupted",
        "idle": "Idle",
        "no_instances": "No instances",
    },
    "ko": {
        "working": "\uc791\uc5c5\uc911",
        "done": "\uc791\uc5c5\uc644\ub8cc",
        "question": "\uc9c8\ubb38\uc788\uc74c",
        "interrupted": "\uc911\ub2e8\ub428",
        "idle": "\ub300\uae30\uc911",
        "no_instances": "\uc778\uc2a4\ud134\uc2a4 \uc5c6\uc74c",
    },
}

# ── Theme ─────────────────────────────────────────────────────────
THEME = {
    "bg":          "#1e1e2e",
    "fg":          "#cdd6f4",
    "dim":         "#6c7086",
    "border":      "#313244",
    "title_bg":    "#181825",
    "working":     "#a6e3a1",   # green
    "done":        "#89b4fa",   # blue
    "question":    "#f9e2af",   # yellow
    "interrupted": "#fab387",   # peach/orange
    "idle":        "#585b70",   # grey
    "hover":       "#313244",
    "recent_bg":   "#303450",
    "status_watch": "#94e2d5",  # teal
    "close_hover": "#f38ba8",   # red
}

DONE_BLINK_SECONDS = 10  # "done"/"interrupted" blink this long, then stay solid
STARTUP_QUIET_SECONDS = 3  # suppress stale catch-up events after opening


# ── Config ────────────────────────────────────────────────────────

def _detect_system_language():
    """Best-effort detect 'ko' for Korean systems, otherwise 'en'.

    Tries Windows UI language → POSIX locale → LANG env var. Anything that
    isn't recognisably Korean falls through to English.
    """
    if IS_WINDOWS:
        try:
            # LCID low byte 0x12 = Korean (Hangul)
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            if (lcid & 0xff) == 0x12:
                return "ko"
        except Exception:
            pass
    try:
        import locale
        loc = (locale.getlocale()[0] or "").lower()
    except Exception:
        loc = ""
    if not loc:
        for v in ("LC_ALL", "LC_MESSAGES", "LANG"):
            val = os.environ.get(v, "")
            if val:
                loc = val.lower()
                break
    if "ko" in loc or "korean" in loc:
        return "ko"
    return "en"


def _default_config():
    return {
        "language": _detect_system_language(),
        "opacity": 0.65,
        # Number of Korean-width chars reserved for the summary column.
        # The widget width is derived from this; write-state.py reads the same
        # value to cap Haiku output to what will actually fit on screen.
        "summary_max_chars": 12,
        "poll_interval_ms": 500,
        "blink_interval_ms": 600,
        "blink_seconds": DONE_BLINK_SECONDS,
        "question_clear_grace_ms": 1000,
        "sound_enabled": True,
        "claude_status_watch_enabled": True,
        "claude_status_check_interval_s": 60,
        "claude_status_watch_ttl_s": 14400,
    }


def load_config():
    """Load config from the Session Monitor runtime dir, falling back to defaults."""
    config = _default_config()
    config_path = config_file()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user = json.load(f)
        for key in config:
            if key in user:
                config[key] = user[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return config


CONFIG = load_config()


def get_state_dir():
    """Return state directory path (env var > default)."""
    return default_state_dir()


def get_label(key):
    """Return localized label."""
    lang = CONFIG.get("language", "en")
    return LABELS.get(lang, LABELS["en"]).get(key, key)


CLAUDE_STATUS_SUMMARY_URL = "https://status.claude.com/api/v2/summary.json"


def _shorten_status_text(text: str, max_chars: int = 34) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "\u2026"


def summarize_claude_status(data: dict) -> tuple[bool, str]:
    """Return (is_operational, compact_label) for Statuspage summary JSON."""
    status = data.get("status") if isinstance(data, dict) else {}
    indicator = (status or {}).get("indicator") or "none"
    description = (status or {}).get("description") or ""
    incidents = data.get("incidents") if isinstance(data, dict) else []
    if not isinstance(incidents, list):
        incidents = []
    unresolved = [
        inc for inc in incidents
        if isinstance(inc, dict)
        and str(inc.get("status") or "").lower() not in ("resolved", "postmortem")
    ]

    if indicator == "none" and not unresolved:
        return True, "Claude status: operational"

    latest = None
    if unresolved:
        latest = max(unresolved, key=lambda inc: str(inc.get("updated_at") or ""))
    if latest:
        name = latest.get("name") or description or "incident"
        phase = latest.get("status") or indicator
        return False, f"Claude: {phase} - {_shorten_status_text(name)}"

    return False, f"Claude: {_shorten_status_text(description or indicator)}"


class ClaudeStatusWatcher:
    """On-demand Statuspage watcher, dormant until a Claude StopFailure."""

    def __init__(self):
        self.enabled = bool(CONFIG.get("claude_status_watch_enabled", True))
        self.url = CLAUDE_STATUS_SUMMARY_URL
        self.interval_s = max(15.0, float(CONFIG.get("claude_status_check_interval_s", 60)))
        self.ttl_s = max(self.interval_s, float(CONFIG.get("claude_status_watch_ttl_s", 14400)))
        self._lock = threading.Lock()
        self._thread = None
        self._active = False
        self._deadline = 0.0
        self._next_check = 0.0
        self._etag = None
        self._seen_issue = False
        self._label = ""
        self._color = THEME["dim"]
        self._restored_notice_until = 0.0
        self._restored_event_pending = False
        self._last_trigger_at = 0

    def trigger(self, interrupt_at: int):
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            if interrupt_at and interrupt_at <= self._last_trigger_at:
                return
            self._last_trigger_at = max(self._last_trigger_at, int(interrupt_at or time.time()))
            if not self._active:
                self._seen_issue = False
            self._active = True
            self._deadline = now + self.ttl_s
            self._next_check = 0.0
            self._label = "Claude status: checking"
            self._color = THEME["status_watch"]
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def snapshot(self) -> tuple[str, str, bool]:
        now = time.monotonic()
        with self._lock:
            if not self._active and now > self._restored_notice_until:
                label = ""
            else:
                label = self._label
            event = self._restored_event_pending
            self._restored_event_pending = False
            return label, self._color, event

    def _run(self):
        while True:
            wait_s = 1.0
            with self._lock:
                if not self._active:
                    return
                now = time.monotonic()
                if now >= self._deadline:
                    self._active = False
                    if not self._seen_issue:
                        self._label = ""
                    return
                if now < self._next_check:
                    wait_s = min(5.0, self._next_check - now)
                    do_check = False
                else:
                    do_check = True
            if not do_check:
                time.sleep(wait_s)
                continue
            self._check_once()

    def _check_once(self):
        headers = {
            "Accept": "application/json",
            "User-Agent": "session-monitor/claude-status-watch",
        }
        with self._lock:
            if self._etag:
                headers["If-None-Match"] = self._etag
        try:
            req = urllib.request.Request(self.url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read(128 * 1024).decode("utf-8", errors="replace")
                etag = resp.headers.get("ETag")
            data = json.loads(raw)
            operational, label = summarize_claude_status(data)
            self._record_result(operational, label, etag)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                self._schedule_next()
            else:
                self._record_error()
        except Exception:
            self._record_error()

    def _record_result(self, operational: bool, label: str, etag: str | None):
        with self._lock:
            if etag:
                self._etag = etag
            if operational:
                if self._seen_issue:
                    self._label = "Claude status: restored"
                    self._color = THEME["done"]
                    self._restored_notice_until = time.monotonic() + 60.0
                    self._restored_event_pending = True
                    self._active = False
                else:
                    self._label = label
                    self._color = THEME["dim"]
                    self._next_check = time.monotonic() + self.interval_s
            else:
                self._seen_issue = True
                self._label = label
                self._color = THEME["interrupted"]
                self._next_check = time.monotonic() + self.interval_s

    def _record_error(self):
        with self._lock:
            self._label = "Claude status: retrying"
            self._color = THEME["dim"]
            self._next_check = time.monotonic() + min(300.0, self.interval_s * 2)

    def _schedule_next(self):
        with self._lock:
            self._next_check = time.monotonic() + self.interval_s


QUESTION_CLEAR_GRACE_S = max(
    0.0, float(CONFIG.get("question_clear_grace_ms", 1000)) / 1000.0
)
_QUESTION_FIELDS = (
    "questionAt",
    "questionTranscriptPath", "questionTranscriptMtimeNs", "questionTranscriptSize",
    "questionSessionPath", "questionSessionMtimeNs", "questionSessionSize",
)


def _atomic_write_json(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _path_changed_since(path, old_mtime_ns, old_size):
    if not path:
        return False
    try:
        st = os.stat(os.path.expanduser(path))
    except OSError:
        return False
    try:
        return int(st.st_mtime_ns) != int(old_mtime_ns) or int(st.st_size) != int(old_size)
    except (TypeError, ValueError):
        return False


def _strip_question_fields(data):
    out = dict(data)
    for key in _QUESTION_FIELDS:
        out.pop(key, None)
    return out


def resolve_question_state_from_files(state_data, sessions_data=None, now=None):
    """Return a replacement state for stale `question`, or None to keep it.

    The expensive signal (hooking every PostToolUse) is replaced with cheap
    file metadata checks captured when `question` was written.
    """
    if state_data.get("state") != "question":
        return None
    question_at = state_data.get("questionAt")
    if not question_at:
        return None
    now = time.time() if now is None else now
    try:
        age = now - float(question_at)
    except (TypeError, ValueError):
        return None
    if age < QUESTION_CLEAR_GRACE_S:
        return None

    session_path = state_data.get("questionSessionPath")
    session_data = None
    if session_path and isinstance(sessions_data, dict):
        session_data = sessions_data.get(session_path)
    if session_data is None and session_path:
        session_data = _read_json_file(session_path)
    session_status = (session_data or {}).get("status")
    if str(session_status or "").lower() == "waiting" or (session_data or {}).get("waitingFor"):
        return None
    if session_status == "idle":
        return "done"

    transcript_changed = _path_changed_since(
        state_data.get("questionTranscriptPath"),
        state_data.get("questionTranscriptMtimeNs"),
        state_data.get("questionTranscriptSize"),
    )
    session_changed = _path_changed_since(
        session_path,
        state_data.get("questionSessionMtimeNs"),
        state_data.get("questionSessionSize"),
    )
    if transcript_changed or session_changed:
        return "working"
    return None


def session_waits_for_user(session_data) -> bool:
    """Return True when Claude's session metadata says it is awaiting input."""
    if not isinstance(session_data, dict):
        return False
    status = str(session_data.get("status") or "").lower()
    waiting_for = str(session_data.get("waitingFor") or "").strip()
    return status == "waiting" or bool(waiting_for)


# ── Helpers ───────────────────────────────────────────────────────

def is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but we lack permission


def _basename_provider(name: str):
    """Map a process basename to its provider id ('claude'/'codex') or None.

    Matches 'claude.exe' and the rename pattern Claude Code uses during
    self-update, e.g. 'claude.exe.old.1777936237180' (already-running sessions
    retain the .old.* path until they exit). Codex CLI installs as 'codex.exe'.
    """
    n = (name or "").lower()
    if n == "claude.exe" or n.startswith("claude.exe.old"):
        return "claude"
    if n == "codex.exe" or n == "codex":
        return "codex"
    return None


def _is_claude_basename(name: str) -> bool:
    """Back-compat: True iff basename is a Claude Code binary (live or .old).
    `_basename_provider` is the multi-provider canonical check; this remains
    for `is_nested_claude_pid` which is specifically scoped to Claude's
    summarizer process tree (Codex never spawns nested Haiku invocations)."""
    return _basename_provider(name) == "claude"


def known_llm_pid_provider(pid: int):
    """Return provider id ('claude'/'codex') if PID is a live LLM CLI, else None."""
    if not IS_WINDOWS:
        if not is_pid_alive(pid):
            return None
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
                comm = f.read().strip()
        except OSError:
            try:
                comm = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    stderr=subprocess.DEVNULL, timeout=1,
                ).decode("utf-8", errors="replace").strip()
            except Exception:
                comm = ""
        return _basename_provider(os.path.basename(comm)) or "claude"
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        ec = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(ec)) or ec.value != STILL_ACTIVE:
            return None
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return _basename_provider(os.path.basename(buf.value))
    finally:
        kernel32.CloseHandle(handle)


def is_claude_pid_alive(pid: int) -> bool:
    """True iff PID belongs to a live LLM CLI we recognise (claude or codex)."""
    return known_llm_pid_provider(pid) is not None


def is_codex_desktop_app_pid(pid: int, tree: dict) -> bool:
    """True for the Codex desktop app's app-server helper, not CLI sessions."""
    if not IS_WINDOWS or not isinstance(pid, int):
        return False
    entry = tree.get(pid)
    if not entry or entry[1] != "codex.exe":
        return False
    parent = tree.get(entry[0])
    return bool(parent and parent[1] == "codex.exe")


def load_nested_pids(state_root: str) -> set:
    """Load PIDs marked nested by our summarizer; clean up stale markers."""
    d = os.path.join(state_root, "nested-pids")
    if not os.path.isdir(d):
        return set()
    alive = set()
    for fn in os.listdir(d):
        if not fn.endswith(".flag"):
            continue
        try:
            pid = int(fn[:-5])
        except ValueError:
            continue
        if is_pid_alive(pid):
            alive.add(pid)
        else:
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass
    return alive


# Cooldown that mirrors write-state.py's _HAIKU_REFRESH_SECONDS so virtual
# rows obey the same refresh cadence Claude Code's Stop-hook path does.
_CODEX_SUMMARY_COOLDOWN_S = 300


def _should_spawn_codex_summary(state_data: dict) -> bool:
    """Same policy write-state.py applies on Stop hook fires."""
    src = state_data.get("summarySource")
    if src in (None, "", "trim"):
        return True
    if src in ("haiku", "codex_mini"):
        return time.time() - state_data.get("summaryAt", 0) >= _CODEX_SUMMARY_COOLDOWN_S
    return False


def _spawn_codex_summary(virtual_id: str):
    """Re-invoke write-state.py in __summarize__ mode for a PID-less row.

    Hook-registered Codex sessions handle this themselves (Stop hook →
    write-state.py main → _spawn_summarizer); PID-less rows have no hook,
    so the overlay calls the same script directly when it sees the row
    transition into 'done'.
    """
    write_state = write_state_file()
    if not os.path.exists(write_state):
        _log.debug("write-state.py not at %s; skipping codex summary spawn", write_state)
        return
    cmd = [sys.executable, write_state, "__summarize__", str(virtual_id)]
    env = os.environ.copy()
    env["SESSION_MONITOR_NESTED"] = "1"
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
        _log.debug("spawned codex summary for %s", virtual_id)
    except Exception:
        _log.error("failed to spawn codex summary", exc_info=True)


def is_nested_claude_pid(pid: int, tree: dict, marker_pids: set) -> bool:
    """True if PID is itself a marker or an ancestor is a marker / claude.exe.

    Used to filter out short-lived `claude -p` subprocesses spawned by our
    background summarizer — they create a sessions/*.json file but should
    never appear as a top-level user instance.
    """
    if pid in marker_pids:
        return True
    if not tree or pid not in tree:
        return False
    visited = set()
    cur = tree[pid][0]  # parent
    while cur and cur not in visited:
        visited.add(cur)
        if cur in marker_pids:
            return True
        entry = tree.get(cur)
        if not entry:
            break
        if _is_claude_basename(entry[1]):
            return True
        cur = entry[0]
    return False


def build_process_tree():
    """Return dict mapping pid -> (parent_pid, exe_name). Windows only."""
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
                try:
                    exe = pe.szExeFile.decode("utf-8", errors="replace")
                except Exception:
                    exe = ""
                tree[pe.th32ProcessID] = (pe.th32ParentProcessID, exe.lower())
                if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(snap)
    return tree


SKIP_COMPONENTS = frozenset({
    "src", "lib", "bin", "build", "dist", "users", "user", "home",
    "documents", "desktop", "projects", "repos", "workspace",
    "workspaces", "code", "dev", "c:", "d:", "e:", "",
})


def find_window_for_pid(target_pid: int, tree: dict, cwd: str = "") -> int | None:
    """Find the main visible window belonging to target_pid or any ancestor. Windows only."""
    if not IS_WINDOWS:
        return None

    user32 = ctypes.windll.user32

    # Collect ancestor chain (ordered: target first, root last)
    chain = []
    visited = set()
    pid = target_pid
    while pid and pid not in visited:
        visited.add(pid)
        chain.append(pid)
        entry = tree.get(pid)
        if not entry:
            break
        pid = entry[0]  # parent_pid

    _log.debug("find_window target=%d cwd=%s", target_pid, cwd)
    _log.debug("  ancestor chain: %s",
               [(p, tree.get(p, (None, "?"))[1]) for p in chain])

    pid_set = set(chain)
    direct_pid_set = set(pid_set)

    # Phase 1b: Add terminal host processes that are descendants of our chain.
    # Windows 11 terminal delegation: conhost.exe (child of shell) launches
    # WindowsTerminal.exe, so the actual window owner is outside the ancestor chain.
    TERMINAL_HOSTS = {"conhost.exe", "windowsterminal.exe", "openconsole.exe"}
    extra = set()
    for p, (parent, exe) in tree.items():
        if parent in pid_set and exe in TERMINAL_HOSTS:
            extra.add(p)
    # Second pass: WindowsTerminal.exe may be a child of conhost.exe
    for p, (parent, exe) in tree.items():
        if parent in extra and exe in TERMINAL_HOSTS:
            extra.add(p)
    pid_set |= extra
    direct_pid_set |= extra

    # Do not add every WindowsTerminal.exe as a broad fallback. It can raise
    # unrelated terminals whose titles happen to contain a shared path segment
    # (for example the username or another project under the same workspace).
    # If the terminal host is not in the process chain or a direct descendant,
    # we prefer doing nothing over focusing the wrong window.

    candidates = []  # list of (chain_index, owning_pid, hwnd, title)

    def enum_callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title_len = user32.GetWindowTextLengthW(hwnd)
        if title_len <= 0:
            return True
        w_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
        if w_pid.value in pid_set:
            try:
                idx = chain.index(w_pid.value)
            except ValueError:
                idx = len(chain)
            buf = ctypes.create_unicode_buffer(title_len + 1)
            user32.GetWindowTextW(hwnd, buf, title_len + 1)
            candidates.append((idx, w_pid.value, hwnd, buf.value))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)

    # Filter out "Program Manager" (explorer.exe desktop window — never correct)
    candidates = [c for c in candidates if c[3] != "Program Manager"]

    _log.debug("  all candidates (%d):", len(candidates))
    for c in candidates:
        _log.debug("    chain_idx=%d pid=%d hwnd=%d title=%r", *c)

    if not candidates:
        _log.debug("  => no candidates found")
        return None

    direct_candidates = [c for c in candidates if c[1] in direct_pid_set]
    require_cwd_match = not direct_candidates
    if direct_candidates:
        if len(direct_candidates) != len(candidates):
            _log.debug("  direct-candidate filter: %d -> %d candidates",
                       len(candidates), len(direct_candidates))
        candidates = direct_candidates
    else:
        _log.debug("  only broad terminal candidates; requiring unique cwd title match")

    # Filter: prefer windows whose title ends with " - Cursor"
    cursor_candidates = [c for c in candidates if c[3].endswith(" - Cursor")]
    if cursor_candidates:
        _log.debug("  Cursor-title filter: %d -> %d candidates",
                   len(candidates), len(cursor_candidates))
        candidates = cursor_candidates

    candidates.sort(key=lambda c: c[0])

    # When multiple candidates share the best chain_index, disambiguate by cwd
    best_idx = candidates[0][0]
    tied = [c for c in candidates if c[0] == best_idx]

    if (len(tied) > 1 or require_cwd_match) and cwd:
        _log.debug("  %d tied candidates at chain_idx=%d, trying path component matching",
                   len(tied), best_idx)
        # Try each path component from innermost to outermost
        parts = cwd.replace("\\", "/").rstrip("/").split("/")
        for part in reversed(parts):
            comp = part.lower().rstrip(":")
            if comp in SKIP_COMPONENTS:
                continue
            matches = [c for c in tied if comp in c[3].lower()]
            _log.debug("    component %r -> %d matches", comp, len(matches))
            if len(matches) == 1:
                _log.debug("  => unique match hwnd=%d title=%r", matches[0][2], matches[0][3])
                return matches[0][2]  # hwnd
        if require_cwd_match:
            _log.debug("  => no unique cwd match; refusing broad terminal fallback")
            return None
    elif require_cwd_match:
        _log.debug("  => no cwd available; refusing broad terminal fallback")
        return None

    # Stable fallback: sort by hwnd to avoid Z-order dependency
    tied.sort(key=lambda c: c[2])
    result = tied[0][2]
    _log.debug("  => fallback hwnd=%d title=%r", tied[0][2], tied[0][3])
    return result


def activate_window(hwnd: int):
    """Bring a window to the foreground, even from background. Windows only."""
    if not IS_WINDOWS:
        return

    user32 = ctypes.windll.user32

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    kernel32 = ctypes.windll.kernel32
    fg_hwnd = user32.GetForegroundWindow()
    our_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)

    if our_tid != fg_tid:
        user32.AttachThreadInput(our_tid, fg_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(our_tid, fg_tid, False)
    else:
        user32.SetForegroundWindow(hwnd)


def short_cwd(cwd: str) -> str:
    """Extract project folder name from CWD path."""
    if not cwd:
        return "unknown"
    return os.path.basename(cwd.rstrip("/\\"))


_FOLDER_MAX_CHARS = 9  # 'firstgame' length cap

# Provider glyphs shown in the left marker column so users can tell Claude vs
# Codex rows at a glance. Filled shapes survive small UI sizes better than
# hollow outlines.
# SESSION_MONITOR_ASCII_GLYPH=1 swaps in ASCII fallbacks for terminals/fonts
# that can't render the geometric shapes cleanly.
_GLYPH_UNICODE = {"claude": "●", "codex": "◆"}
_GLYPH_ASCII = {"claude": "[C]", "codex": "[X]"}


def provider_glyph(provider) -> str:
    """Short visual prefix identifying which LLM CLI a row belongs to."""
    glyph = provider_marker(provider)
    return f"{glyph} " if glyph else ""


def provider_marker(provider) -> str:
    """Single-character row marker identifying which LLM CLI owns the row."""
    if not provider:
        return ""
    ascii_glyph = os.environ.get("SESSION_MONITOR_ASCII_GLYPH")
    table = _GLYPH_ASCII if ascii_glyph == "1" else _GLYPH_UNICODE
    return table.get(provider, "")


def slot_glyph(slot) -> str:
    """Render a slot number as '(N)'; empty when slot is unset/zero."""
    if not isinstance(slot, int) or slot < 1:
        return ""
    return f"({slot})"


def build_folder_head(cwd, slot, provider="claude"):
    """Compose 'folder(N)' (folder capped to _FOLDER_MAX_CHARS)."""
    base = short_cwd(cwd)
    if len(base) > _FOLDER_MAX_CHARS:
        base = base[:_FOLDER_MAX_CHARS - 3] + "..."
    glyph = slot_glyph(slot)
    return f"{base}{glyph}" if glyph else base


def build_summary_text(summary):
    """Subtitle text shown in the summary column."""
    return (summary or "").strip() or "New"


def build_display_name(cwd, slot, summary, provider="claude"):
    """Composite label used for sorting and tooltips."""
    return f"{build_folder_head(cwd, slot, provider)}  {build_summary_text(summary)}"


# ── InstanceTracker ───────────────────────────────────────────────

class Instance:
    __slots__ = ("pid", "cwd", "state", "updated_at", "display_name",
                 "blink_on", "done_since", "hwnd", "wezterm",
                 "slot", "summary", "pinned_at", "provider", "session_id",
                 "state_changed_at", "interrupt_source", "interrupt_at")

    def __init__(self, pid, cwd, state="idle", updated_at=0, provider="claude", session_id=""):
        self.pid = pid
        self.cwd = cwd
        self.session_id = session_id or ""
        self.state = state
        self.updated_at = updated_at
        self.state_changed_at = updated_at or 0
        self.slot = 0
        self.summary = ""
        self.provider = provider
        self.interrupt_source = ""
        self.interrupt_at = 0
        self.display_name = build_display_name(cwd, 0, "", provider)
        self.blink_on = True
        self.done_since = 0.0
        self.hwnd = 0
        self.wezterm = None
        # None = unpinned. Set to time.monotonic() when pinned; pinned rows
        # sort above unpinned rows, earliest-pinned first.
        self.pinned_at = None


class InstanceTracker:
    """Polls session + state files to maintain live instance list."""

    PINS_FILE = pins_file()

    def __init__(self):
        self.sessions_dir = sessions_dir()
        self.state_dir = get_state_dir()
        # Keys are int PIDs for hook-driven rows and "codex:<sid8>" strings
        # for the rollout poller's PID-less virtual rows.
        self.instances: dict = {}
        # Tick-over cache for codex_rollout_poller — keeps decoded session
        # metadata (cwd, sessionId) keyed by file path so we don't re-parse
        # the first line of every rollout JSONL on every poll.
        self._codex_rollout_cache: dict = {}
        self.pins = self._load_pins()

    @staticmethod
    def pin_key(inst: Instance) -> str:
        return inst.session_id or str(inst.pid)

    def _load_pins(self) -> dict:
        try:
            with open(self.PINS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {
                    str(k): float(v)
                    for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, (int, float))
                }
        except Exception:
            pass
        return {}

    def save_pins(self):
        data = {
            self.pin_key(inst): inst.pinned_at
            for inst in self.instances.values()
            if inst.pinned_at is not None
        }
        try:
            os.makedirs(os.path.dirname(self.PINS_FILE), exist_ok=True)
            tmp = f"{self.PINS_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.PINS_FILE)
            self.pins = data
        except Exception:
            _log.debug("failed to save pins", exc_info=True)

    def poll(self):
        """Refresh instance list. Returns (changed, events)."""
        changed = False
        events = []
        seen_pids = set()
        proc_tree = build_process_tree() if IS_WINDOWS else {}
        marker_pids = load_nested_pids(os.path.dirname(self.state_dir))

        # First pass: read every sessions/*.json once, then hand the
        # collected sessionIds to the codex rollout poller. The poller may
        # write new virtual session files; we re-glob below to pick those
        # up (the file load is cached in `sessions_data` so the same path
        # isn't decoded twice).
        sessions_data = {}
        session_by_pid = {}
        for sf in glob.glob(os.path.join(self.sessions_dir, "*.json")):
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    sessions_data[sf] = json.load(f)
            except Exception:
                continue

        if poll_codex_rollouts is not None:
            known_session_ids = {
                sd["sessionId"]
                for sf, sd in sessions_data.items()
                if not os.path.basename(sf).startswith("codex-") and sd.get("sessionId")
            }
            try:
                poll_codex_rollouts(
                    known_session_ids,
                    self._codex_rollout_cache,
                    self.sessions_dir,
                    self.state_dir,
                )
            except Exception:
                _log.debug("codex rollout poller failed", exc_info=True)

        for sf in glob.glob(os.path.join(self.sessions_dir, "*.json")):
            sess = sessions_data.get(sf)
            if sess is None:
                # File written by the poller in the call above; load it now.
                try:
                    with open(sf, "r", encoding="utf-8") as f:
                        sess = json.load(f)
                except Exception:
                    continue
            try:
                pid = sess.get("pid")
                cwd = sess.get("cwd", "")
                session_id = sess.get("sessionId", "") or ""
                if pid is None:
                    continue
                session_by_pid[pid] = sess
            except Exception:
                continue

            if is_virtual_id(pid):
                # Virtual rows are owned by codex_rollout_poller, which
                # writes/evicts both files atomically. Don't attempt the
                # alive check (no real PID) and don't delete on its behalf.
                pass
            elif (
                not isinstance(pid, int)
                or is_codex_desktop_app_pid(pid, proc_tree)
                or not is_claude_pid_alive(pid)
            ):
                if pid in self.instances:
                    del self.instances[pid]
                    changed = True
                try:
                    os.remove(sf)
                except Exception:
                    pass
                if isinstance(pid, int):
                    state_file = os.path.join(self.state_dir, f"{pid}.json")
                    try:
                        os.remove(state_file)
                    except Exception:
                        pass
                continue

            # Skip nested `claude -p` subprocesses (e.g. our own summarizer).
            if IS_WINDOWS and is_nested_claude_pid(pid, proc_tree, marker_pids):
                continue

            seen_pids.add(pid)

            if pid not in self.instances:
                self.instances[pid] = Instance(pid, cwd, session_id=session_id)
                key = self.pin_key(self.instances[pid])
                if key in self.pins:
                    self.instances[pid].pinned_at = self.pins[key]
                changed = True
            elif self.instances[pid].cwd != cwd:
                self.instances[pid].cwd = cwd
                changed = True
            if self.instances[pid].session_id != session_id:
                self.instances[pid].session_id = session_id
                key = self.pin_key(self.instances[pid])
                self.instances[pid].pinned_at = self.pins.get(key)
                changed = True

        for sf in glob.glob(os.path.join(self.state_dir, "*.json")):
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    st = json.load(f)
                pid = st.get("pid")
                state = st.get("state", "working")
                updated_at = st.get("updatedAt", 0)
                saved_hwnd = st.get("hwnd", 0)
                saved_wezterm = st.get("wezterm")
                saved_slot = st.get("slot", 0)
                saved_summary = st.get("summary", "")
                saved_cwd = st.get("cwd") or ""
                saved_provider = st.get("provider", "claude")
                saved_interrupt_source = st.get("interruptSource", "") or ""
                saved_interrupt_at = int(st.get("interruptAt", 0) or 0)
            except Exception:
                continue

            session_data = session_by_pid.get(pid)
            if saved_provider == "claude" and session_waits_for_user(session_data):
                state = "question"

            replacement_state = resolve_question_state_from_files(st, sessions_data)
            if replacement_state:
                st = _strip_question_fields(st)
                st["state"] = replacement_state
                st["updatedAt"] = int(time.time())
                try:
                    _atomic_write_json(sf, st)
                except Exception:
                    _log.debug("failed to clear question state", exc_info=True)
                state = replacement_state
                updated_at = st["updatedAt"]

            # Cleanup orphan state files for dead PIDs (sessions/*.json may have
            # been removed earlier without removing the matching state JSON).
            # Virtual rows skip the alive check — codex_rollout_poller owns
            # eviction for those.
            if is_virtual_id(pid):
                pass
            elif (
                not isinstance(pid, int)
                or is_codex_desktop_app_pid(pid, proc_tree)
                or not is_claude_pid_alive(pid)
            ):
                try:
                    os.remove(sf)
                except OSError:
                    pass
                continue

            if pid in self.instances:
                inst = self.instances[pid]
                inst.hwnd = saved_hwnd or inst.hwnd
                if isinstance(saved_wezterm, dict):
                    inst.wezterm = saved_wezterm
                if isinstance(saved_slot, int) and saved_slot != inst.slot:
                    inst.slot = saved_slot
                    changed = True
                if saved_summary != inst.summary:
                    inst.summary = saved_summary
                    changed = True
                if saved_provider and saved_provider != inst.provider:
                    inst.provider = saved_provider
                    changed = True
                if saved_interrupt_source != inst.interrupt_source:
                    inst.interrupt_source = saved_interrupt_source
                    changed = True
                if saved_interrupt_at != inst.interrupt_at:
                    inst.interrupt_at = saved_interrupt_at
                    changed = True
                # state JSON wins for cwd: sessions/*.json may have been
                # rebuilt with a stale subdirectory cwd after a Claude Code
                # self-update; state JSON preserves the home cwd.
                if saved_cwd and saved_cwd != inst.cwd:
                    inst.cwd = saved_cwd
                    changed = True
                if inst.state != state or inst.updated_at != updated_at:
                    old_state = inst.state
                    inst.state = state
                    inst.updated_at = updated_at
                    if old_state != state and state != "working":
                        inst.state_changed_at = updated_at or int(time.time())
                    if state == "done" and old_state != "done":
                        inst.done_since = time.monotonic()
                        inst.blink_on = True
                        events.append("done")
                        # PID-less codex rows have no Stop hook to spawn the
                        # summarizer for them — do it here on the done edge.
                        if is_virtual_id(pid) and _should_spawn_codex_summary(st):
                            _spawn_codex_summary(pid)
                    elif state == "interrupted" and old_state != "interrupted":
                        inst.done_since = time.monotonic()
                        inst.blink_on = True
                        events.append("interrupted")
                    elif state == "question" and old_state != "question":
                        inst.blink_on = True
                        inst.done_since = 0.0
                        events.append("question")
                    elif state not in ("done", "interrupted"):
                        inst.done_since = 0.0
                    changed = True

        # Proactively resolve hwnd for instances that don't have one yet
        if IS_WINDOWS:
            missing_hwnd = [inst for inst in self.instances.values()
                            if not inst.hwnd and inst.pid in seen_pids
                            and isinstance(inst.pid, int)]
            if missing_hwnd:
                try:
                    tree = build_process_tree()
                    for inst in missing_hwnd:
                        hwnd = find_window_for_pid(inst.pid, tree, inst.cwd)
                        if hwnd:
                            inst.hwnd = hwnd
                except Exception:
                    _log.debug("hwnd resolution failed", exc_info=True)

        # Remove instances whose PID is gone
        for pid in list(self.instances):
            if pid not in seen_pids:
                del self.instances[pid]
                changed = True

        # Compose display names from cwd + slot + summary + provider
        for inst in self.instances.values():
            new_name = build_display_name(inst.cwd, inst.slot, inst.summary, inst.provider)
            if inst.display_name != new_name:
                inst.display_name = new_name
                changed = True

        return changed, events


# ── Overlay UI ────────────────────────────────────────────────────

class MonitorOverlay:
    POSITION_FILE = position_file()

    def __init__(self):
        self.poll_ms = CONFIG.get("poll_interval_ms", 500)
        self.blink_ms = CONFIG.get("blink_interval_ms", 600)
        self.blink_seconds = max(0.0, float(CONFIG.get("blink_seconds", DONE_BLINK_SECONDS)))
        self.summary_max_chars = max(4, int(CONFIG.get("summary_max_chars", 12)))
        self.sound_enabled = CONFIG.get("sound_enabled", True)
        # Suppress sound + blink for whatever state already exists, including
        # delayed catch-up writes that land just after the widget opens.
        self._first_poll = True
        self._quiet_until = time.monotonic() + STARTUP_QUIET_SECONDS
        self._recent_highlight_cleared_at = 0
        self.row_height = 22
        self.header_height = 22
        self.padding = 4

        self.tracker = InstanceTracker()
        self.status_watcher = ClaudeStatusWatcher()
        self.root = tk.Tk()
        self.root.title("Session Monitor")
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", CONFIG.get("opacity", 0.65))
        self.root.configure(bg=THEME["bg"])

        # Fonts (must precede width derivation that calls font.measure)
        try:
            self.font_title = tkfont.Font(family="Segoe UI", size=9, weight="bold")
            self.font_row = tkfont.Font(family="Segoe UI", size=9)
            self.font_state = tkfont.Font(family="Segoe UI", size=8)
            self.font_empty = tkfont.Font(family="Segoe UI", size=9, slant="italic")
        except Exception:
            self.font_title = tkfont.Font(size=9, weight="bold")
            self.font_row = tkfont.Font(size=9)
            self.font_state = tkfont.Font(size=8)
            self.font_empty = tkfont.Font(size=9, slant="italic")

        # Derive column widths and overall widget width.
        # The widest possible folder head is "firstgame(99)" (folder cap + slot).
        # The summary column reserves enough pixels for `summary_max_chars` of
        # CJK width — the same N is passed to write-state.py so Haiku stays
        # within the column.
        self.col_dot_w = max(
            self.font_row.measure("◆  "),
            self.font_row.measure("[X] "),
        )
        # Folder column no longer carries the provider glyph; the left marker
        # column owns provider identity so names stay aligned.
        self.col_folder_w = self.font_row.measure("firstgame(99)") + 8
        # No ellipsis padding — overflow is hidden via grid_propagate(False).
        self.col_summary_w = self.font_row.measure("가") * self.summary_max_chars
        self.col_state_w = self.font_state.measure("Working") + 12
        self.outer_pad = 12
        self.width = (
            self.col_dot_w + self.col_folder_w
            + self.col_summary_w + self.col_state_w + self.outer_pad
        )

        # Persist max-chars so write-state.py reads the same source-of-truth.
        try:
            cfg_path = config_file()
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cur = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                cur = {}
            lang = CONFIG.get("language", "en")
            dirty = False
            if cur.get("summary_max_chars") != self.summary_max_chars:
                cur["summary_max_chars"] = self.summary_max_chars
                dirty = True
            if cur.get("language") != lang:
                cur["language"] = lang
                dirty = True
            if dirty:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cur, f)
        except Exception:
            _log.debug("could not persist summary_max_chars", exc_info=True)

        # Position: load saved or default to bottom-right
        saved = self._load_position()
        if saved:
            x, y = saved
        else:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            x = screen_w - self.width - 16
            y = screen_h - 200
        self.root.geometry(f"{self.width}x120+{x}+{y}")

        # Top bar (close button only)
        self.header = tk.Frame(self.root, bg=THEME["bg"], height=20)
        self.header.pack(fill=tk.X, pady=(4, 0))
        self.header.pack_propagate(False)

        self.close_btn = tk.Label(
            self.header, text=" \u2715 ", font=self.font_state,
            bg=THEME["bg"], fg=THEME["dim"], cursor="hand2",
        )
        self.close_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.close_btn.bind("<Button-1>", lambda _: self.root.destroy())
        self.close_btn.bind("<Enter>", lambda _: self.close_btn.config(fg=THEME["close_hover"]))
        self.close_btn.bind("<Leave>", lambda _: self.close_btn.config(fg=THEME["dim"]))

        self.status_label = tk.Label(
            self.header, text="", font=self.font_state,
            bg=THEME["bg"], fg=THEME["dim"], anchor="e",
        )
        self.status_label.pack(side=tk.RIGHT, padx=(0, 6), fill=tk.X, expand=True)

        # Content frame
        self.content = tk.Frame(self.root, bg=THEME["bg"])
        self.content.pack(fill=tk.BOTH, expand=True, padx=self.padding, pady=(2, 4))

        self.row_widgets: list[dict] = []

        # Drag support
        self._drag_data = {"x": 0, "y": 0}
        for w in (self.header, self.status_label):
            w.bind("<Button-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_motion)
            w.bind("<ButtonRelease-1>", self._on_drag_end)

        # Blink state
        self._blink_phase = True

        # Start loops
        self._poll_loop()
        self._blink_loop()

    def _load_position(self):
        try:
            with open(self.POSITION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            x, y = int(data["x"]), int(data["y"])
            return (x, y)
        except Exception:
            return None

    def _save_position(self):
        try:
            os.makedirs(os.path.dirname(self.POSITION_FILE), exist_ok=True)
            data = {"x": self.root.winfo_x(), "y": self.root.winfo_y()}
            with open(self.POSITION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _on_drag_start(self, event):
        self._drag_data["x"] = event.x_root - self.root.winfo_x()
        self._drag_data["y"] = event.y_root - self.root.winfo_y()

    def _on_drag_motion(self, event):
        x = event.x_root - self._drag_data["x"]
        y = event.y_root - self._drag_data["y"]
        self.root.geometry(f"+{x}+{y}")

    def _on_drag_end(self, event):
        self._save_position()

    def _poll_loop(self):
        try:
            changed, events = self.tracker.poll()
            if self._first_poll or time.monotonic() < self._quiet_until:
                # Quench inherited/catch-up state so it doesn't blink or chime.
                for inst in self.tracker.instances.values():
                    inst.done_since = 0.0
                    inst.blink_on = False
                events = []
                self._first_poll = False
            if changed:
                self._rebuild_rows()
            self._update_status_watch()
            if self.sound_enabled:
                for ev in events:
                    self._play_sound(ev)
            self.root.wm_attributes("-topmost", True)
        except Exception:
            _log.exception("poll loop error")
        finally:
            self.root.after(self.poll_ms, self._poll_loop)

    def _update_status_watch(self):
        for inst in self.tracker.instances.values():
            if (
                inst.provider == "claude"
                and inst.state == "interrupted"
                and inst.interrupt_source == "stop_failure"
            ):
                self.status_watcher.trigger(inst.interrupt_at or inst.updated_at)

        label, color, restored_event = self.status_watcher.snapshot()
        if label:
            max_px = max(40, self.width - self.close_btn.winfo_reqwidth() - 14)
            label = self._fit_status_label(label, max_px)
        if self.status_label.cget("text") != label:
            self.status_label.config(text=label)
        self.status_label.config(fg=color)
        if restored_event and self.sound_enabled:
            self._play_sound("status_restored")

    def _fit_status_label(self, text: str, max_pixels: int) -> str:
        font = self.font_state
        if max_pixels <= 0 or font.measure(text) <= max_pixels:
            return text
        while text and font.measure(text + "\u2026") > max_pixels:
            text = text[:-1]
        return text.rstrip() + "\u2026"

    @staticmethod
    def _play_sound(event: str):
        """Play a short chime in a background thread (non-blocking). Windows only."""
        if not IS_WINDOWS:
            return
        chimes = {
            "done": [(880, 80), (1175, 80), (1397, 120)],      # A5-D6-F6 rising major
            "question": [(1047, 100), (880, 130)],              # C6-A5 descending
            "interrupted": [(880, 80), (660, 80), (440, 120)],  # A5-E5-A4 descending
            "status_restored": [(660, 80), (880, 80), (1175, 140)],
        }
        seq = chimes.get(event)
        if seq:
            def _play():
                for freq, ms in seq:
                    winsound.Beep(freq, ms)
            threading.Thread(target=_play, daemon=True).start()

    def _blink_loop(self):
        try:
            self._blink_phase = not self._blink_phase
            now = time.monotonic()
            for row in self.row_widgets:
                inst: Instance = row.get("instance")
                if not inst:
                    continue
                if inst.state == "done":
                    if inst.done_since > 0 and now - inst.done_since < self.blink_seconds:
                        color = THEME["done"] if self._blink_phase else row.get("base_bg", THEME["bg"])
                    else:
                        color = THEME["done"]
                    row["dot"].config(fg=color)
                    row["state"].config(fg=color)
                elif inst.state == "interrupted":
                    if inst.done_since > 0 and now - inst.done_since < self.blink_seconds:
                        color = THEME["interrupted"] if self._blink_phase else row.get("base_bg", THEME["bg"])
                    else:
                        color = THEME["interrupted"]
                    row["dot"].config(fg=color)
                    row["state"].config(fg=color)
                elif inst.state == "question":
                    row["dot"].config(fg=THEME["question"])
                    row["state"].config(fg=THEME["question"])
        except Exception:
            _log.exception("blink loop error")
        finally:
            self.root.after(self.blink_ms, self._blink_loop)

    def _rebuild_rows(self):
        for row in self.row_widgets:
            row["frame"].destroy()
        self.row_widgets.clear()

        instances = sorted(
            self.tracker.instances.values(),
            key=lambda i: (
                0 if i.pinned_at is not None else 1,
                i.pinned_at if i.pinned_at is not None else 0.0,
                i.display_name.lower(),
                str(i.pid),
            ),
        )
        recent_key = self._recent_state_change_key(instances)

        if not instances:
            frame = tk.Frame(self.content, bg=THEME["bg"])
            frame.pack(fill=tk.X, pady=2)
            lbl = tk.Label(
                frame, text=get_label("no_instances"), font=self.font_empty,
                bg=THEME["bg"], fg=THEME["dim"], anchor="center",
            )
            lbl.pack(fill=tk.X, pady=8)
            self.row_widgets.append({"frame": frame, "instance": None, "dot": lbl})
        else:
            for inst in instances:
                self._add_row(inst, is_recent=self.tracker.pin_key(inst) == recent_key)

        row_count = max(len(instances), 1)
        height = self.header_height + row_count * self.row_height + self.padding
        geo = self.root.geometry()
        parts = geo.split("+")
        x_pos = parts[1] if len(parts) > 1 else "0"
        y_pos = parts[2] if len(parts) > 2 else "0"
        self.root.geometry(f"{self.width}x{height}+{x_pos}+{y_pos}")

    def _fit_label(self, text: str, max_pixels: int) -> str:
        """Trim text with '\u2026' if it would exceed max_pixels rendered in font_row."""
        font = self.font_row
        if max_pixels <= 0 or font.measure(text) <= max_pixels:
            return text
        while text and font.measure(text + "\u2026") > max_pixels:
            text = text[:-1]
        return text.rstrip() + "\u2026"

    def _recent_state_change_key(self, instances):
        candidates = [
            i for i in instances
            if i.state_changed_at and i.state_changed_at > self._recent_highlight_cleared_at
        ]
        if not candidates:
            return None
        inst = max(candidates, key=lambda i: (i.state_changed_at, str(i.pid)))
        return self.tracker.pin_key(inst)

    def _add_row(self, inst: Instance, is_recent=False):
        state_color = THEME.get(inst.state, THEME["idle"])
        state_text = get_label(inst.state)
        base_bg = THEME["recent_bg"] if is_recent else THEME["bg"]

        frame = tk.Frame(self.content, bg=base_bg, cursor="hand2")
        frame._session_monitor_base_bg = base_bg
        frame.pack(fill=tk.X, pady=0)

        # Grid columns: dot | folder(N) | summary | state
        frame.grid_columnconfigure(0, minsize=self.col_dot_w)
        frame.grid_columnconfigure(1, minsize=self.col_folder_w)
        frame.grid_columnconfigure(2, minsize=self.col_summary_w, weight=1)
        frame.grid_columnconfigure(3, minsize=self.col_state_w)

        cell_h = self.row_height - 2

        dot_glyph = provider_marker(inst.provider) or "\u25cf"
        dot_box = tk.Frame(
            frame, bg=base_bg,
            width=self.col_dot_w, height=cell_h,
        )
        dot_box.grid(row=0, column=0, sticky="w", padx=(4, 0))
        dot_box.grid_propagate(False)
        dot_box.pack_propagate(False)
        marker_fg = state_color if inst.pinned_at is None else THEME["fg"]
        dot = tk.Label(
            dot_box, text=dot_glyph, font=self.font_row,
            bg=base_bg, fg=marker_fg, cursor="hand2",
        )
        dot.pack(fill=tk.BOTH, expand=True)

        def on_dot_click(_e, pid=inst.pid):
            self._clear_recent_highlight(pid)
            self._toggle_pin(pid)

        dot_box.bind("<Button-1>", on_dot_click)
        dot.bind("<Button-1>", on_dot_click)

        folder_box = tk.Frame(
            frame, bg=base_bg,
            width=self.col_folder_w, height=cell_h,
        )
        folder_box.grid(row=0, column=1, sticky="w")
        folder_box.grid_propagate(False)
        folder_box.pack_propagate(False)
        folder_lbl = tk.Label(
            folder_box, text=build_folder_head(inst.cwd, inst.slot, inst.provider),
            font=self.font_row, bg=base_bg, fg=THEME["fg"], anchor="w",
        )
        folder_lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        summary_box = tk.Frame(
            frame, bg=base_bg,
            width=self.col_summary_w, height=cell_h,
        )
        summary_box.grid(row=0, column=2, sticky="w")
        summary_box.grid_propagate(False)
        summary_box.pack_propagate(False)
        summary_lbl = tk.Label(
            summary_box, text=build_summary_text(inst.summary),
            font=self.font_row, bg=base_bg, fg=THEME["dim"], anchor="w",
        )
        summary_lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        state_lbl = tk.Label(
            frame, text=state_text, font=self.font_state,
            bg=base_bg, fg=state_color, anchor="e",
        )
        state_lbl.grid(row=0, column=3, sticky="e", padx=(0, 6))

        # Backwards-compat: hover handlers expect a single `name` widget; reuse
        # `summary_lbl` since that's the column users mostly read.
        name_lbl = summary_lbl

        def on_click(_event, pid=inst.pid):
            self._clear_recent_highlight(pid)
            self._activate_terminal(pid)

        # Dot already has its own pin-toggle handler; it still participates in
        # row-hover recoloring via the generic Enter/Leave below.
        for w in (frame, folder_box, folder_lbl,
                  summary_box, summary_lbl, state_lbl):
            w.bind("<Button-1>", on_click)
        for w in (frame, dot_box, dot, folder_box, folder_lbl,
                  summary_box, summary_lbl, state_lbl):
            w.bind("<Enter>", lambda _e, f=frame: self._row_hover(f, True))
            w.bind("<Leave>", lambda _e, f=frame: self._row_hover(f, False))

        self.row_widgets.append({
            "frame": frame, "dot": dot, "name": name_lbl,
            "state": state_lbl, "instance": inst, "base_bg": base_bg,
        })

    def _clear_recent_highlight(self, pid):
        """Dismiss the current recent-change highlight when its row is clicked."""
        inst = self.tracker.instances.get(pid)
        if not inst or not inst.state_changed_at:
            return
        if self.tracker.pin_key(inst) != self._recent_state_change_key(self.tracker.instances.values()):
            return
        self._recent_highlight_cleared_at = max(
            self._recent_highlight_cleared_at,
            inst.state_changed_at,
        )
        self._rebuild_rows()

    def _toggle_pin(self, pid):
        """Toggle pin state for the given instance and rerender rows."""
        inst = self.tracker.instances.get(pid)
        if not inst:
            return
        inst.pinned_at = None if inst.pinned_at is not None else time.time()
        self.tracker.save_pins()
        self._rebuild_rows()

    def _row_hover(self, frame, entering):
        bg = THEME["hover"] if entering else getattr(frame, "_session_monitor_base_bg", THEME["bg"])
        frame.config(bg=bg)
        # Recolor every descendant — grid cells include nested boxes
        # (folder_box → folder_lbl, summary_box → summary_lbl).
        stack = list(frame.winfo_children())
        while stack:
            w = stack.pop()
            try:
                w.config(bg=bg)
            except tk.TclError:
                pass
            stack.extend(w.winfo_children())

    def _activate_terminal(self, claude_pid):
        try:
            inst = self.tracker.instances.get(claude_pid)

            # wezterm 페인 우선 — pane_id 기반 정확 매칭
            if inst and inst.wezterm and self._activate_wezterm_pane(inst.wezterm):
                return

            # Virtual codex rows from the rollout poller don't carry a real
            # PID. Fall back to matching live codex.exe processes by cwd; this
            # is not as precise as hook-captured wezterm pane IDs, but it makes
            # PID-less rows clickable when hooks are unavailable.
            if not isinstance(claude_pid, int):
                if inst and inst.provider == "codex":
                    self._activate_codex_pidless(inst.cwd, inst.session_id)
                else:
                    _log.debug("_activate_terminal: pid-less row has no focus target pid=%s", claude_pid)
                return

            # 저장된 HWND 우선 사용
            if inst and inst.hwnd:
                user32 = ctypes.windll.user32
                if user32.IsWindow(inst.hwnd) and user32.IsWindowVisible(inst.hwnd):
                    activate_window(inst.hwnd)
                    return
                else:
                    inst.hwnd = 0  # stale handle 클리어

            # 폴백: 프로세스 트리 탐색
            tree = build_process_tree()
            cwd = inst.cwd if inst else ""
            hwnd = find_window_for_pid(claude_pid, tree, cwd)
            if hwnd:
                activate_window(hwnd)
            else:
                _log.warning("_activate_terminal: no hwnd found for pid=%d", claude_pid)
        except Exception:
            _log.error("_activate_terminal failed for pid=%d", claude_pid, exc_info=True)

    def _activate_codex_pidless(self, cwd: str, session_id: str = ""):
        """Best-effort window activation for a Codex row the rollout poller
        registered without a PID. Prefer a wezterm pane session-id match, then
        a unique cwd match, then live codex.exe process/window ownership."""
        try:
            if self._activate_wezterm_pane_for_codex_row(cwd, session_id):
                return

            tree = build_process_tree()
            codex_pids = [p for p, (_, exe) in tree.items() if exe == "codex.exe"]
            for cpid in codex_pids:
                hwnd = find_window_for_pid(cpid, tree, cwd)
                if hwnd:
                    activate_window(hwnd)
                    return
            _log.debug("_activate_codex_pidless: no match for cwd=%s among %d codex.exe pids",
                       cwd, len(codex_pids))
        except Exception:
            _log.error("_activate_codex_pidless failed", exc_info=True)

    @staticmethod
    def _normalize_wezterm_cwd(cwd: str) -> str:
        if not cwd:
            return ""
        value = cwd
        if value.startswith("file://"):
            parsed = urllib.parse.urlparse(value)
            value = urllib.parse.unquote(parsed.path or "")
            if IS_WINDOWS and re.match(r"^/[A-Za-z]:/", value):
                value = value[1:]
            value = value.replace("/", os.sep)
        return os.path.normcase(os.path.normpath(value))

    def _activate_wezterm_pane_for_codex_row(self, cwd: str, session_id: str = "") -> bool:
        """Activate the wezterm pane for a PID-less Codex row.

        Prefer session-id text inside panes with the same cwd. If no session id
        is available, cwd is usable only when it identifies one pane exactly.
        """
        target = self._normalize_wezterm_cwd(cwd)
        if not target:
            return False

        try:
            kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 2,
            }
            if IS_WINDOWS:
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            result = subprocess.run(["wezterm", "cli", "list", "--format", "json"], **kwargs)
            if result.returncode != 0:
                _log.debug("wezterm list rc=%s stderr=%s",
                           result.returncode, (result.stderr or "")[:200])
                return False
            panes = json.loads(result.stdout or "[]")
        except FileNotFoundError:
            _log.debug("wezterm.exe not on PATH")
            return False
        except Exception:
            _log.debug("wezterm list failed", exc_info=True)
            return False

        matches = [
            p for p in panes
            if self._normalize_wezterm_cwd(str(p.get("cwd") or "")) == target
        ]
        sid = (session_id or "").strip()
        if sid and len(matches) > 1:
            sid_matches = []
            for pane in matches:
                pane_id = pane.get("pane_id")
                if pane_id is None:
                    continue
                try:
                    kwargs = {
                        "capture_output": True,
                        "text": True,
                        "encoding": "utf-8",
                        "errors": "replace",
                        "timeout": 2,
                    }
                    if IS_WINDOWS:
                        kwargs["creationflags"] = 0x08000000
                    result = subprocess.run(
                        ["wezterm", "cli", "get-text", "--pane-id", str(pane_id)], **kwargs
                    )
                except Exception:
                    _log.debug("wezterm get-text failed for pane_id=%s", pane_id, exc_info=True)
                    continue
                if result.returncode == 0 and sid in (result.stdout or ""):
                    sid_matches.append(pane)
            if len(sid_matches) == 1:
                matches = sid_matches
            else:
                _log.debug("wezterm session match for %s cwd=%s -> %d panes; refusing",
                           sid, cwd, len(sid_matches))
                return False

        if len(matches) != 1:
            _log.debug("wezterm cwd match for %s -> %d panes; refusing", cwd, len(matches))
            return False

        pane = matches[0]
        pane_id = pane.get("pane_id")
        if pane_id is None:
            return False

        try:
            kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 2,
            }
            if IS_WINDOWS:
                kwargs["creationflags"] = 0x08000000
            result = subprocess.run(
                ["wezterm", "cli", "activate-pane", "--pane-id", str(pane_id)], **kwargs
            )
            if result.returncode != 0:
                _log.debug("wezterm activate-pane rc=%s stderr=%s",
                           result.returncode, (result.stderr or "")[:200])
                return False
        except Exception:
            _log.debug("wezterm activate-pane failed", exc_info=True)
            return False

        self._raise_wezterm_client_for_pane(pane_id)
        _log.debug("activated wezterm pane_id=%s for cwd=%s", pane_id, cwd)
        return True

    def _raise_wezterm_client_for_pane(self, pane_id):
        if not IS_WINDOWS:
            return
        try:
            kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 2,
                "creationflags": 0x08000000,
            }
            result = subprocess.run(["wezterm", "cli", "list-clients", "--format", "json"], **kwargs)
            if result.returncode != 0:
                return
            clients = json.loads(result.stdout or "[]")
        except Exception:
            _log.debug("wezterm list-clients failed", exc_info=True)
            return

        selected = None
        for c in clients:
            if c.get("focused_pane_id") == pane_id:
                selected = c
                break
        if selected is None and len(clients) == 1:
            selected = clients[0]
        if not selected:
            return
        try:
            gui_pid = int(selected.get("pid"))
        except (TypeError, ValueError):
            return
        hwnd = self._find_wezterm_hwnd(gui_pid)
        if hwnd:
            activate_window(hwnd)

    def _activate_wezterm_pane(self, wezterm_info: dict) -> bool:
        """Activate wezterm tab via cli + raise GUI window. Returns True if tab activated."""
        tab_id = wezterm_info.get("tab_id")
        socket = wezterm_info.get("socket")
        if tab_id is None or not socket:
            return False

        try:
            env = os.environ.copy()
            env["WEZTERM_UNIX_SOCKET"] = socket
            kwargs = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": env,
                "timeout": 2,
            }
            if IS_WINDOWS:
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            result = subprocess.run(
                ["wezterm", "cli", "activate-tab", "--tab-id", str(tab_id)], **kwargs
            )
            if result.returncode != 0:
                _log.debug("wezterm activate-tab rc=%s stderr=%s",
                           result.returncode, (result.stderr or "")[:200])
                return False
        except FileNotFoundError:
            _log.debug("wezterm.exe not on PATH")
            return False
        except Exception:
            _log.debug("wezterm activate-tab failed", exc_info=True)
            return False

        # OS 윈도우 raise — wezterm cli는 윈도우를 foreground로 올리지 않음 (issue #5855)
        if IS_WINDOWS:
            gui_pid = self._extract_wezterm_gui_pid(socket)
            if gui_pid:
                hwnd = self._find_wezterm_hwnd(gui_pid)
                if hwnd:
                    activate_window(hwnd)
        return True

    @staticmethod
    def _extract_wezterm_gui_pid(socket_path: str) -> int | None:
        """Parse 'gui-sock-<pid>' substring to recover wezterm-gui PID."""
        m = re.search(r"gui-sock-(\d+)", socket_path or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _find_wezterm_hwnd(gui_pid: int) -> int | None:
        """Find a visible top-level window owned by the wezterm GUI process."""
        if not IS_WINDOWS:
            return None
        user32 = ctypes.windll.user32
        found = []

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            w_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
            if w_pid.value == gui_pid:
                found.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        try:
            user32.EnumWindows(WNDENUMPROC(cb), 0)
        except Exception:
            return None
        return found[0] if found else None

    def run(self):
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────

def main():
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"session-monitor {__version__}")
        sys.exit(0)

    app = MonitorOverlay()
    app.run()


if __name__ == "__main__":
    main()
