import importlib.util
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITE_STATE = ROOT / "plugins" / "session-monitor" / "src" / "write-state.py"
POLLER = ROOT / "plugins" / "session-monitor" / "src" / "codex_rollout_poller.py"
MONITOR = ROOT / "plugins" / "session-monitor" / "src" / "session-monitor.py"
START_MONITOR = ROOT / "plugins" / "session-monitor" / "src" / "start-session-monitor.py"
INSTALLER = ROOT / "plugins" / "session-monitor" / "install.py"


def runtime_dir(home: Path) -> Path:
    return home / ".local" / "share" / "session-monitor"


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
        for name in ("write_state", "session_monitor"):
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

    def test_monitor_default_polling_is_less_aggressive(self):
        mod = load_module(MONITOR, "session_monitor_default_polling")
        defaults = mod._default_config()

        self.assertEqual(defaults["poll_interval_ms"], 1000)
        self.assertEqual(defaults["codex_question_check_interval_ms"], 2000)
        self.assertEqual(defaults["sound_files"], {})
        self.assertEqual(defaults["app_done_ttl_s"], 1800)
        self.assertEqual(defaults["latest_done_hotkey"], "")

    def test_parse_latest_done_hotkey_config(self):
        mod = load_module(MONITOR, "session_monitor_parse_hotkey")

        parsed = mod.parse_hotkey("ctrl+alt+space")

        self.assertIsNotNone(parsed)
        modifiers, vk = parsed
        if mod.IS_WINDOWS:
            self.assertTrue(modifiers & mod.MOD_CONTROL)
            self.assertTrue(modifiers & mod.MOD_ALT)
            self.assertTrue(modifiers & mod.MOD_NOREPEAT)
            self.assertEqual(vk, mod.VK_SPACE)
        else:
            self.assertEqual(vk, 0x20)
        self.assertIsNone(mod.parse_hotkey(""))
        self.assertIsNone(mod.parse_hotkey("ctrl+alt"))
        self.assertIsNone(mod.parse_hotkey("ctrl+a+b"))
        parsed_win = mod.parse_hotkey("win+space")
        self.assertIsNotNone(parsed_win)
        if mod.IS_WINDOWS:
            self.assertTrue(parsed_win[0] & mod.MOD_WIN)
            self.assertEqual(parsed_win[1], mod.VK_SPACE)

    def test_monitor_sound_files_config_resolves_existing_path(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            sound = home / "done.mp3"
            sound.write_text("", encoding="utf-8")
            cfg_dir = runtime_dir(home)
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "config.json").write_text(
                json.dumps({"sound_files": {"done": str(sound)}}),
                encoding="utf-8",
            )
            old_home = os.environ.get("HOME")
            old_userprofile = os.environ.get("USERPROFILE")
            try:
                os.environ["HOME"] = td
                os.environ["USERPROFILE"] = td
                mod = load_module(MONITOR, "session_monitor_sound_files_config")

                self.assertEqual(mod.MonitorOverlay._sound_file_for_event("done"), str(sound))
                self.assertEqual(mod.MonitorOverlay._sound_file_for_event("question"), "")
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_userprofile is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = old_userprofile

    def test_monitor_sound_file_uses_afplay_on_macos(self):
        mod = load_module(MONITOR, "session_monitor_sound_file_macos")
        calls = []

        class Result:
            returncode = 0

        old_is_windows = mod.IS_WINDOWS
        old_platform = mod.sys.platform
        old_which = mod.shutil.which
        old_run = mod.subprocess.run
        try:
            mod.IS_WINDOWS = False
            mod.sys.platform = "darwin"
            mod.shutil.which = lambda exe: "/usr/bin/afplay" if exe == "afplay" else None

            def fake_run(cmd, **_kwargs):
                calls.append(cmd)
                return Result()

            mod.subprocess.run = fake_run

            self.assertTrue(mod.MonitorOverlay._play_sound_file("/tmp/done.mp3"))
        finally:
            mod.IS_WINDOWS = old_is_windows
            mod.sys.platform = old_platform
            mod.shutil.which = old_which
            mod.subprocess.run = old_run

        self.assertEqual(calls, [["/usr/bin/afplay", "/tmp/done.mp3"]])

    def test_monitor_sound_file_uses_available_linux_player(self):
        mod = load_module(MONITOR, "session_monitor_sound_file_linux")
        calls = []

        class Result:
            returncode = 0

        old_is_windows = mod.IS_WINDOWS
        old_platform = mod.sys.platform
        old_which = mod.shutil.which
        old_run = mod.subprocess.run
        try:
            mod.IS_WINDOWS = False
            mod.sys.platform = "linux"
            mod.shutil.which = lambda exe: f"/usr/bin/{exe}" if exe == "mpg123" else None

            def fake_run(cmd, **_kwargs):
                calls.append(cmd)
                return Result()

            mod.subprocess.run = fake_run

            self.assertTrue(mod.MonitorOverlay._play_sound_file("/tmp/done.mp3"))
        finally:
            mod.IS_WINDOWS = old_is_windows
            mod.sys.platform = old_platform
            mod.shutil.which = old_which
            mod.subprocess.run = old_run

        self.assertEqual(calls, [["/usr/bin/mpg123", "-q", "/tmp/done.mp3"]])

    def test_claude_desktop_rows_use_distinct_marker(self):
        mod = load_module(MONITOR, "session_monitor_claude_desktop_marker")

        self.assertEqual(mod.row_marker("claude"), "●")
        self.assertEqual(mod.row_marker("claude", "claude-desktop"), "◉")
        self.assertEqual(mod.row_marker("codex", "claude-desktop"), "◆")

    def test_monitor_background_opacity_defaults_darker(self):
        mod = load_module(MONITOR, "session_monitor_default_opacity")
        defaults = mod._default_config()

        self.assertEqual(defaults["background_opacity"], 0.85)
        self.assertEqual(defaults["opacity"], 0.85)
        self.assertEqual(mod._coerce_opacity("0.7"), 0.7)
        self.assertEqual(mod._coerce_opacity("bad"), 0.85)
        self.assertEqual(mod._coerce_opacity(3), 1.0)
        self.assertEqual(mod._coerce_opacity(0), 0.1)

    def test_monitor_legacy_opacity_config_still_applies(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            old_home = os.environ.get("HOME")
            old_userprofile = os.environ.get("USERPROFILE")
            try:
                os.environ["HOME"] = td
                os.environ["USERPROFILE"] = td
                cfg_dir = runtime_dir(Path(td))
                cfg_dir.mkdir(parents=True)
                (cfg_dir / "config.json").write_text('{"opacity": 0.42}', encoding="utf-8")

                mod = load_module(MONITOR, "session_monitor_legacy_opacity")

                self.assertEqual(mod.CONFIG["background_opacity"], 0.42)
                self.assertEqual(mod.get_background_opacity(), 0.42)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_userprofile is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = old_userprofile

    def test_codex_hook_promotes_virtual_session_to_real_pid(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
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
            self.assertTrue((runtime_dir(home) / "codex-hooked" / f"{sid}.json").exists())

    def test_codex_hook_block_refresh_preserves_trust_state(self):
        mod = load_module(INSTALLER, "installer_preserve_codex_trust")
        rendered = mod._render_codex_hook_block()
        misplaced_state = "\n".join([
            "[hooks.state]",
            "",
            "[hooks.state.'C:\\Users\\me\\.codex\\config.toml:permission_request:0:0']",
            'trusted_hash = "sha256:abc123"',
            "",
            '[plugins."browser-use@openai-bundled"]',
            "enabled = true",
        ])
        old_text = rendered.replace(
            mod.CODEX_HOOK_MARKER_CLOSE,
            f"{misplaced_state}\n\n{mod.CODEX_HOOK_MARKER_CLOSE}",
        )

        updated, modified = mod._replace_codex_hook_block(
            old_text,
            mod._render_codex_hook_block(),
        )

        self.assertTrue(modified)
        self.assertIn('trusted_hash = "sha256:abc123"', updated)
        close_idx = updated.index(mod.CODEX_HOOK_MARKER_CLOSE)
        trust_idx = updated.index("[hooks.state]")
        self.assertLess(close_idx, trust_idx)
        managed = updated[
            updated.index(mod.CODEX_HOOK_MARKER_OPEN):close_idx
        ]
        self.assertNotIn("[hooks.state]", managed)

    def test_codex_hook_block_refresh_absorbs_legacy_misplaced_marker(self):
        mod = load_module(INSTALLER, "installer_legacy_marker_span")
        rendered = mod._render_codex_hook_block()
        legacy = rendered.replace(
            mod.CODEX_HOOK_MARKER_OPEN + "\n",
            "[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\n",
            1,
        )
        legacy = legacy.replace(
            "# Auto-generated by session-monitor install.py.",
            mod.CODEX_HOOK_MARKER_OPEN + "\n# Auto-generated by session-monitor install.py.",
            1,
        )

        updated, modified = mod._replace_codex_hook_block(
            legacy,
            mod._render_codex_hook_block(),
        )

        self.assertTrue(modified)
        self.assertEqual(updated.count("[[hooks.SessionStart]]"), 1)
        self.assertEqual(updated.count("[[hooks.SessionStart.hooks]]"), 1)
        self.assertLess(
            updated.index(mod.CODEX_HOOK_MARKER_OPEN),
            updated.index("[[hooks.SessionStart]]"),
        )

    def test_codex_hook_block_refresh_handles_orphan_close_marker(self):
        mod = load_module(INSTALLER, "installer_orphan_close_marker")
        orphan = mod._render_codex_hook_block().replace(
            mod.CODEX_HOOK_MARKER_OPEN + "\n"
            "# Auto-generated by session-monitor install.py.\n"
            "# Run `python uninstall.py` (or delete this fenced block) to remove.\n\n",
            "",
            1,
        )

        updated, modified = mod._replace_codex_hook_block(
            orphan,
            mod._render_codex_hook_block(),
        )

        self.assertTrue(modified)
        self.assertEqual(updated.count("[[hooks.SessionStart]]"), 1)
        self.assertEqual(updated.count(mod.CODEX_HOOK_MARKER_OPEN), 1)
        self.assertEqual(updated.count(mod.CODEX_HOOK_MARKER_CLOSE), 1)

    def test_merge_codex_hooks_updates_orphan_close_marker_instead_of_appending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            old_home = os.environ.get("HOME")
            old_userprofile = os.environ.get("USERPROFILE")
            try:
                os.environ["HOME"] = td
                os.environ["USERPROFILE"] = td
                codex_dir = home / ".codex"
                codex_dir.mkdir()
                mod = load_module(INSTALLER, "installer_merge_orphan_close")
                orphan = mod._render_codex_hook_block().replace(
                    mod.CODEX_HOOK_MARKER_OPEN + "\n"
                    "# Auto-generated by session-monitor install.py.\n"
                    "# Run `python uninstall.py` (or delete this fenced block) to remove.\n\n",
                    "",
                    1,
                )
                (codex_dir / "config.toml").write_text(
                    "[features]\nhooks = true\n\n" + orphan + "\n",
                    encoding="utf-8",
                )
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertTrue(mod.merge_codex_hooks(dry_run=True))
                self.assertIn("Updating Codex hooks", out.getvalue())
                self.assertNotIn("Adding Codex hooks", out.getvalue())
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_userprofile is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = old_userprofile

    def test_codex_desktop_app_server_hook_is_ignored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            sid = "appserv1-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            mod = load_module(WRITE_STATE, "write_state_codex_app_server")
            mod.IS_WINDOWS = True
            py_pid = 5252
            app_pid = 4242
            gui_pid = 3131
            mod.os.getpid = lambda: py_pid
            mod._build_process_tree = lambda: {
                py_pid: (app_pid, "python.exe"),
                app_pid: (gui_pid, "codex.exe"),
                gui_pid: (1, "codex.exe"),
                1: (0, "explorer.exe"),
            }

            payload = json.dumps({
                "session_id": sid,
                "sessionId": sid,
                "cwd": cwd,
                "hook_event_name": "UserPromptSubmit",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "--provider", "codex", "question"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            self.assertEqual(list(sessions_dir.glob("*.json")), [])
            self.assertEqual(list(state_dir.glob("*.json")), [])
            self.assertFalse(
                (runtime_dir(home) / "codex-hooked" / f"{sid}.json").exists()
            )

    def test_codex_subagent_hook_does_not_update_parent_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "15"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            parent_sid = "parent11-1111-2222-3333-abcdefghijkl"
            sub_sid = "subagt11-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            parent_pid = 4242
            (sessions_dir / f"{parent_pid}.json").write_text(
                json.dumps({
                    "pid": parent_pid,
                    "sessionId": parent_sid,
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{parent_pid}.json").write_text(
                json.dumps({
                    "pid": parent_pid,
                    "state": "working",
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (rollout_dir / f"rollout-2026-05-15T00-00-00-{sub_sid}.jsonl").write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {
                        "id": sub_sid,
                        "cwd": cwd,
                        "thread_source": "subagent",
                        "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent_sid}}},
                    },
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_subagent_hook_ignored")
            py_pid = 5252
            mod.os.getpid = lambda: py_pid
            mod._build_process_tree = lambda: {
                py_pid: (parent_pid, "python.exe"),
                parent_pid: (1, "codex.exe"),
                1: (0, "cmd.exe"),
            }

            payload = json.dumps({
                "session_id": sub_sid,
                "sessionId": sub_sid,
                "cwd": cwd,
                "hook_event_name": "user_prompt_submit",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "--provider", "codex", "done"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            parent_session = json.loads((sessions_dir / f"{parent_pid}.json").read_text(encoding="utf-8"))
            parent_state = json.loads((state_dir / f"{parent_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(parent_session["sessionId"], parent_sid)
            self.assertEqual(parent_state["state"], "working")
            self.assertFalse(
                (runtime_dir(home) / "codex-hooked" / f"{sub_sid}.json").exists()
            )

    def test_codex_existing_pid_session_id_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            parent_sid = "parent11-1111-2222-3333-abcdefghijkl"
            spawned_sid = "spawned1-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            parent_pid = 4242
            (sessions_dir / f"{parent_pid}.json").write_text(
                json.dumps({
                    "pid": parent_pid,
                    "sessionId": parent_sid,
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{parent_pid}.json").write_text(
                json.dumps({
                    "pid": parent_pid,
                    "state": "working",
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_codex_no_overwrite")
            py_pid = 5252
            mod.os.getpid = lambda: py_pid
            mod._build_process_tree = lambda: {
                py_pid: (parent_pid, "python.exe"),
                parent_pid: (1, "codex.exe"),
                1: (0, "cmd.exe"),
            }
            mod._codex_session_is_nested = lambda _sid: False

            payload = json.dumps({
                "session_id": spawned_sid,
                "sessionId": spawned_sid,
                "cwd": cwd,
                "hook_event_name": "session_start",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "--provider", "codex", "session_start"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            parent_session = json.loads((sessions_dir / f"{parent_pid}.json").read_text(encoding="utf-8"))
            parent_state = json.loads((state_dir / f"{parent_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(parent_session["sessionId"], parent_sid)
            self.assertEqual(parent_state["state"], "working")

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

    def test_codex_task_complete_false_when_complete_then_started_tie(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_complete_started_tie")
            mod._find_codex_rollout = lambda _sid: str(rollout)

            self.assertFalse(mod._codex_task_complete("sid"))

    def test_codex_task_complete_treats_turn_aborted_as_terminal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_aborted_terminal")
            mod._find_codex_rollout = lambda _sid: str(rollout)

            self.assertTrue(mod._codex_task_complete("sid"))

    def test_codex_rollout_digest_collects_recent_work_signals(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "old request"},
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "localization phase 3 review"},
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "checking localization files and CI guards",
                        },
                    }),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "arguments": {"command": "rg Loc Assets/_Project/Scripts"},
                        },
                    }),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "Assets/_Project/Scripts/Localization/Loc.cs:12",
                        },
                    }),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "final_answer",
                            "message": "found missing localization guard coverage",
                        },
                    }),
                ]),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_digest")

            scan = mod._scan_codex_rollout(str(rollout))

            self.assertIn("localization phase 3 review", scan["digest"])
            self.assertIn("checking localization files", scan["digest"])
            self.assertIn("shell_command", scan["digest"])
            self.assertIn("Assets/_Project/Scripts/Localization/Loc.cs", scan["digest"])
            self.assertIn("missing localization guard", scan["digest"])

    def test_codex_rollout_digest_ignores_parent_fork_segment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            parent_sid = "parent11-1111-2222-3333-abcdefghijkl"
            child_sid = "child111-1111-2222-3333-abcdefghijkl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": child_sid}}),
                    json.dumps({"type": "session_meta", "payload": {"id": parent_sid}}),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "parent-only request"},
                    }),
                    json.dumps({"type": "session_meta", "payload": {"id": child_sid}}),
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "child fork request"},
                    }),
                ]),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_digest_fork")

            scan = mod._scan_codex_rollout(str(rollout), child_sid)

            self.assertIn("child fork request", scan["digest"])
            self.assertNotIn("parent-only request", scan["digest"])

    def test_codex_rollout_digest_is_bounded(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            long_text = "x" * 2000
            rollout.write_text(
                "\n".join(
                    json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": f"request {i} {long_text}"},
                    })
                    for i in range(20)
                ),
                encoding="utf-8",
            )
            mod = load_module(WRITE_STATE, "write_state_codex_digest_bound")

            scan = mod._scan_codex_rollout(str(rollout))

            self.assertLessEqual(len(scan["digest"]), mod._CODEX_DIGEST_MAX_CHARS)
            self.assertLessEqual(
                len(scan["user_messages"]),
                mod._CODEX_DIGEST_SECTION_LIMITS["user_messages"],
            )

    def test_resolve_codex_command_falls_back_to_npm_shim_on_windows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            appdata = Path(td) / "AppData" / "Roaming"
            npm = appdata / "npm"
            npm.mkdir(parents=True)
            shim = npm / "codex.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")

            mod = load_module(WRITE_STATE, "write_state_codex_command")
            mod.IS_WINDOWS = True
            mod.shutil.which = lambda _name: None
            old_appdata = os.environ.get("APPDATA")
            os.environ["APPDATA"] = str(appdata)
            try:
                self.assertEqual(mod._resolve_codex_command(), str(shim))
            finally:
                if old_appdata is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = old_appdata

    def test_codex_summary_prompt_non_korean_preserves_user_language_instruction(self):
        mod = load_module(WRITE_STATE, "write_state_codex_prompt_language")

        self.assertIn("dominant user request", mod._CODEX_SUMMARY_PROMPT_EN)
        self.assertIn("same language", mod._CODEX_SUMMARY_PROMPT_EN)
        self.assertIn("natural spacing", mod._CODEX_SUMMARY_PROMPT_EN)
        self.assertIn("띄어쓰기", mod._CODEX_SUMMARY_PROMPT_KO)

    def test_start_monitor_default_path_is_next_to_launcher(self):
        mod = load_module(START_MONITOR, "start_monitor_default_path")

        self.assertEqual(
            Path(mod._default_monitor_path()),
            START_MONITOR.with_name("session-monitor.py"),
        )

    def test_start_monitor_resolves_sibling_pythonw_on_windows(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            bin_dir = Path(td)
            python = bin_dir / "python.exe"
            pythonw = bin_dir / "pythonw.exe"
            python.write_text("", encoding="utf-8")
            pythonw.write_text("", encoding="utf-8")

            mod = load_module(START_MONITOR, "start_monitor_pythonw")
            old_executable = mod.sys.executable
            try:
                mod.IS_WINDOWS = True
                mod.sys.executable = str(python)
                mod.shutil.which = lambda _name: None

                self.assertEqual(mod._resolve_pythonw(), str(pythonw))
            finally:
                mod.sys.executable = old_executable

    def test_start_monitor_does_not_spawn_duplicate_monitor(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            monitor = Path(td) / "session-monitor.py"
            monitor.write_text("", encoding="utf-8")

            mod = load_module(START_MONITOR, "start_monitor_no_duplicate")
            mod._monitor_running = lambda: True
            mod.subprocess.Popen = lambda *_args, **_kwargs: self.fail("Popen called")

            self.assertIsNone(mod.launch(str(monitor)))

    def test_state_dir_uses_session_monitor_env_name(self):
        mod = load_module(MONITOR, "session_monitor_state_dir_env")
        old_new = os.environ.get("SESSION_MONITOR_STATE_DIR")
        try:
            os.environ["SESSION_MONITOR_STATE_DIR"] = "new-state"

            self.assertEqual(mod.get_state_dir(), "new-state")
        finally:
            if old_new is None:
                os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
            else:
                os.environ["SESSION_MONITOR_STATE_DIR"] = old_new

    def test_command_file_ownership_detection_is_narrow(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            user_command = Path(td) / "session-monitor.md"
            user_command.write_text(
                "# Session Monitor\n\nMy own command.\n",
                encoding="utf-8",
            )
            managed_command = Path(td) / "managed.md"
            managed_command.write_text(
                "start-session-monitor.py\n"
                "Run this single command immediately\n"
                "Do NOT check if it's running beforehand\n"
                "Session monitor launched.\n",
                encoding="utf-8",
            )

            mod = load_module(INSTALLER, "installer_command_ownership")

            self.assertFalse(mod._is_managed_command_file(str(user_command)))
            self.assertTrue(mod._is_managed_command_file(str(managed_command)))

    def test_installer_migrates_legacy_runtime_to_local_share(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            old_home = os.environ.get("HOME")
            old_userprofile = os.environ.get("USERPROFILE")
            try:
                os.environ["HOME"] = td
                os.environ["USERPROFILE"] = td
                legacy = home / ".claude" / "session-monitor"
                legacy_state = legacy / "state"
                legacy_state.mkdir(parents=True)
                (legacy / "config.json").write_text('{"language":"ko"}', encoding="utf-8")
                (legacy_state / "123.json").write_text('{"pid":123}', encoding="utf-8")

                mod = load_module(INSTALLER, "installer_migrate_legacy_runtime")
                with contextlib.redirect_stdout(io.StringIO()):
                    mod.migrate_legacy_runtime(dry_run=False)

                target = runtime_dir(home)
                self.assertEqual(
                    (target / "config.json").read_text(encoding="utf-8"),
                    '{"language":"ko"}',
                )
                self.assertEqual(
                    (target / "state" / "123.json").read_text(encoding="utf-8"),
                    '{"pid":123}',
                )
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                if old_userprofile is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = old_userprofile

    def test_installer_removes_deprecated_question_post_tool_hooks(self):
        mod = load_module(INSTALLER, "installer_deprecated_question_hooks")
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "ExitPlanMode",
                        "hooks": [{
                            "type": "command",
                            "command": 'python "$HOME/.local/share/session-monitor/write-state.py" --provider claude "working"',
                            "timeout": 5,
                        }],
                    },
                    {
                        "matcher": "AskUserQuestion",
                        "hooks": [{
                            "type": "command",
                            "command": 'python "$HOME/.local/share/session-monitor/write-state.py" --provider claude "working"',
                            "timeout": 5,
                        }],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [{
                            "type": "command",
                            "command": "python user-script.py",
                            "timeout": 5,
                        }],
                    },
                ]
            }
        }

        self.assertTrue(mod.merge_hooks(settings))
        post_hooks = settings["hooks"]["PostToolUse"]
        self.assertEqual(len(post_hooks), 1)
        self.assertEqual(post_hooks[0]["matcher"], "Bash")

    def test_installer_adds_stop_failure_hook_for_api_errors(self):
        mod = load_module(INSTALLER, "installer_stop_failure_hook")
        settings = {"hooks": {}}

        self.assertTrue(mod.merge_hooks(settings))

        stop_failure_hooks = settings["hooks"]["StopFailure"]
        self.assertEqual(len(stop_failure_hooks), 1)
        hook_entry = stop_failure_hooks[0]
        self.assertEqual(hook_entry["matcher"], "")
        self.assertEqual(
            hook_entry["hooks"][0]["command"],
            'python "$HOME/.local/share/session-monitor/write-state.py" --provider claude "interrupted"',
        )

    def test_installer_adds_failure_and_notification_hooks(self):
        mod = load_module(INSTALLER, "installer_failure_notification_hooks")
        settings = {"hooks": {}}

        self.assertTrue(mod.merge_hooks(settings))

        tool_failure_hooks = settings["hooks"]["PostToolUseFailure"]
        self.assertEqual(len(tool_failure_hooks), 1)
        self.assertEqual(tool_failure_hooks[0]["matcher"], "")
        self.assertEqual(
            tool_failure_hooks[0]["hooks"][0]["command"],
            'python "$HOME/.local/share/session-monitor/write-state.py" --provider claude "tool_failure"',
        )

        notification_hooks = {
            entry["matcher"]: entry["hooks"][0]["command"]
            for entry in settings["hooks"]["Notification"]
        }
        self.assertEqual(
            notification_hooks["idle_prompt"],
            'python "$HOME/.local/share/session-monitor/write-state.py" --provider claude "idle_prompt"',
        )
        self.assertEqual(
            notification_hooks["permission_prompt"],
            'python "$HOME/.local/share/session-monitor/write-state.py" --provider claude "question"',
        )

    def test_question_state_records_file_snapshots(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
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

    def test_waiting_session_forces_question_on_stale_working_hook(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            sid = "exitplan-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            real_pid = 4242
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "status": "waiting",
                    "waitingFor": "approve ExitPlanMode",
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_waiting_session")
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
                "tool_name": "ExitPlanMode",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "--provider", "claude", "working"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            state = json.loads((state_dir / f"{real_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "question")
            self.assertEqual(state["questionSessionPath"], str(sessions_dir / f"{real_pid}.json"))

    def test_claude_hook_event_name_does_not_force_codex_provider(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            sid = "claude-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            real_pid = 4242
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": sid,
                    "cwd": cwd,
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_claude_provider")
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
                "hook_event_name": "UserPromptSubmit",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "working"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            state = json.loads((state_dir / f"{real_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["provider"], "claude")

    def test_claude_stop_failure_marks_interrupt_source(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            sid = "claude-stopfail-1111-2222-abcdefghijkl"
            cwd = str(home / "project")
            real_pid = 4242
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": sid,
                    "cwd": cwd,
                }),
                encoding="utf-8",
            )

            mod = load_module(WRITE_STATE, "write_state_stop_failure_source")
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
                "hook_event_name": "StopFailure",
            }).encode("utf-8")
            old_argv = mod.sys.argv
            old_stdin = mod.sys.stdin
            try:
                mod.sys.argv = ["write-state.py", "--provider", "claude", "interrupted"]
                mod.sys.stdin = _FakeStdin(payload)
                mod.main()
            finally:
                mod.sys.argv = old_argv
                mod.sys.stdin = old_stdin

            state = json.loads((state_dir / f"{real_pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "interrupted")
            self.assertEqual(state["interruptSource"], "stop_failure")
            self.assertEqual(state["interruptHookEvent"], "StopFailure")
            self.assertIsInstance(state["interruptAt"], int)

    def test_monitor_overlays_waiting_session_as_question(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            real_pid = 4242
            cwd = str(home / "project")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": "exitplan-session",
                    "cwd": cwd,
                    "status": "waiting",
                    "waitingFor": "approve ExitPlanMode",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "state": "working",
                    "cwd": cwd,
                    "provider": "claude",
                    "updatedAt": 123,
                    "slot": 4,
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_waiting_session")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                changed, events = tracker.poll()

                self.assertTrue(changed)
                self.assertEqual(events, ["question"])
                self.assertEqual(tracker.instances[real_pid].state, "question")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_monitor_overlays_pending_codex_user_input_as_question(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            real_pid = 4242
            sid = "codex-question-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "state": "working",
                    "cwd": cwd,
                    "provider": "codex",
                    "updatedAt": 123,
                    "slot": 4,
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_codex_question")
                mod.poll_codex_rollouts = None
                mod.infer_rollout_state_for_session = lambda _sid: "question"
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                changed, events = tracker.poll()

                self.assertTrue(changed)
                self.assertEqual(events, ["question"])
                self.assertEqual(tracker.instances[real_pid].state, "question")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_codex_question_rollout_check_is_throttled(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            state_dir.mkdir(parents=True)

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_codex_question_throttle")
                calls = []

                def fake_infer(_sid):
                    calls.append(_sid)
                    return "question" if len(calls) == 1 else "working"

                mod.infer_rollout_state_for_session = fake_infer
                tracker = mod.InstanceTracker()
                tracker._codex_question_check_s = 60

                self.assertTrue(tracker._codex_waits_for_user_cached("sid"))
                self.assertTrue(tracker._codex_waits_for_user_cached("sid"))
                self.assertEqual(calls, ["sid"])

                tracker._codex_question_cache["sid"]["checked_at"] = 0
                self.assertFalse(tracker._codex_waits_for_user_cached("sid"))
                self.assertEqual(calls, ["sid", "sid"])
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_question_file_change_resolves_to_working_or_done(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            session = home / "session.json"
            transcript = home / "transcript.jsonl"
            session.write_text(json.dumps({"status": "busy"}), encoding="utf-8")
            transcript.write_text("{}\n", encoding="utf-8")

            mod = load_module(MONITOR, "session_monitor_question_resolve")
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

    def test_pending_ask_user_question_keeps_question_after_file_change(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            session = home / "session.json"
            transcript = home / "transcript.jsonl"
            session.write_text(json.dumps({"status": "busy"}), encoding="utf-8")
            transcript.write_text("{}\n", encoding="utf-8")

            mod = load_module(MONITOR, "session_monitor_pending_ask_user_question")
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

            transcript.write_text(
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [{
                            "type": "tool_use",
                            "id": "toolu_question",
                            "name": "AskUserQuestion",
                            "input": {"questions": []},
                        }]
                    },
                }) + "\n",
                encoding="utf-8",
            )
            self.assertIsNone(
                mod.resolve_question_state_from_files(state, now=question_at + 2.0)
            )

    def test_answered_ask_user_question_resolves_to_working(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            session = home / "session.json"
            transcript = home / "transcript.jsonl"
            session.write_text(json.dumps({"status": "busy"}), encoding="utf-8")
            transcript.write_text("{}\n", encoding="utf-8")

            mod = load_module(MONITOR, "session_monitor_answered_ask_user_question")
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

            transcript.write_text(
                "\n".join([
                    json.dumps({
                        "type": "assistant",
                        "message": {
                            "content": [{
                                "type": "tool_use",
                                "id": "toolu_question",
                                "name": "AskUserQuestion",
                                "input": {"questions": []},
                            }]
                        },
                    }),
                    json.dumps({
                        "type": "user",
                        "message": {
                            "content": [{
                                "type": "tool_result",
                                "tool_use_id": "toolu_question",
                                "content": "15~25 minutes",
                            }]
                        },
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                mod.resolve_question_state_from_files(state, now=question_at + 2.0),
                "working",
            )

    def test_tracker_overlays_pending_ask_user_question_from_transcript(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            transcript_dir = home / ".claude" / "projects" / "project"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            transcript_dir.mkdir(parents=True)

            real_pid = 4242
            sid = "desktop-question-session"
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": sid,
                    "cwd": str(home / "project"),
                    "provider": "claude",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "state": "working",
                    "cwd": str(home / "project"),
                    "updatedAt": 100,
                    "provider": "claude",
                }),
                encoding="utf-8",
            )
            (transcript_dir / f"{sid}.jsonl").write_text(
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [{
                            "type": "tool_use",
                            "id": "toolu_question",
                            "name": "AskUserQuestion",
                            "input": {"questions": []},
                        }]
                    },
                }) + "\n",
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_overlay_ask_user_question")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True
                mod.IS_WINDOWS = False

                tracker = mod.InstanceTracker()
                changed, events = tracker.poll()

                self.assertTrue(changed)
                self.assertIn("question", events)
                self.assertEqual(tracker.instances[real_pid].state, "question")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_recent_state_change_key_uses_state_change_time(self):
        mod = load_module(MONITOR, "session_monitor_recent_state_change")
        older = mod.Instance(1, "C:\\a", updated_at=100, session_id="older")
        newer = mod.Instance(2, "C:\\b", updated_at=200, session_id="newer")
        older.state_changed_at = 300
        newer.state_changed_at = 200

        class Tracker:
            @staticmethod
            def pin_key(inst):
                return inst.session_id or str(inst.pid)

        class Overlay:
            tracker = Tracker()
            _recent_highlight_cleared_at = 0

        self.assertEqual(
            mod.MonitorOverlay._recent_state_change_key(Overlay(), [older, newer]),
            "older",
        )

    def test_latest_done_instance_uses_most_recent_done_state_change(self):
        mod = load_module(MONITOR, "session_monitor_latest_done_instance")
        older = mod.Instance(1, "C:\\a", state="done", updated_at=100, session_id="older")
        newer = mod.Instance(2, "C:\\b", state="done", updated_at=200, session_id="newer")
        working = mod.Instance(3, "C:\\c", state="working", updated_at=300, session_id="working")
        older.state_changed_at = 400
        newer.state_changed_at = 500
        working.state_changed_at = 600

        self.assertEqual(
            mod.MonitorOverlay._latest_done_instance([older, newer, working]),
            newer,
        )
        self.assertIsNone(mod.MonitorOverlay._latest_done_instance([working]))

    def test_latest_done_hotkey_activates_most_recent_done_session(self):
        mod = load_module(MONITOR, "session_monitor_latest_done_activate")
        older = mod.Instance(1, "C:\\a", state="done", updated_at=100, session_id="older")
        newer = mod.Instance(2, "C:\\b", state="done", updated_at=200, session_id="newer")
        older.state_changed_at = 400
        newer.state_changed_at = 500

        calls = []

        class Tracker:
            instances = {older.pid: older, newer.pid: newer}

            @staticmethod
            def pin_key(inst):
                return inst.session_id or str(inst.pid)

        class Overlay:
            tracker = Tracker()
            _latest_done_instance = staticmethod(mod.MonitorOverlay._latest_done_instance)

            def _clear_recent_highlight(self, pid):
                calls.append(("clear", pid))

            def _activate_terminal(self, pid):
                calls.append(("activate", pid))

        mod.MonitorOverlay._activate_latest_done_session(Overlay())

        self.assertEqual(calls, [("clear", 2), ("activate", 2)])

    def test_summarize_claude_status_uses_unresolved_incident(self):
        mod = load_module(MONITOR, "session_monitor_status_summary")

        operational, label = mod.summarize_claude_status({
            "status": {"indicator": "major", "description": "Partial System Outage"},
            "incidents": [
                {
                    "name": "Elevated errors for Claude API",
                    "status": "investigating",
                    "updated_at": "2026-05-17T01:00:00Z",
                }
            ],
        })

        self.assertFalse(operational)
        self.assertIn("investigating", label)
        self.assertIn("Elevated errors", label)

    def test_summarize_claude_status_reports_operational(self):
        mod = load_module(MONITOR, "session_monitor_status_operational")

        operational, label = mod.summarize_claude_status({
            "status": {"indicator": "none", "description": "All Systems Operational"},
            "incidents": [],
        })

        self.assertTrue(operational)
        self.assertEqual(label, "Claude status: operational")

    def test_claude_status_watcher_announces_restored_after_seen_issue(self):
        mod = load_module(MONITOR, "session_monitor_status_restored")
        watcher = mod.ClaudeStatusWatcher()
        watcher._active = True

        watcher._record_result(False, "Claude: investigating - API errors", 'W/"1"')
        label, color, event = watcher.snapshot()
        self.assertEqual(label, "Claude: investigating - API errors")
        self.assertEqual(color, mod.THEME["interrupted"])
        self.assertFalse(event)

        watcher._record_result(True, "Claude status: operational", 'W/"2"')
        label, color, event = watcher.snapshot()
        self.assertEqual(label, "Claude status: restored")
        self.assertEqual(color, mod.THEME["done"])
        self.assertTrue(event)

    def test_recent_state_change_key_ignores_dismissed_changes(self):
        mod = load_module(MONITOR, "session_monitor_recent_state_change_dismissed")
        older = mod.Instance(1, "C:\\a", updated_at=100, session_id="older")
        newer = mod.Instance(2, "C:\\b", updated_at=200, session_id="newer")
        future = mod.Instance(3, "C:\\c", updated_at=400, session_id="future")
        older.state_changed_at = 300
        newer.state_changed_at = 200
        future.state_changed_at = 400

        class Tracker:
            @staticmethod
            def pin_key(inst):
                return inst.session_id or str(inst.pid)

        class Overlay:
            tracker = Tracker()
            _recent_highlight_cleared_at = 300

        self.assertEqual(
            mod.MonitorOverlay._recent_state_change_key(Overlay(), [older, newer]),
            None,
        )
        self.assertEqual(
            mod.MonitorOverlay._recent_state_change_key(Overlay(), [older, newer, future]),
            "future",
        )

    def test_working_state_change_does_not_update_recent_highlight_time(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            real_pid = 4242
            cwd = str(home / "project")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({"pid": real_pid, "sessionId": "sid", "cwd": cwd}),
                encoding="utf-8",
            )
            (state_dir / f"{real_pid}.json").write_text(
                json.dumps({"pid": real_pid, "state": "done", "cwd": cwd, "updatedAt": 100}),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_working_highlight")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                tracker.poll()
                self.assertEqual(tracker.instances[real_pid].state_changed_at, 100)

                (state_dir / f"{real_pid}.json").write_text(
                    json.dumps({"pid": real_pid, "state": "working", "cwd": cwd, "updatedAt": 200}),
                    encoding="utf-8",
                )
                tracker.poll()

                self.assertEqual(tracker.instances[real_pid].state, "working")
                self.assertEqual(tracker.instances[real_pid].state_changed_at, 100)
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_done_timestamp_refresh_updates_recent_highlight_time(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            real_pid = 4242
            cwd = str(home / "project")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({"pid": real_pid, "sessionId": "sid", "cwd": cwd}),
                encoding="utf-8",
            )
            state_path = state_dir / f"{real_pid}.json"
            state_path.write_text(
                json.dumps({"pid": real_pid, "state": "done", "cwd": cwd, "updatedAt": 100}),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_done_refresh_highlight")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                changed, events = tracker.poll()
                self.assertTrue(changed)
                self.assertIn("done", events)
                self.assertEqual(tracker.instances[real_pid].state_changed_at, 100)

                state_path.write_text(
                    json.dumps({"pid": real_pid, "state": "done", "cwd": cwd, "updatedAt": 200}),
                    encoding="utf-8",
                )
                changed, events = tracker.poll()

                self.assertTrue(changed)
                self.assertIn("done", events)
                self.assertEqual(tracker.instances[real_pid].state_changed_at, 200)
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_working_timestamp_refresh_does_not_request_redraw(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            real_pid = 4242
            cwd = str(home / "project")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({"pid": real_pid, "sessionId": "sid", "cwd": cwd}),
                encoding="utf-8",
            )
            state_path = state_dir / f"{real_pid}.json"
            state_path.write_text(
                json.dumps({"pid": real_pid, "state": "working", "cwd": cwd, "updatedAt": 100}),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_working_refresh_no_redraw")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                changed, _events = tracker.poll()
                self.assertTrue(changed)

                state_path.write_text(
                    json.dumps({"pid": real_pid, "state": "working", "cwd": cwd, "updatedAt": 200}),
                    encoding="utf-8",
                )
                changed, events = tracker.poll()

                self.assertFalse(changed)
                self.assertEqual(events, [])
                self.assertEqual(tracker.instances[real_pid].updated_at, 200)
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_tracker_loads_stop_failure_interrupt_metadata(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            real_pid = 4242
            cwd = str(home / "project")
            (sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({"pid": real_pid, "sessionId": "sid", "cwd": cwd}),
                encoding="utf-8",
            )
            (state_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "state": "interrupted",
                    "cwd": cwd,
                    "updatedAt": 300,
                    "provider": "claude",
                    "interruptSource": "stop_failure",
                    "interruptAt": 300,
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                mod = load_module(MONITOR, "session_monitor_interrupt_metadata")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                tracker.poll()
                inst = tracker.instances[real_pid]

                self.assertEqual(inst.interrupt_source, "stop_failure")
                self.assertEqual(inst.interrupt_at, 300)
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir

    def test_dismiss_instance_removes_files_and_suppresses_same_session(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            real_pid = 4242
            sid = "dismiss-session"
            cwd = str(home / "project")
            session_payload = {"pid": real_pid, "sessionId": sid, "cwd": cwd}
            state_payload = {
                "pid": real_pid,
                "state": "done",
                "cwd": cwd,
                "updatedAt": 100,
                "provider": "claude",
            }
            session_path = sessions_dir / f"{real_pid}.json"
            state_path = state_dir / f"{real_pid}.json"
            session_path.write_text(json.dumps(session_payload), encoding="utf-8")
            state_path.write_text(json.dumps(state_payload), encoding="utf-8")

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_dismiss_instance")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True
                mod.IS_WINDOWS = False

                tracker = mod.InstanceTracker()
                tracker.poll()
                self.assertIn(real_pid, tracker.instances)

                tracker.instances[real_pid].pinned_at = 1.0
                tracker.save_pins()
                self.assertTrue(tracker.dismiss_instance(real_pid))

                self.assertNotIn(real_pid, tracker.instances)
                self.assertFalse(session_path.exists())
                self.assertFalse(state_path.exists())
                self.assertNotIn(sid, tracker.pins)

                session_path.write_text(json.dumps(session_payload), encoding="utf-8")
                state_path.write_text(json.dumps(state_payload), encoding="utf-8")
                tracker.poll()

                self.assertNotIn(real_pid, tracker.instances)
                self.assertFalse(session_path.exists())
                self.assertFalse(state_path.exists())

                fresh_state_payload = dict(state_payload)
                fresh_state_payload["state"] = "working"
                fresh_state_payload["updatedAt"] = int(mod.time.time()) + 10
                fresh_state_payload["lastSignalSource"] = "hook"
                session_path.write_text(json.dumps(session_payload), encoding="utf-8")
                state_path.write_text(json.dumps(fresh_state_payload), encoding="utf-8")
                tracker.poll()

                self.assertIn(real_pid, tracker.instances)
                self.assertEqual(tracker.instances[real_pid].state, "working")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_dismissed_codex_virtual_row_reappears_after_new_rollout_mtime(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_dismissed_codex_reappears")
                mod.poll_codex_rollouts = None

                pid = "codex-dismissed"
                sid = "dismissed-codex"
                cwd = str(home / "project")
                session_payload = {
                    "pid": pid,
                    "virtualId": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                }
                old_state_payload = {
                    "pid": pid,
                    "state": "done",
                    "cwd": cwd,
                    "updatedAt": 100,
                    "provider": "codex",
                    "lastSignalSource": "rollout",
                    "rolloutMtime": 100,
                }
                session_path = sessions_dir / f"{pid}.json"
                state_path = state_dir / f"{pid}.json"
                session_path.write_text(json.dumps(session_payload), encoding="utf-8")
                state_path.write_text(json.dumps(old_state_payload), encoding="utf-8")

                tracker = mod.InstanceTracker()
                tracker.started_at = 0
                tracker.poll()
                self.assertIn(pid, tracker.instances)

                self.assertTrue(tracker.dismiss_instance(pid))
                self.assertNotIn(pid, tracker.instances)

                session_path.write_text(json.dumps(session_payload), encoding="utf-8")
                state_path.write_text(json.dumps(old_state_payload), encoding="utf-8")
                tracker.poll()

                self.assertNotIn(pid, tracker.instances)

                repaired_state_payload = dict(old_state_payload)
                repaired_state_payload["lastSignalAt"] = int(mod.time.time()) + 10
                session_path.write_text(json.dumps(session_payload), encoding="utf-8")
                state_path.write_text(json.dumps(repaired_state_payload), encoding="utf-8")
                tracker.poll()

                self.assertNotIn(pid, tracker.instances)

                fresh_state_payload = dict(old_state_payload)
                fresh_state_payload["state"] = "working"
                fresh_state_payload["updatedAt"] = int(mod.time.time()) + 10
                session_path.write_text(json.dumps(session_payload), encoding="utf-8")
                state_path.write_text(json.dumps(fresh_state_payload), encoding="utf-8")
                tracker.poll()

                self.assertIn(pid, tracker.instances)
                self.assertEqual(tracker.instances[pid].state, "working")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_startup_filter_removes_old_codex_virtual_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            pid = "codex-old-session"
            sid = "old-session"
            cwd = str(home / "project")
            (sessions_dir / f"{pid}.json").write_text(
                json.dumps({
                    "pid": pid,
                    "virtualId": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{pid}.json").write_text(
                json.dumps({
                    "pid": pid,
                    "sessionId": sid,
                    "state": "done",
                    "cwd": cwd,
                    "updatedAt": 100,
                    "provider": "codex",
                    "lastSignalSource": "rollout",
                    "rolloutMtime": 100,
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_startup_filter_old_codex")
                mod.poll_codex_rollouts = None
                now = int(mod.time.time())
                old_at = now - 3600
                (state_dir / f"{pid}.json").write_text(
                    json.dumps({
                        "pid": pid,
                        "sessionId": sid,
                        "state": "done",
                        "cwd": cwd,
                        "updatedAt": old_at,
                        "provider": "codex",
                        "lastSignalSource": "rollout",
                        "rolloutMtime": old_at,
                        "codexSurface": "app",
                    }),
                    encoding="utf-8",
                )

                tracker = mod.InstanceTracker()
                tracker.started_at = float(now)
                tracker._app_done_ttl_s = 1800
                tracker.poll()

                self.assertNotIn(pid, tracker.instances)
                self.assertFalse((sessions_dir / f"{pid}.json").exists())
                self.assertFalse((state_dir / f"{pid}.json").exists())
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_startup_filter_allows_fresh_codex_virtual_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            pid = "codex-fresh-session"
            sid = "fresh-session"
            cwd = str(home / "project")
            (sessions_dir / f"{pid}.json").write_text(
                json.dumps({
                    "pid": pid,
                    "virtualId": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{pid}.json").write_text(
                json.dumps({
                    "pid": pid,
                    "sessionId": sid,
                    "state": "working",
                    "cwd": cwd,
                    "updatedAt": 300,
                    "provider": "codex",
                    "lastSignalSource": "rollout",
                    "rolloutMtime": 300,
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_startup_filter_fresh_codex")
                mod.poll_codex_rollouts = None
                now = int(mod.time.time())
                recent_at = now - 60
                (state_dir / f"{pid}.json").write_text(
                    json.dumps({
                        "pid": pid,
                        "sessionId": sid,
                        "state": "done",
                        "cwd": cwd,
                        "updatedAt": recent_at,
                        "provider": "codex",
                        "lastSignalSource": "rollout",
                        "rolloutMtime": recent_at,
                        "codexSurface": "app",
                    }),
                    encoding="utf-8",
                )

                tracker = mod.InstanceTracker()
                tracker.started_at = float(now)
                tracker._app_done_ttl_s = 1800
                tracker.poll()

                self.assertIn(pid, tracker.instances)
                self.assertEqual(tracker.instances[pid].state, "done")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_app_done_ttl_hides_expired_codex_app_row_without_deleting_files(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            pid = "codex-expired-app"
            sid = "expired-app-session"
            cwd = str(home / "project")
            session_path = sessions_dir / f"{pid}.json"
            state_path = state_dir / f"{pid}.json"
            session_path.write_text(
                json.dumps({
                    "pid": pid,
                    "virtualId": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                    "codexSurface": "app",
                }),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({
                    "pid": pid,
                    "sessionId": sid,
                    "state": "done",
                    "cwd": cwd,
                    "updatedAt": 100,
                    "provider": "codex",
                    "lastSignalSource": "rollout",
                    "rolloutMtime": 100,
                    "codexSurface": "app",
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_app_done_ttl_expired")
                mod.poll_codex_rollouts = None

                tracker = mod.InstanceTracker()
                tracker.started_at = 0
                tracker._app_done_ttl_s = 1800
                tracker.poll()

                self.assertNotIn(pid, tracker.instances)
                self.assertTrue(session_path.exists())
                self.assertTrue(state_path.exists())

                fresh_state = json.loads(state_path.read_text(encoding="utf-8"))
                fresh_state["state"] = "working"
                fresh_state["updatedAt"] = int(mod.time.time()) + 10
                fresh_state["rolloutMtime"] = fresh_state["updatedAt"]
                state_path.write_text(json.dumps(fresh_state), encoding="utf-8")
                tracker.poll()

                self.assertIn(pid, tracker.instances)
                self.assertEqual(tracker.instances[pid].state, "working")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_app_done_ttl_zero_keeps_expired_codex_app_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)

            pid = "codex-unlimited-app"
            sid = "unlimited-app-session"
            cwd = str(home / "project")
            (sessions_dir / f"{pid}.json").write_text(
                json.dumps({
                    "pid": pid,
                    "virtualId": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "provider": "codex",
                    "codexSurface": "app",
                }),
                encoding="utf-8",
            )
            (state_dir / f"{pid}.json").write_text(
                json.dumps({
                    "pid": pid,
                    "sessionId": sid,
                    "state": "done",
                    "cwd": cwd,
                    "updatedAt": 100,
                    "provider": "codex",
                    "lastSignalSource": "rollout",
                    "rolloutMtime": 100,
                    "codexSurface": "app",
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_app_done_ttl_zero")
                mod.poll_codex_rollouts = None

                tracker = mod.InstanceTracker()
                tracker.started_at = 0
                tracker._app_done_ttl_s = 0
                tracker.poll()

                self.assertIn(pid, tracker.instances)
                self.assertEqual(tracker.instances[pid].state, "done")
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_app_done_ttl_does_not_hide_pinned_app_row(self):
        mod = load_module(MONITOR, "session_monitor_app_done_ttl_pinned")
        tracker = mod.InstanceTracker()
        tracker._app_done_ttl_s = 1800
        tracker.pins = {"pinned-session": 1.0}

        self.assertFalse(tracker._is_app_done_expired(
            "codex",
            "",
            "codex-pinned",
            "pinned-session",
            {
                "state": "done",
                "provider": "codex",
                "codexSurface": "app",
                "lastSignalSource": "rollout",
                "rolloutMtime": 100,
            },
            now=2000,
        ))

    def test_startup_cutoff_passed_to_codex_poller_uses_ttl_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            state_dir = runtime_dir(home) / "state"
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_poller_ttl_cutoff")
                captured = []

                def fake_poll(_known, _cache, _sessions, _state, started_after=0):
                    captured.append(started_after)

                mod.poll_codex_rollouts = fake_poll
                mod.is_claude_pid_alive = lambda _pid: True

                tracker = mod.InstanceTracker()
                tracker.started_at = 1000.0
                tracker._app_done_ttl_s = 300.0
                tracker.poll()

                self.assertEqual(captured, [700.0])
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_claude_desktop_sync_skips_sessions_observed_before_startup(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            native_sessions_dir = home / ".claude" / "sessions"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            native_sessions_dir.mkdir(parents=True)

            real_pid = 4242
            native_path = native_sessions_dir / f"{real_pid}.json"
            native_path.write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": "desktop-session",
                    "cwd": str(home / "project"),
                    "startedAt": 100,
                    "entrypoint": "claude-desktop",
                }),
                encoding="utf-8",
            )
            os.utime(native_path, (100, 100))

            mod = load_module(MONITOR, "session_monitor_claude_desktop_startup_filter")
            mod.is_claude_pid_alive = lambda _pid: True

            mod.sync_claude_desktop_sessions(str(sessions_dir), str(state_dir), started_after=200)
            self.assertFalse((sessions_dir / f"{real_pid}.json").exists())
            self.assertFalse((state_dir / f"{real_pid}.json").exists())

            native_path.write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": "desktop-session",
                    "cwd": str(home / "project"),
                    "updatedAt": 300,
                    "entrypoint": "claude-desktop",
                }),
                encoding="utf-8",
            )
            os.utime(native_path, (300, 300))

            mod.sync_claude_desktop_sessions(str(sessions_dir), str(state_dir), started_after=200)
            self.assertTrue((sessions_dir / f"{real_pid}.json").exists())
            self.assertTrue((state_dir / f"{real_pid}.json").exists())

    def test_tracker_loads_claude_desktop_entrypoint(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            native_sessions_dir = home / ".claude" / "sessions"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            native_sessions_dir.mkdir(parents=True)

            real_pid = 4242
            cwd = str(home / "project")
            (native_sessions_dir / f"{real_pid}.json").write_text(
                json.dumps({
                    "pid": real_pid,
                    "sessionId": "desktop-session",
                    "cwd": cwd,
                    "startedAt": 1780000000000,
                    "entrypoint": "claude-desktop",
                }),
                encoding="utf-8",
            )

            old_state_dir = os.environ.get("SESSION_MONITOR_STATE_DIR")
            old_sessions_dir = os.environ.get("SESSION_MONITOR_SESSIONS_DIR")
            try:
                os.environ["SESSION_MONITOR_STATE_DIR"] = str(state_dir)
                os.environ["SESSION_MONITOR_SESSIONS_DIR"] = str(sessions_dir)
                mod = load_module(MONITOR, "session_monitor_claude_desktop_entrypoint")
                mod.poll_codex_rollouts = None
                mod.is_claude_pid_alive = lambda _pid: True
                mod.IS_WINDOWS = True
                mod.build_process_tree = lambda: {
                    real_pid: (1111, "claude.exe"),
                    1111: (1, "claude.exe"),
                    1: (0, "explorer.exe"),
                }

                tracker = mod.InstanceTracker()
                tracker.started_at = 0
                tracker.poll()
                inst = tracker.instances[real_pid]

                self.assertEqual(inst.entrypoint, "claude-desktop")
                self.assertEqual(inst.provider, "claude")
                self.assertEqual(inst.state, "idle")
                self.assertTrue((sessions_dir / f"{real_pid}.json").exists())
                self.assertTrue((state_dir / f"{real_pid}.json").exists())
            finally:
                if old_state_dir is None:
                    os.environ.pop("SESSION_MONITOR_STATE_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_STATE_DIR"] = old_state_dir
                if old_sessions_dir is None:
                    os.environ.pop("SESSION_MONITOR_SESSIONS_DIR", None)
                else:
                    os.environ["SESSION_MONITOR_SESSIONS_DIR"] = old_sessions_dir

    def test_rollout_cache_hit_repairs_working_pidless_virtual_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
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

            vid = mod.virtual_id_for(sid)
            (sessions_dir / f"{vid}.json").write_text("{}", encoding="utf-8")
            (state_dir / f"{vid}.json").write_text("{}", encoding="utf-8")

            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "working")

    def test_rollout_virtual_ids_do_not_collide_on_timestamp_prefix(self):
        mod = load_module(POLLER, "codex_rollout_poller_full_virtual_id")
        a = "019e27ec-b184-7192-b398-ef2d33000d02"
        b = "019e27ec-c001-7192-b398-ef2d33000d02"

        self.assertNotEqual(mod.virtual_id_for(a), mod.virtual_id_for(b))
        self.assertEqual(mod.virtual_id_for(a), f"codex-{a}")

    def test_rollout_full_virtual_id_removes_legacy_prefix_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "019e27ec-b184-7192-b398-ef2d33000d02"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": "cli"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            legacy_vid = "codex-019e27ec"
            (sessions_dir / f"{legacy_vid}.json").write_text("{}", encoding="utf-8")
            (state_dir / f"{legacy_vid}.json").write_text("{}", encoding="utf-8")

            mod = load_module(POLLER, "codex_rollout_poller_legacy_cleanup")
            vid = mod.virtual_id_for(sid)
            cache = {}
            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            self.assertFalse((sessions_dir / f"{legacy_vid}.json").exists())
            self.assertFalse((state_dir / f"{legacy_vid}.json").exists())

    def test_rollout_interrupted_turns_do_not_leave_later_done_session_working(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_aborted_then_done")

            started, terminal, failed = mod._scan_turn_markers(str(rollout))

            self.assertEqual(started, 2)
            self.assertEqual(terminal, 2)
            self.assertFalse(failed)
            self.assertEqual(
                mod._infer_state(rollout.stat().st_mtime, started, terminal, failed),
                "done",
            )

    def test_rollout_latest_turn_aborted_is_interrupted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_latest_aborted")

            started, terminal, failed = mod._scan_turn_markers(str(rollout))

            self.assertEqual(started, 1)
            self.assertEqual(terminal, 1)
            self.assertTrue(failed)
            self.assertEqual(
                mod._infer_state(rollout.stat().st_mtime, started, terminal, failed),
                "interrupted",
            )

    def test_rollout_pending_user_input_is_question(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "request_user_input",
                            "call_id": "call_question",
                        },
                    }),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_user_input_question")

            started, terminal, failed, waiting = mod._scan_activity(str(rollout))

            self.assertEqual(started, 1)
            self.assertEqual(terminal, 0)
            self.assertFalse(failed)
            self.assertTrue(waiting)
            self.assertEqual(
                mod._infer_state(rollout.stat().st_mtime, started, terminal, failed, waiting),
                "question",
            )

    def test_rollout_fork_scan_ignores_parent_pending_question(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            parent_sid = "parent11-1111-2222-3333-abcdefghijkl"
            child_sid = "child111-1111-2222-3333-abcdefghijkl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": child_sid}}),
                    json.dumps({"type": "session_meta", "payload": {"id": parent_sid}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "request_user_input",
                            "call_id": "parent_question",
                        },
                    }),
                    json.dumps({"type": "session_meta", "payload": {"id": child_sid}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_fork_question")

            started, terminal, failed, waiting = mod._scan_activity(str(rollout), child_sid)

            self.assertEqual(started, 1)
            self.assertEqual(terminal, 1)
            self.assertFalse(failed)
            self.assertFalse(waiting)
            self.assertEqual(
                mod._infer_state(rollout.stat().st_mtime, started, terminal, failed, waiting),
                "done",
            )

    def test_rollout_answered_user_input_returns_to_working(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "request_user_input",
                            "call_id": "call_question",
                        },
                    }),
                    json.dumps({
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_question",
                            "output": "approved",
                        },
                    }),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_user_input_answered")

            started, terminal, failed, waiting = mod._scan_activity(str(rollout))

            self.assertFalse(waiting)
            self.assertEqual(
                mod._infer_state(rollout.stat().st_mtime, started, terminal, failed, waiting),
                "working",
            )

    def test_rollout_complete_then_started_tie_is_working(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            rollout = Path(td) / "rollout.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_complete_started_tie")

            started, terminal, failed, waiting, latest = mod._scan_activity(
                str(rollout), include_latest=True
            )

            self.assertEqual(started, 1)
            self.assertEqual(terminal, 1)
            self.assertEqual(latest, "started")
            self.assertEqual(
                mod._infer_state(rollout.stat().st_mtime, started, terminal, failed, waiting, latest),
                "working",
            )

    def test_pidless_codex_row_uses_cwd_focus_fallback(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_focus")
        called = []

        class Dummy:
            def __init__(self):
                self.tracker = type("Tracker", (), {
                    "instances": {
                        "codex-abc": type("Inst", (), {
                            "provider": "codex",
                            "cwd": "C:\\project",
                            "session_id": "",
                            "wezterm": None,
                        })()
                    }
                })()

            def _activate_codex_pidless(self, cwd, session_id="", *_args):
                called.append((cwd, session_id))

        mod.MonitorOverlay._activate_terminal(Dummy(), "codex-abc")

        self.assertEqual(called, [("C:\\project", "")])

    def test_pidless_codex_app_focus_opens_thread_deep_link_first(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_app_deeplink")
        opened = []
        activated = []

        class Dummy:
            def _open_codex_thread(self, session_id):
                opened.append(session_id)
                return True

            def _activate_codex_app_window(self):
                activated.append(True)
                return True

            def _activate_wezterm_pane_for_codex_row(self, *_args):
                raise AssertionError("terminal fallback should not run")

        mod.MonitorOverlay._activate_codex_pidless(
            Dummy(),
            "C:\\project",
            "019e-desktop-thread",
            "app",
            "Codex Desktop",
        )

        self.assertEqual(opened, ["019e-desktop-thread"])
        self.assertEqual(activated, [True])

    def test_pidless_codex_cli_focus_opens_thread_deep_link_first(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_cli_deeplink")
        opened = []

        class Dummy:
            def _open_codex_thread(self, session_id):
                opened.append(session_id)
                return True

            def _activate_wezterm_pane_for_codex_row(self, *_args):
                raise AssertionError("terminal fallback should not run")

        mod.MonitorOverlay._activate_codex_pidless(
            Dummy(),
            "C:\\project",
            "019e-cli-thread",
            "cli",
            "codex-tui",
        )

        self.assertEqual(opened, ["019e-cli-thread"])

    def test_pidless_codex_focus_does_nothing_without_window_match(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_no_match")
        mod.MonitorOverlay._activate_wezterm_pane_for_codex_row = lambda _self, _cwd, _sid="": False
        mod.build_process_tree = lambda: {
            100: (1, "codex.exe"),
        }
        mod.find_window_for_pid = lambda _pid, _tree, _cwd: None
        mod.activate_window = lambda _hwnd: self.fail("activate_window called")

        class Dummy:
            pass

        mod.MonitorOverlay._activate_codex_pidless(Dummy(), "C:\\project")

    def test_pidless_codex_focus_uses_unique_wezterm_cwd(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_wezterm")
        calls = []

        class Result:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if cmd[:3] == ["wezterm", "cli", "list"]:
                return Result(json.dumps([
                    {
                        "pane_id": 15,
                        "cwd": "file:///C:/Users/vehum/cluade_workspace/claude-code-monitor/",
                    }
                ]))
            if cmd[:3] == ["wezterm", "cli", "activate-pane"]:
                return Result("")
            self.fail(f"unexpected command: {cmd}")

        raised = []
        old_run = mod.subprocess.run
        try:
            mod.subprocess.run = fake_run

            class Dummy:
                _normalize_wezterm_cwd = staticmethod(mod.MonitorOverlay._normalize_wezterm_cwd)

                def _raise_wezterm_client_for_pane(self, pane_id):
                    raised.append(pane_id)

            self.assertTrue(mod.MonitorOverlay._activate_wezterm_pane_for_codex_row(
                Dummy(),
                "C:\\Users\\vehum\\cluade_workspace\\claude-code-monitor",
            ))
        finally:
            mod.subprocess.run = old_run

        self.assertIn(["wezterm", "cli", "activate-pane", "--pane-id", "15"], calls)
        self.assertEqual(raised, [15])

    def test_pidless_codex_focus_uses_session_id_when_cwd_is_duplicated(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_wezterm_session")
        calls = []

        class Result:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            if cmd[:3] == ["wezterm", "cli", "list"]:
                return Result(json.dumps([
                    {"pane_id": 1, "cwd": "file:///C:/project/"},
                    {"pane_id": 2, "cwd": "file:///C:/project/"},
                ]))
            if cmd[:3] == ["wezterm", "cli", "get-text"]:
                pane_id = cmd[-1]
                return Result("session 019e-target" if pane_id == "2" else "other session")
            if cmd[:3] == ["wezterm", "cli", "activate-pane"]:
                return Result("")
            self.fail(f"unexpected command: {cmd}")

        raised = []
        old_run = mod.subprocess.run
        try:
            mod.subprocess.run = fake_run

            class Dummy:
                _normalize_wezterm_cwd = staticmethod(mod.MonitorOverlay._normalize_wezterm_cwd)

                def _raise_wezterm_client_for_pane(self, pane_id):
                    raised.append(pane_id)

            self.assertTrue(mod.MonitorOverlay._activate_wezterm_pane_for_codex_row(
                Dummy(),
                "C:\\project",
                "019e-target",
            ))
        finally:
            mod.subprocess.run = old_run

        self.assertIn(["wezterm", "cli", "activate-pane", "--pane-id", "2"], calls)
        self.assertEqual(raised, [2])

    def test_pidless_codex_focus_rejects_duplicate_wezterm_cwd(self):
        mod = load_module(MONITOR, "session_monitor_pidless_codex_wezterm_duplicate")

        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps([
                {"pane_id": 1, "cwd": "file:///C:/project/"},
                {"pane_id": 2, "cwd": "file:///C:/project/"},
            ])

        old_run = mod.subprocess.run
        try:
            mod.subprocess.run = lambda *_args, **_kwargs: Result()

            class Dummy:
                _normalize_wezterm_cwd = staticmethod(mod.MonitorOverlay._normalize_wezterm_cwd)

                def _raise_wezterm_client_for_pane(self, _pane_id):
                    self.fail("_raise_wezterm_client_for_pane called")

            self.assertFalse(mod.MonitorOverlay._activate_wezterm_pane_for_codex_row(
                Dummy(),
                "C:\\project",
            ))
        finally:
            mod.subprocess.run = old_run

    def test_subagent_rollout_is_ignored_and_virtual_row_cleanup_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
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

    def test_codex_exec_rollout_is_ignored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "exec1111-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                json.dumps({
                    "type": "session_meta",
                    "payload": {
                        "id": sid,
                        "cwd": cwd,
                        "originator": "codex_exec",
                        "source": "exec",
                    },
                }),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_exec_ignore")
            vid = mod.virtual_id_for(sid)
            (sessions_dir / f"{vid}.json").write_text("{}", encoding="utf-8")
            (state_dir / f"{vid}.json").write_text("{}", encoding="utf-8")

            cache = {}
            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue(rollout.exists())
            self.assertFalse((sessions_dir / f"{vid}.json").exists())
            self.assertFalse((state_dir / f"{vid}.json").exists())
            self.assertTrue(cache[str(rollout)]["ignored"])
            self.assertEqual(cache[str(rollout)]["ignore_reason"], "exec")

    def test_codex_rollout_before_monitor_start_is_ignored_until_new_activity_window(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "prestart-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": "cli"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_pre_start_filter")
            vid = mod.virtual_id_for(sid)
            cache = {}
            started_after = mod.time.time() + 10

            mod.poll_codex_rollouts(
                set(), cache, str(sessions_dir), str(state_dir),
                started_after=started_after,
            )

            self.assertFalse((sessions_dir / f"{vid}.json").exists())
            self.assertFalse((state_dir / f"{vid}.json").exists())
            self.assertTrue(cache[str(rollout)]["ignored"])
            self.assertEqual(cache[str(rollout)]["ignore_reason"], "pre_start")

            mod.poll_codex_rollouts(
                set(), cache, str(sessions_dir), str(state_dir),
                started_after=started_after - 20,
            )

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "working")

    def test_hookless_cli_working_rollout_creates_virtual_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "plain111-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": "cli"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_cli_working")
            vid = mod.virtual_id_for(sid)

            cache = {}
            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "working")

    def test_codex_desktop_rollout_creates_app_row_with_title(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            codex_dir = home / ".codex"
            rollout_dir = codex_dir / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "desktop1-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({
                        "type": "session_meta",
                        "payload": {
                            "id": sid,
                            "cwd": cwd,
                            "originator": "Codex Desktop",
                            "source": "vscode",
                            "thread_source": "user",
                        },
                    }),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                ]),
                encoding="utf-8",
            )

            import sqlite3
            con = sqlite3.connect(codex_dir / "state_5.sqlite")
            try:
                con.execute(
                    "create table threads ("
                    "id text, title text, tokens_used integer, updated_at integer, "
                    "source text, cwd text, rollout_path text)"
                )
                con.execute(
                    "insert into threads values (?, ?, ?, ?, ?, ?, ?)",
                    (sid, "Codex 앱 모니터링 조사", 1234, 1780000000, "vscode", cwd, str(rollout)),
                )
                con.commit()
            finally:
                con.close()

            mod = load_module(POLLER, "codex_rollout_poller_desktop_app")
            vid = mod.virtual_id_for(sid)
            cache = {}

            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            session = json.loads((sessions_dir / f"{vid}.json").read_text(encoding="utf-8"))
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(session["codexSurface"], "app")
            self.assertEqual(session["codexOriginator"], "Codex Desktop")
            self.assertEqual(state["state"], "working")
            self.assertEqual(state["codexSurface"], "app")
            self.assertEqual(state["codexTitle"], "Codex 앱 모니터링 조사")
            self.assertEqual(state["summary"], "Codex 앱 모니터링 조사")
            self.assertEqual(state["summarySource"], "trim")
            self.assertEqual(state["tokensUsed"], 1234)

    def test_codex_desktop_thread_metadata_restores_stale_rollout(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            codex_dir = home / ".codex"
            rollout_dir = codex_dir / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            mod = load_module(POLLER, "codex_rollout_poller_desktop_stale_restore")
            sid = "stale111-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({
                        "type": "session_meta",
                        "payload": {
                            "id": sid,
                            "cwd": cwd,
                            "originator": "Codex Desktop",
                            "source": "vscode",
                            "thread_source": "user",
                        },
                    }),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                ]),
                encoding="utf-8",
            )
            old_mtime = int(mod.time.time()) - mod._STALE_EVICTION_S - 60
            os.utime(rollout, (old_mtime, old_mtime))
            thread_updated_at = int(mod.time.time())

            import sqlite3
            con = sqlite3.connect(codex_dir / "state_5.sqlite")
            try:
                con.execute(
                    "create table threads ("
                    "id text, title text, tokens_used integer, updated_at integer, "
                    "source text, cwd text, rollout_path text)"
                )
                con.execute(
                    "insert into threads values (?, ?, ?, ?, ?, ?, ?)",
                    (sid, "재개된 오래된 앱 세션", 42, thread_updated_at, "vscode", cwd, str(rollout)),
                )
                con.commit()
            finally:
                con.close()

            vid = mod.virtual_id_for(sid)
            cache = {}

            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sessionId"], sid)
            self.assertEqual(state["state"], "done")
            self.assertEqual(state["rolloutMtime"], old_mtime)
            self.assertEqual(state["threadUpdatedAt"], thread_updated_at)
            self.assertEqual(state["updatedAt"], thread_updated_at)
            self.assertEqual(state["codexTitle"], "재개된 오래된 앱 세션")

    def test_hookless_cli_done_rollout_creates_virtual_row(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "done1111-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": "cli"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                ]),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_cli_done")
            vid = mod.virtual_id_for(sid)

            cache = {}
            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "done")

    def test_hook_owned_rollout_is_not_reimported_after_pid_exit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            hooked_dir = runtime_dir(home) / "codex-hooked"
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

    def test_ignored_hook_owned_cache_hit_skips_first_line_read(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            hooked_dir = runtime_dir(home) / "codex-hooked"
            rollout_dir = home / ".codex" / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            hooked_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "cached11-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}),
                encoding="utf-8",
            )
            (hooked_dir / f"{sid}.json").write_text(
                json.dumps({"sessionId": sid, "updatedAt": int(rollout.stat().st_mtime)}),
                encoding="utf-8",
            )
            mod = load_module(POLLER, "codex_rollout_poller_ignored_cache_fast")
            mod._read_first_json_line = lambda _path: self.fail("first line should stay cached")
            cache = {
                str(rollout): {
                    "mtime": rollout.stat().st_mtime,
                    "session_id": sid,
                    "ignored": True,
                    "ignore_reason": "hook_owned",
                }
            }

            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue(cache[str(rollout)]["ignored"])

    def test_hook_owned_rollout_reappears_after_new_thread_update(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            home = Path(td)
            os.environ["HOME"] = td
            os.environ["USERPROFILE"] = td
            sessions_dir = runtime_dir(home) / "sessions"
            state_dir = runtime_dir(home) / "state"
            hooked_dir = runtime_dir(home) / "codex-hooked"
            codex_dir = home / ".codex"
            rollout_dir = codex_dir / "sessions" / "2026" / "05" / "08"
            sessions_dir.mkdir(parents=True)
            state_dir.mkdir(parents=True)
            hooked_dir.mkdir(parents=True)
            rollout_dir.mkdir(parents=True)

            sid = "rehook11-1111-2222-3333-abcdefghijkl"
            cwd = str(home / "project")
            rollout = rollout_dir / f"rollout-2026-05-08T00-00-00-{sid}.jsonl"
            rollout.write_text(
                "\n".join([
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": "cli"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}),
                    json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                ]),
                encoding="utf-8",
            )
            (hooked_dir / f"{sid}.json").write_text(
                json.dumps({"sessionId": sid, "updatedAt": 100}),
                encoding="utf-8",
            )

            import sqlite3
            con = sqlite3.connect(codex_dir / "state_5.sqlite")
            try:
                con.execute(
                    "create table threads ("
                    "id text, title text, tokens_used integer, updated_at integer, "
                    "source text, cwd text, rollout_path text)"
                )
                con.execute(
                    "insert into threads values (?, ?, ?, ?, ?, ?, ?)",
                    (sid, "resumed hook-owned session", 7, 200, "cli", cwd, str(rollout)),
                )
                con.commit()
            finally:
                con.close()

            mod = load_module(POLLER, "codex_rollout_poller_hooked_resumed")
            vid = mod.virtual_id_for(sid)
            cache = {
                str(rollout): {
                    "mtime": rollout.stat().st_mtime,
                    "session_id": sid,
                    "ignored": True,
                    "ignore_reason": "hook_owned",
                }
            }

            mod.poll_codex_rollouts(set(), cache, str(sessions_dir), str(state_dir))

            self.assertTrue((sessions_dir / f"{vid}.json").exists())
            self.assertTrue((state_dir / f"{vid}.json").exists())
            state = json.loads((state_dir / f"{vid}.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sessionId"], sid)
            self.assertEqual(state["state"], "done")
            self.assertEqual(state["threadUpdatedAt"], 200)


if __name__ == "__main__":
    unittest.main()
