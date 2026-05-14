#!/usr/bin/env python3
"""Disk sync for Codex CLI rollout rows that are not owned by hooks.

Codex writes a rollout JSONL for every session under
``~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<sid>.jsonl`` regardless of hook
configuration. Hook-less top-level rollouts are mirrored into PID-less rows so
completed sessions remain visible and clickable even when hooks did not bind
them to a real process.

Files written:

Ownership: when a real hook fires for a session, write-state.py records a
``~/.claude/session-monitor/codex-hooked/<sessionId>.json`` marker. The poller then
treats that rollout as hook-owned forever and will not recreate it as a
PID-less fallback row after the real Codex process exits. Subagent and exec
rollouts are also classified as ignored. In all cases the source rollout JSONL
is left untouched; only monitor-generated virtual files are cleaned up.
"""

import glob
import json
import os
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

# Filename prefix for our PID-less virtual rows. Kept ASCII + dashes so
# every filesystem (and our int-PID parsers) handle it cleanly.
VIRTUAL_PREFIX = "codex-"
IGNORE_NESTED = "nested"
IGNORE_HOOK_OWNED = "hook_owned"
IGNORE_EXEC = "exec"


def _codex_sessions_root():
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _codex_hooked_dir(state_dir):
    return os.path.join(os.path.dirname(state_dir), "codex-hooked")


def _is_hook_owned_session(state_dir, session_id):
    if not session_id:
        return False
    return os.path.exists(os.path.join(_codex_hooked_dir(state_dir), f"{session_id}.json"))


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


def _ignore_reason(state_dir, session_id, payload):
    """Return why this rollout should not create a monitor row, or None."""
    if _is_nested_rollout_payload(payload):
        return IGNORE_NESTED
    if _is_exec_rollout_payload(payload):
        return IGNORE_EXEC
    if _is_hook_owned_session(state_dir, session_id):
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


def _scan_turn_markers(path):
    """Single-pass walk of rollout turn markers.

    Returns ``(started, terminal, latest_failed)``. Older Codex rollouts can
    contain interrupted turns followed by later successful turns, so a failure
    marker is only an interrupted monitor state when it is the latest marker.
    """
    started = 0
    terminal = 0
    latest_marker = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
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


def _infer_state(mtime, started, completed, failed):
    """Map turn markers + mtime to our state vocabulary."""
    if failed:
        return "interrupted"
    if started > 0 or completed > 0:
        return "working" if started > completed else "done"
    if time.time() - mtime < _WORKING_FRESHNESS_S:
        return "working"
    return "done"


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


def poll_codex_rollouts(known_session_ids, prev_cache, sessions_dir, state_dir):
    """Sync ~/.codex/sessions rollouts to ~/.claude sessions+state JSONs.

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

    for path in glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        if now - mtime > _STALE_EVICTION_S:
            continue

        cached = prev_cache.get(path)
        # Cache hit when the rollout's mtime hasn't moved since last tick.
        # Codex appends to a rollout in place, so unchanged mtime ⇒
        # session_meta and the turn-marker counts are also unchanged. We
        # skip both the first-line read and the full-file scan, then skip
        # the disk write below as well — Codex sessions can have rollouts
        # in the multi-megabyte range and re-parsing them every 500ms
        # turned the poller into the dominant cost on the hot path.
        cache_hit = cached is not None and cached.get("mtime") == mtime
        if cache_hit and cached.get("ignored"):
            session_id = cached.get("session_id")
            if session_id:
                # Hook ownership may be learned after a rollout was first
                # cached for another ignore reason. Keep the current row hidden
                # and refresh the reason for diagnostics.
                if _is_hook_owned_session(state_dir, session_id):
                    cached["ignore_reason"] = IGNORE_HOOK_OWNED
                _drop_virtual_monitor_row(sessions_dir, state_dir, session_id)
            seen_rollout_paths.add(path)
            continue
        if cache_hit and _is_hook_owned_session(state_dir, cached.get("session_id")):
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
        else:
            meta = _read_first_json_line(path)
            if not meta or meta.get("type") != "session_meta":
                continue
            payload = meta.get("payload") or {}
            session_id = payload.get("id")
            cwd = payload.get("cwd")
            if not session_id or not cwd:
                continue
            reason = _ignore_reason(state_dir, session_id, payload)
            if reason:
                seen_rollout_paths.add(path)
                _ignore_rollout(
                    prev_cache, path, mtime, session_id, reason,
                    sessions_dir, state_dir,
                )
                continue
            started, completed, failed = _scan_turn_markers(path)
            prev_cache[path] = {
                "mtime": mtime,
                "session_id": session_id,
                "cwd": cwd,
                "started": started,
                "completed": completed,
                "failed": failed,
            }

        seen_rollout_paths.add(path)
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

        state = _infer_state(mtime, started, completed, failed)

        sess_record = {
            "pid": vid,
            "virtualId": vid,
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": int(mtime * 1000),
            "provider": "codex",
        }

        existing = _read_existing(state_path)
        new_state = {
            "pid": vid,
            "state": state,
            "cwd": cwd,
            "updatedAt": int(mtime),
            "provider": "codex",
            "lastSignalSource": "rollout",
            "lastSignalAt": int(now),
            "rolloutPath": path,
            "rolloutMtime": int(mtime),
        }
        for k in _PRESERVED_STATE_FIELDS:
            if k in existing:
                new_state[k] = existing[k]
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
                or state_existing.get("state") != state
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
        rollout_path = d.get("rolloutPath")
        rollout_mtime = d.get("rolloutMtime", 0)
        is_stale = (
            (not rollout_path or not os.path.exists(rollout_path))
            or (now - rollout_mtime > _STALE_EVICTION_S)
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
