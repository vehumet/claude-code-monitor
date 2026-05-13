#!/usr/bin/env python3
"""Install Session Monitor from the repository root."""

import os
import runpy


ROOT = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(ROOT, "plugins", "session-monitor", "install.py")


if __name__ == "__main__":
    runpy.run_path(INSTALLER, run_name="__main__")
