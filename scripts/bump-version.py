#!/usr/bin/env python3
"""Bump version across all three version sources in this project.

Usage:
    python scripts/bump-version.py patch          # 0.0.2 -> 0.0.3
    python scripts/bump-version.py minor          # 0.0.2 -> 0.1.0
    python scripts/bump-version.py major          # 0.0.2 -> 1.0.0
    python scripts/bump-version.py 1.2.3          # set explicit version

stdlib only — no external dependencies.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLUGIN_JSON = os.path.join(ROOT, "plugins", "claude-code-monitor", ".claude-plugin", "plugin.json")
MARKETPLACE_JSON = os.path.join(ROOT, ".claude-plugin", "marketplace.json")
MONITOR_PY = os.path.join(ROOT, "plugins", "claude-code-monitor", "src", "claude-code-monitor.py")

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_current_version():
    """Read current version from plugin.json (source of truth)."""
    with open(PLUGIN_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["version"]


def compute_new_version(current: str, bump: str) -> str:
    """Return new version string given a bump type or explicit version."""
    m = VERSION_RE.match(bump)
    if m:
        return bump  # explicit version

    parts = VERSION_RE.match(current)
    if not parts:
        print(f"Error: current version '{current}' is not valid semver", file=sys.stderr)
        sys.exit(1)

    major, minor, patch = int(parts[1]), int(parts[2]), int(parts[3])

    if bump == "patch":
        patch += 1
    elif bump == "minor":
        minor += 1
        patch = 0
    elif bump == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        print(f"Error: unknown bump type '{bump}'. Use patch/minor/major or an explicit version.", file=sys.stderr)
        sys.exit(1)

    return f"{major}.{minor}.{patch}"


def update_plugin_json(version: str):
    with open(PLUGIN_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = version
    with open(PLUGIN_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def update_marketplace_json(version: str):
    with open(MARKETPLACE_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    for plugin in data.get("plugins", []):
        if plugin.get("name") == "claude-code-monitor":
            plugin["version"] = version
    with open(MARKETPLACE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def update_monitor_py(version: str):
    with open(MONITOR_PY, "r", encoding="utf-8") as f:
        content = f.read()
    new_content = re.sub(
        r'^__version__\s*=\s*"[^"]*"',
        f'__version__ = "{version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if new_content == content:
        print(f"Warning: __version__ pattern not found in {MONITOR_PY}", file=sys.stderr)
    with open(MONITOR_PY, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/bump-version.py <patch|minor|major|X.Y.Z>", file=sys.stderr)
        sys.exit(1)

    bump = sys.argv[1]
    current = read_current_version()
    new_version = compute_new_version(current, bump)

    print(f"Bumping version: {current} -> {new_version}")

    update_plugin_json(new_version)
    print(f"  Updated {os.path.relpath(PLUGIN_JSON, ROOT)}")

    update_marketplace_json(new_version)
    print(f"  Updated {os.path.relpath(MARKETPLACE_JSON, ROOT)}")

    update_monitor_py(new_version)
    print(f"  Updated {os.path.relpath(MONITOR_PY, ROOT)}")

    print(f"Done. All versions set to {new_version}")


if __name__ == "__main__":
    main()
