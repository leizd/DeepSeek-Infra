"""Scheduled backup run executor (4.4.7).

Drives a claimed run through its phase state machine. The first attempt freezes
a :class:`backup_run_plan` for the schedule slot; later retries reuse that plan
and any verified spool ciphertext instead of re-snapshotting and re-encrypting.
A :class:`backup_scheduler.RunLeaseGuard` renews the lease on a heartbeat for
the whole run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_incremental,
    backup_policies,
    backup_publish,
    backup_run_plan,
    backup_retention,
    backup_scheduled,
    backup_scheduler,
    backup_spool,
    backup_writer_lease,
    backups,
    mutation_gate,
)
from deepseek_infra.infra.workspace.backup_target_store import commit_slot_digest


def _gate_root() -> Path:
    return backups.BACKUP_DIR.parent


def _index_available() -> bool:
    try:
        return backup_incremental.INDEX_DB.exists() and backup_incremental.INDEX_DB.stat().st_size > 0
    except OSError:
        return False


def _record_committed_index(
    *,
    target_id: str,
    policy_id: str,
    backup_id: str,
    package: Any,
    run_plan: dict[str, Any],
) -> None:
    """Persist a committed snapshot into the rebuildable index (best effort)."""
    try:
        manifest = getattr(package, "manifest", None) or {}
        files = manifest.get("files") or []
        records = [
            backup_incremental.FileRecord(
                contributor_id=str(item.get("contributorId") or ""),
                logical_path=str(item["path"]),
                size=int(item["size"]),
                sha256=str(item["sha256"]),
            )
            for item in files
        ]
        snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        snapshot_kind = str(run_plan.get("snapshotKind") or "full")
        if snapshot_kind == "incremental":
            parent = str(run_plan.get("parentBackupId") or "")
            base = str(run_plan.get("baseBackupId") or parent)
            depth = int(run_plan.get("chainDepth") or 1)
            root = str(snapshot.get("rootDigest") or backup_incremental.snapshot_root(records))
            # Store the effective tree (current + inherited) for lineage continuity.
            previous = []
            if parent:
                previous = backup_incremental.load_snapshot_files(target_id, policy_id, parent)
            effective = backup_incremental.effective_current(
                previous,
                records,
                successful_contributors={item.contributor_id for item in records},
            )
            backup_incremental.record_committed_snapshot(
                target_id=target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                parent_backup_id=parent or None,
                base_backup_id=base or None,
                chain_depth=depth,
                root_digest=root,
                files=effective,
            )
        else:
            backup_incremental.record_committed_snapshot(
                target_id=target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                parent_backup_id=None,
                base_backup_id=backup_id,
                chain_depth=0,
                root_digest=backup_incremental.snapshot_root(records),
                files=records,
            )
        chunk_records = getattr(package, "chunk_records", None) or []
        if chunk_records:
            backup_incremental.record_snapshot_chunks(
                target_id=target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                chunks=list(chunk_records),
            )
    except Exception:
        # Index is a performance cache; never fail the run on index errors.
        pass


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


def _target_head_hash(target: backup_publish.ResolvedTarget) -> str:
    if target.root is not None:
        latest = backup_publish.latest_commit(target.root)
        return str(latest.get("commitHash") or ("0" * 64)) if latest else ("0" * 64)
    try:  # pragma: no cover - remote full-executor path
        latest = backup_publish.latest_commit_store(target.require_store())
    except Exception:
        return "0" * 64
    return str(latest.get("commitHash") or ("0" * 64)) if latest else ("0" * 64)


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
    policy_id = str(policy.get("policyId") or "")
    slot_digest = commit_slot_digest(run.schedule_slot)
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
        target_id = str(policy.get("targetId") or "managed-local")
        try:
            target = backup_publish.resolve_target(target_id)
        except AppError as exc:
            if "blocked-target-unavailable" in str(exc) or "unsupported-conditional-target" in str(exc):
                backup_scheduler.record_target_health(target_id, "blocked", str(exc)[:200])
                return _blocked_target_outcome(run, policy, current, guard, str(exc), outcome)
            raise  # pragma: no cover - other resolve errors bubble to outer handler

        # Select snapshot kind / lineage once, then freeze; retries reuse it.
        context = backup_scheduled._context_from_policy(policy)
        contributor_plan = backups._contributor_plan(context)
        index_available = _index_available()
        selected = backup_incremental.select_snapshot_plan(
            policy=policy,
            target_id=target_id,
            policy_id=policy_id,
            index_available=index_available,
        )
        snapshot_kind, lineage_id, parent_backup_id, chain_depth, parent_commit_hash, parent_receipt_digest, force_full_reason = selected
        run_plan = backup_run_plan.freeze_run_plan(
            policy=policy,
            schedule_slot=run.schedule_slot,
            slot_digest=slot_digest,
            contributor_plan=contributor_plan,
            target_id=target_id,
            target_head_hash=_target_head_hash(target),
            snapshot_kind=str(snapshot_kind or "full"),
            lineage_id=lineage_id,
            parent_backup_id=parent_backup_id,
            base_backup_id=(lineage_id if snapshot_kind == "incremental" else None),
            chain_depth=int(chain_depth or 0),
            parent_commit_hash=parent_commit_hash,
            parent_receipt_digest=parent_receipt_digest,
            force_full_reason=force_full_reason,
        )
        outcome["runPlanDigest"] = str(run_plan.get("runPlanDigest") or "")
        outcome["backupId"] = str(run_plan.get("backupId") or "")
        outcome["snapshotKind"] = str(run_plan.get("snapshotKind") or "full")
        if run_plan.get("forceFullReason"):
            outcome["forceFullReason"] = str(run_plan["forceFullReason"])

        # A reclaimed run whose slot is already committed by a different worker
        # must not reuse the frozen plan/spool: keep slot-commit-conflict
        # semantics by rebuilding a distinct package identity.
        conflicting_slot = False
        if target.root is not None:
            marker = backup_publish.find_commit_marker_path(target.root, policy_id, run.schedule_slot)
            if marker is not None and marker.is_file():
                import json as _json

                try:
                    existing_marker = _json.loads(marker.read_text(encoding="utf-8"))
                except Exception:  # pragma: no cover
                    existing_marker = {}
                if existing_marker and str(existing_marker.get("runId") or "") != run.run_id:
                    conflicting_slot = True
        if conflicting_slot:
            backup_run_plan.clear_run_plan(policy_id, slot_digest)
            backup_spool.clear_slot(policy_id, slot_digest)

        package: Any | None = None
        spooled = None
        if not conflicting_slot:
            spooled = backup_spool.lookup_verified_package(
                policy_id=policy_id,
                slot_digest=slot_digest,
                run_plan_digest=str(run_plan.get("runPlanDigest") or ""),
            )
        if spooled is not None:
            backup_scheduler.record_run_phase(
                run.run_id,
                "publishing",
                instance_id=instance_id,
                fencing_token=run.fencing_token,
                reason="verified-spool-reused",
                now=guard.now(),
            )
            package = spooled
            outcome["spoolReused"] = True
        else:
            backup_scheduler.record_run_phase(run.run_id, "snapshotting", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
            package = backup_scheduled.build_scheduled_backup(
                policy,
                run_id=run.run_id,
                staging_root=backup_scheduler.staging_root(),
                schedule_slot=run.schedule_slot,
                cancel_event=guard.cancel_event,
                backup_id=None if conflicting_slot else str(run_plan.get("backupId") or ""),
                contributor_plan=contributor_plan,
                snapshot_kind=str(run_plan.get("snapshotKind") or "full"),
                parent_backup_id=run_plan.get("parentBackupId"),
                base_backup_id=run_plan.get("baseBackupId"),
                lineage_id=run_plan.get("lineageId"),
                chain_depth=int(run_plan.get("chainDepth") or 0),
            )
            # Persist verified ciphertext before publish so retries can resume.
            backup_spool.store_verified_package(
                package,
                policy_id=policy_id,
                schedule_slot=run.schedule_slot,
                run_id=run.run_id,
                slot_digest=slot_digest,
                run_plan_digest=str(run_plan.get("runPlanDigest") or ""),
            )
            outcome["spoolReused"] = False
            guard.checkpoint()
            backup_scheduler.record_run_phase(run.run_id, "verifying", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
            backup_scheduler.record_run_phase(run.run_id, "publishing", instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())

        if target.root is not None:
            incomplete = backup_publish.slot_has_incomplete_journal(target.root, policy_id=policy_id, schedule_slot=run.schedule_slot, exclude_run_id=run.run_id)
        else:  # pragma: no cover - remote full-executor path
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
        else:  # pragma: no cover - remote full-executor path requires a live adapter
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
        # Successful commit: persist index lineage (best effort), then clear plan.
        _record_committed_index(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=str(published.receipt.get("backupId") or package.backup_id),
            package=package,
            run_plan=run_plan,
        )
        backup_run_plan.clear_run_plan(policy_id, slot_digest)
        return {
            **outcome,
            "phase": "complete",
            "backupId": str(published.receipt.get("backupId") or package.backup_id),
            "filename": filename,
        }
    except AppError as exc:
        message = str(exc)
        if exc.status == 499 or (exc.status == 409 and "lease" in message.casefold()):
            return {**outcome, "phase": "abandoned", "error": message}
        if exc.status == 409 and "slot-commit-conflict" in message:
            try:
                backup_scheduler.fail_run(run.run_id, error=message, instance_id=instance_id, fencing_token=run.fencing_token, phase="superseded", reason="slot-commit-conflict", now=guard.now())
            except AppError:  # pragma: no cover
                return {**outcome, "phase": "abandoned", "error": message}
            backup_run_plan.clear_run_plan(policy_id, slot_digest)
            backup_spool.clear_slot(policy_id, slot_digest)
            return {**outcome, "phase": "superseded", "reason": "slot-commit-conflict", "error": message}
        if "blocked-target-unavailable" in message:
            try:
                return _blocked_target_outcome(run, policy, current, guard, message, outcome)
            except AppError:  # pragma: no cover
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
            except AppError:  # pragma: no cover
                return {**outcome, "phase": "abandoned", "error": message}
            return {**outcome, "phase": "queued", "error": message, "retryInSeconds": delay}
        try:
            backup_scheduler.fail_run(run.run_id, error=message, instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        except AppError:  # pragma: no cover
            return {**outcome, "phase": "abandoned", "error": message}
        return {**outcome, "phase": "failed", "error": message}
    except Exception as exc:  # defensive: unexpected errors must still close the run
        try:
            backup_scheduler.fail_run(run.run_id, error=str(exc), instance_id=instance_id, fencing_token=run.fencing_token, now=guard.now())
        except AppError:  # pragma: no cover
            return {**outcome, "phase": "abandoned", "error": str(exc)}
        return {**outcome, "phase": "failed", "error": str(exc)}
    finally:
        if writer is not None:
            writer.release()
        guard.stop()
        backup_scheduler.cleanup_run_staging(run.run_id)
