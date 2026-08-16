#!/usr/bin/env python3
"""Run multi-target replica self-healing and lifecycle governance E2E scenarios."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 4.5.3 replica self-healing E2E scenarios")
    parser.add_argument("--json", action="store_true", help="Emit json report")
    args = parser.parse_args()

    import pytest

    code = pytest.main([
        "-q",
        "--no-cov",
        "tests/test_backup_453_replica_healing_e2e.py",
        "tests/test_backup_453_replica_healing.py",
    ])

    if args.json:
        report = {
            "version": VERSION,
            "title": "Two-Target Replica Self-Healing and Lifecycle Governance E2E",
            "pytestExitCode": code,
            "status": "PASS" if code == 0 else "FAIL",
        }
        print(json.dumps(report, indent=2))

    return code


if __name__ == "__main__":
    sys.exit(main())
