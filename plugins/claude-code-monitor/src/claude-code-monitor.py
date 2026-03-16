#!/usr/bin/env python3
"""Claude Code Monitor Overlay — always-on-top widget showing all Claude Code instances.

Usage:
    python  claude-code-monitor.py      # with console
    pythonw claude-code-monitor.py      # no console window (Windows)

No external dependencies — stdlib + tkinter + ctypes only.
"""

__version__ = "0.0.6"

import json
import logging
import logging.handlers
import os
import sys
import glob
import time
import threading
import tkinter as tk
from tkinter import font as tkfont

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
    log_dir = os.path.join(os.path.expanduser("~"), ".claude", "monitor")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("claude_monitor")
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
        "idle": "Idle",
        "no_instances": "No instances",
    },
    "ko": {
        "working": "\uc791\uc5c5\uc911",
        "done": "\uc791\uc5c5\uc644\ub8cc",
        "question": "\uc9c8\ubb38\uc788\uc74c",
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
    "idle":        "#585b70",   # grey
    "hover":       "#313244",
    "close_hover": "#f38ba8",   # red
}

DONE_BLINK_SECONDS = 5  # "done" blinks for this long, then stays solid


# ── Config ────────────────────────────────────────────────────────

def _default_config():
    return {
        "language": "en",
        "opacity": 0.65,
        "width": 260,
        "poll_interval_ms": 500,
        "blink_interval_ms": 600,
        "sound_enabled": True,
    }


def load_config():
    """Load config from ~/.claude/monitor/config.json, falling back to defaults."""
    config = _default_config()
    config_path = os.path.join(
        os.path.expanduser("~"), ".claude", "monitor", "config.json"
    )
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
    return os.environ.get(
        "CLAUDE_MONITOR_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".claude", "monitor", "state"),
    )


def get_label(key):
    """Return localized label."""
    lang = CONFIG.get("language", "en")
    return LABELS.get(lang, LABELS["en"]).get(key, key)


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

    # Phase 1c: Windows 11 delegation model fallback
    # WindowsTerminal.exe is NOT a descendant of the shell process.
    # Add all WT PIDs so their windows become candidates.
    wt_pids = {p for p, (parent, exe) in tree.items()
                if exe == "windowsterminal.exe"}
    if wt_pids:
        _log.debug("  Phase 1c: adding %d WindowsTerminal PIDs as candidates", len(wt_pids))
        pid_set |= wt_pids

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

    if len(tied) > 1 and cwd:
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


# ── InstanceTracker ───────────────────────────────────────────────

class Instance:
    __slots__ = ("pid", "cwd", "state", "updated_at", "display_name",
                 "blink_on", "done_since", "hwnd")

    def __init__(self, pid, cwd, state="idle", updated_at=0):
        self.pid = pid
        self.cwd = cwd
        self.state = state
        self.updated_at = updated_at
        self.display_name = short_cwd(cwd)
        self.blink_on = True
        self.done_since = 0.0
        self.hwnd = 0


class InstanceTracker:
    """Polls session + state files to maintain live instance list."""

    def __init__(self):
        home = os.path.expanduser("~")
        self.sessions_dir = os.path.join(home, ".claude", "sessions")
        self.state_dir = get_state_dir()
        self.instances: dict[int, Instance] = {}

    def poll(self):
        """Refresh instance list. Returns (changed, events)."""
        changed = False
        events = []
        seen_pids = set()

        for sf in glob.glob(os.path.join(self.sessions_dir, "*.json")):
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    sess = json.load(f)
                pid = sess.get("pid")
                cwd = sess.get("cwd", "")
                if pid is None:
                    continue
            except Exception:
                continue

            if not is_pid_alive(pid):
                if pid in self.instances:
                    del self.instances[pid]
                    changed = True
                try:
                    os.remove(sf)
                except Exception:
                    pass
                state_file = os.path.join(self.state_dir, f"{pid}.json")
                try:
                    os.remove(state_file)
                except Exception:
                    pass
                continue

            seen_pids.add(pid)

            if pid not in self.instances:
                self.instances[pid] = Instance(pid, cwd)
                changed = True
            elif self.instances[pid].cwd != cwd:
                self.instances[pid].cwd = cwd
                self.instances[pid].display_name = short_cwd(cwd)
                changed = True

        for sf in glob.glob(os.path.join(self.state_dir, "*.json")):
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    st = json.load(f)
                pid = st.get("pid")
                state = st.get("state", "working")
                updated_at = st.get("updatedAt", 0)
                saved_hwnd = st.get("hwnd", 0)
            except Exception:
                continue

            if pid in self.instances:
                inst = self.instances[pid]
                inst.hwnd = saved_hwnd or inst.hwnd
                if inst.state != state or inst.updated_at != updated_at:
                    old_state = inst.state
                    inst.state = state
                    inst.updated_at = updated_at
                    if state == "done" and old_state != "done":
                        inst.done_since = time.monotonic()
                        inst.blink_on = True
                        events.append("done")
                    elif state == "question" and old_state != "question":
                        inst.blink_on = True
                        inst.done_since = 0.0
                        events.append("question")
                    elif state != "done":
                        inst.done_since = 0.0
                    changed = True

        # Proactively resolve hwnd for instances that don't have one yet
        if IS_WINDOWS:
            missing_hwnd = [inst for inst in self.instances.values()
                            if not inst.hwnd and inst.pid in seen_pids]
            if missing_hwnd:
                tree = build_process_tree()
                for inst in missing_hwnd:
                    hwnd = find_window_for_pid(inst.pid, tree, inst.cwd)
                    if hwnd:
                        inst.hwnd = hwnd

        # Remove instances whose PID is gone
        for pid in list(self.instances):
            if pid not in seen_pids:
                del self.instances[pid]
                changed = True

        # Deduplicate display names
        name_counts: dict[str, int] = {}
        for inst in self.instances.values():
            base = short_cwd(inst.cwd)
            name_counts[base] = name_counts.get(base, 0) + 1

        name_seen: dict[str, int] = {}
        for inst in sorted(self.instances.values(), key=lambda i: i.pid):
            base = short_cwd(inst.cwd)
            if name_counts[base] > 1:
                idx = name_seen.get(base, 0) + 1
                name_seen[base] = idx
                new_name = f"{base} ({idx})"
            else:
                new_name = base
            if inst.display_name != new_name:
                inst.display_name = new_name
                changed = True

        return changed, events


# ── Overlay UI ────────────────────────────────────────────────────

class MonitorOverlay:
    POSITION_FILE = os.path.join(
        os.path.expanduser("~"), ".claude", "monitor", "position.json"
    )

    def __init__(self):
        self.poll_ms = CONFIG.get("poll_interval_ms", 500)
        self.blink_ms = CONFIG.get("blink_interval_ms", 600)
        self.width = CONFIG.get("width", 260)
        self.sound_enabled = CONFIG.get("sound_enabled", True)
        self.row_height = 28
        self.header_height = 24
        self.padding = 6

        self.tracker = InstanceTracker()
        self.root = tk.Tk()
        self.root.title("Claude Monitor")
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", CONFIG.get("opacity", 0.65))
        self.root.configure(bg=THEME["bg"])

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

        # Fonts
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

        # Content frame
        self.content = tk.Frame(self.root, bg=THEME["bg"])
        self.content.pack(fill=tk.BOTH, expand=True, padx=self.padding, pady=(2, 4))

        self.row_widgets: list[dict] = []

        # Drag support
        self._drag_data = {"x": 0, "y": 0}
        for w in (self.header,):
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
        changed, events = self.tracker.poll()
        if changed:
            self._rebuild_rows()
        if self.sound_enabled:
            for ev in events:
                self._play_sound(ev)
        self.root.after(self.poll_ms, self._poll_loop)

    @staticmethod
    def _play_sound(event: str):
        """Play a short chime in a background thread (non-blocking). Windows only."""
        if not IS_WINDOWS:
            return
        chimes = {
            "done": [(880, 80), (1175, 80), (1397, 120)],      # A5-D6-F6 rising major
            "question": [(1047, 100), (880, 130)],              # C6-A5 descending
        }
        seq = chimes.get(event)
        if seq:
            def _play():
                for freq, ms in seq:
                    winsound.Beep(freq, ms)
            threading.Thread(target=_play, daemon=True).start()

    def _blink_loop(self):
        self._blink_phase = not self._blink_phase
        now = time.monotonic()
        for row in self.row_widgets:
            inst: Instance = row.get("instance")
            if not inst:
                continue
            if inst.state == "done":
                if inst.done_since > 0 and now - inst.done_since < DONE_BLINK_SECONDS:
                    color = THEME["done"] if self._blink_phase else THEME["bg"]
                else:
                    color = THEME["done"]
                row["dot"].config(fg=color)
                row["state"].config(fg=color)
            elif inst.state == "question":
                row["dot"].config(fg=THEME["question"])
                row["state"].config(fg=THEME["question"])
        self.root.after(self.blink_ms, self._blink_loop)

    def _rebuild_rows(self):
        for row in self.row_widgets:
            row["frame"].destroy()
        self.row_widgets.clear()

        instances = sorted(self.tracker.instances.values(), key=lambda i: i.pid)

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
                self._add_row(inst)

        row_count = max(len(instances), 1)
        height = self.header_height + row_count * self.row_height + self.padding + 6
        geo = self.root.geometry()
        parts = geo.split("+")
        x_pos = parts[1] if len(parts) > 1 else "0"
        y_pos = parts[2] if len(parts) > 2 else "0"
        self.root.geometry(f"{self.width}x{height}+{x_pos}+{y_pos}")

    def _add_row(self, inst: Instance):
        state_color = THEME.get(inst.state, THEME["idle"])
        state_text = get_label(inst.state)

        frame = tk.Frame(self.content, bg=THEME["bg"], cursor="hand2")
        frame.pack(fill=tk.X, pady=1)

        dot = tk.Label(
            frame, text="\u25cf", font=self.font_row,
            bg=THEME["bg"], fg=state_color, width=2,
        )
        dot.pack(side=tk.LEFT)

        name_lbl = tk.Label(
            frame, text=inst.display_name, font=self.font_row,
            bg=THEME["bg"], fg=THEME["fg"], anchor="w",
        )
        name_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        state_lbl = tk.Label(
            frame, text=state_text, font=self.font_state,
            bg=THEME["bg"], fg=state_color, anchor="e", padx=4,
        )
        state_lbl.pack(side=tk.RIGHT)

        def on_click(_event, pid=inst.pid):
            self._activate_terminal(pid)

        for w in (frame, dot, name_lbl, state_lbl):
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>", lambda _e, f=frame, d=dot, n=name_lbl, s=state_lbl:
                   self._row_hover(f, d, n, s, True))
            w.bind("<Leave>", lambda _e, f=frame, d=dot, n=name_lbl, s=state_lbl, i=inst:
                   self._row_hover(f, d, n, s, False, i))

        self.row_widgets.append({
            "frame": frame, "dot": dot, "name": name_lbl,
            "state": state_lbl, "instance": inst,
        })

    def _row_hover(self, frame, dot, name, state, entering, inst=None):
        bg = THEME["hover"] if entering else THEME["bg"]
        for w in (frame, dot, name, state):
            w.config(bg=bg)

    def _activate_terminal(self, claude_pid: int):
        try:
            inst = self.tracker.instances.get(claude_pid)

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

    def run(self):
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────

def main():
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"claude-code-monitor {__version__}")
        sys.exit(0)

    app = MonitorOverlay()
    app.run()


if __name__ == "__main__":
    main()
