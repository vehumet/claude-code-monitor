#!/usr/bin/env python3
"""Uninstall Session Monitor from the repository root."""

import os
import runpy


ROOT = os.path.dirname(os.path.abspath(__file__))
UNINSTALLER = os.path.join(ROOT, "plugins", "session-monitor", "uninstall.py")


if __name__ == "__main__":
    runpy.run_path(UNINSTALLER, run_name="__main__")
