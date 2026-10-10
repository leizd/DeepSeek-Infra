#!/usr/bin/env bash
# Double-click (or run from a terminal) to open the DeepSeek Infra local app window.
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
if command -v python3 >/dev/null 2>&1; then
    exec python3 launch.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python launch.py "$@"
else
    echo "Python 3.10+ is required. Please install it then re-run this script."
    exit 1
fi
