"""Durable BackupReplicationJob for Recovery Replica Sets (4.5.2).

Encrypt-once, multi-target publish: Primary commits first; each replica gets an
independent writer lease, target-local Receipt v4 and Commit v4 over the same
ciphertext object digests. Required replica spool retention until terminal.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_object_set,
    backup_publish,
    backup_scheduler,
    backup_spool,
    backup_writer_lease,
)

REPLICATION_DIR = config.ROOT / ".backup-replication"
JOB_SCHEMA_VERSION = 1
TERMINAL_PHASES = frozenset({"committed", "failed", "superseded"})
ACTIVE_PHASES = frozenset(
    {
        "queued",
        "checking-target",
        "transferring-components",
        "components-verified",
        "writing-receipt",
        "committing",
    }
)
MODES = frozenset({"required", "best-effort"})

_LOCK = threading.RLock()


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _job_path(job_id: str) -> Path:
    return REPLICATION_DIR / f"{job_id}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_job(job_id: str) -> dict[str, Any] | None:
    path = _job_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt job file
        return None
    return data if isinstance(data, dict) else None


def list_jobs(
    *,
    policy_id: str | None = None,
    backup_id: str | None = None,
    phase: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not REPLICATION_DIR.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for path in sorted(REPLICATION_DIR.glob("*.json"), reverse=True):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if policy_id and str(data.get("policyId") or "") != policy_id:
            continue
        if backup_id and str(data.get("backupId") or "") != backup_id:
            continue
        if phase and str(data.get("phase") or "") != phase:
            continue
        jobs.append(data)
        if len(jobs) >= limit:
            break
    return jobs


def has_open_required_jobs(*, policy_id: str, slot_digest: str | None = None, backup_id: str | None = None) -> bool:
    for job in list_jobs(policy_id=policy_id, backup_id=backup_id, limit=500):
        if str(job.get("mode") or "") != "required":
            continue
        if str(job.get("phase") or "") in TERMINAL_PHASES:
            continue
        if slot_digest and str(job.get("slotDigest") or "") not in {"", slot_digest}:
            continue
        return True
    return False


def enqueue_replica_jobs(
    *,
    policy: dict[str, Any],
    primary_target_id: str,
    backup_id: str,
    package: Any,
    run_id: str,
    schedule_slot: str,
    slot_digest: str,
    primary_receipt: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Create durable replication jobs for each configured replica target."""
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return []
    targets = list(replication.get("targets") or [])
    if not targets:
        return []
    policy_id = str(policy.get("policyId") or "")
    object_set_digest = ""
    control_digest = ""
    objects: list[dict[str, Any]] = []
    if isinstance(package, backup_object_set.ObjectSetPackage):
        object_set_digest = str(package.object_set_digest or "")
        control_digest = str(package.control.ciphertext_digest or "")
        objects = backup_object_set.remote_object_inventory(package.components)
    elif primary_receipt:
        object_set_digest = str(primary_receipt.get("objectSetDigest") or "")
        control_digest = str(primary_receipt.get("controlObjectDigest") or primary_receipt.get("objectDigest") or "")
        objects = list(primary_receipt.get("objects") or []) if isinstance(primary_receipt.get("objects"), list) else []

    created: list[dict[str, Any]] = []
    with _LOCK:
        REPLICATION_DIR.mkdir(parents=True, exist_ok=True)
        for entry in targets:
            if not isinstance(entry, dict):
                continue
            replica_id = str(entry.get("targetId") or "").strip()
            mode = str(entry.get("mode") or "required")
            if not replica_id or replica_id == primary_target_id:
                continue
            # Idempotent: one open job per (policy, backup, replica)
            existing = [
                j
                for j in list_jobs(policy_id=policy_id, backup_id=backup_id, limit=100)
                if str(j.get("replicaTargetId") or "") == replica_id and str(j.get("phase") or "") not in TERMINAL_PHASES
            ]
            if existing:
                created.append(existing[0])
                continue
            job_id = f"repl_{uuid.uuid4().hex[:16]}"
            job = {
                "schemaVersion": JOB_SCHEMA_VERSION,
                "jobId": job_id,
                "policyId": policy_id,
                "backupId": backup_id,
                "primaryTargetId": primary_target_id,
                "replicaTargetId": replica_id,
                "mode": mode if mode in MODES else "required",
                "phase": "queued",
                "runId": run_id,
                "scheduleSlot": schedule_slot,
                "slotDigest": slot_digest,
                "objectSetDigest": object_set_digest,
                "controlObjectDigest": control_digest,
                "objects": objects,
                "primaryReceiptSnapshot": {
                    k: primary_receipt.get(k)
                    for k in (
                        "snapshotKind",
                        "parentBackupId",
                        "baseBackupId",
                        "lineageId",
                        "chainDepth",
                        "chunkProtocol",
                        "logicalBytes",
                        "size",
                        "storageProtocol",
                        "creationVerified",
                    )
                    if isinstance(primary_receipt, dict) and k in primary_receipt
                }
                if isinstance(primary_receipt, dict)
                else {},
                "createdAt": _utc_iso(),
                "updatedAt": _utc_iso(),
                "error": None,
                "attempts": 0,
            }
            _atomic_write(_job_path(job_id), job)
            created.append(job)
    return created


def _set_phase(job: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
    job = dict(job)
    job["phase"] = phase
    job["updatedAt"] = _utc_iso()
    for key, value in extra.items():
        job[key] = value
    _atomic_write(_job_path(str(job["jobId"])), job)
    return job


def _load_package_from_spool(job: dict[str, Any]) -> Any:
    package = backup_spool.lookup_verified_package(
        policy_id=str(job.get("policyId") or ""),
        slot_digest=str(job.get("slotDigest") or ""),
    )
    if package is None:
        raise AppError(
            "replication spool package missing; cannot re-encrypt for replica",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    return package


def execute_replication_job(job_id: str, *, instance_id: str = "repl-worker") -> dict[str, Any]:
    """Advance one replication job; restart-safe and idempotent."""
    with _LOCK:
        job = read_job(job_id)
        if job is None:
            raise AppError("Replication job not found", code=ErrorCode.NOT_FOUND, status=404)
        if str(job.get("phase") or "") in TERMINAL_PHASES:
            return job
        job = _set_phase(job, "checking-target", attempts=int(job.get("attempts") or 0) + 1)

    replica_target_id = str(job.get("replicaTargetId") or "")
    policy_id = str(job.get("policyId") or "")
    backup_id = str(job.get("backupId") or "")
    schedule_slot = str(job.get("scheduleSlot") or f"replica/{backup_id}")
    mode = str(job.get("mode") or "required")

    try:
        target = backup_publish.resolve_target(replica_target_id)
    except Exception as exc:
        return _fail_job(job, exc, mode=mode)

    try:
        package = _load_package_from_spool(job)
        job = _set_phase(job, "transferring-components")
        fencing = backup_scheduler.allocate_fencing_token()
        writer = backup_writer_lease.TargetWriterLease(
            target.root,
            store=target.store if target.root is None else None,
            target_id=replica_target_id,
            owner_run_id=str(job.get("runId") or job_id),
            owner_instance_id=instance_id,
            fencing_token=fencing,
        )
        writer.acquire()
        try:
            job = _set_phase(job, "writing-receipt")
            # Target-specific Receipt v4 from same package — never copy primary receipt bytes.
            published = backup_publish.publish_backup(
                target,
                package,
                run_id=str(job.get("runId") or job_id),
                policy_id=policy_id,
                schedule_slot=schedule_slot,
                fencing_token=fencing,
            )
            job = _set_phase(job, "committing")
            # Verify ciphertext identity preserved
            if isinstance(package, backup_object_set.ObjectSetPackage):
                pub_digest = str((published.receipt or {}).get("objectSetDigest") or "")
                if pub_digest and pub_digest != str(package.object_set_digest):
                    raise AppError("replica objectSetDigest diverged from primary package", code=ErrorCode.INTERNAL, status=500)
                for component in package.components:
                    # content-addressed path identity is package digest
                    if not component.ciphertext_digest:
                        raise AppError("replica component missing ciphertext digest", code=ErrorCode.INTERNAL, status=500)
            job = _set_phase(
                job,
                "committed",
                receipt=published.receipt,
                commit=published.commit,
                converged=bool(published.converged),
                error=None,
            )
            try:
                backup_dr_ledger.record_logical_recovery_copy(
                    target_id=replica_target_id,
                    policy_id=policy_id,
                    backup_id=backup_id,
                    committed_at=str((published.commit or {}).get("committedAt") or (published.receipt or {}).get("createdAt") or _utc_iso()),
                    object_set_digest=str((published.receipt or {}).get("objectSetDigest") or job.get("objectSetDigest") or "") or None,
                    recoverable=True,
                    role="replica",
                    mode=mode,
                    snapshot_kind=str((published.receipt or {}).get("snapshotKind") or "full"),
                )
            except Exception:  # pragma: no cover - ledger best-effort
                pass
            return job
        finally:
            try:
                writer.release()
            except Exception:  # pragma: no cover - release best-effort
                pass
    except Exception as exc:
        return _fail_job(job, exc, mode=mode)


def _fail_job(job: dict[str, Any], exc: BaseException, *, mode: str) -> dict[str, Any]:
    message = str(exc)[:500]
    # best-effort may remain failed without blocking primary
    return _set_phase(job, "failed", error=message, mode=mode)


def process_pending_jobs(*, instance_id: str = "repl-worker", limit: int = 10) -> dict[str, int]:
    """Drain a bounded batch of queued/active replication jobs."""
    pending = [
        j
        for j in list_jobs(limit=500)
        if str(j.get("phase") or "") in ACTIVE_PHASES or str(j.get("phase") or "") == "queued"
    ]
    processed = failed = committed = 0
    for job in pending[:limit]:
        result = execute_replication_job(str(job["jobId"]), instance_id=instance_id)
        processed += 1
        phase = str(result.get("phase") or "")
        if phase == "committed":
            committed += 1
        elif phase == "failed":
            failed += 1
    return {"processed": processed, "committed": committed, "failed": failed}


def replication_compliance(
    *,
    policy: dict[str, Any],
    backup_id: str,
) -> dict[str, Any]:
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"enabled": False, "compliance": "healthy", "committedCopies": 1, "requiredCopies": 1}
    required = int(replication.get("minCommittedCopies") or 1)
    policy_id = str(policy.get("policyId") or "")
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
    committed = [c for c in copies if c.get("recoverable")]
    jobs = list_jobs(policy_id=policy_id, backup_id=backup_id, limit=100)
    open_required = [
        j for j in jobs if str(j.get("mode")) == "required" and str(j.get("phase") or "") not in TERMINAL_PHASES
    ]
    failed_required = [
        j for j in jobs if str(j.get("mode")) == "required" and str(j.get("phase") or "") == "failed"
    ]
    compliance = "healthy"
    if len(committed) < required or open_required or failed_required:
        compliance = "degraded"
    return {
        "enabled": True,
        "compliance": compliance,
        "committedCopies": len(committed),
        "requiredCopies": required,
        "healthyCopies": len(committed),
        "openRequiredJobs": len(open_required),
        "failedRequiredJobs": len(failed_required),
        "available": len(committed) >= 1,
    }
