"""Durable BackupReplicationJob and Replica Self-Healing Reconciler (4.5.3).

Encrypt-once, multi-target publish: Primary commits first; each replica gets an
independent writer lease, target-local Receipt v4 and Commit v4 over the same
ciphertext object digests. Required replica spool retention until durable repair
or healthy copy exists.

Desired-State Self-Healing Reconciler:
Continuous convergence between desired copies and observed copies.
Repairs work purely on the ciphertext plane (Zero Age decrypt, Zero Age encrypt).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
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
HOLDS_DIR = REPLICATION_DIR / "holds"
REPAIRS_DIR = REPLICATION_DIR / "repairs"
JOB_SCHEMA_VERSION = 2

TERMINAL_PHASES = frozenset({"committed", "failed-terminal", "failed", "superseded"})
ACTIVE_PHASES = frozenset(
    {
        "queued",
        "checking-target",
        "transferring-components",
        "components-verified",
        "writing-receipt",
        "committing",
        "retry-wait",
        "repair-needed",
    }
)
MODES = frozenset({"required", "best-effort"})

_LOCK = threading.RLock()


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _job_path(job_id: str) -> Path:
    return REPLICATION_DIR / f"{job_id}.json"


def _repair_job_path(repair_id: str) -> Path:
    return REPAIRS_DIR / f"{repair_id}.json"


def _hold_path(hold_id: str) -> Path:
    return HOLDS_DIR / f"{hold_id}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# ── Source Hold Mechanism ───────────────────────────────────────────────────


class SourceHold:
    """Protects a healthy recovery copy from being pruned or deleted during repair."""

    def __init__(self, hold_id: str, target_id: str, policy_id: str, backup_id: str, holder_id: str):
        self.hold_id = hold_id
        self.target_id = target_id
        self.policy_id = policy_id
        self.backup_id = backup_id
        self.holder_id = holder_id
        self.created_at = _utc_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdId": self.hold_id,
            "targetId": self.target_id,
            "policyId": self.policy_id,
            "backupId": self.backup_id,
            "holderId": self.holder_id,
            "createdAt": self.created_at,
        }

    def release(self) -> None:
        release_source_hold(self)


def acquire_source_hold(
    target_id: str,
    policy_id: str,
    backup_id: str,
    holder_id: str,
) -> SourceHold:
    with _LOCK:
        HOLDS_DIR.mkdir(parents=True, exist_ok=True)
        hold_id = f"hold_{target_id}_{policy_id}_{backup_id}_{uuid.uuid4().hex[:8]}"
        hold = SourceHold(hold_id, target_id, policy_id, backup_id, holder_id)
        _atomic_write(_hold_path(hold_id), hold.to_dict())
        return hold


def release_source_hold(hold: SourceHold | str) -> None:
    hold_id = hold.hold_id if isinstance(hold, SourceHold) else str(hold)
    with _LOCK:
        path = _hold_path(hold_id)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def is_source_held(target_id: str, policy_id: str, backup_id: str) -> bool:
    with _LOCK:
        if not HOLDS_DIR.is_dir():
            return False
        for path in HOLDS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if (
                    str(data.get("targetId")) == target_id
                    and str(data.get("policyId")) == policy_id
                    and str(data.get("backupId")) == backup_id
                ):
                    return True
            except Exception:
                continue
    return False


# ── Target-Local Catalog Append ─────────────────────────────────────────────


def append_target_local_catalog(target: Any, receipt: dict[str, Any]) -> None:
    """Append receipt metadata to target-local catalog."""
    policy_id = str(receipt.get("policyId") or "default")
    backup_id = str(receipt.get("backupId") or "")
    if not backup_id:
        return
    if target.root is not None:
        cats_dir = target.root / "catalogs"
        cats_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
        cat_file = cats_dir / f"{policy_id}.jsonl"
        with cat_file.open("a", encoding="utf-8") as h:
            h.write(line)
            h.flush()
    elif target.store is not None:
        cat_key = f"catalogs/{policy_id}/{backup_id}.json"
        target.store.put_bytes(cat_key, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


# ── Replication Job CRUD ────────────────────────────────────────────────────


def read_job(job_id: str) -> dict[str, Any] | None:
    path = _job_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover
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
                "maxAttempts": 5,
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
                    if not component.ciphertext_digest:
                        raise AppError("replica component missing ciphertext digest", code=ErrorCode.INTERNAL, status=500)

            # Append to target-local catalog
            append_target_local_catalog(target, published.receipt or {})

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
            except Exception:  # pragma: no cover
                pass
            return job
        finally:
            try:
                writer.release()
            except Exception:  # pragma: no cover
                pass
    except Exception as exc:
        return _fail_job(job, exc, mode=mode)


def _fail_job(job: dict[str, Any], exc: BaseException, *, mode: str) -> dict[str, Any]:
    message = str(exc)[:500]
    attempts = int(job.get("attempts") or 1)
    max_attempts = int(job.get("maxAttempts") or 5)

    if mode == "required":
        if "spool" in message.casefold() and "missing" in message.casefold():
            return _set_phase(job, "repair-needed", error=message, attempts=attempts)
        if attempts < max_attempts:
            backoff_sec = min(300, 2 ** min(attempts, 8))
            next_retry = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=backoff_sec))
            return _set_phase(job, "retry-wait", error=message, attempts=attempts, nextRetryAt=next_retry)
        return _set_phase(job, "failed-terminal", error=message, attempts=attempts)
    return _set_phase(job, "failed", error=message, attempts=attempts)


def process_pending_jobs(*, instance_id: str = "repl-worker", limit: int = 10) -> dict[str, int]:
    """Drain a bounded batch of queued/active replication jobs."""
    pending = [
        j
        for j in list_jobs(limit=500)
        if str(j.get("phase") or "") in ACTIVE_PHASES or str(j.get("phase") or "") == "queued"
    ]
    processed = failed = committed = 0
    for job in pending[:limit]:
        phase = str(job.get("phase") or "")
        if phase == "retry-wait":
            next_retry = job.get("nextRetryAt")
            if next_retry and _parse_iso(next_retry) and _parse_iso(next_retry) > datetime.now(tz=timezone.utc):  # type: ignore[operator]
                continue
        result = execute_replication_job(str(job["jobId"]), instance_id=instance_id)
        processed += 1
        res_phase = str(result.get("phase") or "")
        if res_phase == "committed":
            committed += 1
        elif res_phase in {"failed", "failed-terminal"}:
            failed += 1
    return {"processed": processed, "committed": committed, "failed": failed}


# ── Ciphertext-Plane Replica Self-Healing (ReplicaRepairJob) ────────────────


def execute_replica_repair(
    *,
    policy_id: str,
    backup_id: str,
    dest_target_id: str,
    source_target_id: str | None = None,
    run_id: str | None = None,
    instance_id: str = "healer-worker",
) -> dict[str, Any]:
    """Repair missing/corrupted replica copy purely on the ciphertext plane.

    Age decrypt count = 0
    Age encrypt count = 0
    """
    if backup_dr_ledger.is_logical_recovery_point_retired(policy_id, backup_id):
        return {"status": "skipped", "reason": "retired", "policyId": policy_id, "backupId": backup_id}

    r_id = run_id or f"repair_{uuid.uuid4().hex[:12]}"
    start_time = time.monotonic()

    # 1. Resolve healthy source
    if source_target_id is None:
        copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
        healthy = [
            c for c in copies
            if str(c.get("targetId")) != dest_target_id and c.get("recoverable") and c.get("state") == "healthy"
        ]
        if not healthy:
            raise AppError(
                f"No healthy source copy available to repair {dest_target_id} for {backup_id}",
                code=ErrorCode.NOT_FOUND,
                status=404,
            )
        source_target_id = str(healthy[0]["targetId"])

    # 2. Acquire SourceHold to protect source from pruning
    hold = acquire_source_hold(source_target_id, policy_id, backup_id, holder_id=r_id)
    try:
        source_target = backup_publish.resolve_target(source_target_id)
        dest_target = backup_publish.resolve_target(dest_target_id)

        # 3. Acquire WriterLease on destination
        fencing = backup_scheduler.allocate_fencing_token()
        writer = backup_writer_lease.TargetWriterLease(
            dest_target.root,
            store=dest_target.store if dest_target.root is None else None,
            target_id=dest_target_id,
            owner_run_id=r_id,
            owner_instance_id=instance_id,
            fencing_token=fencing,
        )
        writer.acquire()
        try:
            # 4. Read source receipt
            r_key = f"receipts/{backup_id}.json"
            source_raw_receipt: bytes | None = None
            if source_target.root is not None:
                p = source_target.root / "receipts" / f"{backup_id}.json"
                if p.is_file():
                    source_raw_receipt = p.read_bytes()
            elif source_target.store is not None:
                source_raw_receipt = source_target.store.get_bytes(r_key)

            if source_raw_receipt is None:
                raise AppError(f"Source receipt missing: {r_key}", code=ErrorCode.NOT_FOUND, status=404)

            source_receipt = json.loads(source_raw_receipt.decode("utf-8"))
            objects = list(source_receipt.get("objects") or [])
            bytes_transferred = 0

            # 5. Component Diff & Ciphertext Stream
            for obj in objects:
                digest = str(obj.get("digest") or "")
                if not digest:
                    continue
                comp_rel = f"objects/{digest[:2]}/{digest[2:4]}/{digest}.age"
                control_rel = f"control/{digest}.age"

                # Check if component exists in source
                source_bytes: bytes | None = None
                is_control = False
                if source_target.root is not None:
                    cp = source_target.root / comp_rel
                    ctrl_p = source_target.root / "control" / f"{digest}.age"
                    if cp.is_file():
                        source_bytes = cp.read_bytes()
                    elif ctrl_p.is_file():
                        source_bytes = ctrl_p.read_bytes()
                        is_control = True
                elif source_target.store is not None:
                    source_bytes = source_target.store.get_bytes(comp_rel)
                    if source_bytes is None:
                        source_bytes = source_target.store.get_bytes(control_rel)
                        if source_bytes is not None:
                            is_control = True

                if source_bytes is None:
                    continue

                # Verify source digest
                calc_digest = hashlib.sha256(source_bytes).hexdigest()
                if calc_digest != digest:
                    raise AppError(f"Source component corrupt: {digest}", code=ErrorCode.INTERNAL, status=500)

                # Check destination
                target_rel = control_rel if is_control else comp_rel
                dest_needs_stream = True
                dest_bytes: bytes | None = None
                if dest_target.root is not None:
                    dp = dest_target.root / target_rel
                    if dp.is_file():
                        dest_bytes = dp.read_bytes()
                elif dest_target.store is not None:
                    dest_bytes = dest_target.store.get_bytes(target_rel)

                if dest_bytes is not None:
                    if hashlib.sha256(dest_bytes).hexdigest() == digest:
                        dest_needs_stream = False
                    else:
                        # Quarantine corrupted destination component
                        if dest_target.root is not None:
                            q_dir = dest_target.root / ".quarantine"
                            q_dir.mkdir(parents=True, exist_ok=True)
                            (dest_target.root / target_rel).rename(q_dir / f"{digest}.corrupt.{time.time_ns()}")
                        elif dest_target.store is not None:
                            dest_target.store.put_if_absent(f".quarantine/{digest}.corrupt.{time.time_ns()}", dest_bytes)

                if dest_needs_stream:
                    # Pure stream: NO decrypt, NO encrypt
                    if dest_target.root is not None:
                        dp = dest_target.root / target_rel
                        dp.parent.mkdir(parents=True, exist_ok=True)
                        dp.write_bytes(source_bytes)
                    elif dest_target.store is not None:
                        dest_target.store.put_if_absent(target_rel, source_bytes)
                    bytes_transferred += len(source_bytes)

            # 6. Publish target-local receipt & commit marker
            dest_receipt = dict(source_receipt)
            dest_receipt["targetId"] = dest_target_id
            dest_receipt_bytes = (json.dumps(dest_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            dest_receipt_digest = hashlib.sha256(dest_receipt_bytes).hexdigest()

            if dest_target.root is not None:
                rp = dest_target.root / "receipts" / f"{backup_id}.json"
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_bytes(dest_receipt_bytes)
            elif dest_target.store is not None:
                dest_target.store.put_if_absent(f"receipts/{backup_id}.json", dest_receipt_bytes)

            def _next_generation_and_previous(target: Any) -> tuple[int, str]:
                latest = None
                if target.root is not None:
                    latest = backup_publish.latest_commit(target.root)
                elif target.store is not None:
                    latest = backup_publish.latest_commit_store(target.store)
                gen = int(latest["targetGeneration"]) + 1 if latest is not None else 1
                prev_hash = str(latest["commitHash"]) if latest is not None else backup_publish.GENESIS_COMMIT_HASH
                return gen, prev_hash

            # Target-local commit
            schedule_slot = f"repair/{backup_id}"
            gen, prev_hash = _next_generation_and_previous(dest_target)
            commit = {
                "schemaVersion": 4,
                "targetGeneration": gen,
                "previousCommitHash": prev_hash,
                "fencingToken": fencing,
                "runId": r_id,
                "policyId": policy_id,
                "scheduleSlot": schedule_slot,
                "slotDigest": hashlib.sha256(f"{policy_id}:{schedule_slot}".encode("utf-8")).hexdigest(),
                "backupId": backup_id,
                "committedAt": _utc_iso(),
                "receiptDigest": dest_receipt_digest,
                "storageProtocol": "object-set-v1",
                "objectSetDigest": dest_receipt.get("objectSetDigest"),
                "controlObjectDigest": dest_receipt.get("controlObjectDigest"),
            }
            commit_bytes = (json.dumps(commit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            commit["commitHash"] = hashlib.sha256(commit_bytes).hexdigest()

            head_bytes = json.dumps({"latestCommitHash": commit["commitHash"], "targetGeneration": gen}, indent=2).encode("utf-8") + b"\n"
            if dest_target.root is not None:
                cp = dest_target.root / "commits" / policy_id / f"{backup_id}.json"
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_bytes(json.dumps(commit, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                hp = dest_target.root / "control" / "head.json"
                hp.parent.mkdir(parents=True, exist_ok=True)
                hp.write_bytes(head_bytes)
            elif dest_target.store is not None:
                dest_target.store.put_if_absent(f"commits/{policy_id}/{backup_id}.json", json.dumps(commit, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                head_stat = dest_target.store.stat("control/head.json")
                if head_stat is not None:
                    dest_target.store.put_if_match("control/head.json", head_bytes, expected_etag=head_stat.etag)
                else:
                    dest_target.store.put_if_absent("control/head.json", head_bytes)

            append_target_local_catalog(dest_target, dest_receipt)

            # 7. Update DR ledger
            backup_dr_ledger.record_logical_recovery_copy(
                target_id=dest_target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                committed_at=str(commit["committedAt"]),
                object_set_digest=str(dest_receipt.get("objectSetDigest") or "") or None,
                recoverable=True,
                role="replica",
                mode="required",
                state="healthy",
                last_verified_at=_utc_iso(),
                last_repair_at=_utc_iso(),
            )
            duration_ms = (time.monotonic() - start_time) * 1000.0
            backup_dr_ledger.record_stage_sample(
                target_id=dest_target_id,
                stage="repair",
                bytes_transferred=bytes_transferred,
                duration_ms=duration_ms,
                result="success",
            )
            return {
                "status": "success",
                "repairId": r_id,
                "policyId": policy_id,
                "backupId": backup_id,
                "sourceTargetId": source_target_id,
                "destTargetId": dest_target_id,
                "bytesRepaired": bytes_transferred,
                "durationMs": duration_ms,
            }
        finally:
            writer.release()
    finally:
        hold.release()


# ── Desired-State Reconciler ────────────────────────────────────────────────


def reconcile_policy_replicas(
    policy_id: str,
    *,
    instance_id: str = "reconciler-worker",
) -> dict[str, Any]:
    """Reconcile desired replica copies against observed copies in DR Ledger."""
    from deepseek_infra.infra.workspace import backup_policies

    policy = backup_policies.get_policy(policy_id)
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"status": "skipped", "reason": "replication-disabled", "policyId": policy_id}

    target_entries = list(replication.get("targets") or [])
    expected_targets = [str(e["targetId"]) for e in target_entries if isinstance(e, dict) and e.get("targetId")]
    if not expected_targets:
        return {"status": "noop", "policyId": policy_id}

    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=200)
    # Group by backupId
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for c in copies:
        by_backup.setdefault(str(c["backupId"]), []).append(c)

    scanned = 0
    repairs_triggered = 0
    repairs_succeeded = 0
    repairs_failed = 0

    for backup_id, copy_list in by_backup.items():
        if backup_dr_ledger.is_logical_recovery_point_retired(policy_id, backup_id):
            continue
        scanned += 1
        existing_targets = {str(c["targetId"]): c for c in copy_list}
        healthy_sources = [
            c for c in copy_list
            if c.get("recoverable") and c.get("state") == "healthy"
        ]
        if not healthy_sources:
            continue
        source_target_id = str(healthy_sources[0]["targetId"])

        for dest_tid in expected_targets:
            existing = existing_targets.get(dest_tid)
            needs_repair = False
            if existing is None or not existing.get("recoverable") or existing.get("state") != "healthy":
                needs_repair = True

            if needs_repair:
                repairs_triggered += 1
                try:
                    res = execute_replica_repair(
                        policy_id=policy_id,
                        backup_id=backup_id,
                        dest_target_id=dest_tid,
                        source_target_id=source_target_id,
                        instance_id=instance_id,
                    )
                    if res.get("status") == "success":
                        repairs_succeeded += 1
                    else:
                        repairs_failed += 1
                except Exception:
                    repairs_failed += 1

    return {
        "status": "completed",
        "policyId": policy_id,
        "scannedPoints": scanned,
        "repairsTriggered": repairs_triggered,
        "repairsSucceeded": repairs_succeeded,
        "repairsFailed": repairs_failed,
    }


# ── Lag Telemetry & Compliance ──────────────────────────────────────────────


def calculate_replica_lag(
    policy_id: str,
    replica_target_id: str,
    *,
    primary_target_id: str = "managed-local",
) -> dict[str, Any]:
    """Calculate point lag and seconds lag between primary and replica target."""
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=200)
    p_copies = [c for c in copies if str(c.get("targetId")) == primary_target_id and c.get("recoverable")]
    r_copies = [c for c in copies if str(c.get("targetId")) == replica_target_id and c.get("recoverable")]

    primary_pt, _ = backup_dr_ledger.get_latest_recoverable_point(primary_target_id, policy_id)
    if primary_pt is None and p_copies:
        primary_pt = p_copies[0]

    replica_pt, _ = backup_dr_ledger.get_latest_recoverable_point(replica_target_id, policy_id)
    if replica_pt is None and r_copies:
        replica_pt = r_copies[0]

    if primary_pt is None:
        return {"lagRecoveryPoints": 0, "lagSeconds": 0, "status": "no-primary"}
    if replica_pt is None:
        return {"lagRecoveryPoints": 999, "lagSeconds": 999999, "status": "no-replica"}

    p_time = _parse_iso(primary_pt.get("committedAt"))
    r_time = _parse_iso(replica_pt.get("committedAt"))

    lag_seconds = 0
    if p_time and r_time:
        lag_seconds = max(0, int((p_time - r_time).total_seconds()))

    p_backups = {str(c["backupId"]) for c in p_copies}
    r_backups = {str(c["backupId"]) for c in r_copies}
    lag_points = max(0, len(p_backups - r_backups))

    return {
        "lagRecoveryPoints": lag_points,
        "lagSeconds": lag_seconds,
        "primaryCommittedAt": primary_pt.get("committedAt"),
        "replicaCommittedAt": replica_pt.get("committedAt"),
        "status": "calculated",
    }


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
    primary_target = str(policy.get("targetId") or "managed-local")
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
    committed = [c for c in copies if c.get("recoverable") and c.get("state") == "healthy"]
    jobs = list_jobs(policy_id=policy_id, backup_id=backup_id, limit=100)
    open_required = [
        j for j in jobs if str(j.get("mode")) == "required" and str(j.get("phase") or "") not in TERMINAL_PHASES
    ]
    failed_required = [
        j for j in jobs if str(j.get("mode")) == "required" and str(j.get("phase") or "") in {"failed", "failed-terminal"}
    ]
    compliance = "healthy"
    reasons: list[str] = []

    if len(committed) < required:
        compliance = "degraded"
        reasons.append("insufficient-committed-copies")
    if open_required:
        compliance = "degraded"
        reasons.append("open-required-jobs")
    if failed_required:
        compliance = "degraded"
        reasons.append("failed-required-jobs")

    # Evaluate replica lag objectives
    max_lag = (policy.get("recoveryObjectives") or {}).get("maxReplicaLagSeconds") or replication.get("maxReplicaLagSeconds")
    if max_lag is not None:
        for t_entry in list(replication.get("targets") or []):
            if isinstance(t_entry, dict) and t_entry.get("targetId"):
                t_id = str(t_entry["targetId"])
                lag_info = calculate_replica_lag(policy_id, t_id, primary_target_id=primary_target)
                if lag_info.get("lagSeconds", 0) > int(max_lag):
                    compliance = "degraded"
                    reasons.append(f"replica-lag-exceeded:{t_id}")

    return {
        "enabled": True,
        "compliance": compliance,
        "reasons": reasons,
        "committedCopies": len(committed),
        "requiredCopies": required,
        "healthyCopies": len(committed),
        "openRequiredJobs": len(open_required),
        "failedRequiredJobs": len(failed_required),
        "available": len(committed) >= 1,
    }
