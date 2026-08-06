#!/usr/bin/env python3
"""Manage filesystem backup targets (4.4.4).

Usage:
    python scripts/backup_target.py init /mnt/backup
    python scripts/backup_target.py probe <target-id>
    python scripts/backup_target.py list
    python scripts/backup_target.py delete <target-id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.workspace import backup_scheduler, backup_targets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage filesystem backup targets")
    commands = parser.add_subparsers(dest="command", required=True)
    init_cmd = commands.add_parser("init", help="initialize a directory as a backup target")
    init_cmd.add_argument("path", type=Path)
    init_cmd.add_argument("--label", default="")
    probe_cmd = commands.add_parser("probe", help="re-verify a target marker")
    probe_cmd.add_argument("target_id")
    commands.add_parser("list", help="list registered targets")
    delete_cmd = commands.add_parser("delete", help="remove a target from the registry")
    delete_cmd.add_argument("target_id")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = backup_targets.init_target(args.path, label=args.label)
        elif args.command == "probe":
            result = backup_targets.probe_target(args.target_id)
            backup_scheduler.record_target_health(args.target_id, "ok" if result.get("ready") else "blocked", str(result.get("detail") or "")[:200] or None)
        elif args.command == "list":
            result = {"targets": backup_targets.list_targets(), "health": backup_scheduler.target_health()}
        else:
            result = backup_targets.delete_target(args.target_id)
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
