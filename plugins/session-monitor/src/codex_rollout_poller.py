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


def _scan_activity(path, session_id=None, include_latest=False):
    """Return turn markers plus whether Codex is awaiting user input.

    Codex's in-chat questions are emitted as a ``request_user_input`` tool call,
    not as a PermissionRequest hook. The call remains pending until its matching
    ``function_call_output`` is appended to the rollout.
    """
    started = 0
    terminal = 0
    latest_marker = None
    pending_questions = set()
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
                line_type = d.get("type")
                payload = d.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if line_type == "event_msg":
                    pt = payload.get("type")
                    if pt in _START_TYPES:
                        started += 1
                        latest_marker = "started"
                    elif pt in _COMPLETE_TYPES:
                        terminal += 1
                        latest_marker = "completed"
                    elif pt in _FAIL_TYPES:
                        terminal += 1
                        latest_marker = "failed"
                elif line_type == "response_item":
                    item_type = payload.get("type")
                    call_id = payload.get("call_id")
                    if (
                        item_type == "function_call"
                        and payload.get("name") in _QUESTION_TOOL_NAMES
                        and call_id
                    ):
                        pending_questions.add(call_id)
                    elif item_type == "function_call_output" and call_id:
                        pending_questions.discard(call_id)
    except OSError:
        result = (0, 0, False, False)
        return result + (None,) if include_latest else result
    result = (started, terminal, latest_marker == "failed", bool(pending_questions))
    return result + (latest_marker,) if include_latest else result


def _infer_state(mtime, started, completed, failed, waiting_for_user=False, latest_marker=None):
    """Map turn markers + mtime to our state vocabulary."""
    if failed:
        return "interrupted"
    if waiting_for_user:
        return "question"
    if latest_marker == "started":
        return "working"
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


def infer_rollout_state_for_session(session_id):
    """Return current rollout-derived state for a Codex session, or None."""
    path = find_rollout_for_session(session_id)
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    started, completed, failed, waiting, latest_marker = _scan_activity(
        path, session_id, include_latest=True
    )
    return _infer_state(mtime, started, completed, failed, waiting, latest_marker)


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

    for path in glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
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
        cache_valid = cached is not None and cached.get("mtime") == mtime
        ignored_cache_hit = cache_valid and cached.get("ignored")
        cache_hit = cache_valid and not cached.get("ignored")
        session_id = cached.get("session_id") if cache_valid else None
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
        if not cache_hit:
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
            source = cached.get("source", "")
            originator = cached.get("originator", "")
        else:
            reason = _ignore_reason(state_dir, session_id, payload, metadata)
            if reason:
                seen_rollout_paths.add(path)
                _ignore_rollout(
                    prev_cache, path, mtime, session_id, reason,
                    sessions_dir, state_dir,
                )
                continue
            started, completed, failed, waiting, latest_marker = _scan_activity(
                path, session_id, include_latest=True
            )
            prev_cache[path] = {
                "mtime": mtime,
                "session_id": session_id,
                "cwd": cwd,
                "started": started,
                "completed": completed,
                "failed": failed,
                "waiting": waiting,
                "latest_marker": latest_marker,
                "source": source,
                "originator": originator,
            }

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

    # Drop cache entries for rollouts we didn't visit this tick — either
    # the file vanished or it crossed the eviction window. Without this an
    # always-on monitor accumulates cache entries for every rollout it has
    # ever seen.
    for stale in list(prev_cache):
        if stale in seen_rollout_paths:
            continue
        cached = prev_cache.get(stale, {})
        cached_mtime = cached.get("mtime", 0)
        if not os.path.exists(stale) or now - cached_mtime > _SCAN_MAX_AGE_S:
            del prev_cache[stale]
