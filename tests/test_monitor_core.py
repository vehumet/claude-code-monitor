import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITE_STATE = ROOT / "plugins" / "claude-code-monitor" / "src" / "write-state.py"
POLLER = ROOT / "plugins" / "claude-code-monitor" / "src" / "codex_rollout_poller.py"
MONITOR = ROOT / "plugins" / "claude-code-monitor" / "src" / "claude-code-monitor.py"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class MonitorCoreTests(unittest.TestCase):
    def tearDown(self):
        for name in ("write_state", "claude_monitor"):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_session_pid_accepts_virtual_codex_ids(self):
        mod = load_module(WRITE_STATE, "write_state_session_pid")

        self.assertEqual(
            mod._session_pid({"sessionId": "sid"}, "codex-abcdefgh"),
            "codex-abcdefgh",
        )
        self.assertEqual(mod._session_pid({"sessionId": "sid"}, "12345"), 12345)
        self.assertEqual(mod._session_pid({"pid": 777}, "codex-abcdefgh"), 777)

    def test_codex_hook_promotes_virtual_session_to_real_pid(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = home / ".claude" / "monitor" / "state"
            sessions_dir = home / ".claude" / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            sid = "abcdefgh-1111-2222-3333-abcdefghijkl"
            vid = "codex-abcdefgh"
            cwd = str(home / "project")
            (sessions_dir / f"{vid}.json").write_text(
                json.dumps({
                    "pid": vid,
                    "virtualId": vid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{vid}.json").write_text(
                json.dumps({
                    "pid": vid,
                    "state": "done",
                    "cwd": cwd,
                    "provider": "codex",
                    "slot": 3,
                    "summary": "old topic",
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_promote")
            real_pid = 4242
            py_pid = 5252
            mod.os.getpid = lambda: py_pid
            mod._build_process_tree = lambda: {
                py_pid: (real_pid, "python.exe"),
                real_pid: (1, "codex.exe"),
                1: (0, "cmd.exe"),
            }

            payload = json.dumps({
                "session_id": sid,
                "sessionId": sid,
                "cwd": cwd,
                "hook_event_name": "post_tool_use",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "--provider", "codex", "working"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            real_session = json.loads((sessions_dir / f"{real_pid}.json").read_text(encoding="utf-8"))
            real_state = json.loads((state_dir / f"{real_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(real_session["sessionId"], sid)
            self.assertEqual(real_session["provider"], "codex")
            self.assertEqual(real_state["pid"], real_pid)
            self.assertEqual(real_state["provider"], "codex")
            self.assertEqual(real_state["slot"], 3)
            self.assertEqual(real_state["summary"], "old topic")
            self.assertFalse((sessions_dir / f"{vid}.json").exists())
            self.assertFalse((state_dir / f"{vid}.json").exists())
            self.assertTrue((home / ".claude" / "monitor" / "codex-hooked" / f"{sid}.json").exists())

    def test_codex_task_complete_prefers_latest_marker(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_latest_complete")
            mod._find_codex_rollout = lambda _sid: str(rollout)

            self.assertTrue(mod._codex_task_complete("sid"))

    def test_codex_task_complete_false_when_latest_marker_started(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_latest_started")
            mod._find_codex_rollout = lambda _sid: str(rollout)

            self.assertFalse(mod._codex_task_complete("sid"))

    def test_question_state_records_file_snapshots(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = home / ".claude" / "monitor" / "state"
            sessions_dir = home / ".claude" / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            sid = "question-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            real_pid = 4242
            transcript = home / "transcript.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "status": "busy",
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_question_snapshot")
            py_pid = 5252
            mod.os.getpid = lambda: py_pid
            mod._build_process_tree = lambda: {
                py_pid: (real_pid, "python.exe"),
                real_pid: (1, "claude.exe"),
                1: (0, "cmd.exe"),
            }

            payload = json.dumps({
                "session_id": sid,
                "cwd": cwd,
                "transcript_path": str(transcript),
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "question"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            state = json.loads((state_dir / f"{real_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "question")
            self.assertEqual(state["questionTranscriptPath"], str(transcript))
            self.assertEqual(state["questionTranscriptSize"], transcript.stat().st_size)
            self.assertEqual(state["questionSessionPath"], str(sessions_dir / f"{real_pid}.json"))

    def test_question_file_change_resolves_to_working_or_done(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            session = home / "session.json"
            transcript = home / "transcript.jsonl"
            session.write_text(json.dumps({"status": "busy"}), encoding="utf-8")
            transcript.write_text("{}\n", encoding="utf-8")

            mod = load_module(MONITOR, "claude_monitor_question_resolve")
            question_at = 100.0
            state = {
                "state": "question",
                "questionAt": question_at,
                "questionSessionPath": str(session),
                "questionSessionMtimeNs": session.stat().st_mtime_ns,
                "questionSessionSize": session.stat().st_size,
                "questionTranscriptPath": str(transcript),
                "questionTranscriptMtimeNs": transcript.stat().st_mtime_ns,
                "questionTranscriptSize": transcript.stat().st_size,
            }

            self.assertIsNone(
                mod.resolve_question_state_from_files(state, now=question_at + 0.1)
            )
            transcript.write_text("{}\n{}\n", encoding="utf-8")
            self.assertEqual(
                mod.resolve_question_state_from_files(state, now=question_at + 2.0),
                "working",
            )

            session.write_text(json.dumps({"status": "idle"}), encoding="utf-8")
            self.assertEqual(
                mod.resolve_question_state_from_files(state, now=question_at + 2.0),
                "done",
            )

    def test_rollout_cache_hit_repairs_missing_files(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = home / ".claude" / "sessions"
            state_dir = home / ".claude" / "monitor" / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "abcdefgh-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            mtime = rollout.stat().st_mtime

            mod = load_module(POLLER, "codex_rollout_poller_repair")
            cache = {
                str(rollout): {
                    "mtime": mtime,
                    "session_id": sid,
                    "cwd": cwd,
                    "started": 1,
                    "completed": 0,
                    "failed": False,
                }
            }

            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            vid = "codex-abcdefgh"
            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "working")

    def test_subagent_rollout_is_ignored_and_virtual_row_cleanup_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = home / ".claude" / "sessions"
            state_dir = home / ".claude" / "monitor" / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "subagent1-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {
                        "id": sid,
                        "cwd": cwd,
                        "thread_source": "subagent",
                        "source": {"subagent": {"agent_role": "researcher"}},
                    },
                }),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_ignore")
            vid = mod.virtual_id_for(sid)
            (sessions_dir / f"{vid}.json").write_text("{}", encoding="utf-8")
            (state_dir / f"{vid}.json").write_text("{}", encoding="utf-8")
            cache = {}
            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue(rollout.exists())
            self.assertFalse((sessions_dir / f"{vid}.json").exists())
            self.assertFalse((state_dir / f"{vid}.json").exists())
            self.assertTrue(cache[str(rollout)]["ignored"])
            self.assertEqual(cache[str(rollout)]["ignore_reason"], "nested")

    def test_hook_owned_rollout_is_not_reimported_after_pid_exit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = home / ".claude" / "sessions"
            state_dir = home / ".claude" / "monitor" / "state"
            hooked_dir = home / ".claude" / "monitor" / "codex-hooked"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            hooked_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "hooked11-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "thread_source": "user"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            (hooked_dir / f"{sid}.json").write_text("{}", encoding="utf-8")
            mod = load_module(POLLER, "codex_rollout_poller_hooked")
            vid = mod.virtual_id_for(sid)
            (sessions_dir / f"{vid}.json").write_text("{}", encoding="utf-8")
            (state_dir / f"{vid}.json").write_text("{}", encoding="utf-8")

            cache = {}
            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue(rollout.exists())
            self.assertFalse((sessions_dir / f"{vid}.json").exists())
            self.assertFalse((state_dir / f"{vid}.json").exists())
            self.assertTrue(cache[str(rollout)]["ignored"])
            self.assertEqual(cache[str(rollout)]["ignore_reason"], "hook_owned")


if __name__ == "__main__":
    unittest.main()
