"""Shared filesystem paths for Session Monitor runtime data."""

import os


APP_DIR_NAME = "session-monitor"


def home_dir() -> str:
    return os.path.expanduser("~")


def runtime_dir() -> str:
    """Neutral per-user runtime/data directory.

    SESSION_MONITOR_ROOT is intended for tests and advanced installs. The
    default deliberately avoids client-owned directories such as ~/.claude or
    ~/.codex because the monitor is shared by multiple providers.
    """
    return os.environ.get("SESSION_MONITOR_ROOT") or os.path.join(
        home_dir(), ".local", "share", APP_DIR_NAME
    )


def legacy_runtime_dir() -> str:
    return os.path.join(home_dir(), ".claude", APP_DIR_NAME)


def state_dir() -> str:
    return os.environ.get("SESSION_MONITOR_STATE_DIR") or os.path.join(
        runtime_dir(), "state"
    )


def sessions_dir() -> str:
    return os.environ.get("SESSION_MONITOR_SESSIONS_DIR") or os.path.join(
        runtime_dir(), "sessions"
    )


def logs_dir() -> str:
    return os.path.join(runtime_dir(), "logs")


def config_file() -> str:
    return os.path.join(runtime_dir(), "config.json")


def position_file() -> str:
    return os.path.join(runtime_dir(), "position.json")


def pins_file() -> str:
    return os.path.join(runtime_dir(), "pins.json")


def codex_hooked_dir() -> str:
    return os.path.join(runtime_dir(), "codex-hooked")


def nested_pids_dir() -> str:
    return os.path.join(runtime_dir(), "nested-pids")


def write_state_file() -> str:
    return os.path.join(runtime_dir(), "write-state.py")
