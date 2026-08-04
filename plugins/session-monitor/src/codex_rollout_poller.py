#!/usr/bin/env python3
"""Disk sync for Codex CLI rollout rows that are not owned by hooks.

Codex writes a rollout JSONL for every session under
``~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<sid>.jsonl`` regardless of hook
configuration. Hook-less top-level rollouts are mirrored into PID-less rows so
completed sessions remain visible and clickable even when hooks did not bind
them to a real process.

Files written:

Ownership: when a real hook fires for a session, write-state.py records a
``~/.local/share/session-monitor/codex-hooked/<sessionId>.json`` marker. The
poller then treats that rollout as hook-owned forever and will not recreate it
as a PID-less fallback row after the real Codex process exits. Subagent and
exec rollouts are also classified as ignored. In all cases the source rollout
JSONL is left untouched; only monitor-generated virtual files are cleaned up.
"""

import glob
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime

# Cap the rollout walk: rollouts older than 24h are unlikely to be active
# and parsing them every tick is pure overhead.
_SCAN_MAX_AGE_S = 24 * 60 * 60
# After this much rollout silence we evict the row entirely.
_STALE_EVICTION_S = 30 * 60
# mtime-based fallback when the rollout has no task_started/task_complete
# events at all (very early in a session). Generous window because Codex's
# reasoning bursts can sit silent for tens of seconds while the model is
# thinking; a 5s threshold flips working↔done in a way that's distracting.
_WORKING_FRESHNESS_S = 30
# Full discovery is only needed to notice newly-created rollouts. Between
# discoveries, poll the small set of recently-active paths retained below.
_ROLLOUT_DISCOVERY_INTERVAL_S = 10

# Known event_msg payload.type markers, verified against Codex CLI 0.128.0.
# Comparing task_started vs terminal turn markers is the most reliable
# in-band state signal — mtime is the fallback before the first marker.
_START_TYPES = {"task_started"}
_COMPLETE_TYPES = {"task_complete", "agent_turn_complete", "turn_complete"}
_FAIL_TYPES = {"error", "task_failed", "turn_failed", "turn_aborted"}
_QUESTION_TOOL_NAMES = {"request_user_input"}

# Filename prefix for our PID-less virtual rows. Kept ASCII + dashes so
# every filesystem (and our int-PID parsers) handle it cleanly.
VIRTUAL_PREFIX = "codex-"
IGNORE_NESTED = "nested"
IGNORE_HOOK_OWNED = "hook_owned"
IGNORE_EXEC = "exec"
IGNORE_PRE_START = "pre_start"
_THREAD_META_CACHE_KEY = "__codex_thread_meta__"
_ROLLOUT_PATH_CACHE_KEY = "__codex_rollout_paths__"
_CACHE_META_KEYS = frozenset({_THREAD_META_CACHE_KEY, _ROLLOUT_PATH_CACHE_KEY})


def _codex_sessions_root():
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _codex_state_db():
    return os.path.join(os.path.expanduser("~"), ".codex", "state_5.sqlite")


def _codex_hooked_dir(state_dir):
    return os.path.join(os.path.dirname(state_dir), "codex-hooked")


def _hook_owned_at(state_dir, session_id):
    if not session_id:
        return 0.0
    path = os.path.join(_codex_hooked_dir(state_dir), f"{session_id}.json")
    if not os.path.exists(path):
        return 0.0
    data = _read_existing(path)
    value = _safe_float(data.get("updatedAt"))
    if value:
        return value
    try:
        return os.path.getmtime(path)
    except OSError:
        return 1.0


def _thread_newer_than_hook(metadata, hook_owned_at):
    return bool(hook_owned_at and _safe_float(metadata.get("threadUpdatedAt")) > hook_owned_at)


def _is_hook_owned_session(state_dir, session_id, metadata=None):
    hook_owned_at = _hook_owned_at(state_dir, session_id)
    if not hook_owned_at:
        return False
    return not _thread_newer_than_hook(metadata or {}, hook_owned_at)


def virtual_id_for(session_id: str) -> str:
    """Stable virtual ID derived from the Codex session UUID."""
    return f"{VIRTUAL_PREFIX}{session_id}"


def _legacy_virtual_id_for(session_id: str) -> str:
    """Old virtual ID format kept only to remove pre-migration files."""
    return f"{VIRTUAL_PREFIX}{session_id[:8]}"


def is_virtual_id(pid) -> bool:
    """True for the string virtual IDs this module writes."""
    return isinstance(pid, str) and pid.startswith(VIRTUAL_PREFIX)


def _read_first_json_line(path):
    """Return the first parsed JSON object in a JSONL file (Codex writes
    ``session_meta`` there). Returns None on any I/O or parse error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
        return json.loads(line)
    except (OSError, ValueError):
        return None


def _is_nested_rollout_payload(payload):
    """True for Codex subagent/nested exec rollouts, not user-facing sessions."""
    if not isinstance(payload, dict):
        return False
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source


def _is_exec_rollout_payload(payload):
    """True for non-interactive `codex exec` rollouts."""
    if not isinstance(payload, dict):
        return False
    return payload.get("originator") == "codex_exec" or payload.get("source") == "exec"


def _ignore_reason(state_dir, session_id, payload, metadata=None):
    """Return why this rollout should not create a monitor row, or None."""
    if _is_nested_rollout_payload(payload):
        return IGNORE_NESTED
    if _is_exec_rollout_payload(payload):
        return IGNORE_EXEC
    if _is_hook_owned_session(state_dir, session_id, metadata):
        return IGNORE_HOOK_OWNED
    return None


def _cache_ignored(prev_cache, path, mtime, session_id, reason):
    prev_cache[path] = {
        "mtime": mtime,
        "session_id": session_id,
        "ignored": True,
        "ignore_reason": reason,
    }


def _drop_virtual_monitor_row(sessions_dir, state_dir, session_id):
    """Remove only monitor-generated virtual files for an ignored rollout.

    The source rollout JSONL under ~/.codex/sessions is never touched.
    """
    for vid in {virtual_id_for(session_id), _legacy_virtual_id_for(session_id)}:
        for p in (
            os.path.join(sessions_dir, f"{vid}.json"),
            os.path.join(state_dir, f"{vid}.json"),
        ):
            try:
                os.remove(p)
            except OSError:
                pass


def _ignore_rollout(prev_cache, path, mtime, session_id, reason, sessions_dir, state_dir):
    """Mark a rollout ignored and remove only monitor-generated virtual row files."""
    _cache_ignored(prev_cache, path, mtime, session_id, reason)
    _drop_virtual_monitor_row(sessions_dir, state_dir, session_id)


def _payload_session_id(record):
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    return payload.get("id")


def _record_time(record):
    """Return a rollout record timestamp in unix seconds, or 0."""
    if not isinstance(record, dict):
        return 0
    value = record.get("timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("completed_at", "finished_at", "updated_at"):
            try:
                return int(float(payload.get(key) or 0))
            except (TypeError, ValueError):
                pass
    return 0


def _is_target_session(current_session_id, target_session_id):
    return not target_session_id or current_session_id is None or current_session_id == target_session_id


def _scan_turn_markers(path, session_id=None):
    """Single-pass walk of rollout turn markers.

    Returns ``(started, terminal, latest_failed)``. Older Codex rollouts can
    contain interrupted turns followed by later successful turns, so a failure
    marker is only an interrupted monitor state when it is the latest marker.
    """
    started = 0
    terminal = 0
    latest_marker = None
    current_session_id = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                record_session_id = _payload_session_id(d)
                if record_session_id:
                    current_session_id = record_session_id
                    continue
                if not _is_target_session(current_session_id, session_id):
                    continue
                if d.get("type") != "event_msg":
                    continue
                payload = d.get("payload") or {}
                pt = payload.get("type") if isinstance(payload, dict) else None
                if pt is None:
                    continue
                if pt in _START_TYPES:
                    started += 1
                    latest_marker = "started"
                elif pt in _COMPLETE_TYPES:
                    terminal += 1
                    latest_marker = "completed"
                elif pt in _FAIL_TYPES:
                    terminal += 1
                    latest_marker = "failed"
    except OSError:
        return 0, 0, False
    return started, terminal, latest_marker == "failed"


def _empty_activity_cache(session_id=None):
    return {
        "scan_session_id": session_id,
        "scan_offset": 0,
        "scan_dev": None,
        "scan_ino": None,
        "scan_size": 0,
        "started": 0,
        "completed": 0,
        "latest_marker": None,
        "completed_at": 0,
        "pending_questions": set(),
        "current_session_id": None,
    }


def _scan_tail(path, offset, limit=256):
    size = min(limit, max(0, offset))
    try:
        with open(path, "rb") as f:
            f.seek(offset - size)
            return f.read(size)
    except OSError:
        return b""


def _activity_cache_from_state(path, path_stat, session_id, state_data):
    """Seed a first-run scan from state written at the same rollout mtime."""
    if not isinstance(state_data, dict):
        return None
    if state_data.get("sessionId") not in (None, "", session_id):
        return None
    if (
        state_data.get("rolloutPath")
        and _norm_rollout_path(state_data["rolloutPath"]) != _norm_rollout_path(path)
    ):
        return None
    if state_data.get("rolloutMtime") != int(path_stat.st_mtime):
        return None
    # A pending question needs its real call_id set to detect its answer, so
    # bootstrap that uncommon state with a full scan.
    marker = {
        "working": "started",
        "done": "completed",
        "interrupted": "failed",
    }.get(state_data.get("state"))
    if not marker:
        return None
    activity = _empty_activity_cache(session_id)
    activity.update({
        "scan_offset": path_stat.st_size,
        "scan_dev": path_stat.st_dev,
        "scan_ino": path_stat.st_ino,
        "scan_size": path_stat.st_size,
        "scan_tail": _scan_tail(path, path_stat.st_size),
        "started": 1,
        "completed": 1 if marker in ("completed", "failed") else 0,
        "latest_marker": marker,
        "completed_at": state_data.get("completedAt", 0),
        "current_session_id": session_id,
        "mtime": path_stat.st_mtime,
    })
    return activity


def _scan_activity_incremental(path, session_id=None, cached=None):
    """Extend cached rollout activity by parsing only newly-appended records.

    A truncation, file replacement, session change, or legacy cache entry falls
    back to one full scan. Invalid partially-written final JSON is retried
    safely on the next poll.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return _empty_activity_cache(session_id)

    reusable = bool(
        isinstance(cached, dict)
        and cached.get("scan_session_id") == session_id
        and isinstance(cached.get("scan_offset"), int)
        and cached.get("scan_offset", 0) <= stat.st_size
        and cached.get("scan_dev") == stat.st_dev
        and cached.get("scan_ino") == stat.st_ino
        and (
            stat.st_size > cached.get("scan_size", 0)
            or stat.st_mtime == cached.get("mtime")
        )
    )
    if reusable and cached.get("scan_tail") is not None:
        tail = cached.get("scan_tail")
        try:
            with open(path, "rb") as check:
                check.seek(max(0, cached["scan_offset"] - len(tail)))
                reusable = check.read(len(tail)) == tail
        except OSError:
            reusable = False
    activity = dict(cached) if reusable else _empty_activity_cache(session_id)
    activity["pending_questions"] = set(activity.get("pending_questions") or ())
    offset = activity.get("scan_offset", 0) if reusable else 0

    try:
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    # Retry an in-flight final JSON record on the next poll.
                    if not line.endswith(b"\n"):
                        f.seek(line_start)
                        break
                    continue
                record_session_id = _payload_session_id(d)
                if record_session_id:
                    activity["current_session_id"] = record_session_id
                    continue
                if not _is_target_session(activity.get("current_session_id"), session_id):
                    continue
                line_type = d.get("type")
                payload = d.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if line_type == "event_msg":
                    pt = payload.get("type")
                    if pt in _START_TYPES:
                        activity["started"] += 1
                        activity["latest_marker"] = "started"
                    elif pt in _COMPLETE_TYPES:
                        activity["completed"] += 1
                        activity["latest_marker"] = "completed"
                        activity["completed_at"] = _record_time(d) or activity["completed_at"]
                    elif pt in _FAIL_TYPES:
                        activity["completed"] += 1
                        activity["latest_marker"] = "failed"
                elif line_type == "response_item":
                    item_type = payload.get("type")
                    call_id = payload.get("call_id")
                    if (
                        item_type == "function_call"
                        and payload.get("name") in _QUESTION_TOOL_NAMES
                        and call_id
                    ):
                        activity["pending_questions"].add(call_id)
                    elif item_type == "function_call_output" and call_id:
                        activity["pending_questions"].discard(call_id)
            activity["scan_offset"] = f.tell()
            tail_size = min(256, activity["scan_offset"])
            f.seek(activity["scan_offset"] - tail_size)
            activity["scan_tail"] = f.read(tail_size)
    except OSError:
        return activity

    activity["scan_session_id"] = session_id
    activity["scan_dev"] = stat.st_dev
    activity["scan_ino"] = stat.st_ino
    activity["scan_size"] = stat.st_size
    activity["mtime"] = stat.st_mtime
    return activity


def _activity_result(activity, include_latest=False, include_completed_at=False):
    latest_marker = activity.get("latest_marker")
    result = (
        activity.get("started", 0),
        activity.get("completed", 0),
        latest_marker == "failed",
        bool(activity.get("pending_questions")),
    )
    if include_latest:
        result = result + (latest_marker,)
    if include_completed_at:
        result = result + (activity.get("completed_at", 0),)
    return result


def _scan_activity(path, session_id=None, include_latest=False, include_completed_at=False):
    """Return rollout activity after a full scan (compatibility/test helper)."""
    activity = _scan_activity_incremental(path, session_id)
    return _activity_result(activity, include_latest, include_completed_at)


def _infer_state(mtime, started, completed, failed, waiting_for_user=False, latest_marker=None):
    """Map turn markers + mtime to our state vocabulary."""
    if failed:
        return "interrupted"
    if waiting_for_user:
        return "question"
    if latest_marker == "started":
        return "working"
    if latest_marker == "completed":
        return "done"
    if started > 0 or completed > 0:
        return "working" if started > completed else "done"
    if time.time() - mtime < _WORKING_FRESHNESS_S:
        return "working"
    return "done"


def find_rollout_for_session(session_id):
    """Locate Codex's rollout JSONL for a session ID."""
    if not session_id:
        return None
    root = _codex_sessions_root()
    if not os.path.isdir(root):
        return None
    for path in glob.glob(os.path.join(root, "*", "*", "*", f"rollout-*-{session_id}.jsonl")):
        return path
    for path in glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        meta = _read_first_json_line(path)
        if meta and meta.get("type") == "session_meta" and (meta.get("payload") or {}).get("id") == session_id:
            return path
    return None


def infer_rollout_state_for_session(session_id, scan_cache=None):
    """Return current rollout-derived state for a Codex session, or None."""
    cached = scan_cache.get(session_id) if isinstance(scan_cache, dict) else None
    path = cached.get("path") if isinstance(cached, dict) else None
    if not path or not os.path.exists(path):
        path = find_rollout_for_session(session_id)
    if not path:
        return None
    activity = _scan_activity_incremental(path, session_id, cached)
    activity["path"] = path
    if isinstance(scan_cache, dict):
        scan_cache[session_id] = activity
    started, completed, failed, waiting, latest_marker = _activity_result(
        activity, include_latest=True
    )
    return _infer_state(
        activity.get("mtime", 0), started, completed, failed, waiting, latest_marker
    )


def _atomic_write_json(path, data):
    """Best-effort atomic write — unique tempfile + os.replace retry."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = None
    for attempt in range(5):
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
                tmp = None
                return True
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except PermissionError:
            if attempt == 4:
                break
            time.sleep(0.03 * (attempt + 1))
        except OSError:
            break
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            tmp = None
    return False


def _read_existing(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _trim_summary(value, limit=32):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _thread_meta_mtime(db_path):
    mtimes = []
    for suffix in ("", "-wal", "-shm"):
        try:
            mtimes.append(os.path.getmtime(db_path + suffix))
        except OSError:
            pass
    return max(mtimes) if mtimes else 0


def _load_thread_metadata(prev_cache):
    """Read Codex's normalized thread index when available.

    Rollout JSONL is the real-time signal, while state_5.sqlite carries useful
    app metadata such as the thread title and token usage. Treat it as optional:
    schema or lock failures should never hide rollout-derived rows.
    """
    db_path = _codex_state_db()
    mtime = _thread_meta_mtime(db_path)
    if not mtime:
        return {}

    cached = prev_cache.get(_THREAD_META_CACHE_KEY)
    if cached and cached.get("mtime") == mtime:
        return cached.get("data", {})

    data = {}
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.1)
        con.row_factory = sqlite3.Row
        for row in con.execute(
            "select id, title, tokens_used, updated_at, source, cwd, rollout_path "
            "from threads"
        ):
            sid = row["id"]
            if not sid:
                continue
            data[sid] = {
                "title": row["title"] or "",
                "tokensUsed": row["tokens_used"],
                "threadUpdatedAt": row["updated_at"],
                "source": row["source"] or "",
                "cwd": row["cwd"] or "",
                "rolloutPath": row["rollout_path"] or "",
            }
    except sqlite3.Error:
        data = {}
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass

    prev_cache[_THREAD_META_CACHE_KEY] = {"mtime": mtime, "data": data}
    return data


def _safe_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _thread_metadata_recent(metadata, cutoff):
    if not isinstance(metadata, dict):
        return False
    return _safe_float(metadata.get("threadUpdatedAt")) >= cutoff


def _rollout_activity_time(mtime, metadata):
    return max(float(mtime or 0), _safe_float((metadata or {}).get("threadUpdatedAt")))


def _norm_rollout_path(path):
    if not path:
        return ""
    value = str(path)
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.abspath(os.path.normpath(value)))


def _codex_surface(source, originator):
    if str(originator or "").lower() == "codex desktop":
        return "app"
    source_text = str(source or "").lower()
    if source_text == "cli":
        return "cli"
    if source_text in ("vscode", "ide"):
        return "ide"
    return source_text or "unknown"


def _norm_path(p):
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


def _allocate_slot(state_dir, my_id, my_cwd):
    """Lowest unused slot among rows in the same cwd, virtual or not.
    Kept in lockstep with write-state.py's _allocate_slot so PID-having
    Claude/Codex rows and PID-less virtual rows share one slot space."""
    norm_my = _norm_path(my_cwd)
    used = set()
    for sf in glob.glob(os.path.join(state_dir, "*.json")):
        base = os.path.splitext(os.path.basename(sf))[0]
        if base == my_id:
            continue
        d = _read_existing(sf)
        if _norm_path(d.get("cwd", "")) != norm_my:
            continue
        s = d.get("slot")
        if isinstance(s, int) and s >= 1:
            used.add(s)
    n = 1
    while n in used:
        n += 1
    return n


# State fields that should carry over across writes — summary cache from a
# prior summariser run, slot assignment, anything we don't recompute every
# tick. Mirrors write-state.py's _PRESERVED_FIELDS for the same reason.
_PRESERVED_STATE_FIELDS = (
    "slot", "summary", "summarySource",
    "summaryAt", "summaryMsgCount", "summarySessionId",
    "completedAt",
)


def poll_codex_rollouts(known_session_ids, prev_cache, sessions_dir, state_dir, started_after=0.0):
    """Sync ~/.codex/sessions rollouts to monitor sessions+state JSONs.

    The InstanceTracker's standard sessions/state loops pick up the files
    we write on the next tick, so this function only does I/O — it returns
    nothing.

    ``known_session_ids`` are sessionIds already owned by hook-registered
    Codex sessions (PID-having); we delete any virtual files we previously
    wrote for them so the row promotes cleanly on next render.

    ``prev_cache`` is a tick-over dict keyed by rollout path; we use it to
    skip the first-line read when a rollout's mtime hasn't changed.
    """
    root = _codex_sessions_root()
    if not os.path.isdir(root):
        return

    try:
        os.makedirs(sessions_dir, exist_ok=True)
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        return

    now = time.time()
    cutoff = now - _SCAN_MAX_AGE_S
    seen_virtual_ids = set()
    seen_rollout_paths = set()
    thread_meta = _load_thread_metadata(prev_cache)
    thread_rollout_paths = {
        _norm_rollout_path(metadata.get("rolloutPath"))
        for metadata in thread_meta.values()
        if metadata.get("rolloutPath") and _thread_metadata_recent(metadata, cutoff)
    }
    thread_rollout_files = {
        metadata.get("rolloutPath")
        for metadata in thread_meta.values()
        if metadata.get("rolloutPath") and _thread_metadata_recent(metadata, cutoff)
    }

    discovery = prev_cache.get(_ROLLOUT_PATH_CACHE_KEY, {})
    discovery_due = (
        not isinstance(discovery, dict)
        or now - discovery.get("checked_at", 0) >= _ROLLOUT_DISCOVERY_INTERVAL_S
    )
    if discovery_due:
        rollout_paths = set(glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")))
        discovery_checked_at = now
    else:
        rollout_paths = set(discovery.get("paths") or ())
        discovery_checked_at = discovery.get("checked_at", now)
    rollout_paths.update(thread_rollout_files)

    for path in rollout_paths:
        try:
            path_stat = os.stat(path)
            mtime = path_stat.st_mtime
        except OSError:
            continue
        path_key = _norm_rollout_path(path)
        if mtime < cutoff and path_key not in thread_rollout_paths:
            continue

        cached = prev_cache.get(path)
        # Cache hit when the rollout's mtime hasn't moved since last tick.
        # Codex appends to a rollout in place, so unchanged mtime ⇒
        # session_meta and the turn-marker counts are also unchanged. We
        # skip both the first-line read and the full-file scan, then skip
        # the disk write below as well — Codex sessions can have rollouts
        # in the multi-megabyte range and re-parsing them every 500ms
        # turned the poller into the dominant cost on the hot path.
        cache_valid = bool(
            cached is not None
            and cached.get("mtime") == mtime
            and (
                "scan_size" not in cached
                or cached.get("scan_size") == path_stat.st_size
            )
        )
        ignored_cache_hit = cache_valid and cached.get("ignored")
        cache_hit = cache_valid and not cached.get("ignored")
        cache_extendable = bool(
            isinstance(cached, dict)
            and not cached.get("ignored")
            and cached.get("session_id")
            and cached.get("scan_session_id") == cached.get("session_id")
            and isinstance(cached.get("scan_offset"), int)
            and cached.get("scan_offset", 0) <= path_stat.st_size
            and cached.get("scan_dev") == path_stat.st_dev
            and cached.get("scan_ino") == path_stat.st_ino
            and (
                path_stat.st_size > cached.get("scan_size", 0)
                or path_stat.st_mtime == cached.get("mtime")
            )
        )
        session_id = cached.get("session_id") if cache_valid else None
        if cache_extendable:
            session_id = cached.get("session_id")
        metadata = thread_meta.get(session_id, {}) if session_id else {}
        if ignored_cache_hit and session_id:
            reason = cached.get("ignore_reason")
            if reason == IGNORE_PRE_START:
                if started_after and _rollout_activity_time(mtime, metadata) >= started_after:
                    ignored_cache_hit = False
                else:
                    _drop_virtual_monitor_row(sessions_dir, state_dir, session_id)
                    seen_rollout_paths.add(path)
                    continue
            elif reason == IGNORE_HOOK_OWNED and not _is_hook_owned_session(state_dir, session_id, metadata):
                ignored_cache_hit = False
            elif reason:
                _drop_virtual_monitor_row(sessions_dir, state_dir, session_id)
                seen_rollout_paths.add(path)
                continue
        if not cache_hit and not cache_extendable:
            meta = _read_first_json_line(path)
            if not meta or meta.get("type") != "session_meta":
                continue
            payload = meta.get("payload") or {}
            session_id = payload.get("id")
            cwd = payload.get("cwd")
            source = payload.get("source") or ""
            originator = payload.get("originator") or ""
            if not session_id or not cwd:
                continue
            metadata = thread_meta.get(session_id, {})
        if mtime < cutoff and not _thread_metadata_recent(metadata, cutoff):
            continue
        if now - mtime > _STALE_EVICTION_S and not metadata:
            continue
        if started_after and _rollout_activity_time(mtime, metadata) < started_after:
            if session_id:
                seen_rollout_paths.add(path)
                _ignore_rollout(
                    prev_cache, path, mtime, session_id, IGNORE_PRE_START,
                    sessions_dir, state_dir,
                )
            continue
        if ignored_cache_hit:
            reason = _ignore_reason(state_dir, session_id, payload, metadata)
            if not reason:
                ignored_cache_hit = False
            else:
                _cache_ignored(prev_cache, path, mtime, session_id, reason)
        if ignored_cache_hit:
            if session_id:
                # Hook ownership may be learned after a rollout was first
                # cached for another ignore reason. Keep the current row hidden
                # and refresh the reason for diagnostics.
                if _is_hook_owned_session(state_dir, session_id, metadata):
                    cached["ignore_reason"] = IGNORE_HOOK_OWNED
                _drop_virtual_monitor_row(sessions_dir, state_dir, session_id)
            seen_rollout_paths.add(path)
            continue
        if cache_hit and _is_hook_owned_session(state_dir, cached.get("session_id"), metadata):
            session_id = cached.get("session_id")
            if session_id:
                _ignore_rollout(
                    prev_cache, path, mtime, session_id, IGNORE_HOOK_OWNED,
                    sessions_dir, state_dir,
                )
            seen_rollout_paths.add(path)
            continue
        if cache_hit:
            session_id = cached["session_id"]
            cwd = cached["cwd"]
            started = cached["started"]
            completed = cached["completed"]
            failed = cached["failed"]
            waiting = cached.get("waiting", False)
            latest_marker = cached.get("latest_marker")
            completed_at = cached.get("completed_at", 0)
            source = cached.get("source", "")
            originator = cached.get("originator", "")
        else:
            reason = (
                IGNORE_HOOK_OWNED
                if cache_extendable and _is_hook_owned_session(state_dir, session_id, metadata)
                else None
            )
            if not cache_extendable:
                reason = _ignore_reason(state_dir, session_id, payload, metadata)
            if reason:
                seen_rollout_paths.add(path)
                _ignore_rollout(
                    prev_cache, path, mtime, session_id, reason,
                    sessions_dir, state_dir,
                )
                continue
            scan_seed = cached if cache_extendable else None
            if scan_seed is None:
                bootstrap_state = _read_existing(
                    os.path.join(state_dir, f"{virtual_id_for(session_id)}.json")
                )
                scan_seed = _activity_cache_from_state(
                    path, path_stat, session_id, bootstrap_state
                )
            activity = _scan_activity_incremental(path, session_id, scan_seed)
            started, completed, failed, waiting, latest_marker, completed_at = _activity_result(
                activity, include_latest=True, include_completed_at=True
            )
            activity.update({
                "session_id": session_id,
                "cwd": cwd,
                "failed": failed,
                "waiting": waiting,
                "source": source,
                "originator": originator,
            })
            prev_cache[path] = activity

        seen_rollout_paths.add(path)
        title = metadata.get("title") or ""
        tokens_used = metadata.get("tokensUsed")
        thread_updated_at = metadata.get("threadUpdatedAt")
        db_source = metadata.get("source") or source
        surface = _codex_surface(db_source, originator)
        vid = virtual_id_for(session_id)
        legacy_vid = _legacy_virtual_id_for(session_id)
        sess_path = os.path.join(sessions_dir, f"{vid}.json")
        state_path = os.path.join(state_dir, f"{vid}.json")
        if legacy_vid != vid:
            for p in (
                os.path.join(sessions_dir, f"{legacy_vid}.json"),
                os.path.join(state_dir, f"{legacy_vid}.json"),
            ):
                try:
                    os.remove(p)
                except OSError:
                    pass

        if session_id in known_session_ids:
            # A hook already owns this session. Drop any virtual files we
            # may have written earlier so the standard PID-having row is
            # the only one rendered.
            for p in (sess_path, state_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            continue

        seen_virtual_ids.add(vid)

        state = _infer_state(mtime, started, completed, failed, waiting, latest_marker)
        activity_time = int(_rollout_activity_time(mtime, metadata))

        sess_record = {
            "pid": vid,
            "virtualId": vid,
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": int(mtime * 1000),
            "provider": "codex",
            "codexSource": db_source,
            "codexOriginator": originator,
            "codexSurface": surface,
            "title": title,
        }

        existing = _read_existing(state_path)
        new_state = {
            "pid": vid,
            "sessionId": session_id,
            "state": state,
            "cwd": cwd,
            "updatedAt": activity_time,
            "provider": "codex",
            "lastSignalSource": "rollout",
            "lastSignalAt": int(now),
            "rolloutPath": path,
            "rolloutMtime": int(mtime),
            "codexSource": db_source,
            "codexOriginator": originator,
            "codexSurface": surface,
            "codexTitle": title,
        }
        if tokens_used is not None:
            new_state["tokensUsed"] = tokens_used
        if thread_updated_at is not None:
            new_state["threadUpdatedAt"] = thread_updated_at
        for k in _PRESERVED_STATE_FIELDS:
            if k in existing:
                new_state[k] = existing[k]
        if state == "done":
            new_state["completedAt"] = int(completed_at or new_state.get("completedAt") or activity_time)
        else:
            new_state.pop("completedAt", None)
        if title and not new_state.get("summary"):
            new_state["summary"] = _trim_summary(title)
            new_state["summarySource"] = "trim"
            new_state["summaryAt"] = int(now)
        if "slot" not in new_state:
            new_state["slot"] = _allocate_slot(state_dir, vid, cwd)

        if cache_hit:
            # Normal steady state: unchanged rollout already has matching disk
            # files. Still repair missing/corrupt files so manual cleanup or a
            # failed prior write does not hide the row until the next rollout
            # append.
            sess_existing = _read_existing(sess_path)
            state_existing = _read_existing(state_path)
            if sess_existing.get("sessionId") != session_id:
                _atomic_write_json(sess_path, sess_record)
            if (
                state_existing.get("pid") != vid
                or state_existing.get("rolloutMtime") != int(mtime)
                or state_existing.get("updatedAt") != activity_time
                or state_existing.get("state") != state
                or state_existing.get("codexTitle") != title
                or state_existing.get("codexSurface") != surface
                or state_existing.get("threadUpdatedAt") != thread_updated_at
                or state_existing.get("completedAt") != new_state.get("completedAt")
            ):
                _atomic_write_json(state_path, new_state)
            continue

        _atomic_write_json(sess_path, sess_record)
        _atomic_write_json(state_path, new_state)

    # Stale eviction: remove virtual files whose source rollout vanished
    # or whose stored rolloutMtime crossed the silence threshold.
    for sf in glob.glob(os.path.join(state_dir, f"{VIRTUAL_PREFIX}*.json")):
        base = os.path.splitext(os.path.basename(sf))[0]
        if base in seen_virtual_ids:
            continue
        d = _read_existing(sf)
        session_id = d.get("sessionId")
        if not session_id and base.startswith(VIRTUAL_PREFIX):
            session_id = base[len(VIRTUAL_PREFIX):]
        metadata = thread_meta.get(session_id, {})
        metadata_recent = _thread_metadata_recent(metadata, cutoff)
        rollout_path = d.get("rolloutPath")
        rollout_mtime = d.get("rolloutMtime", 0)
        is_stale = (
            ((not rollout_path or not os.path.exists(rollout_path)) and not metadata_recent)
            or (now - rollout_mtime > _STALE_EVICTION_S and not metadata_recent)
        )
        if is_stale:
            for p in (sf, os.path.join(sessions_dir, f"{base}.json")):
                try:
                    os.remove(p)
                except OSError:
                    pass

    prev_cache[_ROLLOUT_PATH_CACHE_KEY] = {
        "checked_at": discovery_checked_at,
        "paths": tuple(seen_rollout_paths),
    }

    # Drop cache entries for rollouts we didn't visit this tick — either
    # the file vanished or it crossed the eviction window. Without this an
    # always-on monitor accumulates cache entries for every rollout it has
    # ever seen.
    for stale in list(prev_cache):
        if stale in _CACHE_META_KEYS:
            continue
        if stale in seen_rollout_paths:
            continue
        cached = prev_cache.get(stale, {})
        cached_mtime = cached.get("mtime", 0)
        if not os.path.exists(stale) or now - cached_mtime > _SCAN_MAX_AGE_S:
            del prev_cache[stale]
