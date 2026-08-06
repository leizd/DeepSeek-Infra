"""Scheduled backup run executor (4.4.4).

Drives a claimed run through its phase state machine: restore-fence check,
target check, mirror check, federated snapshot, unattended encryption and
verification, atomic publication, cataloging and retention. Every step that
touches shared state first asserts the run lease so a stale worker can never
publish over a newer owner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_policies,
    backup_publish,
    backup_retention,
    backup_scheduled,
    backup_scheduler,
    backups,
    mutation_gate,
)

def _gate_root() -> Path:
    return backups.BACKUP_DIR.parent


def _retry_section(policy: dict[str, Any]) -> dict[str, Any]:
    value = policy.get("retry")
    return value if isinstance(value, dict) else {}


def _retry_delay_seconds(policy: dict[str, Any], attempt: int) -> int:
    retry = _retry_section(policy)
    initial = int(retry.get("initialBackoffSeconds") or 60)
    maximum = int(retry.get("maxBackoffSeconds") or 900)
    return min(maximum, initial * (2 ** max(0, attempt - 1)))


def _max_attempts(policy: dict[str, Any]) -> int:
    return max(1, int(_retry_section(policy).get("maxAttempts") or 3))


def execute_run(run: backup_scheduler.ClaimedRun, *, instance_id: str, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(tz=timezone.utc)
    outcome: dict[str, Any] = {"runId": run.run_id, "policyId": run.policy_id}
    try:
        policy = backup_policies.get_policy(run.policy_id)
    except AppError:
        backup_scheduler.fail_run(run.run_id, error="policy-missing", reason="policy-missing")
        return {**outcome, "phase": "failed", "reason": "policy-missing"}
    try:
        if mutation_gate.read_fence(root=_gate_root()) is not None:
            backup_scheduler.fail_run(
                run.run_id,
                error="workspace-restore-active",
                instance_id=instance_id,
                fencing_token=run.fencing_token,
                phase="deferred",
                reason="workspace-restore-active",
            )
            return {**outcome, "phase": "deferred", "reason": "workspace-restore-active"}
        backup_scheduler.assert_run_lease(run.run_id, instance_id, run.fencing_token, now=current)
        backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id=instance_id, fencing_token=run.fencing_token)
        package = backup_scheduled.build_scheduled_backup(
            policy,
            run_id=run.run_id,
            staging_root=backup_scheduler.staging_root(),
            schedule_slot=run.schedule_slot,
        )
        backup_scheduler.assert_run_lease(run.run_id, instance_id, run.fencing_token, now=current)
        backup_scheduler.record_run_phase(run.run_id, "verifying", instance_id=instance_id, fencing_token=run.fencing_token)
        backup_scheduler.record_run_phase(run.run_id, "publishing", instance_id=instance_id, fencing_token=run.fencing_token)
        target_id = str(policy.get("targetId") or "managed-local")
        try:
            target = backup_publish.resolve_target(target_id)
        except AppError as exc:
            if "blocked-target-unavailable" in str(exc):
                backup_scheduler.record_target_health(target_id, "blocked", str(exc)[:200])
                backup_scheduler.fail_run(
                    run.run_id,
                    error=str(exc),
                    instance_id=instance_id,
                    fencing_token=run.fencing_token,
                    phase="blocked",
                    reason="blocked-target-unavailable",
                )
                return {**outcome, "phase": "blocked", "reason": "blocked-target-unavailable"}
            raise
        published = backup_publish.publish_backup(
            target,
            package,
            run_id=run.run_id,
            policy_id=str(policy.get("policyId") or ""),
            schedule_slot=run.schedule_slot,
        )
        backup_scheduler.assert_run_lease(run.run_id, instance_id, run.fencing_token, now=current)
        backup_scheduler.record_run_phase(run.run_id, "cataloging", instance_id=instance_id, fencing_token=run.fencing_token)
        backup_catalog.append_receipt(target.root, published.receipt)
        backup_scheduler.assert_run_lease(run.run_id, instance_id, run.fencing_token, now=current)
        backup_scheduler.record_run_phase(run.run_id, "pruning", instance_id=instance_id, fencing_token=run.fencing_token)
        retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
        policy_timezone = str((policy.get("schedule") or {}).get("timezone") or "UTC")
        backup_retention.apply_retention(retention, target.root, policy_timezone=policy_timezone, now=current)
        backup_retention.finalize_retention(retention, target.root, policy_timezone=policy_timezone, now=current)
        backup_scheduler.assert_run_lease(run.run_id, instance_id, run.fencing_token, now=current)
        backup_scheduler.complete_run(
            run.run_id,
            backup_id=package.backup_id,
            filename=published.path.name,
            instance_id=instance_id,
            fencing_token=run.fencing_token,
        )
        return {**outcome, "phase": "complete", "backupId": package.backup_id, "filename": published.path.name}
    except AppError as exc:
        if exc.status == 409 and "lease" in str(exc).casefold():
            backup_scheduler.cleanup_run_staging(run.run_id)
            return {**outcome, "phase": "abandoned", "error": str(exc)}
        if run.attempt < _max_attempts(policy) and exc.status in {409, 423, 500, 502, 503}:
            delay = _retry_delay_seconds(policy, run.attempt)
            try:
                backup_scheduler.requeue_run(
                    run.run_id,
                    instance_id=instance_id,
                    fencing_token=run.fencing_token,
                    retry_at=current + timedelta(seconds=delay),
                    error=str(exc),
                )
            except AppError:
                return {**outcome, "phase": "abandoned", "error": str(exc)}
            return {**outcome, "phase": "queued", "error": str(exc), "retryInSeconds": delay}
        backup_scheduler.fail_run(run.run_id, error=str(exc), instance_id=instance_id, fencing_token=run.fencing_token)
        return {**outcome, "phase": "failed", "error": str(exc)}
    except Exception as exc:  # defensive: unexpected errors must still close the run
        backup_scheduler.fail_run(run.run_id, error=str(exc), instance_id=instance_id, fencing_token=run.fencing_token)
        return {**outcome, "phase": "failed", "error": str(exc)}
    finally:
        backup_scheduler.cleanup_run_staging(run.run_id)
