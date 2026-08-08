"""Scheduled backup run executor (4.4.5).

Drives a claimed run through its phase state machine: restore-fence check,
target check, mirror check, federated snapshot, unattended encryption and
verification, atomic publication, cataloging and retention. A
:class:`backup_scheduler.RunLeaseGuard` renews the lease on a heartbeat for
the whole run; every step that touches shared state checkpoints the guard
against the current clock, so a worker whose lease expired mid-run can never
reach a visible commit step.
"""

from __future__ import annotations

from collections.abc import Callable
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
    backup_writer_lease,
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


def _blocked_target_outcome(
    run: backup_scheduler.ClaimedRun,
    policy: dict[str, Any],
    current: datetime,
    guard: backup_scheduler.RunLeaseGuard,
    message: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    schedule_cfg = policy.get("schedule") or {}
    catchup = int(schedule_cfg.get("catchupWindowSeconds") or 86400)
    try:
        slot_time = datetime.fromisoformat(run.scheduled_for.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        slot_time = current
    terminal = run.attempt >= _max_attempts(policy) or slot_time < current - timedelta(seconds=catchup)
    delay = _retry_delay_seconds(policy, run.attempt)
    backup_scheduler.block_run(
        run.run_id,
        instance_id=guard.instance_id,
        fencing_token=run.fencing_token,
        error=message,
        reason="blocked-target-unavailable",
        retry_at=None if terminal else current + timedelta(seconds=delay),
        terminal=terminal,
        now=guard.now(),
    )
    result = {**outcome, "phase": "blocked-terminal" if terminal else "blocked-retryable", "reason": "blocked-target-unavailable"}
    if not terminal:
        result["retryInSeconds"] = delay
    return result


def _anchored_clock(anchor: datetime, started_at: datetime) -> Callable[[], datetime]:
    return lambda: anchor + (datetime.now(tz=timezone.utc) - started_at)


def execute_run(
    run: backup_scheduler.ClaimedRun,
    *,
    instance_id: str,
    now: datetime | None = None,
    lease_seconds: int = backup_scheduler.DEFAULT_LEASE_SECONDS,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(tz=timezone.utc)
    if clock is None:
        clock = (lambda: datetime.now(tz=timezone.utc)) if now is None else _anchored_clock(current, datetime.now(tz=timezone.utc))
    outcome: dict[str, Any] = {"runId": run.run_id, "policyId": run.policy_id}
    try:
        policy = backup_policies.get_policy(run.policy_id)
    except AppError:
        backup_scheduler.fail_run(run.run_id, error="policy-missing", reason="policy-missing")
        return {**outcome, "phase": "failed", "reason": "policy-missing"}
    guard = backup_scheduler.RunLeaseGuard(run.run_id, instance_id, run.fencing_token, lease_seconds=lease_seconds, clock=clock)
    guard.start_heartbeat()
    writer: backup_writer_lease.TargetWriterLease | None = None
    try:
        if mutation_gate.read_fence(root=_gate_root()) is not None:
            backup_scheduler.fail_run(
                run.run_id,
                error="workspace-restore-active",
                instance_id=instance_id,
                fencing_token=run.fencing_token,
                phase="deferred",
                reason="workspace-restore-active",
                now=guard.now(),
            )
            return {**outcome, "phase": "deferred", "reason": "workspace-restore-active"}
        guard.checkpoint()
        backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        package = backup_scheduled.build_scheduled_backup(
            policy,
            run_id=run.run_id,
            staging_root=backup_scheduler.staging_root(),
            schedule_slot=run.schedule_slot,
            cancel_event=guard.cancel_event,
        )
        guard.checkpoint()
        backup_scheduler.record_run_phase(run.run_id, "verifying", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        backup_scheduler.record_run_phase(run.run_id, "publishing", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        target_id = str(policy.get("targetId") or "managed-local")
        try:
            target = backup_publish.resolve_target(target_id)
        except AppError as exc:
            if "blocked-target-unavailable" in str(exc):
                backup_scheduler.record_target_health(target_id, "blocked", str(exc)[:200])
                return _blocked_target_outcome(run, policy, current, guard, str(exc), outcome)
            raise
        policy_id = str(policy.get("policyId") or "")
        if target.root is not None:
            incomplete = backup_publish.slot_has_incomplete_journal(target.root, policy_id=policy_id, schedule_slot=run.schedule_slot, exclude_run_id=run.run_id)
        else:
            incomplete = backup_publish.slot_has_incomplete_journal_store(target.require_store(), policy_id=policy_id, schedule_slot=run.schedule_slot, exclude_run_id=run.run_id)
        if incomplete:
            backup_scheduler.record_run_phase(run.run_id, "reconciling", instance_id=instance_id, fencing_token=run.fencing_token, reason="interrupted-target-transaction", now=guard.now())
        writer = backup_writer_lease.TargetWriterLease(
            target.root,
            store=target.store if target.root is None else None,
            target_id=target_id,
            owner_run_id=run.run_id,
            owner_instance_id=instance_id,
            fencing_token=run.fencing_token,
            clock=clock,
        )
        writer.acquire()
        guard.attach_writer(writer)
        published = backup_publish.publish_backup(
            target,
            package,
            run_id=run.run_id,
            policy_id=policy_id,
            schedule_slot=run.schedule_slot,
            fencing_token=run.fencing_token,
            checkpoint=guard.checkpoint,
        )
        guard.checkpoint()
        backup_scheduler.record_run_phase(run.run_id, "cataloging", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        if target.root is not None:
            if not published.converged or str(published.receipt.get("backupId") or "") not in backup_catalog.catalog_state(target.root):
                backup_catalog.append_receipt(target.root, published.receipt, writer=writer, precondition=backup_catalog.catalog_precondition(target.root))
            guard.checkpoint()
            backup_scheduler.record_run_phase(run.run_id, "pruning", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
            retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
            policy_timezone = str((policy.get("schedule") or {}).get("timezone") or "UTC")
            backup_retention.apply_retention(retention, target.root, policy_timezone=policy_timezone, now=current, checkpoint=guard.checkpoint, writer=writer)
            backup_retention.finalize_retention(retention, target.root, policy_timezone=policy_timezone, now=current, checkpoint=guard.checkpoint, writer=writer)
        else:
            # Remote catalog/retention use event objects + logical trash (store path).
            backup_catalog.append_receipt_store(target.require_store(), published.receipt, writer=writer)
            guard.checkpoint()
            backup_scheduler.record_run_phase(run.run_id, "pruning", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
            retention = backup_retention.get_retention_policy(str(policy.get("retentionPolicyId") or "default"))
            policy_timezone = str((policy.get("schedule") or {}).get("timezone") or "UTC")
            backup_retention.apply_retention_store(retention, target.require_store(), policy_timezone=policy_timezone, now=current, checkpoint=guard.checkpoint, writer=writer)
            backup_retention.finalize_retention_store(retention, target.require_store(), policy_timezone=policy_timezone, now=current, checkpoint=guard.checkpoint, writer=writer)
        guard.checkpoint()
        filename = str(published.receipt.get("filename") or (published.path.name if published.path is not None else package.filename))
        backup_scheduler.complete_run(
            run.run_id,
            backup_id=str(published.receipt.get("backupId") or package.backup_id),
            filename=filename,
            instance_id=instance_id,
            fencing_token=run.fencing_token,
            now=guard.now(),
        )
        return {**outcome, "phase": "complete", "backupId": str(published.receipt.get("backupId") or package.backup_id), "filename": filename}
    except AppError as exc:
        message = str(exc)
        if exc.status == 499 or (exc.status == 409 and "lease" in message.casefold()):
            return {**outcome, "phase": "abandoned", "error": message}
        if exc.status == 409 and "slot-commit-conflict" in message:
            try:
                backup_scheduler.fail_run(run.run_id, error=message, instance_id=instance_id, fencing_token=run.fencing_token, phase="superseded", reason="slot-commit-conflict", now=guard.now())
            except AppError:
                return {**outcome, "phase": "abandoned", "error": message}
            return {**outcome, "phase": "superseded", "reason": "slot-commit-conflict", "error": message}
        if "blocked-target-unavailable" in message:
            try:
                return _blocked_target_outcome(run, policy, current, guard, message, outcome)
            except AppError:
                return {**outcome, "phase": "abandoned", "error": message}
        if run.attempt < _max_attempts(policy) and exc.status in {409, 423, 500, 502, 503}:
            delay = _retry_delay_seconds(policy, run.attempt)
            try:
                backup_scheduler.requeue_run(
                    run.run_id,
                    instance_id=instance_id,
                    fencing_token=run.fencing_token,
                    retry_at=current + timedelta(seconds=delay),
                    error=message,
                    now=guard.now(),
                )
            except AppError:
                return {**outcome, "phase": "abandoned", "error": message}
            return {**outcome, "phase": "queued", "error": message, "retryInSeconds": delay}
        try:
            backup_scheduler.fail_run(run.run_id, error=message, instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        except AppError:
            return {**outcome, "phase": "abandoned", "error": message}
        return {**outcome, "phase": "failed", "error": message}
    except Exception as exc:  # defensive: unexpected errors must still close the run
        try:
            backup_scheduler.fail_run(run.run_id, error=str(exc), instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        except AppError:
            return {**outcome, "phase": "abandoned", "error": str(exc)}
        return {**outcome, "phase": "failed", "error": str(exc)}
    finally:
        if writer is not None:
            writer.release()
        guard.stop()
        backup_scheduler.cleanup_run_staging(run.run_id)
