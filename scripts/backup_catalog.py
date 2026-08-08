#!/usr/bin/env python3
"""Inspect and maintain the backup catalog (4.4.5).

Usage:
    python scripts/backup_catalog.py list [--target-id <id>]
    python scripts/backup_catalog.py scrub <backup-id>
    python scripts/backup_catalog.py retention-preview <policy-id>
    python scripts/backup_catalog.py pin <backup-id> [--unpin]
    python scripts/backup_catalog.py rebuild [--target-id <id>]
    python scripts/backup_catalog.py verify-unlock <backup-id>
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.workspace import (  # noqa: E402
    backup_catalog,
    backup_policies,
    backup_publish,
    backup_retention,
    backup_scrub,
    backups,
)


def _root(target_id: str) -> Path:
    return backup_publish.resolve_target(target_id).require_root()


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and maintain the backup catalog")
    commands = parser.add_subparsers(dest="command", required=True)
    list_cmd = commands.add_parser("list", help="list cataloged backups")
    list_cmd.add_argument("--target-id", default="managed-local")
    scrub_cmd = commands.add_parser("scrub", help="re-verify one ciphertext")
    scrub_cmd.add_argument("backup_id")
    scrub_cmd.add_argument("--target-id", default="managed-local")
    preview_cmd = commands.add_parser("retention-preview", help="preview GFS retention for a policy")
    preview_cmd.add_argument("policy_id")
    pin_cmd = commands.add_parser("pin", help="pin or unpin a backup")
    pin_cmd.add_argument("backup_id")
    pin_cmd.add_argument("--target-id", default="managed-local")
    pin_cmd.add_argument("--unpin", action="store_true")
    rebuild_cmd = commands.add_parser("rebuild", help="rebuild the catalog from receipts")
    rebuild_cmd.add_argument("--target-id", default="managed-local")
    drill_cmd = commands.add_parser("verify-unlock", help="run a read-only unlock drill")
    drill_cmd.add_argument("backup_id")
    drill_cmd.add_argument("--target-id", default="managed-local")
    args = parser.parse_args()
    try:
        if args.command == "list":
            root = _root(args.target_id)
            result = {
                "backups": backup_catalog.list_backups(root),
                "chainValid": backup_catalog.verify_chain(root),
                "integrity": backup_catalog.find_orphans_and_missing(root),
                "health": backup_scrub.backup_health(root),
            }
        elif args.command == "scrub":
            result = backup_scrub.scrub_backup(_root(args.target_id), args.backup_id, target_id=args.target_id)
        elif args.command == "retention-preview":
            policy = backup_policies.get_policy(args.policy_id)
            retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
            root = _root(str(policy.get("targetId") or "managed-local"))
            timezone_name = str((policy.get("schedule") or {}).get("timezone") or "UTC")
            result = backup_retention.preview_retention(retention, root, policy_timezone=timezone_name)
            result.pop("trashRecords", None)
        elif args.command == "pin":
            backup_catalog.pin_backup(_root(args.target_id), args.backup_id, not args.unpin)
            result = {"backupId": args.backup_id, "pinned": not args.unpin}
        elif args.command == "rebuild":
            result = backup_catalog.rebuild_catalog_from_receipts(_root(args.target_id))
        else:
            identity_text = getpass.getpass("Recovery Identity: ").strip()
            identity = bytearray(identity_text.encode("utf-8"))
            try:
                result = backup_scrub.verify_unlock_drill(_root(args.target_id), args.backup_id, identity, staged_root=backups.RESTORE_DIR / "drills")
            finally:
                for index in range(len(identity)):
                    identity[index] = 0
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
