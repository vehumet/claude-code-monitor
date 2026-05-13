#!/usr/bin/env python3
"""Detached launcher for Session Monitor."""

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
    return os.path.join(_script_dir(), "session-monitor.py")


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


def _monitor_running():
    if IS_WINDOWS:
        cmd = (
            "$needle = '[\\\\/]session-monitor\\.py(\"|\\s|$)'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.ProcessId -ne $PID -and $_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -match $needle } | "
            "Select-Object -First 1 -ExpandProperty ProcessId"
        )
        kwargs = {
            "capture_output": True,
            "text": True,
            "stdin": subprocess.DEVNULL,
            "creationflags": CREATE_NO_WINDOW,
        }
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", cmd],
                timeout=5,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return any(ch.isdigit() for ch in proc.stdout)

    pgrep = shutil.which("pgrep")
    if not pgrep:
        return False
    try:
        proc = subprocess.run(
            [pgrep, "-f", r"[/\\][s]ession-monitor.py([[:space:]]|$)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def launch(monitor_path=None):
    monitor_path = os.path.abspath(monitor_path or _default_monitor_path())
    if not os.path.exists(monitor_path):
        raise FileNotFoundError(monitor_path)
    if _monitor_running():
        return None

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
