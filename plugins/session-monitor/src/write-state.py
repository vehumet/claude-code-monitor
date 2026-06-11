#!/usr/bin/env python3
"""Hook entrypoint: write Claude Code/Codex session state for Session Monitor.

Usage (from hook script):
    echo "$INPUT" | python write-state.py <state>

Direct states: working, done, question, interrupted
Meta-states (resolved from hook payload + prior state):
    idle_prompt   — Notification:idle_prompt. interrupted if prev=working, else skip.
    tool_failure  — PostToolUseFailure. interrupted only if is_interrupt=true.
"""
import json
import logging
import logging.handlers
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
import glob
import io
import ctypes
import ctypes.wintypes as wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_monitor_paths import (
    config_file,
    logs_dir,
    sessions_dir as default_sessions_dir,
    state_dir as default_state_dir,
)

IS_WINDOWS = sys.platform == "win32"
QUESTION_GUARD_SECONDS = 2  # catch-all "working"이 "question"과 레이스하는 것을 방지

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
    log_dir = logs_dir()
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


def _get_ancestor_pids(my_pid, tree):
    """Return set of ancestor PIDs (including my_pid itself)."""
    ancestors = set()
    pid = my_pid
    visited = set()
    while pid and pid not in visited:
        visited.add(pid)
        ancestors.add(pid)
        entry = tree.get(pid)
        pid = entry[0] if entry else None
    return ancestors


TERMINAL_HOSTS = {"windowsterminal.exe", "conhost.exe", "openconsole.exe"}


def _capture_foreground_hwnd(my_pid, tree, is_user_prompt=False):
    """Capture foreground window HWND if it belongs to an ancestor process.

    When *is_user_prompt* is True (UserPromptSubmit hook — user just pressed
    Enter, so the terminal is foreground), also accept windows owned by known
    terminal host processes (WindowsTerminal, conhost, OpenConsole).
    """
    if not IS_WINDOWS:
        return None
    if not is_user_prompt:
        return None  # 포그라운드 윈도우는 UserPromptSubmit에서만 신뢰 가능
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
        # Windows 11 delegation: terminal host is NOT an ancestor
        if is_user_prompt:
            owner_entry = tree.get(owner_pid.value)
            if owner_entry and owner_entry[1] in TERMINAL_HOSTS:
                return hwnd
        return None
    except Exception:
        return None


_WEZTERM_CLI_TIMEOUT = 2
_CREATE_NO_WINDOW = 0x08000000  # subprocess flag — suppress console flash on Windows


def _resolve_wezterm_info(existing):
    """If running inside wezterm, return {pane_id, tab_id, window_id, socket}.

    Reuses cached info from *existing* state when pane_id+socket match, to avoid
    re-running `wezterm cli list` on every hook invocation.
    """
    pane_id_str = os.environ.get("WEZTERM_PANE")
    socket = os.environ.get("WEZTERM_UNIX_SOCKET")
    if not pane_id_str or not socket:
        return None
    try:
        pane_id = int(pane_id_str)
    except (TypeError, ValueError):
        return None

    if existing and isinstance(existing.get("wezterm"), dict):
        cached = existing["wezterm"]
        if cached.get("pane_id") == pane_id and cached.get("socket") == socket:
            return cached

    try:
        env = os.environ.copy()
        env["WEZTERM_UNIX_SOCKET"] = socket
        kwargs = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
            "timeout": _WEZTERM_CLI_TIMEOUT,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"], **kwargs
        )
        if result.returncode != 0:
            _log.debug("wezterm cli list rc=%s stderr=%s",
                       result.returncode, (result.stderr or "")[:200])
            return None
        panes = json.loads(result.stdout)
    except FileNotFoundError:
        return None
    except Exception:
        _log.debug("wezterm cli list failed", exc_info=True)
        return None

    for p in panes:
        if p.get("pane_id") == pane_id:
            return {
                "pane_id": pane_id,
                "tab_id": p.get("tab_id"),
                "window_id": p.get("window_id"),
                "socket": socket,
            }
    return None


def get_state_dir():
    """Return state directory path (env var > default)."""
    return default_state_dir()


def _monitor_root():
    return os.path.dirname(get_state_dir())


def _mark_codex_hooked_session(session_id):
    """Persist that Codex hooks own this sessionId.

    The rollout poller uses this marker to avoid resurrecting the same rollout
    as a PID-less fallback row after the real Codex process exits.
    """
    if not session_id:
        return
    try:
        d = os.path.join(_monitor_root(), "codex-hooked")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{session_id}.json")
        data = {"sessionId": session_id, "updatedAt": int(time.time())}
        _atomic_write_json(path, data, "codex-hooked")
    except Exception:
        _log.debug("failed to mark hooked codex session", exc_info=True)


# Carry-over fields preserved across writes. Slot is stable for session lifetime;
# summary is updated by Stop hook / UserPromptSubmit fallback. Provider is
# recomputed from explicit hook commands or the live process tree so older
# misclassified state files can self-correct.
_PRESERVED_FIELDS = (
    "slot", "summary", "summarySource",
    "summaryAt", "summaryMsgCount", "summarySessionId",
)
_SUMMARY_FIELDS = {
    "summary",
    "summarySource",
    "summaryAt",
    "summaryMsgCount",
    "summarySessionId",
}

def _file_snapshot(path):
    if not path:
        return None
    try:
        st = os.stat(os.path.expanduser(path))
        return {
            "path": path,
            "mtime_ns": int(st.st_mtime_ns),
            "size": int(st.st_size),
        }
    except OSError:
        return None


def _attach_question_snapshot(state_data, hook_data, session_file, now):
    """Record cheap file markers so the overlay can clear question without
    a PostToolUse catch-all hook."""
    state_data["questionAt"] = now
    transcript_path = (
        hook_data.get("transcript_path")
        or hook_data.get("transcriptPath")
        or hook_data.get("transcript")
    )
    transcript = _file_snapshot(transcript_path)
    if transcript:
        state_data["questionTranscriptPath"] = transcript["path"]
        state_data["questionTranscriptMtimeNs"] = transcript["mtime_ns"]
        state_data["questionTranscriptSize"] = transcript["size"]
    session = _file_snapshot(session_file)
    if session:
        state_data["questionSessionPath"] = session["path"]
        state_data["questionSessionMtimeNs"] = session["mtime_ns"]
        state_data["questionSessionSize"] = session["size"]


def _basename_provider(name):
    """Map a process basename to a known-LLM provider id, or None.

    'claude.exe' / 'claude.exe.old.*' (rename pattern Claude Code uses during
    self-update; already-running sessions retain the .old.* path until they
    exit) → 'claude'. 'codex.exe' → 'codex'. Anything else → None.
    """
    n = (name or "").lower()
    if n == "claude.exe" or n.startswith("claude.exe.old"):
        return "claude"
    if n == "codex.exe" or n == "codex":
        return "codex"
    return None


def _pid_is_known_llm(pid):
    """Return provider id ('claude'/'codex') if PID is a live LLM CLI, else None.

    Defends against PID reuse: when an LLM CLI session exits, Windows is free
    to recycle its PID into an unrelated process (browser, Slack, etc.). A
    naive alive check would treat the recycled process as the original session
    and let its slot stay reserved indefinitely.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not IS_WINDOWS:
        # POSIX: read /proc/<pid>/comm (Linux) or ps -p <pid> -o comm= (macOS)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            # process exists but we cannot inspect — assume known LLM to keep
            # legacy behaviour conservative; falls through to claude default
            return "claude"
        comm = ""
        try:
            with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
                comm = f.read().strip()
        except OSError:
            try:
                out = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    stderr=subprocess.DEVNULL, timeout=1,
                )
                comm = out.decode("utf-8", errors="replace").strip()
            except Exception:
                comm = ""
        return _basename_provider(os.path.basename(comm))
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        ec = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(ec)) or ec.value != 259:
            return None
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        name = os.path.basename(buf.value).lower()
        return _basename_provider(name)
    finally:
        k32.CloseHandle(h)


def _pid_is_claude(pid):
    """Back-compat wrapper — `_pid_is_known_llm` returning provider id is the
    canonical check. Existing callers expect a bool for claude-only contexts
    (slot allocation predates multi-provider support and treated any non-claude
    PID as 'not ours' anyway)."""
    return _pid_is_known_llm(pid) is not None


def _allocate_slot(state_dir, my_pid, my_cwd):
    """Pick the lowest unused positive integer among live state files in
    the same cwd. Dead PIDs are excluded so slots compact back down as
    sessions exit. Virtual rows (`codex-<sid8>.json`) are treated as alive
    while their file exists — codex_rollout_poller evicts stale ones on
    its own schedule and this helper honours that ownership.
    """
    norm_my = _norm_path(my_cwd)
    used = set()
    for sf in glob.glob(os.path.join(state_dir, "*.json")):
        base = os.path.splitext(os.path.basename(sf))[0]
        try:
            other_pid = int(base)
            is_alive = _pid_is_claude(other_pid)
        except ValueError:
            if not base.startswith("codex-"):
                continue
            other_pid = base
            is_alive = True
        except OSError:
            continue
        if not is_alive:
            continue
        if str(other_pid) == str(my_pid):
            continue
        try:
            with open(sf, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if _norm_path(d.get("cwd", "")) != norm_my:
            continue
        s = d.get("slot")
        if isinstance(s, int) and s >= 1:
            used.add(s)
    n = 1
    while n in used:
        n += 1
    return n


def _truncate_label(text, max_chars=20):
    """Trim text for display: first non-empty line, collapsed whitespace, max_chars."""
    if not text:
        return ""
    # Strip lone surrogates that may sneak in from stdin decoding edge cases.
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("<"):
            line = re.sub(r"\s+", " ", line)
            if len(line) <= max_chars:
                return line
            return line[:max_chars].rstrip() + "…"
    return ""


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


def _session_pid(sess, basename):
    """Return the session's real PID, or its virtual session id.

    Codex rollout fallback files are named like `codex-<sid8>.json` and do not
    always carry a numeric pid. Avoid eager `int(basename)` conversions because
    hook commands must tolerate those virtual rows while matching real sessions.
    """
    pid = sess.get("pid")
    if pid is not None:
        return pid
    try:
        return int(basename)
    except (TypeError, ValueError):
        return basename


def _session_waits_for_user(sess):
    if not isinstance(sess, dict):
        return False
    status = str(sess.get("status") or "").lower()
    waiting_for = str(sess.get("waitingFor") or "").strip()
    return status == "waiting" or bool(waiting_for)


def _is_virtual_id(pid):
    return isinstance(pid, str) and pid.startswith("codex-")


def _is_codex_desktop_app_pid(pid, tree):
    """True for the Codex desktop app's background app-server process.

    The Windows desktop app launches ``app/resources/codex.exe app-server``
    under the GUI ``Codex.exe`` process. Hooks from that helper do not behave
    like an interactive CLI session, so surfacing them creates sticky rows.
    """
    if not IS_WINDOWS or not isinstance(pid, int):
        return False
    entry = tree.get(pid)
    if not entry or entry[1] != "codex.exe":
        return False
    parent = tree.get(entry[0])
    return bool(parent and parent[1] == "codex.exe")


def _find_ancestor_llm_pid(my_pid, tree):
    p = my_pid
    visited = set()
    while p and p not in visited:
        visited.add(p)
        entry = tree.get(p)
        if not entry:
            break
        if _basename_provider(entry[1]):
            return p
        p = entry[0]
    return None


def _atomic_write_json(path, data, log_label="json"):
    """Write JSON via temp file + os.replace, retrying transient Windows races."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    for attempt in range(5):
        try:
            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp_path, path)
                tmp_path = None
                return True
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except PermissionError:
            if attempt == 4:
                _log.error("%s: failed to replace %s", log_label, path, exc_info=True)
                break
            time.sleep(0.03 * (attempt + 1))
        except Exception:
            _log.error("%s: failed to write %s", log_label, path, exc_info=True)
            break
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            tmp_path = None
    return False


def _encoded_cwd(cwd):
    """Mimic Claude Code's project directory encoding.

    Examples:
        C:\\Users\\vehum\\proj    → C--Users-vehum-proj
        C:\\Users\\my_repo       → C--Users-my-repo
        /home/user/proj         → -home-user-proj
    """
    if len(cwd) >= 2 and cwd[1] == ":":
        head = cwd[0] + "--"
        tail = cwd[2:].lstrip("\\/")
    else:
        head = ""
        tail = cwd
    return head + tail.replace("\\", "-").replace("/", "-").replace("_", "-")


def _find_session_jsonl(home, cwd, session_id):
    if not (cwd and session_id):
        return None
    proj_root = os.path.join(home, ".claude", "projects")
    if not os.path.isdir(proj_root):
        return None
    target = _encoded_cwd(cwd).lower()
    try:
        for d in os.listdir(proj_root):
            if d.lower() == target:
                fp = os.path.join(proj_root, d, f"{session_id}.jsonl")
                if os.path.exists(fp):
                    return fp
    except OSError:
        return None
    return None


def _latest_ai_title(jsonl_fp):
    """Return the last `type:'ai-title'` aiTitle (mirrors wezterm tab label)."""
    if not jsonl_fp or not os.path.exists(jsonl_fp):
        return None
    last = None
    try:
        with open(jsonl_fp, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "ai-title":
                    t = d.get("aiTitle")
                    if isinstance(t, str) and t.strip():
                        last = t.strip()
    except OSError:
        return None
    return last or None


def _latest_away_summary(jsonl_fp):
    """Return the last `system/away_summary` content (Claude Code recap), or None."""
    if not jsonl_fp or not os.path.exists(jsonl_fp):
        return None
    last = None
    try:
        with open(jsonl_fp, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "away_summary":
                    c = d.get("content")
                    if isinstance(c, str) and c.strip():
                        last = c
    except OSError:
        return None
    if last:
        # Drop the "(disable recaps in /config)" footer Claude Code appends.
        last = re.sub(r"\s*\(disable recaps in /config\)\s*$", "", last).strip()
    return last or None


def _iter_user_messages(jsonl_fp):
    """Yield (text,) for each meaningful user message in a JSONL session file."""
    if not jsonl_fp or not os.path.exists(jsonl_fp):
        return
    try:
        with open(jsonl_fp, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message")
                c = msg.get("content") if isinstance(msg, dict) else None
                txt = None
                if isinstance(c, str):
                    txt = c
                elif isinstance(c, list) and c and isinstance(c[0], dict):
                    txt = c[0].get("text", "")
                if not txt or txt.startswith("<"):
                    continue
                yield txt
    except OSError:
        return


def _nested_pids_dir():
    return os.path.join(_monitor_root(), "nested-pids")


def _mark_nested_pid(pid):
    """Touch a marker file so the overlay can recognise this PID as ours."""
    try:
        d = _nested_pids_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{pid}.flag"), "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except Exception:
        _log.error("failed to mark nested pid %s", pid, exc_info=True)


def _spawn_summarizer(target_pid):
    """Re-invoke ourselves detached in __summarize__ mode for the given PID."""
    cmd = [sys.executable, os.path.abspath(__file__),
           "__summarize__", str(target_pid)]
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
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        _mark_nested_pid(proc.pid)
        _log.debug("=> Spawned summarizer for pid=%s (child=%d)", target_pid, proc.pid)
    except Exception:
        _log.error("Failed to spawn summarizer", exc_info=True)


def _load_monitor_config():
    """Read the widget's config (summary_max_chars + language)."""
    cfg = config_file()
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {
            "summary_max_chars": max(4, int(d.get("summary_max_chars", 12))),
            "language": d.get("language", "en"),
        }
    except Exception:
        return {"summary_max_chars": 12, "language": "en"}


_HAIKU_PROMPT_KO = (
    "다음은 Claude Code 세션에서 사용자가 보낸 메시지들이야. "
    "이 세션의 작업 주제를 한국어 명사구 {n}자 이내로 요약해. "
    "{n}자를 넘기면 잘려서 사용자가 알아볼 수 없으니 반드시 {n}자 이내로. "
    "따옴표·접두사·마크다운(**, *, _, `, # 등) 없이 평문 한 줄만 출력."
    "\n\n{transcript}"
)

_HAIKU_PROMPT_EN = (
    "Below are user messages from a Claude Code session. "
    "Summarize the session's task topic as a short English noun phrase, "
    "max {words} words (under {n} characters). "
    "It is rendered in a narrow widget column — exceeding the limit causes "
    "the label to be cut off and unreadable. "
    "Output plain text only — no quotes, prefixes, trailing punctuation, "
    "or markdown (**, *, _, `, # etc)."
    "\n\n{transcript}"
)

_CODEX_SUMMARY_PROMPT_KO = (
    "아래는 Codex 세션의 압축 digest야. "
    "사용자가 지금 실제로 맡긴 작업을 한국어 명사구 {n}자 이내로 요약해. "
    "띄어쓰기를 생략하지 말고 자연스러운 한국어 띄어쓰기를 유지해. "
    "도구명이나 '세션', '요약', '작업' 같은 일반어만 쓰지 말고, "
    "사용자 요청·수정 파일·도구 호출을 근거로 구체적인 주제를 골라. "
    "예: '코덱스 요약 개선', '로컬라이제이션 검토', '훅 설치 정리'. "
    "따옴표·접두사·마크다운 없이 한 줄만 출력."
    "\n\nDigest:\n{transcript}"
)

_CODEX_SUMMARY_PROMPT_EN = (
    "Below is a compact digest from a Codex session. "
    "Summarize the concrete task the user is working on in the same language "
    "as the dominant user request when practical, as a short noun phrase, "
    "max {words} words and under {n} characters. "
    "Keep natural spacing between words; do not remove spaces to satisfy "
    "the character limit. "
    "Do not answer with generic labels such as 'session summary', 'task summary', "
    "or tool names alone. Use the user request, edited files, and tool calls "
    "to name the actual work. "
    "Output plain text only: no quotes, prefixes, punctuation, or markdown."
    "\n\nDigest:\n{transcript}"
)


_MARKDOWN_MARKERS_RE = re.compile(r"(\*+|_+|`+|~+|^#+\s*)")


def _strip_markdown(text):
    """Remove common inline-markdown markers that occasionally leak into output."""
    if not text:
        return text
    return _MARKDOWN_MARKERS_RE.sub("", text).strip()


def _find_codex_rollout(session_id):
    """Locate Codex's rollout JSONL whose session_meta.id matches session_id.

    Codex names rollouts ``rollout-<iso>-<sessionId>.jsonl``, so the glob
    catches the file directly. The fallback scan is kept for older CLI
    versions or if the naming convention shifts.
    """
    if not session_id:
        return None
    root = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    if not os.path.isdir(root):
        return None
    # Fast path: filename includes the sessionId.
    for path in glob.glob(os.path.join(root, "*", "*", "*", f"rollout-*-{session_id}.jsonl")):
        return path
    # Fallback: read each rollout's session_meta header.
    for path in glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                d = json.loads(f.readline())
            if d.get("type") == "session_meta" and (d.get("payload") or {}).get("id") == session_id:
                return path
        except (OSError, ValueError):
            continue
    return None


def _codex_rollout_payload(session_id):
    """Return the session_meta payload for a Codex rollout, if available."""
    rollout = _find_codex_rollout(session_id)
    if not rollout:
        return None
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("type") != "session_meta":
                    return None
                payload = d.get("payload") or {}
                return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None
    return None


def _is_nested_codex_payload(payload):
    """True for subagent rollouts that must not update the parent row."""
    if not isinstance(payload, dict):
        return False
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source


def _codex_session_is_nested(session_id):
    return _is_nested_codex_payload(_codex_rollout_payload(session_id))


_CODEX_DIGEST_MAX_CHARS = 6000
_CODEX_DIGEST_SECTION_LIMITS = {
    "user_messages": 3,
    "commentary": 6,
    "final_answers": 3,
    "tool_calls": 12,
    "file_hints": 16,
    "plan_updates": 4,
}


def _clip_text(text, max_chars):
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _append_limited(items, value, limit):
    value = (value or "").strip()
    if not value:
        return
    if len(items) >= limit:
        del items[0]
    items.append(value)


def _extract_codex_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text") or item.get("content") or item.get("message")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    if isinstance(value, dict):
        txt = value.get("text") or value.get("content") or value.get("message")
        if isinstance(txt, str):
            return txt
    return ""


def _extract_codex_command(payload, item):
    name = (
        item.get("name")
        or item.get("call_id")
        or item.get("tool_name")
        or payload.get("name")
        or payload.get("tool_name")
        or payload.get("call_id")
    )
    args = (
        item.get("arguments")
        or item.get("input")
        or item.get("params")
        or payload.get("arguments")
        or payload.get("input")
        or payload.get("params")
    )
    if isinstance(args, str):
        arg_text = args
    elif args:
        try:
            arg_text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except Exception:
            arg_text = str(args)
    else:
        arg_text = ""
    if name and arg_text:
        return f"{name}: {_clip_text(arg_text, 240)}"
    if name:
        return str(name)
    if arg_text:
        return _clip_text(arg_text, 240)
    return ""


def _extract_codex_file_hints(payload, item):
    hints = []
    for source in (payload, item):
        if not isinstance(source, dict):
            continue
        for key in ("path", "file", "filename", "uri"):
            val = source.get(key)
            if isinstance(val, str) and val:
                hints.append(val)
        for key in ("files", "paths"):
            vals = source.get(key)
            if isinstance(vals, list):
                hints.extend(v for v in vals if isinstance(v, str) and v)
    text = _extract_codex_text(item.get("output") or item.get("content") or payload.get("output"))
    if text:
        for m in re.finditer(r"(?:(?:[A-Za-z]:)?[\\/])?[\w.\-]+(?:[\\/][\w.\-]+)+", text):
            hints.append(m.group(0))
    return hints


def _codex_item(payload):
    item = payload.get("item")
    return item if isinstance(item, dict) else payload


def _codex_record_session_id(record):
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    return payload.get("id")


def _codex_is_target_session(current_session_id, target_session_id):
    return not target_session_id or current_session_id is None or current_session_id == target_session_id


def _scan_codex_rollout(rollout_path, session_id=None):
    """Single-pass scan of a Codex rollout JSONL pulling out the signals
    the summarizer cares about.

    Returns a dict with:
      - recent user/commentary/final-answer messages
      - recent tool calls and file hints
      - a compact digest for the LLM summarizer

    Verified against Codex CLI 0.128.0 rollouts.
    """
    limits = _CODEX_DIGEST_SECTION_LIMITS
    out = {
        "user_messages": [],
        "commentary": [],
        "final_answers": [],
        "tool_calls": [],
        "file_hints": [],
        "plan_updates": [],
        "digest": "",
    }
    seen_files = set()
    if not rollout_path or not os.path.exists(rollout_path):
        return out
    current_session_id = None
    try:
        with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                record_session_id = _codex_record_session_id(d)
                if record_session_id:
                    current_session_id = record_session_id
                    continue
                if not _codex_is_target_session(current_session_id, session_id):
                    continue
                line_type = d.get("type")
                if line_type not in ("event_msg", "response_item", "compacted"):
                    continue
                payload = d.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                item = _codex_item(payload)
                pt = payload.get("type") or item.get("type")
                phase = payload.get("phase") or item.get("phase")
                msg = payload.get("message") or _extract_codex_text(
                    item.get("content") or item.get("text") or item.get("message")
                )

                if pt == "user_message":
                    _append_limited(
                        out["user_messages"], _clip_text(msg, 700), limits["user_messages"]
                    )
                elif pt == "agent_message":
                    if phase == "final_answer":
                        _append_limited(
                            out["final_answers"], _clip_text(msg, 900), limits["final_answers"]
                        )
                    elif phase == "commentary":
                        _append_limited(
                            out["commentary"], _clip_text(msg, 500), limits["commentary"]
                        )
                elif pt in ("function_call", "custom_tool_call", "web_search_call", "mcp_tool_call"):
                    _append_limited(
                        out["tool_calls"],
                        _extract_codex_command(payload, item),
                        limits["tool_calls"],
                    )
                elif pt in (
                    "function_call_output",
                    "custom_tool_call_output",
                    "mcp_tool_call_end",
                    "patch_apply_end",
                ):
                    for hint in _extract_codex_file_hints(payload, item):
                        norm = hint.replace("\\", "/")
                        if norm in seen_files:
                            continue
                        seen_files.add(norm)
                        _append_limited(
                            out["file_hints"], _clip_text(norm, 180), limits["file_hints"]
                        )
                elif pt in ("plan_update", "update_plan"):
                    plan_text = _extract_codex_text(
                        item.get("text") or item.get("content") or payload.get("plan")
                    )
                    _append_limited(
                        out["plan_updates"], _clip_text(plan_text, 600), limits["plan_updates"]
                    )
    except OSError:
        pass
    out["digest"] = _build_codex_digest(out)
    return out


def _build_codex_digest(scan):
    sections = []

    def add_section(title, values):
        values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
        if not values:
            return
        lines = [f"{idx}. {v}" for idx, v in enumerate(values, 1)]
        sections.append(f"{title}:\n" + "\n".join(lines))

    add_section("Recent user requests", scan.get("user_messages", []))
    add_section("Recent assistant progress/comments", scan.get("commentary", []))
    add_section("Recent final answers", scan.get("final_answers", []))
    add_section("Recent tool calls", scan.get("tool_calls", []))
    add_section("Touched/mentioned files", scan.get("file_hints", []))
    add_section("Plan updates", scan.get("plan_updates", []))
    return _clip_text("\n\n".join(sections), _CODEX_DIGEST_MAX_CHARS)


# Codex rollout event type markers — mirrors codex_rollout_poller.py's sets.
_ROLLOUT_START_TYPES = {"task_started"}
_ROLLOUT_COMPLETE_TYPES = {"task_complete", "agent_turn_complete", "turn_complete"}
_ROLLOUT_FAIL_TYPES = {"error", "task_failed", "turn_failed", "turn_aborted"}
_CODEX_DONE_RECHECK_ENV = "SESSION_MONITOR_CODEX_DONE_RECHECK"
_CODEX_DONE_RECHECK_DELAY_S = 1.0


def _codex_task_complete(session_id):
    """True when the rollout JSONL confirms the Codex task is finished.

    Codex fires Stop after each model turn in agentic mode, not only at
    actual task completion.  Checking task_started vs terminal turn markers
    (same logic as codex_rollout_poller._infer_state) lets us tell apart
    intermediate per-turn stops from the final one.

    Fails safe — returns True (allow done) when rollout is unavailable or
    has no markers yet.  Returns False (suppress done) only when
    the latest marker is task_started (task is clearly still in progress).
    """
    rollout = _find_codex_rollout(session_id)
    if not rollout:
        return True
    started = 0
    completed = 0
    latest_marker = None
    current_session_id = None
    try:
        with open(rollout, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                record_session_id = _codex_record_session_id(d)
                if record_session_id:
                    current_session_id = record_session_id
                    continue
                if not _codex_is_target_session(current_session_id, session_id):
                    continue
                if d.get("type") != "event_msg":
                    continue
                payload = d.get("payload") or {}
                pt = payload.get("type") if isinstance(payload, dict) else None
                if pt in _ROLLOUT_START_TYPES:
                    started += 1
                    latest_marker = "started"
                elif pt in _ROLLOUT_COMPLETE_TYPES:
                    completed += 1
                    latest_marker = "completed"
                elif pt in _ROLLOUT_FAIL_TYPES:
                    completed += 1
                    latest_marker = "completed"
    except OSError:
        return True
    if started == 0 and completed == 0:
        return True  # no markers yet — conservative
    if latest_marker == "started":
        return False
    if latest_marker == "completed":
        return True
    return completed >= started


def _spawn_codex_done_recheck(hook_data):
    """Retry Codex Stop once after rollout JSONL has had time to flush.

    Codex can fire the Stop hook before the trailing task_complete marker is
    visible on disk. A detached one-shot retry prevents rows from sticking in
    working when the immediate Stop was merely early.
    """
    if os.environ.get(_CODEX_DONE_RECHECK_ENV) == "1":
        return
    payload = json.dumps({
        "argv": ["--provider", "codex", "done"],
        "hook_data": hook_data,
    }).encode("utf-8")
    cmd = [sys.executable, os.path.abspath(__file__), "__codex_done_recheck__"]
    env = os.environ.copy()
    env[_CODEX_DONE_RECHECK_ENV] = "1"
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except Exception:
            pass
        _log.debug("spawned codex done recheck")
    except Exception:
        _log.error("failed to spawn codex done recheck", exc_info=True)


# Default summarisation model for Codex sessions. `gpt-5.4-mini` is the
# cheapest model on the standard Codex profile; users can override via
# config.json `codex_summary_model` or env var.
_CODEX_DEFAULT_SUMMARY_MODEL = "gpt-5.4-mini"


def _resolve_codex_command():
    """Find Codex even when pythonw starts with a GUI-shortened PATH."""
    found = shutil.which("codex")
    if found:
        return found
    candidates = []
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "codex.cmd"),
                os.path.join(appdata, "npm", "codex"),
            ])
    else:
        home = os.path.expanduser("~")
        candidates.extend([
            os.path.join(home, ".local", "bin", "codex"),
            os.path.join(home, ".npm-global", "bin", "codex"),
        ])
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return "codex"


def _call_codex_summary(transcript):
    """Run `codex exec` with --ephemeral + --ignore-user-config to get a
    one-line summary without spawning a new persisted Codex session and
    without re-firing our own hooks."""
    cfg = _load_monitor_config()
    base_n = cfg["summary_max_chars"]
    if cfg["language"] == "ko":
        prompt = _CODEX_SUMMARY_PROMPT_KO.format(
            n=base_n, transcript=transcript[:_CODEX_DIGEST_MAX_CHARS]
        )
    else:
        en_n = base_n * 2
        words = max(2, en_n // 6)
        prompt = _CODEX_SUMMARY_PROMPT_EN.format(
            n=en_n, words=words, transcript=transcript[:_CODEX_DIGEST_MAX_CHARS]
        )

    model = (
        os.environ.get("SESSION_MONITOR_CODEX_SUMMARY_MODEL")
        or cfg.get("codex_summary_model")
        or _CODEX_DEFAULT_SUMMARY_MODEL
    )

    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="codex-summary-")
    os.close(fd)

    cmd = [
        _resolve_codex_command(), "exec",
        "--ephemeral",             # don't write a rollout JSONL
        "--ignore-user-config",    # don't load our own hooks → no recursion
        "--skip-git-repo-check",
        "--color", "never",
        "-m", model,
        "-o", output_path,
        "-",
    ]
    env = os.environ.copy()
    env["SESSION_MONITOR_NESTED"] = "1"  # belt-and-braces if hooks fire anyway
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW

    def _cleanup():
        try:
            os.unlink(output_path)
        except OSError:
            pass

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        _log.debug("codex summary: codex CLI not on PATH")
        _cleanup()
        return None
    except Exception:
        _log.error("codex summary spawn failed", exc_info=True)
        _cleanup()
        return None
    _mark_nested_pid(proc.pid)
    try:
        proc.communicate(prompt.encode("utf-8"), timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        _log.debug("codex summary: timeout")
        _cleanup()
        return None
    except Exception:
        _log.error("codex summary communicate failed", exc_info=True)
        _cleanup()
        return None

    if proc.returncode != 0:
        _log.debug("codex summary rc=%s", proc.returncode)
        _cleanup()
        return None

    try:
        with open(output_path, "r", encoding="utf-8", errors="replace") as f:
            out = f.read()
    except OSError:
        _cleanup()
        return None
    _cleanup()

    out = out.strip()
    if not out:
        return None
    out = out.splitlines()[0].strip().strip('"').strip("'").strip()
    out = _strip_markdown(out)
    return out or None


def _call_haiku(transcript):
    """Run `claude -p ... --model claude-haiku-4-5`. Returns label or None."""
    cfg = _load_monitor_config()
    base_n = cfg["summary_max_chars"]
    if cfg["language"] == "ko":
        prompt = _HAIKU_PROMPT_KO.format(n=base_n, transcript=transcript[:4000])
    else:
        # The summary column width is sized for `base_n` CJK glyphs; an
        # English glyph is roughly half a CJK glyph in this font, so allow
        # ~2× the character budget.
        en_n = base_n * 2
        words = max(2, en_n // 6)
        prompt = _HAIKU_PROMPT_EN.format(n=en_n, words=words, transcript=transcript[:4000])
    cmd = [
        "claude", "-p", prompt,
        "--model", "claude-haiku-4-5",
        "--output-format", "text",
    ]
    env = os.environ.copy()
    env["SESSION_MONITOR_NESTED"] = "1"
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        _log.debug("haiku: `claude` CLI not found on PATH")
        return None
    except Exception:
        _log.error("haiku spawn failed", exc_info=True)
        return None
    _mark_nested_pid(proc.pid)
    try:
        stdout, stderr = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        _log.debug("haiku: timeout")
        return None
    except Exception:
        _log.error("haiku communicate failed", exc_info=True)
        return None
    if proc.returncode != 0:
        try:
            err = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        except Exception:
            err = ""
        _log.debug("haiku rc=%s stderr=%s", proc.returncode, err[:200])
        return None
    try:
        out = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
    except Exception:
        out = ""
    out = out.strip()
    if not out:
        return None
    out = out.splitlines()[0].strip().strip('"').strip("'").strip()
    out = _strip_markdown(out)
    # No length cap — the widget's summary cell clips overflow on its own
    # (grid + width-clamped Frame with propagate disabled). Trailing "…" was
    # confusing because it suggested truncation we'd applied vs the widget did.
    return out or None


def _do_summarize(target_pid):
    """Background mode: read transcript, call summariser, merge result into
    state file. Provider is read from state JSON so the same entry point
    handles both Claude and Codex sessions."""
    # Mark ourselves so the overlay knows to skip our PID and any descendants.
    _mark_nested_pid(os.getpid())
    home = os.path.expanduser("~")
    state_dir = get_state_dir()
    state_file = os.path.join(state_dir, f"{target_pid}.json")
    sess_file = os.path.join(default_sessions_dir(), f"{target_pid}.json")

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state_data = json.load(f)
    except Exception:
        _log.debug("summarizer: no state file for pid=%s", target_pid)
        return
    cwd = state_data.get("cwd", "")
    provider = state_data.get("provider", "claude")

    session_id = None
    name = None
    try:
        with open(sess_file, "r", encoding="utf-8") as f:
            sd = json.load(f)
        session_id = sd.get("sessionId")
        name = sd.get("name") or None
    except Exception:
        pass
    if not session_id:
        _log.debug("summarizer: no sessionId for pid=%s", target_pid)
        return

    if provider == "codex":
        rollout = _find_codex_rollout(session_id)
        if not rollout:
            _log.debug("summarizer: codex rollout not found sid=%s", session_id)
            return
        scan = _scan_codex_rollout(rollout, session_id)
        msgs = scan["user_messages"]
        full_count = len(msgs)

        # Codex has no stable Claude-style away_summary. Keep cost bounded by
        # reducing the rollout locally into a small structured digest, then ask
        # the mini model to normalize that into the row label.
        if scan["digest"]:
            transcript = scan["digest"]
            signal_kind = "codex_digest"
        elif msgs:
            sample = msgs[:5] if full_count >= 5 else msgs
            transcript = "\n\n---\n\n".join(m[:400] for m in sample)
            signal_kind = "codex_user_msgs"
        else:
            _log.debug("summarizer: codex rollout has no usable signals")
            return
        label = _call_codex_summary(transcript)
        summary_source = "codex_mini"
    else:
        jsonl = _find_session_jsonl(home, cwd, session_id)
        if not jsonl:
            _log.debug("summarizer: jsonl not found cwd=%s sid=%s", cwd, session_id)
            return
        msgs = list(_iter_user_messages(jsonl))
        full_count = len(msgs)
        # Signal priority — pick exactly one, hand it to Haiku for length/language
        # normalization. (1) jsonl ai-title (the same source wezterm tab labels
        # use); (2) sessions/{pid}.json `name` (user `/rename`); (3) latest
        # away_summary recap; (4) first user messages.
        if (ai_title := _latest_ai_title(jsonl)):
            transcript = ai_title
            signal_kind = "ai_title"
        elif name:
            transcript = name
            signal_kind = "name"
        elif (away := _latest_away_summary(jsonl)):
            transcript = away[:1000]
            signal_kind = "away_summary"
        elif msgs:
            sample = msgs[:5] if full_count >= 5 else msgs
            transcript = "\n\n---\n\n".join(m[:400] for m in sample)
            signal_kind = "user_msgs"
        else:
            return
        label = _call_haiku(transcript)
        summary_source = "haiku"

    if not label:
        return
    _log.debug("=> %s signal=%s -> %r", provider, signal_kind, label)

    # Re-read for concurrent-write safety, then merge.
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = state_data
    cur["summary"] = label
    cur["summarySource"] = summary_source
    cur["summaryAt"] = int(time.time())
    cur["summaryMsgCount"] = full_count
    cur["summarySessionId"] = session_id

    if not _atomic_write_json(state_file, cur, "summarizer"):
        return
    _log.debug("=> %s summary written: pid=%s %r (msgs=%d)",
               provider, target_pid, label, full_count)


# Refresh policy for Haiku summarization (seconds).
# trim/empty → spawn immediately; haiku → require gap.
_HAIKU_REFRESH_SECONDS = 300


def _should_summarize(state_data):
    """Return True if Stop-hook should spawn a summarizer (Claude Haiku or
    Codex mini)."""
    src = state_data.get("summarySource")
    if src in (None, "", "trim"):
        return True
    if src in ("haiku", "codex_mini"):
        last = state_data.get("summaryAt", 0)
        now = int(time.time())
        if now - last < _HAIKU_REFRESH_SECONDS:
            return False
        # Time elapsed; let summarizer re-evaluate against full message count.
        return True
    return False


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(0)

    if args[0] == "__codex_done_recheck__":
        try:
            raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            time.sleep(_CODEX_DONE_RECHECK_DELAY_S)
            sys.argv = [sys.argv[0]] + data.get("argv", [])
            hook_payload = json.dumps(data.get("hook_data", {})).encode("utf-8")
            sys.stdin = io.TextIOWrapper(io.BytesIO(hook_payload), encoding="utf-8")
            main()
        except Exception:
            _log.error("codex done recheck failed", exc_info=True)
        return

    # Background summarizer mode (re-entry) — runs from a child env with the
    # NESTED marker set; treat as the trusted summarizer path, not a hook.
    if args[0] == "__summarize__" and len(args) >= 2:
        raw = args[1]
        # Accept both numeric PIDs (Claude / hook-registered Codex) and the
        # `codex-<sid8>` string IDs codex_rollout_poller writes for hook-less
        # sessions; downstream f-string formatting handles either.
        try:
            target_pid = int(raw)
        except ValueError:
            target_pid = raw
        if not target_pid:
            sys.exit(0)
        try:
            _do_summarize(target_pid)
        except Exception:
            _log.error("summarizer crashed", exc_info=True)
        return

    # Skip hook firings inside any nested `claude` invocation we spawned for
    # summarization, otherwise that child's hooks would create a transient
    # session entry that the overlay flickers in and out of view.
    if os.environ.get("SESSION_MONITOR_NESTED") == "1":
        sys.exit(0)

    # Optional `--provider claude|codex` prefix injected by Codex hook commands.
    # Codex hook payloads and environment conventions differ from Claude's, so
    # the installer makes the provider explicit. Falls through to auto-detection
    # below when omitted.
    forced_provider = None
    if args and args[0] == "--provider" and len(args) >= 2:
        forced_provider = args[1]
        args = args[2:]

    if not args:
        sys.exit(0)
    state = args[0]  # working / done / question
    requested_state = state
    home = os.path.expanduser("~")
    state_dir = get_state_dir()
    sessions_dir = default_sessions_dir()
    my_pid = os.getpid()

    os.makedirs(state_dir, exist_ok=True)

    # Read stdin JSON (hook event data) — force UTF-8 decode of raw bytes,
    # since Windows default sys.stdin.encoding is cp949 and would mangle Korean
    # text in the prompt payload (resulting in surrogate-escaped summaries).
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
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
    matched_session = None
    tree = _build_process_tree()

    # Provider precedence: --provider arg > env var > ancestor process tree >
    # 'claude' default (back-compat). Do not infer from hook_event_name:
    # Claude and Codex payloads both expose that key in recent CLIs.
    provider = (
        forced_provider
        or os.environ.get("SESSION_MONITOR_PROVIDER")
        or None
    )
    if not provider:
        p = my_pid
        _visited = set()
        while p and p not in _visited:
            _visited.add(p)
            entry = tree.get(p)
            if not entry:
                break
            prov = _basename_provider(entry[1])
            if prov:
                provider = prov
                break
            p = entry[0]
    if not provider:
        provider = "claude"

    llm_pid = _find_ancestor_llm_pid(my_pid, tree)
    if provider == "codex" and llm_pid and _is_codex_desktop_app_pid(llm_pid, tree):
        _log.debug(
            "=> Codex desktop app-server hook ignored: pid=%s session_id=%s",
            llm_pid, session_id,
        )
        return

    if provider == "codex" and session_id and _codex_session_is_nested(session_id):
        _log.debug("=> Codex subagent hook ignored: session_id=%s", session_id)
        return

    if provider == "codex" and session_id:
        _mark_codex_hooked_session(session_id)

    promoted_virtual_id = None
    session_rollover = False

    # Phase 2: Match by session_id (full scan)
    session_matched = False
    if session_id:
        for sf, basename, sess in sessions:
            if sess.get("sessionId") == session_id:
                session_matched = True
                candidate_pid = _session_pid(sess, basename)
                matched_cwd = sess.get("cwd", cwd)
                if provider == "codex" and _is_virtual_id(candidate_pid) and llm_pid:
                    # A rollout fallback row exists for this session, but this
                    # invocation came from a real Codex hook. Let Phase 2.5
                    # register the real PID, then remove the virtual files.
                    promoted_virtual_id = candidate_pid
                    _log.debug("Phase 2: virtual session match -> promote %s via pid=%s",
                               candidate_pid, llm_pid)
                else:
                    pid = candidate_pid
                    matched_session = sess
                    _log.debug("Phase 2: session_id match -> pid=%s file=%s", pid, basename)
                break
        if pid is None and not session_matched:
            _log.debug("Phase 2: no session_id match found")

    # Phase 2.5: Self-register — fix session file when sessionId mismatches
    if pid is None and session_id and llm_pid:
        real_pid = llm_pid
        sess_file = os.path.join(sessions_dir, f"{real_pid}.json")
        try:
            if os.path.exists(sess_file):
                with open(sess_file, "r", encoding="utf-8") as f:
                    sess = json.load(f)
                existing_sid = sess.get("sessionId")
                if provider == "codex" and existing_sid and existing_sid != session_id:
                    if state == "session_start":
                        _log.debug(
                            "Phase 2.5: codex session_start mismatch ignored in %s.json old=%s new=%s",
                            real_pid, existing_sid, session_id,
                        )
                        return
                    _log.debug(
                        "Phase 2.5: codex session rollover in %s.json old=%s new=%s",
                        real_pid, existing_sid, session_id,
                    )
                    session_rollover = True
                if existing_sid != session_id:
                    sess["sessionId"] = session_id
                    sess["startedAt"] = int(time.time() * 1000)
                    _atomic_write_json(sess_file, sess, "session-register")
                    _log.debug("Phase 2.5: updated sessionId in %s.json", real_pid)
                pid = real_pid
                matched_session = sess
                matched_cwd = sess.get("cwd", cwd)
            else:
                # Prefer the cwd already recorded in our state file (the
                # home cwd captured at first hook fire) over the current
                # tool cwd, which may be a subdirectory.
                home_cwd = matched_cwd or cwd
                state_fps = [os.path.join(state_dir, f"{real_pid}.json")]
                if promoted_virtual_id:
                    state_fps.insert(0, os.path.join(state_dir, f"{promoted_virtual_id}.json"))
                for state_fp in state_fps:
                    try:
                        with open(state_fp, "r", encoding="utf-8") as fh:
                            home_cwd = json.load(fh).get("cwd") or home_cwd
                            break
                    except Exception:
                        pass
                sess = {
                    "pid": real_pid,
                    "sessionId": session_id,
                    "cwd": home_cwd,
                    "startedAt": int(time.time() * 1000),
                    "provider": provider,
                }
                _atomic_write_json(sess_file, sess, "session-register")
                pid = real_pid
                matched_session = sess
                matched_cwd = home_cwd
                _log.debug("Phase 2.5: created session file %s.json", real_pid)
        except Exception:
            _log.error("Phase 2.5: failed", exc_info=True)

    # Phase 3: Match by cwd (only if session_id didn't match)
    if pid is None and cwd:
        cwd_matches = [
            (sf, basename, sess) for sf, basename, sess in sessions
            if _norm_path(sess.get("cwd", "")) == norm_cwd
        ]
        _log.debug("Phase 3: cwd matches = %d", len(cwd_matches))

        if len(cwd_matches) == 1:
            sf, basename, sess = cwd_matches[0]
            pid = _session_pid(sess, basename)
            matched_session = sess
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
                sess_pid = _session_pid(sess, basename)
                if sess_pid in ancestors:
                    ancestor_match = (sf, basename, sess, sess_pid)
                    _log.debug("Phase 3: ancestor match -> sess_pid=%s", sess_pid)
                    break

            if ancestor_match:
                sf, basename, sess, sess_pid = ancestor_match
                pid = sess_pid
                matched_session = sess
                matched_cwd = sess.get("cwd", cwd)
            else:
                _log.debug("Phase 3: no ancestor match among %d cwd matches, skipping",
                           len(cwd_matches))

    # Phase 3.5: ancestor PID matching (no CWD constraint)
    if pid is None:
        ancestors = _get_ancestor_pids(my_pid, tree)
        _log.debug("Phase 3.5: trying ancestor match without CWD, ancestors=%s", ancestors)
        for sf, basename, sess in sessions:
            sess_pid = _session_pid(sess, basename)
            if sess_pid in ancestors:
                pid = sess_pid
                matched_session = sess
                matched_cwd = sess.get("cwd", cwd)
                _log.debug("Phase 3.5: ancestor match -> pid=%s file=%s", pid, basename)
                break
        if pid is None:
            _log.debug("Phase 3.5: no ancestor match found")

    # No match — do not write to an unrelated session
    if pid is None:
        _log.debug("No session match found; skipping state write")
        sys.exit(0)

    # Write state file
    state_file = os.path.join(state_dir, f"{pid}.json")
    promoted_state_file = (
        os.path.join(state_dir, f"{promoted_virtual_id}.json")
        if promoted_virtual_id else None
    )
    promoted_session_file = (
        os.path.join(sessions_dir, f"{promoted_virtual_id}.json")
        if promoted_virtual_id else None
    )
    now = int(time.time())

    # Read existing state (for hwnd preservation and same-state skip)
    existing = None
    for existing_fp in (state_file, promoted_state_file):
        if not existing_fp:
            continue
        try:
            with open(existing_fp, "r", encoding="utf-8") as f:
                existing = json.load(f)
            break
        except Exception:
            pass

    is_user_prompt = "prompt" in hook_data
    captured_hwnd = _capture_foreground_hwnd(my_pid, tree, is_user_prompt=is_user_prompt)
    if captured_hwnd is None and existing:
        captured_hwnd = existing.get("hwnd")

    # Resolve meta-states based on prior state + payload
    # idle_prompt: 텍스트 생성 중 ESC vs 단순 유휴 구분.
    #   prev=working  → interrupted (ESC로 생성 중단된 것으로 추정)
    #   prev=그 외    → skip (done/idle/question을 덮어쓰지 않음)
    if state == "idle_prompt":
        prev = existing.get("state") if existing else None
        if prev == "working":
            state = "interrupted"
            _log.debug("=> idle_prompt resolved to 'interrupted' (prev=working)")
        else:
            _log.debug("=> idle_prompt skipped (prev=%s, not working)", prev)
            return

    # tool_failure: PostToolUseFailure 훅 payload의 is_interrupt 플래그로 분기.
    #   is_interrupt=true → interrupted (도구 실행 중 ESC)
    #   그 외             → skip (일반 도구 실패는 Claude가 이어감)
    if state == "tool_failure":
        if hook_data.get("is_interrupt"):
            state = "interrupted"
            _log.debug("=> tool_failure resolved to 'interrupted' (is_interrupt=true)")
        else:
            _log.debug("=> tool_failure skipped (is_interrupt not set)")
            return

    # ExitPlanMode can fire PostToolUse while Claude is still waiting for plan
    # approval. Treat that stale "working" write as question state when the
    # session metadata already exposes the waiting flag.
    if state == "working" and provider == "claude" and _session_waits_for_user(matched_session):
        state = "question"
        _log.debug("=> working resolved to 'question' (session waitingFor=%r)",
                   matched_session.get("waitingFor") if matched_session else None)

    # Guard: catch-all "working"이 동시 실행된 "question"을 덮어쓰는 것을 방지
    # PreToolUse에서 catch-all("working")과 specific("question") 훅이 동시에 실행됨
    if (
        state == "working"
        and existing
        and existing.get("state") == "question"
        and (now - existing.get("updatedAt", 0)) < QUESTION_GUARD_SECONDS
    ):
        _log.debug("=> Guard: skipping 'working' — 'question' was written %ds ago",
                    now - existing.get("updatedAt", 0))
        return

    # Guard: "interrupted"가 이미 확정된 "done"/"interrupted"/"idle"을 덮어쓰는 것을 방지
    # done→interrupted 전환은 정당한 이유가 없음 (정상: working→done 또는 working→interrupted)
    if (
        state == "interrupted"
        and existing
        and existing.get("state") in ("done", "interrupted", "idle")
    ):
        _log.debug("=> Guard: skipping 'interrupted' — current state is '%s'",
                    existing.get("state"))
        return

    wezterm_info = _resolve_wezterm_info(existing)

    # session_start: capture wezterm info immediately so click-to-focus works
    # before the user sends any prompt. Don't change state — only attach the
    # wezterm field. Outside wezterm this is a no-op.
    if state == "session_start":
        if wezterm_info is None:
            _log.debug("=> session_start: not in wezterm, skipping")
            return
        if existing and existing.get("wezterm") == wezterm_info:
            _log.debug("=> session_start: wezterm info unchanged, skipping")
            return
        if existing:
            state_data = dict(existing)
            state_data["wezterm"] = wezterm_info
            state_data["updatedAt"] = now
        else:
            state_data = {
                "pid": pid,
                "state": "idle",
                "cwd": matched_cwd,
                "updatedAt": now,
                "wezterm": wezterm_info,
            }
        if "slot" not in state_data:
            state_data["slot"] = _allocate_slot(state_dir, pid, matched_cwd)
        _log.debug("=> session_start: writing wezterm info pid=%s slot=%s",
                   pid, state_data.get("slot"))
        if _atomic_write_json(state_file, state_data, "state"):
            for stale_fp in (promoted_state_file, promoted_session_file):
                if stale_fp and stale_fp != state_file:
                    try:
                        os.remove(stale_fp)
                    except OSError:
                        pass
        return

    # Codex Stop guard: each model turn ends with a Stop hook, so Stop fires
    # multiple times during agentic execution, not just at task completion.
    # Cross-check the rollout JSONL; if task_started > task_complete the task
    # is still running — suppress this intermediate done.
    if state == "done" and provider == "codex" and session_id:
        if not _codex_task_complete(session_id):
            _log.debug("=> Codex Stop: agentic turn boundary, not final done — suppressing")
            _spawn_codex_done_recheck(hook_data)
            return

    # Skip write if state unchanged AND wezterm info unchanged
    # (legacy catch-all hooks may still be installed, so keep the optimization)
    # Exceptions: missing slot needs allocation; UserPromptSubmit may need to stamp
    # a trim summary even when state stays "working" (e.g. consecutive prompts).
    if existing and existing.get("state") == state:
        existing_wt = existing.get("wezterm") if isinstance(existing.get("wezterm"), dict) else None
        has_slot = isinstance(existing.get("slot"), int)
        same_provider = existing.get("provider") == provider
        needs_question_snapshot = state == "question" and not existing.get("questionAt")
        if (
            wezterm_info == existing_wt
            and has_slot
            and same_provider
            and not is_user_prompt
            and not needs_question_snapshot
        ):
            _log.debug("=> State unchanged (%s), skipping write", state)
            return

    # Preserve cwd once it's been recorded — hooks fire from arbitrary tool
    # cwds (e.g. a PowerShell call inside a subdirectory), but the session's
    # home cwd is set once at startup and shouldn't drift to whichever
    # subdirectory a tool happens to run in.
    final_cwd = (existing.get("cwd") if existing and existing.get("cwd") else matched_cwd)

    state_data = {
        "pid": pid,
        "state": state,
        "cwd": final_cwd,
        "updatedAt": now,
        "provider": provider,
        # Marks this write as authoritative so the Codex rollout-JSONL poller
        # (codex_rollout_poller.py) skips PIDs that a real hook just touched.
        "lastSignalSource": "hook",
        "lastSignalAt": now,
    }
    if state == "interrupted":
        hook_event = hook_data.get("hook_event_name") or ""
        if provider == "claude" and (requested_state == "interrupted" or hook_event == "StopFailure"):
            state_data["interruptSource"] = "stop_failure"
            state_data["interruptAt"] = now
            if hook_event:
                state_data["interruptHookEvent"] = hook_event
        elif requested_state == "idle_prompt":
            state_data["interruptSource"] = "idle_prompt"
            state_data["interruptAt"] = now
        elif requested_state == "tool_failure":
            state_data["interruptSource"] = "tool_failure"
            state_data["interruptAt"] = now
    if captured_hwnd is not None:
        state_data["hwnd"] = captured_hwnd
    if wezterm_info is not None:
        state_data["wezterm"] = wezterm_info

    # Preserve slot/summary across writes; assign slot on first sight
    for k in _PRESERVED_FIELDS:
        if existing and k in existing:
            if session_rollover and k in _SUMMARY_FIELDS:
                continue
            if (
                provider == "codex"
                and session_id
                and k in _SUMMARY_FIELDS
                and existing.get("summarySessionId")
                and existing.get("summarySessionId") != session_id
            ):
                continue
            state_data[k] = existing[k]
    if "slot" not in state_data:
        state_data["slot"] = _allocate_slot(state_dir, pid, matched_cwd)
        _log.debug("=> Allocated slot=%d for pid=%s cwd=%s",
                   state_data["slot"], pid, matched_cwd)

    if state == "question":
        session_file = os.path.join(sessions_dir, f"{pid}.json")
        _attach_question_snapshot(state_data, hook_data, session_file, now)

    # UserPromptSubmit fallback: stamp a trimmed first-line summary ONLY when
    # the row currently has no summary at all. Subsequent prompts must not
    # overwrite the stamped value — otherwise the label would jitter to each
    # latest message until the next Stop fires the Haiku summarizer.
    if is_user_prompt:
        prompt_text = hook_data.get("prompt") or ""
        trimmed = _truncate_label(prompt_text, max_chars=18)
        if trimmed and not state_data.get("summary"):
            state_data["summary"] = trimmed
            state_data["summarySource"] = "trim"
            state_data["summaryAt"] = now
            _log.debug("=> Trim summary set: %r", trimmed)

    _log.debug("=> Writing state: pid=%s state=%s file=%s", pid, state, state_file)

    if not _atomic_write_json(state_file, state_data, "state"):
        return
    for stale_fp in (promoted_state_file, promoted_session_file):
        if stale_fp and stale_fp != state_file:
            try:
                os.remove(stale_fp)
            except OSError:
                pass

    # On Stop hook ("done"), kick off a background summarizer if due. The
    # summarizer reads provider from state JSON and shells out to either
    # `claude -p --model claude-haiku-4-5` or `codex exec -m gpt-5.4-mini
    # --ephemeral --ignore-user-config`.
    if state == "done" and _should_summarize(state_data):
        _spawn_summarizer(pid)


if __name__ == "__main__":
    main()
