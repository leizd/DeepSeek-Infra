"""Generate a small, deterministic Python oracle for the native backup list view."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from deepseek_infra.infra.workspace.backup_scheduler import next_run_for_policy


CASES = (
    ("utc-daily", "2026-06-01T04:00:00Z", {"policyId": "p-utc", "schedule": {"cron": "0 3 * * *", "timezone": "UTC"}}),
    ("singapore-daily", "2026-01-01T00:00:00Z", {"policyId": "p-sgt", "schedule": {"cron": "0 3 * * *", "timezone": "Asia/Singapore"}}),
    ("spring-skip", "2026-03-08T00:00:00Z", {"policyId": "p-gap", "schedule": {"cron": "30 2 * * *", "timezone": "America/New_York"}}),
    ("spring-run-once", "2026-03-08T00:00:00Z", {"policyId": "p-gap", "schedule": {"cron": "30 2 * * *", "timezone": "America/New_York", "misfirePolicy": "run-once"}}),
    ("fall-first-fold", "2026-11-01T00:00:00Z", {"policyId": "p-fold", "schedule": {"cron": "30 1 * * *", "timezone": "America/New_York"}}),
    ("day-or-weekday", "2026-06-07T00:00:00Z", {"policyId": "p-or", "schedule": {"cron": "0 0 1 * 1", "timezone": "UTC"}}),
    ("quarter-hour", "2026-06-01T00:08:00Z", {"policyId": "p-quarter", "schedule": {"cron": "*/15 * * * *", "timezone": "UTC"}}),
    ("jitter", "2026-06-01T00:08:00Z", {"policyId": "p-jitter", "schedule": {"cron": "*/15 * * * *", "timezone": "UTC", "jitterSeconds": 600}}),
    ("invalid-cron", "2026-06-01T00:00:00Z", {"policyId": "p-bad", "schedule": {"cron": "bad", "timezone": "UTC"}}),
    ("unknown-zone", "2026-06-01T00:00:00Z", {"policyId": "p-zone", "schedule": {"cron": "0 3 * * *", "timezone": "Nowhere/Zone"}}),
    ("no-schedule", "2026-06-01T00:00:00Z", {"policyId": "p-none"}),
)


def generate() -> bytes:
    cases = []
    for name, after, policy in CASES:
        now = datetime.fromisoformat(after.replace("Z", "+00:00")).astimezone(timezone.utc)
        cases.append({"name": name, "now": after, "policy": policy, "nextRun": next_run_for_policy(policy, now=now)})
    document = {"schema": "backup-next-run-python-oracle-v1", "cases": cases}
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = generate()
    if args.check:
        if args.output.read_bytes() != expected:
            raise SystemExit("backup next-run Python oracle drift")
        return
    if args.output.exists():
        raise SystemExit("output already exists; use --check or inspect before replacing the frozen oracle")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(expected)


if __name__ == "__main__":
    main()
