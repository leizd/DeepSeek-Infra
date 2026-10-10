#!/usr/bin/env bash
# Phone-friendly launcher for Termux, Pydroid terminals, and small Linux shells.
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
if command -v python3 >/dev/null 2>&1; then
    exec python3 launch_mobile.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python launch_mobile.py "$@"
else
    echo "Python 3.10+ is required. Please install it then re-run this script."
    exit 1
fi
