#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR="${1:-$SCRIPT_DIR/claude-code-monitor.py}"
pythonw "$MONITOR" 2>/dev/null &
