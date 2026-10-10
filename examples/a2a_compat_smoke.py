#!/usr/bin/env python3
"""Example entrypoint for the A2A compatibility smoke runner.

This wrapper keeps the discoverable example path short while delegating the real
checks to ``scripts/smoke_a2a_compat.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.smoke_a2a_compat import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
