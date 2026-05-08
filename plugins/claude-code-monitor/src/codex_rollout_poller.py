#!/usr/bin/env python3
"""Disk sync for Codex CLI sessions that aren't reaching us via hooks.

Codex writes a rollout JSONL for every session under
``~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<sid>.jsonl`` regardless of hook
configuration. We mirror those — for sessions not already registered by a
real Codex hook — into the standard monitor stores under PID-less virtual
IDs so the rest of the pipeline (slot allocation, summarisation,
overlay rendering) treats them like first-class rows.

Files written:

  ``~/.claude/sessions/codex-<sid8>.json``
      Minimal session record (sessionId, cwd, virtualId, provider).
      ``_do_summarize`` reads ``sessionId`` from here to find the rollout.

  ``~/.claude/monitor/state/codex-<sid8>.json``
      State record (state, cwd, updatedAt, provider, summary fields…).
      The overlay renders this directly. ``rolloutPath`` and
      ``rolloutMtime`` track the source file so we can evict when the
      rollout goes stale or is deleted.

Promotion: when a real hook fires for a session we're already mirroring,
its sessionId shows up in ``known_session_ids`` and we delete our virtual
files so the row "promotes" to the hook-managed PID-having form on the
next tick.
"""

import glob
import json
import os
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
# Comparing task_started vs task_complete counts is the most reliable
# in-band state signal — mtime is the fallback before the first marker.
_START_TYPES = {"task_started"}
_COMPLETE_TYPES = {"task_complete", "agent_turn_complete", "turn_complete"}
_FAIL_TYPES = {"error", "task_failed", "turn_failed"}

# Filename prefix for our PID-less virtual rows. Kept ASCII + dashes so
# every filesystem (and our int-PID parsers) handle it cleanly.
VIRTUAL_PREFIX = "codex-"


def _codex_sessions_root():
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def virtual_id_for(session_id: str) -> str:
    """Stable virtual ID derived from the Codex session UUID."""
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


def _scan_turn_markers(path):
    """Single-pass walk of the rollout — count task_started / task_complete
    / failure markers. Returns ``(started, completed, failed)``."""
    started = 0
    completed = 0
    failed = False
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
                elif pt in _COMPLETE_TYPES:
                    completed += 1
                elif pt in _FAIL_TYPES:
                    failed = True
    except OSError:
        return 0, 0, False
    return started, completed, failed


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
    """Best-effort atomic write — tempfile + os.replace."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
        sess_path = os.path.join(sessions_dir, f"{vid}.json")
        state_path = os.path.join(state_dir, f"{vid}.json")

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

        if cache_hit:
            # Files on disk already reflect this rollout — no write needed.
            # Skipping here also stops the overlay from picking up phantom
            # mtime bumps every tick and re-reading state JSON unnecessarily.
            continue

        state = _infer_state(mtime, started, completed, failed)

        sess_record = {
            "pid": vid,
            "virtualId": vid,
            "sessionId": session_id,
            "cwd": cwd,
            "startedAt": int(mtime * 1000),
            "provider": "codex",
        }
        _atomic_write_json(sess_path, sess_record)

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
