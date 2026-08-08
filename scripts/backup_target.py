#!/usr/bin/env python3
"""Manage filesystem and S3-compatible backup targets (4.4.6).

Usage:
    python scripts/backup_target.py init /mnt/backup
    python scripts/backup_target.py init-s3 --bucket my-backups --prefix deepseek-infra/home --region ap-northeast-1 --profile deepseek-backup
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
from deepseek_infra.infra.workspace import backup_scheduler, backup_target_s3, backup_targets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage backup targets")
    commands = parser.add_subparsers(dest="command", required=True)
    init_cmd = commands.add_parser("init", help="initialize a directory as a backup target")
    init_cmd.add_argument("path", type=Path)
    init_cmd.add_argument("--label", default="")
    s3_cmd = commands.add_parser("init-s3", help="register a secret-free S3-compatible target")
    s3_cmd.add_argument("--bucket", required=True)
    s3_cmd.add_argument("--prefix", default="")
    s3_cmd.add_argument("--region", default="")
    s3_cmd.add_argument("--endpoint-url", default="")
    s3_cmd.add_argument("--expected-bucket-owner", default="")
    s3_cmd.add_argument("--profile", default="")
    s3_cmd.add_argument("--label", default="")
    s3_cmd.add_argument("--no-probe", action="store_true")
    probe_cmd = commands.add_parser("probe", help="re-verify a target marker or S3 capabilities")
    probe_cmd.add_argument("target_id")
    commands.add_parser("list", help="list registered targets")
    delete_cmd = commands.add_parser("delete", help="remove a target from the registry")
    delete_cmd.add_argument("target_id")
    commands.add_parser("capabilities", help="show local adapter availability")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = backup_targets.init_target(args.path, label=args.label)
        elif args.command == "init-s3":
            provider: dict[str, object]
            if args.profile:
                provider = {"type": "aws-profile", "profile": args.profile}
            else:
                provider = {"type": "aws-default-chain"}
            result = backup_targets.init_s3_target(
                bucket=args.bucket,
                prefix=args.prefix,
                region=args.region or None,
                endpoint_url=args.endpoint_url or None,
                expected_bucket_owner=args.expected_bucket_owner or None,
                label=args.label,
                credential_provider=provider,
                probe=not args.no_probe,
            )
        elif args.command == "probe":
            result = backup_targets.probe_target(args.target_id)
            backup_scheduler.record_target_health(
                args.target_id,
                "ok" if result.get("ready") else "blocked",
                str(result.get("detail") or result.get("status") or "")[:200] or None,
            )
        elif args.command == "list":
            result = {"targets": backup_targets.list_targets(), "health": backup_scheduler.target_health()}
        elif args.command == "capabilities":
            result = {
                "s3TargetAvailable": backup_target_s3.s3_sdk_available(),
                "webdavTargetAvailable": False,
                "supportedKinds": ["filesystem", "s3"] if backup_target_s3.s3_sdk_available() else ["filesystem"],
            }
        else:
            result = backup_targets.delete_target(args.target_id)
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
