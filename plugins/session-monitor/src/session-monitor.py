#!/usr/bin/env python3
"""Session Monitor overlay — always-on-top widget showing Claude Code and Codex sessions.

Usage:
    python  session-monitor.py      # with console
    pythonw session-monitor.py      # no console window (Windows)

No external dependencies — stdlib + tkinter + ctypes only.
"""

__version__ = "0.0.30"

import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import glob
import time
import threading
import tkinter as tk
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
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
    from codex_rollout_poller import (
        infer_rollout_state_for_session,
        is_virtual_id,
        poll_codex_rollouts,
    )
except ImportError:
    poll_codex_rollouts = None  # standalone fallback: degrade gracefully
    infer_rollout_state_for_session = None
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
    SW_SHOW = 5
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    WM_HOTKEY = 0x0312
    GWLP_WNDPROC = -4
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000
    VK_SPACE = 0x20
    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    KEYEVENTF_KEYUP = 0x0002
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    WM_QUIT = 0x0012

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

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong),
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
DEFAULT_BACKGROUND_OPACITY = 0.85


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
        # 1.0 is fully opaque. "opacity" is kept as a legacy alias.
        "background_opacity": DEFAULT_BACKGROUND_OPACITY,
        "opacity": DEFAULT_BACKGROUND_OPACITY,
        # Number of Korean-width chars reserved for the summary column.
        # The widget width is derived from this; write-state.py reads the same
        # value to cap Haiku output to what will actually fit on screen.
        "summary_max_chars": 12,
        "poll_interval_ms": 1000,
        "codex_question_check_interval_ms": 2000,
        "blink_interval_ms": 600,
        "blink_seconds": DONE_BLINK_SECONDS,
        "question_clear_grace_ms": 1000,
        # Auto-hide completed app-surface rows after 30 minutes. Terminal
        # sessions keep using process lifetime instead.
        "app_done_ttl_s": 1800,
        # Global hotkey that focuses the most recently completed visible row.
        # Empty string disables it. Example: "ctrl+alt+space".
        "latest_done_hotkey": "",
        "sound_enabled": True,
        # Optional event -> audio file path map. Supports done, question,
        # interrupted, and status_restored. File playback is best-effort and
        # falls back to the built-in beep sequence on failure.
        "sound_files": {},
        "claude_status_watch_enabled": True,
        "claude_status_check_interval_s": 60,
        "claude_status_watch_ttl_s": 14400,
    }


def _coerce_opacity(value, default=DEFAULT_BACKGROUND_OPACITY):
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        opacity = default
    return min(1.0, max(0.1, opacity))


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
        if "background_opacity" not in user and "opacity" in user:
            config["background_opacity"] = user["opacity"]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return config


CONFIG = load_config()


def get_background_opacity():
    """Return configured overlay opacity, accepting the legacy opacity key."""
    value = CONFIG.get("background_opacity", CONFIG.get("opacity", DEFAULT_BACKGROUND_OPACITY))
    return _coerce_opacity(value)


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


def _message_content_items(obj):
    if not isinstance(obj, dict):
        return ()
    message = obj.get("message")
    content = None
    if isinstance(message, dict):
        content = message.get("content")
    if content is None:
        content = obj.get("content")
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return ()


def _transcript_has_pending_ask_user_question(path):
    if not path:
        return False
    pending = set()
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                for item in _message_content_items(obj):
                    item_type = item.get("type")
                    if item_type == "tool_use" and item.get("name") == "AskUserQuestion":
                        tool_id = item.get("id")
                        if tool_id:
                            pending.add(str(tool_id))
                    elif item_type == "tool_result":
                        tool_id = item.get("tool_use_id")
                        if tool_id:
                            pending.discard(str(tool_id))
    except OSError:
        return False
    return bool(pending)


def _find_claude_transcript_path(session_id):
    if not session_id:
        return ""
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    pattern = os.path.join(root, "*", f"{session_id}.jsonl")
    try:
        matches = glob.glob(pattern)
    except OSError:
        return ""
    return matches[0] if matches else ""


def claude_session_has_pending_ask_user_question(session_data):
    if not isinstance(session_data, dict):
        return False
    transcript_path = (
        session_data.get("transcriptPath")
        or session_data.get("transcript_path")
        or _find_claude_transcript_path(session_data.get("sessionId"))
    )
    return _transcript_has_pending_ask_user_question(transcript_path)


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

    transcript_path = state_data.get("questionTranscriptPath")
    transcript_changed = _path_changed_since(
        transcript_path,
        state_data.get("questionTranscriptMtimeNs"),
        state_data.get("questionTranscriptSize"),
    )
    session_changed = _path_changed_since(
        session_path,
        state_data.get("questionSessionMtimeNs"),
        state_data.get("questionSessionSize"),
    )
    if transcript_changed or session_changed:
        if _transcript_has_pending_ask_user_question(transcript_path):
            return None
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


def find_codex_app_window(tree: dict) -> int | None:
    """Find a visible Codex Desktop app window. Windows only."""
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32
    candidates = []

    def enum_callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title_len = user32.GetWindowTextLengthW(hwnd)
        if title_len <= 0:
            return True
        w_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(w_pid))
        entry = tree.get(w_pid.value)
        if not entry or entry[1] != "codex.exe":
            return True
        # Child codex.exe helpers do not own the main Desktop window.
        if is_codex_desktop_app_pid(w_pid.value, tree):
            return True
        buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, buf, title_len + 1)
        title = buf.value
        if title and title != "Program Manager":
            rank = 0 if "codex" in title.lower() else 1
            candidates.append((rank, hwnd, title))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    candidates.sort(key=lambda item: (item[0], item[1]))
    if candidates:
        _log.debug("Codex app window candidates: %s", candidates[:5])
        return candidates[0][1]
    return None


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


def load_claude_native_session(pid):
    """Read Claude Code's own ~/.claude/sessions/{pid}.json metadata."""
    if not isinstance(pid, int):
        return {}
    path = os.path.join(os.path.expanduser("~"), ".claude", "sessions", f"{pid}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _claude_native_observed_at(path: str, data: dict) -> float:
    observed = 0.0
    if isinstance(data, dict):
        for key in ("updatedAt", "lastSignalAt", "startedAt"):
            try:
                value = float(data.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 1_000_000_000_000:
                value /= 1000.0
            observed = max(observed, value)
    try:
        observed = max(observed, os.path.getmtime(path))
    except OSError:
        pass
    return observed


def sync_claude_desktop_sessions(sessions_root: str, state_root: str, started_after: float = 0.0):
    """Mirror live Claude Desktop agent sessions into the monitor runtime.

    Claude Desktop starts Claude Code as a child claude.exe process. Those agent
    sessions can be discovered from ~/.claude/sessions even if our hook-owned
    runtime files were removed by an older monitor build.
    """
    native_root = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    if not os.path.isdir(native_root):
        return
    now = int(time.time())
    try:
        os.makedirs(sessions_root, exist_ok=True)
        os.makedirs(state_root, exist_ok=True)
    except OSError:
        return

    for path in glob.glob(os.path.join(native_root, "*.json")):
        data = _read_json_file(path)
        if not isinstance(data, dict) or data.get("entrypoint") != "claude-desktop":
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not is_claude_pid_alive(pid):
            continue
        if started_after and _claude_native_observed_at(path, data) < started_after:
            continue
        cwd = data.get("cwd", "") or ""
        session_id = data.get("sessionId", "") or ""
        sess_path = os.path.join(sessions_root, f"{pid}.json")
        state_path = os.path.join(state_root, f"{pid}.json")

        existing_sess = _read_json_file(sess_path)
        if not isinstance(existing_sess, dict) or existing_sess.get("entrypoint") != "claude-desktop":
            started_at = data.get("startedAt")
            if not started_at:
                try:
                    started_at = int(os.path.getmtime(path) * 1000)
                except OSError:
                    started_at = now * 1000
            sess_record = {
                "pid": pid,
                "sessionId": session_id,
                "cwd": cwd,
                "startedAt": started_at,
                "provider": "claude",
                "entrypoint": "claude-desktop",
            }
            try:
                _atomic_write_json(sess_path, sess_record)
            except OSError:
                pass

        if not os.path.exists(state_path):
            state_record = {
                "pid": pid,
                "state": "idle",
                "cwd": cwd,
                "updatedAt": now,
                "provider": "claude",
                "lastSignalSource": "desktop_session",
                "lastSignalAt": now,
            }
            try:
                _atomic_write_json(state_path, state_record)
            except OSError:
                pass


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
    kernel32 = ctypes.windll.kernel32

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)

    fg_hwnd = user32.GetForegroundWindow()
    our_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    try:
        for tid in {fg_tid, target_tid}:
            if tid and tid != our_tid and user32.AttachThreadInput(our_tid, tid, True):
                attached.append(tid)
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetForegroundWindow(hwnd)
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
    finally:
        for tid in attached:
            user32.AttachThreadInput(our_tid, tid, False)

    if user32.GetForegroundWindow() == hwnd:
        return

    # When invoked from a low-level keyboard hook, Windows may still deny
    # foreground activation and only flash the taskbar. SendInput(Alt) is the
    # most reliable foreground-lock bypass because Windows itself treats Alt as
    # permission to change foreground.
    try:
        send_alt_input()
        user32.BringWindowToTop(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetForegroundWindow(hwnd)
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
    except Exception:
        _log.debug("SendInput foreground fallback failed", exc_info=True)

    if user32.GetForegroundWindow() == hwnd:
        return

    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        _log.debug("foreground fallback failed", exc_info=True)


if IS_WINDOWS:
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", INPUT_UNION),
        ]


def send_alt_input():
    """Ask Windows to unlock foreground changes by sending an Alt tap."""
    if not IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32
    inputs = (INPUT * 2)()
    inputs[0].type = 1  # INPUT_KEYBOARD
    inputs[0].u.ki = KEYBDINPUT(VK_MENU, 0, 0, 0, None)
    inputs[1].type = 1
    inputs[1].u.ki = KEYBDINPUT(VK_MENU, 0, KEYEVENTF_KEYUP, 0, None)
    sent = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
    return sent == 2


_HOTKEY_MODIFIERS = {
    "alt": MOD_ALT if IS_WINDOWS else 0,
    "ctrl": MOD_CONTROL if IS_WINDOWS else 0,
    "control": MOD_CONTROL if IS_WINDOWS else 0,
    "shift": MOD_SHIFT if IS_WINDOWS else 0,
    "win": MOD_WIN if IS_WINDOWS else 0,
    "windows": MOD_WIN if IS_WINDOWS else 0,
    "meta": MOD_WIN if IS_WINDOWS else 0,
    "cmd": MOD_WIN if IS_WINDOWS else 0,
}

_HOTKEY_KEYS = {
    "space": VK_SPACE if IS_WINDOWS else 0x20,
    "esc": 0x1B,
    "escape": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}
for _i in range(1, 25):
    _HOTKEY_KEYS[f"f{_i}"] = 0x70 + _i - 1


def parse_hotkey(value):
    """Parse 'ctrl+alt+space' style config into (modifiers, vk), or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = [p.strip().lower() for p in re.split(r"[+\-]", value) if p.strip()]
    if not parts:
        return None
    modifiers = 0
    key = None
    for part in parts:
        if part in _HOTKEY_MODIFIERS:
            modifiers |= _HOTKEY_MODIFIERS[part]
            continue
        if key is not None:
            return None
        if part in _HOTKEY_KEYS:
            key = _HOTKEY_KEYS[part]
        elif len(part) == 1 and "a" <= part <= "z":
            key = ord(part.upper())
        elif len(part) == 1 and "0" <= part <= "9":
            key = ord(part)
        else:
            return None
    if key is None:
        return None
    return (modifiers | (MOD_NOREPEAT if IS_WINDOWS else 0), key)


def short_cwd(cwd: str) -> str:
    """Extract project folder name from CWD path."""
    if not cwd:
        return "unknown"
    return os.path.basename(cwd.rstrip("/\\"))


_FOLDER_MAX_CHARS = 9  # 'firstgame' length cap

_PROVIDER_MARKERS = {"claude": "C", "codex": "G"}


def provider_glyph(provider) -> str:
    """Short visual prefix identifying which LLM CLI a row belongs to."""
    glyph = provider_marker(provider)
    return f"{glyph} " if glyph else ""


def provider_marker(provider) -> str:
    """Single-character row marker identifying which LLM CLI owns the row."""
    if not provider:
        return ""
    return _PROVIDER_MARKERS.get(provider, "")


def row_marker(provider, entrypoint="") -> str:
    """Marker glyph for a row."""
    return provider_marker(provider)


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
                 "state_changed_at", "completed_at", "interrupt_source", "interrupt_at",
                 "codex_source", "codex_originator", "codex_surface",
                 "entrypoint")

    def __init__(self, pid, cwd, state="idle", updated_at=0, provider="claude", session_id=""):
        self.pid = pid
        self.cwd = cwd
        self.session_id = session_id or ""
        self.state = state
        self.updated_at = updated_at
        self.state_changed_at = updated_at or 0
        self.completed_at = updated_at if state == "done" else 0
        self.slot = 0
        self.summary = ""
        self.provider = provider
        self.entrypoint = ""
        self.codex_source = ""
        self.codex_originator = ""
        self.codex_surface = ""
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


def snapshot_instance(inst: Instance) -> Instance:
    """Copy activation-relevant row data after a terminal state is observed."""
    snap = Instance(
        inst.pid,
        inst.cwd,
        state=inst.state,
        updated_at=inst.updated_at,
        provider=inst.provider,
        session_id=inst.session_id,
    )
    for attr in Instance.__slots__:
        setattr(snap, attr, getattr(inst, attr))
    return snap


class InstanceTracker:
    """Polls session + state files to maintain live instance list."""

    PINS_FILE = pins_file()

    def __init__(self):
        self.sessions_dir = sessions_dir()
        self.state_dir = get_state_dir()
        self.started_at = time.time()
        # Keys are int PIDs for hook-driven rows and "codex:<sid8>" strings
        # for the rollout poller's PID-less virtual rows.
        self.instances: dict = {}
        # Tick-over cache for codex_rollout_poller — keeps decoded session
        # metadata (cwd, sessionId) keyed by file path so we don't re-parse
        # the first line of every rollout JSONL on every poll.
        self._codex_rollout_cache: dict = {}
        # PID-owning Codex rows need rollout inspection to detect
        # request_user_input waits; keep it throttled because rollout files can
        # grow large.
        self._codex_question_cache: dict = {}
        self._codex_question_check_s = max(
            0.5,
            float(CONFIG.get("codex_question_check_interval_ms", 2000)) / 1000.0,
        )
        self._app_done_ttl_s = max(0.0, float(CONFIG.get("app_done_ttl_s", 1800)))
        self._dismissed_keys = {}
        self.latest_completed_instance = None
        self.pins = self._load_pins()

    @staticmethod
    def pin_key(inst: Instance) -> str:
        return inst.session_id or str(inst.pid)

    @staticmethod
    def dismiss_key(provider, session_id, pid) -> str:
        if session_id:
            return f"{provider or 'claude'}:session:{session_id}"
        return f"{provider or 'claude'}:pid:{pid}"

    @staticmethod
    def _dismiss_observed_at(state_data) -> float:
        if not isinstance(state_data, dict):
            return 0.0
        # Native Claude Desktop session discovery recreates idle files for live
        # agents. That is not a new turn signal, so don't use it to undo a
        # manual dismissal.
        if state_data.get("lastSignalSource") == "desktop_session":
            return 0.0
        observed = 0.0
        if state_data.get("lastSignalSource") == "rollout":
            keys = ("rolloutMtime", "updatedAt", "threadUpdatedAt")
        else:
            keys = ("lastSignalAt", "updatedAt", "threadUpdatedAt", "rolloutMtime")
        for key in keys:
            try:
                value = float(state_data.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            observed = max(observed, value)
        return observed

    @staticmethod
    def _activity_observed_at(state_data) -> float:
        if not isinstance(state_data, dict):
            return 0.0
        observed = 0.0
        for key in ("lastSignalAt", "updatedAt", "threadUpdatedAt", "rolloutMtime"):
            try:
                value = float(state_data.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            observed = max(observed, value)
        return observed

    @staticmethod
    def _ttl_observed_at(state_data) -> float:
        if not isinstance(state_data, dict):
            return 0.0
        if state_data.get("lastSignalSource") == "rollout":
            keys = ("rolloutMtime", "updatedAt", "threadUpdatedAt")
        elif state_data.get("lastSignalSource") == "desktop_session":
            keys = ("updatedAt", "threadUpdatedAt")
        else:
            keys = ("lastSignalAt", "updatedAt", "threadUpdatedAt", "rolloutMtime")
        observed = 0.0
        for key in keys:
            try:
                value = float(state_data.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            observed = max(observed, value)
        return observed

    @staticmethod
    def _is_app_passive_row(provider, entrypoint, state_data) -> bool:
        if provider == "claude":
            return (
                entrypoint == "claude-desktop"
                or (
                    isinstance(state_data, dict)
                    and state_data.get("lastSignalSource") == "desktop_session"
                )
            )
        if provider == "codex" and isinstance(state_data, dict):
            return state_data.get("codexSurface") == "app"
        return False

    def _is_pre_start_passive_row(self, provider, entrypoint, pid, state_data) -> bool:
        cutoff = self._startup_visibility_cutoff()
        if not cutoff:
            return False
        passive = (
            is_virtual_id(pid)
            or (provider == "claude" and entrypoint == "claude-desktop")
            or (
                provider == "claude"
                and isinstance(state_data, dict)
                and state_data.get("lastSignalSource") == "desktop_session"
            )
        )
        return passive and self._activity_observed_at(state_data) < cutoff

    def _startup_visibility_cutoff(self) -> float:
        if not self.started_at or self._app_done_ttl_s <= 0:
            return 0.0
        return max(0.0, self.started_at - self._app_done_ttl_s)

    def _is_app_done_expired(self, provider, entrypoint, pid, session_id, state_data, now=None) -> bool:
        if self._app_done_ttl_s <= 0:
            return False
        if not isinstance(state_data, dict) or state_data.get("state") != "done":
            return False
        if not self._is_app_passive_row(provider, entrypoint, state_data):
            return False
        if (session_id or str(pid)) in self.pins:
            return False
        observed_at = self._ttl_observed_at(state_data)
        if observed_at <= 0:
            return False
        return (now or time.time()) - observed_at >= self._app_done_ttl_s

    def _state_observed_at_for_pid(self, pid) -> float:
        try:
            data = _read_json_file(os.path.join(self.state_dir, f"{pid}.json"))
        except Exception:
            data = None
        return self._dismiss_observed_at(data)

    def _is_dismissed(self, provider, session_id, pid, observed_at=0.0) -> bool:
        key = self.dismiss_key(provider, session_id, pid)
        dismissed_at = self._dismissed_keys.get(key)
        if dismissed_at is None:
            return False
        try:
            observed_at = float(observed_at or 0)
        except (TypeError, ValueError):
            observed_at = 0.0
        if observed_at > dismissed_at:
            self._dismissed_keys.pop(key, None)
            return False
        return True

    def _remove_monitor_files(self, pid):
        for root in (self.sessions_dir, self.state_dir):
            try:
                os.remove(os.path.join(root, f"{pid}.json"))
            except OSError:
                pass

    def dismiss_instance(self, pid) -> bool:
        """Hide a row for the current monitor run and remove its runtime files."""
        inst = self.instances.get(pid)
        if not inst:
            return False
        key = self.dismiss_key(inst.provider, inst.session_id, inst.pid)
        self._dismissed_keys[key] = time.time()
        pin_key = self.pin_key(inst)
        self.pins.pop(pin_key, None)
        self._remove_monitor_files(inst.pid)
        del self.instances[pid]
        self.save_pins()
        return True

    def remember_completed_instance(self, inst: Instance):
        if not inst or inst.state != "done":
            return
        if not inst.completed_at:
            inst.completed_at = inst.updated_at or int(time.time())
        current = self.latest_completed_instance
        if current:
            current_key = (current.completed_at or current.updated_at or 0, str(current.pid))
            next_key = (inst.completed_at or inst.updated_at or 0, str(inst.pid))
            if next_key < current_key:
                return
        self.latest_completed_instance = snapshot_instance(inst)

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

    def _codex_waits_for_user_cached(self, session_id) -> bool:
        if not session_id:
            return False
        now = time.monotonic()
        cached = self._codex_question_cache.get(session_id)
        if cached and now - cached.get("checked_at", 0) < self._codex_question_check_s:
            return cached.get("state") == "question"
        state = None
        if infer_rollout_state_for_session is not None:
            try:
                state = infer_rollout_state_for_session(session_id)
            except Exception:
                _log.debug("failed to infer Codex rollout state", exc_info=True)
        self._codex_question_cache[session_id] = {
            "checked_at": now,
            "state": state,
        }
        return state == "question"

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
        try:
            sync_claude_desktop_sessions(
                self.sessions_dir,
                self.state_dir,
                self._startup_visibility_cutoff(),
            )
        except Exception:
            _log.debug("claude desktop session sync failed", exc_info=True)
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
                    started_after=self._startup_visibility_cutoff(),
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
                entrypoint = sess.get("entrypoint", "") or ""
                provider = sess.get("provider", "claude") or "claude"
                if provider == "claude" and isinstance(pid, int) and not entrypoint:
                    native_sess = load_claude_native_session(pid)
                    entrypoint = native_sess.get("entrypoint", "") or ""
                    cwd = cwd or native_sess.get("cwd", "") or ""
                    session_id = session_id or native_sess.get("sessionId", "") or ""
                if pid is None:
                    continue
                session_by_pid[pid] = sess
            except Exception:
                continue

            if self._is_dismissed(provider, session_id, pid, self._state_observed_at_for_pid(pid)):
                self._remove_monitor_files(pid)
                continue

            state_for_pid = _read_json_file(os.path.join(self.state_dir, f"{pid}.json"))
            if self._is_pre_start_passive_row(provider, entrypoint, pid, state_for_pid):
                self._remove_monitor_files(pid)
                continue
            if self._is_app_done_expired(provider, entrypoint, pid, session_id, state_for_pid):
                if pid in self.instances:
                    self.remember_completed_instance(self.instances[pid])
                    del self.instances[pid]
                    changed = True
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
            if (
                IS_WINDOWS
                and entrypoint != "claude-desktop"
                and is_nested_claude_pid(pid, proc_tree, marker_pids)
            ):
                continue

            seen_pids.add(pid)

            if pid not in self.instances:
                self.instances[pid] = Instance(pid, cwd, session_id=session_id)
                self.instances[pid].entrypoint = entrypoint
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
            if self.instances[pid].entrypoint != entrypoint:
                self.instances[pid].entrypoint = entrypoint
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
                saved_completed_at = int(st.get("completedAt", 0) or 0)
                saved_codex_source = st.get("codexSource", "") or ""
                saved_codex_originator = st.get("codexOriginator", "") or ""
                saved_codex_surface = st.get("codexSurface", "") or ""
            except Exception:
                continue

            session_data = session_by_pid.get(pid)
            state_session_id = (session_data or {}).get("sessionId") or st.get("sessionId") or ""
            state_entrypoint = (session_data or {}).get("entrypoint", "") or ""
            if self._is_pre_start_passive_row(saved_provider, state_entrypoint, pid, st):
                self._remove_monitor_files(pid)
                continue
            if self._is_app_done_expired(saved_provider, state_entrypoint, pid, state_session_id, st):
                if pid in self.instances:
                    self.remember_completed_instance(self.instances[pid])
                    del self.instances[pid]
                    changed = True
                continue
            if self._is_dismissed(saved_provider, state_session_id, pid, self._dismiss_observed_at(st)):
                self._remove_monitor_files(pid)
                continue

            if saved_provider == "claude" and session_waits_for_user(session_data):
                state = "question"
            elif saved_provider == "claude" and claude_session_has_pending_ask_user_question(session_data):
                state = "question"
            elif saved_provider == "codex" and self._codex_waits_for_user_cached(
                (session_data or {}).get("sessionId")
            ):
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
                desired_completed_at = saved_completed_at if state == "done" else 0
                if state == "done" and not desired_completed_at:
                    desired_completed_at = inst.completed_at or updated_at or 0
                if desired_completed_at != inst.completed_at:
                    inst.completed_at = desired_completed_at
                    changed = True
                if saved_codex_source != inst.codex_source:
                    inst.codex_source = saved_codex_source
                    changed = True
                if saved_codex_originator != inst.codex_originator:
                    inst.codex_originator = saved_codex_originator
                    changed = True
                if saved_codex_surface != inst.codex_surface:
                    inst.codex_surface = saved_codex_surface
                    changed = True
                # state JSON wins for cwd: sessions/*.json may have been
                # rebuilt with a stale subdirectory cwd after a Claude Code
                # self-update; state JSON preserves the home cwd.
                if saved_cwd and saved_cwd != inst.cwd:
                    inst.cwd = saved_cwd
                    changed = True
                old_updated_at = inst.updated_at
                state_changed = inst.state != state
                terminal_refreshed = (
                    not state_changed
                    and state == "done"
                    and updated_at
                    and updated_at > old_updated_at
                )
                if state_changed or old_updated_at != updated_at:
                    old_state = inst.state
                    inst.state = state
                    inst.updated_at = updated_at
                    if (state_changed or terminal_refreshed) and state != "working":
                        inst.state_changed_at = updated_at or int(time.time())
                    if state == "done" and (old_state != "done" or terminal_refreshed):
                        if old_state != "done":
                            inst.completed_at = saved_completed_at or updated_at or int(time.time())
                        inst.done_since = time.monotonic()
                        inst.blink_on = True
                        events.append("done")
                        self.remember_completed_instance(inst)
                        # PID-less codex rows have no Stop hook to spawn the
                        # summarizer for them — do it here on the done edge.
                        if is_virtual_id(pid) and _should_spawn_codex_summary(st):
                            _spawn_codex_summary(pid)
                    elif state == "interrupted" and old_state != "interrupted":
                        inst.completed_at = 0
                        inst.done_since = time.monotonic()
                        inst.blink_on = True
                        events.append("interrupted")
                    elif state == "question" and old_state != "question":
                        inst.blink_on = True
                        inst.completed_at = 0
                        inst.done_since = 0.0
                        events.append("question")
                    elif state not in ("done", "interrupted"):
                        inst.completed_at = 0
                        inst.done_since = 0.0
                if state_changed or terminal_refreshed:
                    changed = True
                if inst.state == "done":
                    self.remember_completed_instance(inst)

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
                self.remember_completed_instance(self.instances[pid])
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
        self.latest_done_hotkey = CONFIG.get("latest_done_hotkey", "")
        self._hotkey_id = 0x534D
        self._hotkey_hwnd = 0
        self._hotkey_registered = False
        self._hotkey_prev_wndproc = None
        self._hotkey_wndproc = None
        self._hotkey_parsed = parse_hotkey(self.latest_done_hotkey)
        self._keyboard_hook_handle = 0
        self._keyboard_hook_thread_id = 0
        self._keyboard_hook_thread = None
        self._keyboard_hook_proc = None
        self._keyboard_hook_event = threading.Event()
        self._keyboard_hook_last_fire = 0.0
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
        self.root.wm_attributes("-alpha", get_background_opacity())
        self.root.configure(bg=THEME["bg"])

        # Fonts (must precede width derivation that calls font.measure)
        try:
            self.font_title = tkfont.Font(family="Segoe UI", size=9, weight="bold")
            self.font_row = tkfont.Font(family="Segoe UI", size=9)
            self.font_marker = tkfont.Font(family="Segoe UI", size=9, weight="bold")
            self.font_state = tkfont.Font(family="Segoe UI", size=8)
            self.font_empty = tkfont.Font(family="Segoe UI", size=9, slant="italic")
        except Exception:
            self.font_title = tkfont.Font(size=9, weight="bold")
            self.font_row = tkfont.Font(size=9)
            self.font_marker = tkfont.Font(size=9, weight="bold")
            self.font_state = tkfont.Font(size=8)
            self.font_empty = tkfont.Font(size=9, slant="italic")

        # Derive column widths and overall widget width.
        # The widest possible folder head is "firstgame(99)" (folder cap + slot).
        # The summary column reserves enough pixels for `summary_max_chars` of
        # CJK width — the same N is passed to write-state.py so Haiku stays
        # within the column.
        self.col_dot_w = max(
            self.font_marker.measure("G  "),
            self.font_marker.measure("C  "),
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
        self.close_btn.bind("<Button-1>", lambda _: self._close())
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
        self.root.bind("<Destroy>", self._on_destroy, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._install_latest_done_hotkey()
        self._poll_keyboard_hook_event()

        # Start loops
        self._poll_loop()
        self._blink_loop()

    def _close(self):
        self._unregister_latest_done_hotkey()
        self.root.destroy()

    def _on_destroy(self, event):
        if event.widget == self.root:
            self._unregister_latest_done_hotkey()

    def _install_latest_done_hotkey(self):
        if not self._hotkey_parsed or not IS_WINDOWS:
            return
        try:
            # Tk window subclassing through ctypes is fragile under pythonw and
            # can terminate the process without a Python traceback. Use a
            # low-level keyboard hook instead; it is slightly broader but does
            # not mutate Tk's native WndProc.
            self._install_keyboard_hook_hotkey()
        except Exception:
            _log.debug("failed to install latest_done_hotkey", exc_info=True)
            self._unregister_latest_done_hotkey()
            self._install_keyboard_hook_hotkey()

    def _install_keyboard_hook_hotkey(self):
        if not IS_WINDOWS or not self._hotkey_parsed or self._keyboard_hook_thread:
            return

        def hook_thread():
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hook_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            def low_level_keyboard_proc(n_code, w_param, l_param):
                if n_code >= 0 and int(w_param) in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    data = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if self._keyboard_hook_matches(data.vkCode):
                        now = time.monotonic()
                        if now - self._keyboard_hook_last_fire > 0.4:
                            self._keyboard_hook_last_fire = now
                            self._activate_latest_done_session(clear_highlight=False)
                        return 1
                return user32.CallNextHookEx(self._keyboard_hook_handle, n_code, w_param, l_param)

            self._keyboard_hook_thread_id = kernel32.GetCurrentThreadId()
            self._keyboard_hook_proc = hook_proc_type(low_level_keyboard_proc)
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                hook_proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
            module_handle = kernel32.GetModuleHandleW(None)
            hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                self._keyboard_hook_proc,
                module_handle,
                0,
            )
            if not hook:
                _log.warning("failed to install keyboard hook for latest_done_hotkey=%r", self.latest_done_hotkey)
                return
            self._keyboard_hook_handle = hook
            _log.info("installed keyboard hook for latest_done_hotkey=%r", self.latest_done_hotkey)
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        self._keyboard_hook_thread = threading.Thread(target=hook_thread, name="session-monitor-hotkey", daemon=True)
        self._keyboard_hook_thread.start()

    def _keyboard_hook_matches(self, vk_code) -> bool:
        if not IS_WINDOWS or not self._hotkey_parsed:
            return False
        modifiers, vk = self._hotkey_parsed
        if int(vk_code) != int(vk):
            return False
        user32 = ctypes.windll.user32

        def down(vk_value):
            return bool(user32.GetAsyncKeyState(vk_value) & 0x8000)

        base_modifiers = modifiers & ~MOD_NOREPEAT
        if base_modifiers & MOD_CONTROL and not down(VK_CONTROL):
            return False
        if base_modifiers & MOD_ALT and not down(VK_MENU):
            return False
        if base_modifiers & MOD_SHIFT and not down(VK_SHIFT):
            return False
        if base_modifiers & MOD_WIN and not (down(VK_LWIN) or down(VK_RWIN)):
            return False
        return True

    def _poll_keyboard_hook_event(self):
        if self._keyboard_hook_event.is_set():
            self._keyboard_hook_event.clear()
            self._activate_latest_done_session()
        if self._keyboard_hook_thread:
            self.root.after(50, self._poll_keyboard_hook_event)

    def _subclass_hotkey_window(self, hwnd):
        if not IS_WINDOWS or self._hotkey_wndproc is not None:
            return
        user32 = ctypes.windll.user32
        long_ptr = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        wndproc_type = ctypes.WINFUNCTYPE(
            long_ptr,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        set_window_long = user32.SetWindowLongPtrW
        set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
        set_window_long.restype = long_ptr
        call_window_proc = user32.CallWindowProcW
        call_window_proc.argtypes = [long_ptr, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        call_window_proc.restype = long_ptr

        def wndproc(h_wnd, msg, w_param, l_param):
            if msg == WM_HOTKEY and int(w_param) == self._hotkey_id:
                self.root.after(0, self._activate_latest_done_session)
                return 0
            return call_window_proc(self._hotkey_prev_wndproc, h_wnd, msg, w_param, l_param)

        self._hotkey_wndproc = wndproc_type(wndproc)
        prev = set_window_long(hwnd, GWLP_WNDPROC, long_ptr(ctypes.cast(self._hotkey_wndproc, ctypes.c_void_p).value))
        self._hotkey_prev_wndproc = prev

    def _unregister_latest_done_hotkey(self):
        if not IS_WINDOWS:
            return
        try:
            user32 = ctypes.windll.user32
            if self._hotkey_registered and self._hotkey_hwnd:
                user32.UnregisterHotKey(self._hotkey_hwnd, self._hotkey_id)
            if self._hotkey_hwnd and self._hotkey_prev_wndproc:
                long_ptr = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
                set_window_long = user32.SetWindowLongPtrW
                set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
                set_window_long.restype = long_ptr
                set_window_long(self._hotkey_hwnd, GWLP_WNDPROC, long_ptr(self._hotkey_prev_wndproc))
            if self._keyboard_hook_handle:
                user32.UnhookWindowsHookEx(self._keyboard_hook_handle)
            if self._keyboard_hook_thread_id:
                user32.PostThreadMessageW(self._keyboard_hook_thread_id, WM_QUIT, 0, 0)
        except Exception:
            _log.debug("failed to unregister latest_done_hotkey", exc_info=True)
        finally:
            self._hotkey_registered = False
            self._hotkey_hwnd = 0
            self._hotkey_prev_wndproc = None
            self._hotkey_wndproc = None
            self._keyboard_hook_handle = 0
            self._keyboard_hook_thread_id = 0
            self._keyboard_hook_thread = None
            self._keyboard_hook_proc = None

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
            try:
                if not self.root.attributes("-topmost"):
                    self.root.wm_attributes("-topmost", True)
            except Exception:
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
        """Play a configured sound file or a short fallback chime."""
        chimes = {
            "done": [(880, 80), (1175, 80), (1397, 120)],      # A5-D6-F6 rising major
            "question": [(1047, 100), (880, 130)],              # C6-A5 descending
            "interrupted": [(880, 80), (660, 80), (440, 120)],  # A5-E5-A4 descending
            "status_restored": [(660, 80), (880, 80), (1175, 140)],
        }
        seq = chimes.get(event)
        sound_file = MonitorOverlay._sound_file_for_event(event)
        if sound_file or (IS_WINDOWS and seq):
            def _play():
                if sound_file and MonitorOverlay._play_sound_file(sound_file):
                    return
                if not IS_WINDOWS or not seq:
                    return
                for freq, ms in seq:
                    winsound.Beep(freq, ms)
            threading.Thread(target=_play, daemon=True).start()

    @staticmethod
    def _sound_file_for_event(event: str) -> str:
        files = CONFIG.get("sound_files")
        if not isinstance(files, dict):
            return ""
        value = files.get(event)
        if not isinstance(value, str) or not value.strip():
            return ""
        path = os.path.expandvars(os.path.expanduser(value.strip()))
        return path if os.path.exists(path) else ""

    @staticmethod
    def _play_sound_file(path: str) -> bool:
        """Best-effort cross-platform audio playback without extra deps."""
        if not path:
            return False
        try:
            if IS_WINDOWS:
                return MonitorOverlay._play_sound_file_windows(path)
            if sys.platform == "darwin":
                return MonitorOverlay._run_sound_player(["afplay", path])
            players = (
                ("paplay", [path]),
                ("aplay", [path]),
                ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet", path]),
                ("mpg123", ["-q", path]),
                ("mpv", ["--no-video", "--really-quiet", path]),
                ("cvlc", ["--play-and-exit", "--intf", "dummy", path]),
            )
            for exe, args in players:
                if MonitorOverlay._run_sound_player([exe, *args]):
                    return True
        except Exception:
            _log.debug("sound file playback failed: %s", path, exc_info=True)
        return False

    @staticmethod
    def _play_sound_file_windows(path: str) -> bool:
        if not IS_WINDOWS:
            return False
        alias = (
            f"session_monitor_sound_{os.getpid()}_"
            f"{threading.get_ident()}_{int(time.time() * 1000)}"
        )
        winmm = ctypes.windll.winmm

        def mci(command: str) -> int:
            return int(winmm.mciSendStringW(command, None, 0, None))

        quoted = path.replace('"', '')
        ext = os.path.splitext(path)[1].lower()
        type_arg = " type mpegvideo" if ext in (".mp3", ".mpeg", ".mpg") else ""
        if mci(f'open "{quoted}"{type_arg} alias {alias}') != 0:
            return False
        try:
            return mci(f"play {alias} wait") == 0
        finally:
            mci(f"close {alias}")

    @staticmethod
    def _run_sound_player(cmd: list[str]) -> bool:
        exe = shutil.which(cmd[0])
        if not exe:
            return False
        try:
            result = subprocess.run(
                [exe, *cmd[1:]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

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
        new_geo = f"{self.width}x{height}+{x_pos}+{y_pos}"
        if geo != new_geo:
            self.root.geometry(new_geo)

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

        dot_glyph = row_marker(inst.provider, inst.entrypoint) or "?"
        dot_box = tk.Frame(
            frame, bg=base_bg,
            width=self.col_dot_w, height=cell_h,
        )
        dot_box.grid(row=0, column=0, sticky="w", padx=(4, 0))
        dot_box.grid_propagate(False)
        dot_box.pack_propagate(False)
        marker_fg = state_color if inst.pinned_at is None else THEME["fg"]
        dot = tk.Label(
            dot_box, text=dot_glyph, font=self.font_marker,
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

        def on_row_dismiss(event, pid=inst.pid):
            self._dismiss_row(pid)
            return "break"

        # Dot already has its own pin-toggle handler; it still participates in
        # row-hover recoloring via the generic Enter/Leave below.
        for w in (frame, folder_box, folder_lbl,
                  summary_box, summary_lbl, state_lbl):
            w.bind("<Button-1>", on_click)
        for w in (frame, dot_box, dot, folder_box, folder_lbl,
                  summary_box, summary_lbl, state_lbl):
            w.bind("<Button-3>", on_row_dismiss)
            w.bind("<Button-2>", on_row_dismiss)
            w.bind("<Control-Button-1>", on_row_dismiss)
        for w in (frame, dot_box, dot, folder_box, folder_lbl,
                  summary_box, summary_lbl, state_lbl):
            w.bind("<Enter>", lambda _e, f=frame: self._row_hover(f, True))
            w.bind("<Leave>", lambda _e, f=frame: self._row_hover(f, False))

        self.row_widgets.append({
            "frame": frame, "dot": dot, "name": name_lbl,
            "state": state_lbl, "instance": inst, "base_bg": base_bg,
        })

    def _dismiss_row(self, pid):
        """Remove a row from the current monitor view without killing the session."""
        if self.tracker.dismiss_instance(pid):
            self._rebuild_rows()

    @staticmethod
    def _latest_done_instance(instances):
        candidates = [inst for inst in instances if inst.state == "done"]
        if not candidates:
            return None
        return max(candidates, key=lambda i: (i.completed_at or i.updated_at or 0, str(i.pid)))

    def _activate_latest_done_session(self, clear_highlight=True):
        inst = self._latest_done_instance(self.tracker.instances.values())
        if not inst:
            inst = getattr(self.tracker, "latest_completed_instance", None)
        if not inst:
            _log.debug("latest_done_hotkey pressed but no completed session is known")
            return
        if clear_highlight:
            self._clear_recent_highlight(inst.pid)
        self._activate_terminal(inst.pid, fallback_inst=inst)

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

    def _activate_terminal(self, claude_pid, fallback_inst=None):
        try:
            inst = self.tracker.instances.get(claude_pid) or fallback_inst

            # wezterm 페인 우선 — pane_id 기반 정확 매칭
            if inst and inst.wezterm and self._activate_wezterm_pane(inst.wezterm):
                return

            # Virtual codex rows from the rollout poller don't carry a real
            # PID. Fall back to matching live codex.exe processes by cwd; this
            # is not as precise as hook-captured wezterm pane IDs, but it makes
            # PID-less rows clickable when hooks are unavailable.
            if not isinstance(claude_pid, int):
                if inst and inst.provider == "codex":
                    self._activate_codex_pidless(
                        inst.cwd,
                        inst.session_id,
                        getattr(inst, "codex_surface", ""),
                        getattr(inst, "codex_originator", ""),
                    )
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

            if (
                inst
                and inst.provider == "claude"
                and inst.entrypoint == "claude-desktop"
                and self._activate_claude_desktop(inst)
            ):
                return

            # 폴백: 프로세스 트리 탐색
            tree = build_process_tree()
            cwd = inst.cwd if inst else ""
            hwnd = find_window_for_pid(claude_pid, tree, cwd)
            if hwnd:
                activate_window(hwnd)
            else:
                _log.warning("_activate_terminal: no hwnd found for pid=%d", claude_pid)
        except Exception:
            _log.error("_activate_terminal failed for pid=%s", claude_pid, exc_info=True)

    def _activate_claude_desktop(self, inst: Instance) -> bool:
        """Raise the Claude Desktop window for a Claude Code agent subprocess."""
        if not IS_WINDOWS or not isinstance(inst.pid, int):
            return False
        try:
            tree = build_process_tree()
            hwnd = find_window_for_pid(inst.pid, tree, inst.cwd)
            if hwnd:
                activate_window(hwnd)
                return True
        except Exception:
            _log.debug("_activate_claude_desktop failed", exc_info=True)
        return False

    def _activate_codex_pidless(
        self,
        cwd: str,
        session_id: str = "",
        codex_surface: str = "",
        codex_originator: str = "",
    ):
        """Best-effort window activation for a Codex row the rollout poller
        registered without a PID. App rows can be opened directly by thread
        deep link; CLI rows prefer terminal/pane activation."""
        try:
            if session_id and self._open_codex_thread(session_id):
                if codex_surface == "app" or str(codex_originator or "").lower() == "codex desktop":
                    if self._activate_codex_app_window():
                        return
                else:
                    return

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

    def _activate_codex_app_window(self) -> bool:
        if not IS_WINDOWS:
            return False
        for _ in range(12):
            tree = build_process_tree()
            hwnd = find_codex_app_window(tree)
            if hwnd:
                activate_window(hwnd)
                if ctypes.windll.user32.GetForegroundWindow() == hwnd:
                    return True
            time.sleep(0.1)
        return False

    def _open_codex_thread(self, session_id: str) -> bool:
        """Open a local Codex app thread via the documented codex:// scheme."""
        sid = (session_id or "").strip()
        if not sid:
            return False
        url = "codex://threads/" + urllib.parse.quote(sid, safe="")
        try:
            if IS_WINDOWS and hasattr(os, "startfile"):
                os.startfile(url)  # type: ignore[attr-defined]
                return True
            return bool(webbrowser.open(url))
        except Exception:
            _log.debug("failed to open Codex thread deep link: %s", url, exc_info=True)
            return False

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
