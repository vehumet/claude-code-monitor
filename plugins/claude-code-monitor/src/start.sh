#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python "$SCRIPT_DIR/start-monitor.py" "$@" 2>/dev/null
