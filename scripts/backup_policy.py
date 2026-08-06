#!/usr/bin/env python3
"""Manage durable scheduled backup policies (4.4.4).

Usage:
    python scripts/backup_policy.py create --cron "0 3 * * *" --timezone Asia/Singapore --recipient age1... --target <target-id>
    python scripts/backup_policy.py list
    python scripts/backup_policy.py run <policy-id>
    python scripts/backup_policy.py list-runs
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
from deepseek_infra.infra.workspace import backup_executor, backup_policies, backup_scheduler  # noqa: E402


def _create(args: argparse.Namespace) -> dict[str, object]:
    recipients = list(args.recipient or [])
    if not recipients:
        recipients = [getpass.getpass("公开 age1... Recipient: ").strip()]
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "name": args.name,
        "enabled": not args.disabled,
        "schedule": {
            "cron": args.cron,
            "timezone": args.timezone,
            "misfirePolicy": args.misfire_policy,
            "catchupWindowSeconds": args.catchup_window_seconds,
            "jitterSeconds": args.jitter_seconds,
        },
        "scope": {
            "mode": "project" if args.project else "full",
            "projectIds": list(args.project or []),
            "includeHistory": not args.no_history,
            "includeExternalState": not args.no_external_state,
            "coveragePolicy": args.coverage_policy,
        },
        "frontendMirror": {"mode": args.mirror_mode, "maxAgeSeconds": args.mirror_max_age_seconds},
        "protection": {"mode": "age-recipient", "recipients": recipients},
        "targetId": args.target,
        "retentionPolicyId": args.retention_policy_id,
    }
    return backup_policies.create_policy(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage scheduled backup policies")
    commands = parser.add_subparsers(dest="command", required=True)
    create_cmd = commands.add_parser("create", help="create a scheduled backup policy")
    create_cmd.add_argument("--name", default="定时备份")
    create_cmd.add_argument("--cron", required=True)
    create_cmd.add_argument("--timezone", required=True)
    create_cmd.add_argument("--misfire-policy", choices=["skip", "run-once"], default="skip")
    create_cmd.add_argument("--catchup-window-seconds", type=int, default=86400)
    create_cmd.add_argument("--jitter-seconds", type=int, default=0)
    create_cmd.add_argument("--recipient", action="append")
    create_cmd.add_argument("--target", default="managed-local")
    create_cmd.add_argument("--retention-policy-id", default="default")
    create_cmd.add_argument("--mirror-mode", choices=["required", "best-effort", "excluded"], default="best-effort")
    create_cmd.add_argument("--mirror-max-age-seconds", type=int, default=3600)
    create_cmd.add_argument("--coverage-policy", choices=["strict", "best-effort"], default="strict")
    create_cmd.add_argument("--project", action="append")
    create_cmd.add_argument("--no-history", action="store_true")
    create_cmd.add_argument("--no-external-state", action="store_true")
    create_cmd.add_argument("--disabled", action="store_true")
    commands.add_parser("list", help="list policies")
    run_cmd = commands.add_parser("run", help="run a policy now")
    run_cmd.add_argument("policy_id")
    runs_cmd = commands.add_parser("list-runs", help="list recent runs")
    runs_cmd.add_argument("--policy-id", default=None)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = _create(args)
        elif args.command == "list":
            result = {"policies": backup_policies.list_policies()}
        elif args.command == "run":
            policy = backup_policies.get_policy(args.policy_id)
            instance_id = backup_scheduler.instance_id_from_environment()
            run = backup_scheduler.claim_manual_run(policy, instance_id=instance_id)
            result = backup_executor.execute_run(run, instance_id=instance_id)
        else:
            result = {"runs": backup_scheduler.list_runs(policy_id=args.policy_id)}
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
