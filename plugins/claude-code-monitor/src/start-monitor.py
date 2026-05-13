#!/usr/bin/env python3
"""Detached launcher for Claude Code Monitor."""

import os
import shutil
import subprocess
import sys


IS_WINDOWS = sys.platform == "win32"

# Avoid importing subprocess internals; these Win32 flags are stable.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def _script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _default_monitor_path():
    return os.path.join(_script_dir(), "claude-code-monitor.py")


def _resolve_pythonw():
    exe = sys.executable or ""
    if IS_WINDOWS:
        if os.path.basename(exe).lower() == "pythonw.exe":
            return exe
        if exe:
            sibling = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(sibling):
                return sibling
        found = shutil.which("pythonw.exe") or shutil.which("pythonw")
        if found:
            return found
    return shutil.which("pythonw") or exe or "python"


def launch(monitor_path=None):
    monitor_path = os.path.abspath(monitor_path or _default_monitor_path())
    if not os.path.exists(monitor_path):
        raise FileNotFoundError(monitor_path)

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen([_resolve_pythonw(), monitor_path], **kwargs)


def main():
    monitor_path = sys.argv[1] if len(sys.argv) > 1 else None
    launch(monitor_path)


if __name__ == "__main__":
    main()
