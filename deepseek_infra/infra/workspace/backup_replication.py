"""Durable BackupReplicationJob and Autonomous Replica Self-Healing Control Plane (4.5.4).

Encrypt-once, multi-target publish: Primary commits first; each replica gets an
independent writer lease, target-local Receipt v4 and Commit v4 over the same
ciphertext object digests. Required replica spool retention until durable repair
or healthy copy exists.

Autonomous Desired-State Self-Healing:
Continuous convergence between desired copies and observed copies.
Repairs work purely on the ciphertext plane (Zero Age decrypt, Zero Age encrypt).
Durable ReplicaRepairJob state machine with per-component progress checkpointing,
bounded streaming (O(buffer * workers) RAM), CAS-guarded remote corruption replacement,
target-side protection leases, and strict separation between Replica Provision
and In-Place Committed Copy Healing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
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
    backup_transfer_budget,
    backup_writer_lease,
)

REPLICATION_DIR = config.ROOT / ".backup-replication"
HOLDS_DIR = REPLICATION_DIR / "holds"
REPAIRS_DIR = REPLICATION_DIR / "repairs"
REBALANCE_DIR = config.ROOT / ".backup-rebalance"
CURSORS_PATH = REPLICATION_DIR / "cursors.json"

JOB_SCHEMA_VERSION = 2
REPAIR_JOB_SCHEMA_VERSION = 2
REBALANCE_JOB_SCHEMA_VERSION = 1
DEFAULT_BUFFER_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB bounded streaming buffer

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

REPAIR_TERMINAL_PHASES = frozenset({"healthy", "failed-terminal", "failed", "quarantined", "superseded", "skipped"})
REPAIR_ACTIVE_PHASES = frozenset(
    {
        "queued",
        "selecting-source",
        "acquiring-source-hold",
        "validating-source-control",
        "scanning-destination",
        "transferring-components",
        "verifying-components",
        "finalizing",
        "retry-wait",
    }
)

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


def _rebalance_job_path(rebalance_id: str) -> Path:
    return REBALANCE_DIR / f"{rebalance_id}.json"


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


class RepairLeaseLostError(AppError):
    def __init__(self, message: str = "repair source protection lease lost"):
        super().__init__(message, code=ErrorCode.INVALID_REQUEST, status=412)


# ── Target-Side Durable Source Hold Mechanism ───────────────────────────────


class SourceHold:
    """Protects a healthy recovery copy from being pruned or deleted during repair."""

    def __init__(
        self,
        hold_id: str,
        target_id: str,
        policy_id: str,
        backup_id: str,
        holder_id: str,
        *,
        target_root: Path | None = None,
        target_store: Any | None = None,
        object_set_digest: str | None = None,
        expires_at: str | None = None,
        generation: int = 1,
        etag: str | None = None,
    ) -> None:
        self.hold_id = hold_id
        self.target_id = target_id
        self.policy_id = policy_id
        self.backup_id = backup_id
        self.holder_id = holder_id
        self.target_root = target_root
        self.target_store = target_store
        self.object_set_digest = object_set_digest
        self.created_at = _utc_iso()
        self.expires_at = expires_at or _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=3600))
        self.generation = generation
        self.etag = etag

    def to_dict(self) -> dict[str, Any]:
        return {
            "holderKind": "replica-repair",
            "holderId": self.holder_id,
            "holdId": self.hold_id,
            "targetId": self.target_id,
            "policyId": self.policy_id,
            "backupId": self.backup_id,
            "objectSetDigest": self.object_set_digest,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "generation": self.generation,
            "etag": self.etag,
        }

    def renew(self, duration_seconds: int = 3600) -> None:
        self.expires_at = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=duration_seconds))
        self.generation += 1
        payload = self.to_dict()
        _atomic_write(_hold_path(self.hold_id), payload)
        if self.target_root is not None:
            t_hold = self.target_root / "holds" / "repair" / f"{self.hold_id}.json"
            _atomic_write(t_hold, payload)
        elif self.target_store is not None:
            key = f"holds/repair/{self.hold_id}.json"
            data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            try:
                if self.etag:
                    res = self.target_store.put_if_match(key, data, expected_etag=self.etag)
                else:
                    stat = self.target_store.stat(key)
                    if stat is not None:
                        res = self.target_store.put_if_match(key, data, expected_etag=stat.etag)
                    else:
                        res = self.target_store.put_if_absent(key, data)
                self.etag = res.etag
            except Exception as exc:
                raise RepairLeaseLostError(f"repair-source-lease-lost: {exc}") from exc

    def release(self) -> None:
        release_source_hold(self)


def acquire_source_hold(
    target_id: str,
    policy_id: str,
    backup_id: str,
    holder_id: str,
    *,
    hold_seconds: int = 3600,
    object_set_digest: str | None = None,
    target_root: Path | None = None,
    target_store: Any | None = None,
) -> SourceHold:
    with _LOCK:
        HOLDS_DIR.mkdir(parents=True, exist_ok=True)
        hold_id = f"hold_{target_id}_{policy_id}_{backup_id}_{uuid.uuid4().hex[:8]}"
        exp = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=hold_seconds))
        etag: str | None = None

        if target_store is not None:
            key = f"holds/repair/{hold_id}.json"
            pre_payload = {
                "holderKind": "replica-repair",
                "holderId": holder_id,
                "holdId": hold_id,
                "targetId": target_id,
                "policyId": policy_id,
                "backupId": backup_id,
                "objectSetDigest": object_set_digest,
                "createdAt": _utc_iso(),
                "expiresAt": exp,
                "generation": 1,
            }
            try:
                data = json.dumps(pre_payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                res = target_store.put_if_absent(key, data)
                etag = getattr(res, "etag", None) if res and not isinstance(res, bool) else None
            except Exception as exc:
                raise AppError(f"failed to acquire remote source hold: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc

        hold = SourceHold(
            hold_id,
            target_id,
            policy_id,
            backup_id,
            holder_id,
            target_root=target_root,
            target_store=target_store,
            object_set_digest=object_set_digest,
            expires_at=exp,
            generation=1,
            etag=etag,
        )
        payload = hold.to_dict()
        _atomic_write(_hold_path(hold_id), payload)

        if target_root is not None:
            t_hold = target_root / "holds" / "repair" / f"{hold_id}.json"
            _atomic_write(t_hold, payload)
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
        if isinstance(hold, SourceHold):
            if hold.target_root is not None:
                t_hold = hold.target_root / "holds" / "repair" / f"{hold_id}.json"
                if t_hold.is_file():
                    try:
                        t_hold.unlink()
                    except OSError:
                        pass
            elif hold.target_store is not None:
                try:
                    hold.target_store.delete_if_match(f"holds/repair/{hold_id}.json")
                except Exception:
                    pass


def is_source_held(
    target_id: str,
    policy_id: str,
    backup_id: str,
    *,
    target: Any | None = None,
    now: datetime | None = None,
) -> bool:
    """Check if a source copy is protected by an unexpired durable hold."""
    current = now or datetime.now(tz=timezone.utc)
    with _LOCK:
        # Check local HOLDS_DIR
        if HOLDS_DIR.is_dir():
            for path in HOLDS_DIR.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    exp = _parse_iso(data.get("expiresAt"))
                    if exp is not None and current > exp:
                        continue
                    if (
                        str(data.get("targetId")) == target_id
                        and str(data.get("policyId")) == policy_id
                        and str(data.get("backupId")) == backup_id
                    ):
                        return True
                except Exception:
                    continue

        # Check target-side holds if target is provided
        if target is not None:
            if getattr(target, "root", None) is not None:
                r_dir = target.root / "holds" / "repair"
                if r_dir.is_dir():
                    for p in r_dir.glob("*.json"):
                        try:
                            data = json.loads(p.read_text(encoding="utf-8"))
                            exp = _parse_iso(data.get("expiresAt"))
                            if exp is not None and current > exp:
                                continue
                            if (
                                str(data.get("policyId")) == policy_id
                                and str(data.get("backupId")) == backup_id
                            ):
                                return True
                        except Exception:
                            continue
            elif getattr(target, "store", None) is not None:
                try:
                    page = target.store.list_objects("holds/repair/")
                    for obj in page.objects:
                        raw = target.store.get_bytes(obj.key)
                        if raw:
                            data = json.loads(raw.decode("utf-8"))
                            exp = _parse_iso(data.get("expiresAt"))
                            if exp is not None and current > exp:
                                continue
                            if (
                                str(data.get("policyId")) == policy_id
                                and str(data.get("backupId")) == backup_id
                            ):
                                return True
                except Exception:
                    pass

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
        target.store.put_if_absent(cat_key, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


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
        if not isinstance(data, dict) or not str(data.get("jobId", "")):
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
            published = backup_publish.publish_backup(
                target,
                package,
                run_id=str(job.get("runId") or job_id),
                policy_id=policy_id,
                schedule_slot=schedule_slot,
                fencing_token=fencing,
                traffic_class=(
                    backup_transfer_budget.TrafficClass.P3_REQUIRED_REPLICATION
                    if mode == "required"
                    else backup_transfer_budget.TrafficClass.P6_BEST_EFFORT
                ),
            )
            job = _set_phase(job, "committing")
            if isinstance(package, backup_object_set.ObjectSetPackage):
                pub_digest = str((published.receipt or {}).get("objectSetDigest") or "")
                if pub_digest and pub_digest != str(package.object_set_digest):
                    raise AppError("replica objectSetDigest diverged from primary package", code=ErrorCode.INTERNAL, status=500)
                for component in package.components:
                    if not component.ciphertext_digest:
                        raise AppError("replica component missing ciphertext digest", code=ErrorCode.INTERNAL, status=500)

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


# ── Bounded Streaming Ciphertext Transfer ───────────────────────────────────


def _iter_source_stream(target: Any, rel_key: str, *, chunk_size: int = DEFAULT_BUFFER_CHUNK_SIZE) -> Iterator[bytes]:
    """Yield bounded chunks from either filesystem or remote target store."""
    if target.root is not None:
        p = target.root / rel_key
        if not p.is_file():
            return
        with p.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    elif target.store is not None:
        # Check if get_stream is supported and yields chunks
        stream = target.store.get_stream(rel_key)
        for chunk in stream:
            if chunk:
                # If stream yields large chunk, slice into bounded buffer chunks
                for offset in range(0, len(chunk), chunk_size):
                    yield chunk[offset : offset + chunk_size]


def _managed_source_stream(
    source_target: Any,
    dest_target: Any,
    rel_key: str,
    *,
    chunk_size: int,
    traffic_class: backup_transfer_budget.TrafficClass,
) -> Iterator[bytes]:
    manager = backup_transfer_budget.get_global_transfer_budget_manager()
    return manager.throttled_generator(
        _iter_source_stream(source_target, rel_key, chunk_size=chunk_size),
        traffic_class=traffic_class,
        source_target_id=str(getattr(source_target, "target_id", "") or "") or None,
        dest_target_id=str(getattr(dest_target, "target_id", "") or "") or None,
    )


def authenticate_recovery_copy(
    target: Any,
    policy_id: str,
    backup_id: str,
    *,
    expected_object_set_digest: str | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Authenticate a recovery copy on target using Receipt v4 and Commit v4 bindings.

    Returns (status, receipt, commit) where status is one of:
    - 'missing': Neither receipt nor commit exists on target.
    - 'authenticated': Receipt and commit exist, raw receipt SHA-256 matches commit.receiptDigest,
      policy/backup/schema bindings match, and objectSetDigest matches expectation.
    - 'conflicting': Copy exists but has different objectSetDigest or conflicting metadata.
    - 'corrupt': Receipt or commit is damaged, unparseable, or hash binding fails.
    """
    r_key = f"receipts/{backup_id}.json"
    c_key = f"commits/{policy_id}/{backup_id}.json"

    raw_receipt: bytes | None = None
    raw_commit: bytes | None = None

    if target.root is not None:
        rp = target.root / "receipts" / f"{backup_id}.json"
        cp = target.root / "commits" / policy_id / f"{backup_id}.json"
        if rp.is_file():
            try:
                raw_receipt = rp.read_bytes()
            except OSError:
                return "corrupt", None, None
        if cp.is_file():
            try:
                raw_commit = cp.read_bytes()
            except OSError:
                return "corrupt", None, None
        elif raw_receipt is not None:
            try:
                rc_json = json.loads(raw_receipt.decode("utf-8"))
                slot = str(rc_json.get("scheduleSlot") or "")
                if slot:
                    found_cp = backup_publish.find_commit_marker_path(target.root, policy_id, slot)
                    if found_cp is not None and found_cp.is_file():
                        raw_commit = found_cp.read_bytes()
            except Exception:
                pass
            if raw_commit is None:
                commits_dir = target.root / "commits" / policy_id
                if commits_dir.is_dir():
                    for cand in commits_dir.glob("*.json"):
                        try:
                            c_bytes = cand.read_bytes()
                            c_dict = json.loads(c_bytes.decode("utf-8"))
                            if isinstance(c_dict, dict) and str(c_dict.get("backupId") or "") == backup_id:
                                raw_commit = c_bytes
                                break
                        except Exception:
                            continue
    elif target.store is not None:
        try:
            raw_receipt = target.store.get_bytes(r_key)
        except Exception:
            raw_receipt = None
        try:
            raw_commit = target.store.get_bytes(c_key)
        except Exception:
            raw_commit = None
        if raw_commit is None and raw_receipt is not None:
            try:
                rc_json = json.loads(raw_receipt.decode("utf-8"))
                slot = str(rc_json.get("scheduleSlot") or "")
                if slot:
                    for k in backup_publish.commit_marker_keys(policy_id, slot):
                        try:
                            raw_commit = target.store.get_bytes(k)
                            if raw_commit:
                                break
                        except Exception:
                            continue
            except Exception:
                pass

    if raw_receipt is None and raw_commit is None:
        return "missing", None, None
    if raw_receipt is None or raw_commit is None:
        return "corrupt", None, None

    try:
        receipt = json.loads(raw_receipt.decode("utf-8"))
        commit = json.loads(raw_commit.decode("utf-8"))
    except Exception:
        return "corrupt", None, None

    if not isinstance(receipt, dict) or not isinstance(commit, dict):
        return "corrupt", None, None

    # Check commit hash binding
    calc_receipt_digest = hashlib.sha256(raw_receipt).hexdigest()
    if str(commit.get("receiptDigest")) != calc_receipt_digest:
        return "corrupt", receipt, commit

    # Check schema and IDs
    schema_ver = int(commit.get("schemaVersion") or 0)
    if schema_ver not in {1, 2, 3, 4}:
        return "corrupt", receipt, commit
    if str(commit.get("policyId")) != policy_id or str(commit.get("backupId")) != backup_id:
        return "conflicting", receipt, commit
    if str(receipt.get("policyId")) != policy_id or str(receipt.get("backupId")) != backup_id:
        return "conflicting", receipt, commit

    # Check objectSetDigest / objectDigest
    r_osd = receipt.get("objectSetDigest") or receipt.get("objectDigest")
    c_osd = commit.get("objectSetDigest") or commit.get("objectDigest")
    if not r_osd:
        return "corrupt", receipt, commit
    if c_osd is not None and r_osd != c_osd:
        return "corrupt", receipt, commit

    if expected_object_set_digest is not None:
        if str(r_osd) != expected_object_set_digest:
            return "conflicting", receipt, commit

    return "authenticated", receipt, commit


def authenticate_committed_copy(
    target: Any,
    policy_id: str,
    backup_id: str,
    *,
    expected_object_set_digest: str | None = None,
    expected_previous_commit_hash: str | None = None,
    expected_target_generation: int | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Authenticate a committed recovery copy on target using Receipt v4 and Commit v4 cryptographic bindings."""
    status, receipt, commit = authenticate_recovery_copy(
        target,
        policy_id,
        backup_id,
        expected_object_set_digest=expected_object_set_digest,
    )
    if status != "authenticated" or commit is None or receipt is None:
        return status, receipt, commit

    if commit.get("commitHash"):
        calc_commit_hash = backup_publish._commit_hash(commit)
        if str(commit.get("commitHash")) != calc_commit_hash:
            return "corrupt", receipt, commit

    if expected_previous_commit_hash is not None and str(commit.get("previousCommitHash") or "") != expected_previous_commit_hash:
        return "conflicting", receipt, commit

    if expected_target_generation is not None and int(commit.get("targetGeneration") or 0) != expected_target_generation:
        return "conflicting", receipt, commit

    return "authenticated", receipt, commit


def authenticate_transition_parent(
    target: Any,
    policy_id: str,
    *,
    expected_parent_backup_id: str,
    expected_receipt_digest: str | None = None,
    expected_commit_hash: str | None = None,
    expected_lineage_id: str | None = None,
    expected_object_set_digest: str | None = None,
) -> tuple[bool, str]:
    """Strictly authenticate that candidate target possesses the exact expected parent commitment."""
    status, receipt, commit = authenticate_recovery_copy(
        target,
        policy_id,
        expected_parent_backup_id,
        expected_object_set_digest=expected_object_set_digest,
    )
    if status != "authenticated" or commit is None or receipt is None:
        return False, f"parent-copy-status-{status}"

    if expected_receipt_digest is not None:
        if str(commit.get("receiptDigest") or "") != expected_receipt_digest:
            return False, "parent-receipt-digest-mismatch"

    if expected_commit_hash is not None:
        if str(commit.get("commitHash") or "") != expected_commit_hash:
            return False, "parent-commit-hash-mismatch"

    if expected_lineage_id is not None:
        c_lineage = commit.get("lineageId") or receipt.get("lineageId")
        if c_lineage and str(c_lineage) != expected_lineage_id:
            return False, "parent-lineage-mismatch"

    if expected_object_set_digest is not None:
        c_osd = (
            commit.get("objectSetDigest")
            or receipt.get("objectSetDigest")
            or commit.get("objectDigest")
            or receipt.get("objectDigest")
        )
        if str(c_osd or "") != expected_object_set_digest:
            return False, "parent-object-set-digest-mismatch"

    return True, "authenticated"


def _verify_destination_component(dest_target: Any, target_rel: str, expected_digest: str) -> tuple[bool, bool]:
    """Check if component on destination is valid or corrupt using streaming/provider hashes.

    Returns (is_valid, is_corrupt).
    """
    if dest_target.root is not None:
        dp = dest_target.root / target_rel
        if not dp.is_file():
            return False, False
        hasher = hashlib.sha256()
        with dp.open("rb") as f:
            while chunk := f.read(DEFAULT_BUFFER_CHUNK_SIZE):
                hasher.update(chunk)
        calc = hasher.hexdigest()
        return (calc == expected_digest, calc != expected_digest)
    elif dest_target.store is not None:
        stat = dest_target.store.stat(target_rel)
        if stat is None:
            return False, False
        if stat.sha256 and stat.sha256 == expected_digest:
            return True, False
        if stat.provider_sha256 and stat.provider_sha256 == expected_digest:
            return True, False
        hasher = hashlib.sha256()
        has_data = False
        for chunk in dest_target.store.get_stream(target_rel):
            if chunk:
                has_data = True
                hasher.update(chunk)
        if not has_data:
            return False, False
        calc = hasher.hexdigest()
        return (calc == expected_digest, calc != expected_digest)
    return False, False


def _part_number(part: dict[str, Any]) -> int:
    return int(part.get("partNumber") or part.get("number") or 0)


def _normalized_etag(value: Any) -> str:
    return str(value or "").strip().strip('"').lower()


def _part_matches_source(part: dict[str, Any], chunk: bytes) -> tuple[bool, str]:
    size = int(part.get("size") or 0)
    if size != len(chunk):
        return False, f"size-mismatch:{size}!={len(chunk)}"
    checksum_b64 = str(part.get("checksumSHA256") or "")
    expected_sha256 = hashlib.sha256(chunk).hexdigest()
    if checksum_b64:
        expected_b64 = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
        if checksum_b64 != expected_b64:
            return False, "checksum-mismatch"
        return True, "provider-checksum"
    etag = _normalized_etag(part.get("etag"))
    expected_md5 = hashlib.md5(chunk, usedforsecurity=False).hexdigest()
    if etag not in {expected_sha256, expected_md5}:
        return False, "etag-unverifiable-or-mismatch"
    return True, "etag-content-binding"


def _canonical_progress_part(part: dict[str, Any], chunk: bytes) -> dict[str, Any]:
    return {
        "number": _part_number(part),
        "etag": str(part.get("etag") or ""),
        "size": len(chunk),
        "checksumSha256": hashlib.sha256(chunk).hexdigest(),
    }


def _provider_upload_part(part: dict[str, Any], chunk: bytes) -> dict[str, Any]:
    result = {
        "partNumber": _part_number(part),
        "etag": str(part.get("etag") or ""),
        "size": len(chunk),
    }
    if part.get("checksumSHA256"):
        result["checksumSHA256"] = str(part["checksumSHA256"])
    return result


def _upload_result_part(result: Any, part_number: int, chunk: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(result, dict):
        provider = dict(result)
    else:
        provider = {
            "partNumber": int(getattr(result, "part_number", part_number) or part_number),
            "etag": str(getattr(result, "etag", "") or ""),
            "size": int(getattr(result, "size", len(chunk)) or len(chunk)),
        }
    provider["partNumber"] = int(provider.get("partNumber") or part_number)
    provider["etag"] = str(provider.get("etag") or "")
    provider["size"] = int(provider.get("size") or len(chunk))
    return provider, _canonical_progress_part(provider, chunk)


def _multipart_upload_missing(exc: BaseException) -> bool:
    return int(getattr(exc, "status", 0) or 0) == 404 or "multipart-upload-not-found" in str(exc).lower()


def _quarantine_multipart_progress(
    progress_state: dict[str, Any],
    *,
    upload_id: str,
    reason: str,
    local_parts: list[dict[str, Any]],
    remote_parts: list[dict[str, Any]],
) -> None:
    progress_state["multipartQuarantine"] = {
        "uploadId": upload_id,
        "reason": reason,
        "localParts": local_parts,
        "remoteParts": remote_parts,
        "quarantinedAt": _utc_iso(),
    }
    progress_state.pop("multipartUploadId", None)
    progress_state["parts"] = []
    progress_state["nextOffset"] = 0


def stream_ciphertext_transfer(
    source_target: Any,
    dest_target: Any,
    source_rel: str,
    dest_rel: str,
    expected_digest: str,
    *,
    chunk_size: int = DEFAULT_BUFFER_CHUNK_SIZE,
    progress_state: dict[str, Any] | None = None,
    on_part: Callable[[int, str, int], None] | None = None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR,
) -> int:
    """Stream ciphertext from source to destination using bounded buffer RAM.

    Zero Age decrypt, Zero Age encrypt.
    Validates SHA-256 matches expected_digest.
    Uses S3 multipart upload for remote destination to keep memory O(buffer * workers).
    Returns total bytes transferred.
    """
    hasher = hashlib.sha256()
    bytes_transferred = 0

    if dest_target.root is not None:
        dp = dest_target.root / dest_rel
        dp.parent.mkdir(parents=True, exist_ok=True)
        tmp = dp.with_name(f".{dp.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            with tmp.open("wb") as out_h:
                for chunk in _managed_source_stream(
                    source_target,
                    dest_target,
                    source_rel,
                    chunk_size=chunk_size,
                    traffic_class=traffic_class,
                ):
                    hasher.update(chunk)
                    out_h.write(chunk)
                    bytes_transferred += len(chunk)
                out_h.flush()
                os.fsync(out_h.fileno())

            calc_digest = hasher.hexdigest()
            if calc_digest != expected_digest:
                tmp.unlink(missing_ok=True)
                raise AppError(
                    f"source component corrupt: ciphertext transfer digest mismatch: calculated={calc_digest}, expected={expected_digest}",
                    code=ErrorCode.INTERNAL,
                    status=500,
                )
            os.replace(tmp, dp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    elif dest_target.store is not None:
        store = dest_target.store
        stream = _managed_source_stream(
            source_target,
            dest_target,
            source_rel,
            chunk_size=chunk_size,
            traffic_class=traffic_class,
        )

        # Reconcile the durable local checkpoint with provider ListParts before
        # sending any resumed bytes. Provider parts are accepted only after
        # they are content-bound to the immutable source ciphertext.
        if progress_state is not None and progress_state.get("multipartUploadId"):
            from deepseek_infra.infra.workspace.backup_target_store import MultipartUpload

            upload_id = str(progress_state["multipartUploadId"])
            local_parts = [dict(item) for item in list(progress_state.get("parts") or []) if isinstance(item, dict)]
            upload = MultipartUpload(key=dest_rel, upload_id=upload_id, checksum_sha256=expected_digest)
            try:
                remote_parts = sorted(
                    [dict(item) for item in store.list_multipart_parts(upload) if isinstance(item, dict)],
                    key=_part_number,
                )
            except Exception as exc:
                if not _multipart_upload_missing(exc):
                    raise
                progress_state["multipartRestart"] = {
                    "previousUploadId": upload_id,
                    "reason": "provider-upload-not-found",
                    "restartedAt": _utc_iso(),
                }
                progress_state.pop("multipartUploadId", None)
                progress_state["parts"] = []
                progress_state["nextOffset"] = 0
            else:
                conflict_reason: str | None = None
                if len(remote_parts) < len(local_parts):
                    conflict_reason = f"remote-part-count-behind:{len(remote_parts)}<{len(local_parts)}"
                canonical_remote: list[dict[str, Any]] = []
                provider_remote: list[dict[str, Any]] = []
                expected_local_offset = 0
                for index, remote_part in enumerate(remote_parts, start=1):
                    if _part_number(remote_part) != index:
                        conflict_reason = conflict_reason or f"non-contiguous-remote-part:{_part_number(remote_part)}"
                        break
                    try:
                        source_chunk = next(stream)
                    except StopIteration:
                        conflict_reason = conflict_reason or "remote-parts-exceed-source"
                        break
                    matches, match_reason = _part_matches_source(remote_part, source_chunk)
                    if not matches:
                        conflict_reason = conflict_reason or f"remote-part-{index}-{match_reason}"
                        break
                    if index <= len(local_parts):
                        local_part = local_parts[index - 1]
                        if _part_number(local_part) != index:
                            conflict_reason = conflict_reason or f"non-contiguous-local-part:{_part_number(local_part)}"
                            break
                        local_size = int(local_part.get("size") or len(source_chunk))
                        if local_size != int(remote_part.get("size") or 0):
                            conflict_reason = conflict_reason or f"part-{index}-local-remote-size-conflict"
                            break
                        local_etag = _normalized_etag(local_part.get("etag"))
                        remote_etag = _normalized_etag(remote_part.get("etag"))
                        if len(local_etag) in {32, 64} and local_etag != remote_etag:
                            conflict_reason = conflict_reason or f"part-{index}-local-remote-etag-conflict"
                            break
                        local_checksum = str(local_part.get("checksumSha256") or "")
                        if local_checksum and local_checksum != hashlib.sha256(source_chunk).hexdigest():
                            conflict_reason = conflict_reason or f"part-{index}-local-checksum-conflict"
                            break
                        expected_local_offset += len(source_chunk)
                    hasher.update(source_chunk)
                    bytes_transferred += len(source_chunk)
                    canonical_remote.append(_canonical_progress_part(remote_part, source_chunk))
                    provider_remote.append(_provider_upload_part(remote_part, source_chunk))

                local_next_offset = int(progress_state.get("nextOffset") or 0)
                if conflict_reason is None and local_next_offset != expected_local_offset:
                    conflict_reason = f"local-offset-conflict:{local_next_offset}!={expected_local_offset}"
                if conflict_reason is not None:
                    store.abort_multipart(upload)
                    _quarantine_multipart_progress(
                        progress_state,
                        upload_id=upload_id,
                        reason=conflict_reason,
                        local_parts=local_parts,
                        remote_parts=remote_parts,
                    )
                    raise AppError(
                        f"multipart-reconciliation-conflict:{conflict_reason}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=409,
                    )

                upload.parts = provider_remote
                progress_state["parts"] = canonical_remote
                progress_state["nextOffset"] = sum(int(item["size"]) for item in canonical_remote)
                reconcile_status = "remote-ahead-adopted" if len(remote_parts) > len(local_parts) else "remote-matches-local"
                progress_state["multipartReconciliation"] = {
                    "status": reconcile_status,
                    "uploadId": upload_id,
                    "localPartCount": len(local_parts),
                    "remotePartCount": len(remote_parts),
                    "reconciledAt": _utc_iso(),
                }
                if on_part is not None:
                    for adopted in canonical_remote[len(local_parts) :]:
                        on_part(int(adopted["number"]), str(adopted["etag"]), int(adopted["size"]))

                part_num = len(remote_parts)
                for chunk in stream:
                    part_num += 1
                    hasher.update(chunk)
                    bytes_transferred += len(chunk)
                    result = store.upload_part(
                        upload,
                        part_num,
                        chunk,
                        checksum_sha256=hashlib.sha256(chunk).hexdigest(),
                    )
                    provider_part, progress_part = _upload_result_part(result, part_num, chunk)
                    upload.parts = [item for item in upload.parts if _part_number(item) != part_num]
                    upload.parts.append(provider_part)
                    upload.parts.sort(key=_part_number)
                    progress_state["parts"].append(progress_part)
                    progress_state["nextOffset"] = int(progress_state["nextOffset"]) + len(chunk)
                    if on_part is not None:
                        on_part(part_num, str(progress_part["etag"]), len(chunk))

                calc_digest = hasher.hexdigest()
                if calc_digest != expected_digest:
                    store.abort_multipart(upload)
                    _quarantine_multipart_progress(
                        progress_state,
                        upload_id=upload_id,
                        reason="source-ciphertext-digest-mismatch",
                        local_parts=local_parts,
                        remote_parts=remote_parts,
                    )
                    raise AppError(
                        f"source component corrupt: ciphertext transfer digest mismatch: calculated={calc_digest}, expected={expected_digest}",
                        code=ErrorCode.INTERNAL,
                        status=500,
                    )
                upload.expected_size = bytes_transferred
                store.complete_multipart_if_absent(upload)
                return bytes_transferred

        # A missing provider upload is restarted from byte zero.
        stream = _managed_source_stream(
            source_target,
            dest_target,
            source_rel,
            chunk_size=chunk_size,
            traffic_class=traffic_class,
        )
        hasher = hashlib.sha256()
        bytes_transferred = 0

        try:
            first_chunk = next(stream)
        except StopIteration:
            first_chunk = b""

        hasher.update(first_chunk)
        bytes_transferred += len(first_chunk)

        try:
            second_chunk = next(stream)
        except StopIteration:
            second_chunk = None

        if second_chunk is None:
            calc_digest = hasher.hexdigest()
            if calc_digest != expected_digest:
                raise AppError(
                    f"source component corrupt: ciphertext transfer digest mismatch: calculated={calc_digest}, expected={expected_digest}",
                    code=ErrorCode.INTERNAL,
                    status=500,
                )
            store.put_if_absent(dest_rel, first_chunk, checksum_sha256=expected_digest)
        else:
            hasher.update(second_chunk)
            bytes_transferred += len(second_chunk)
            upload = store.begin_multipart(dest_rel, checksum_sha256=expected_digest)
            if progress_state is not None:
                progress_state["multipartUploadId"] = upload.upload_id
                progress_state["parts"] = []
                progress_state["nextOffset"] = 0
                progress_state["multipartReconciliation"] = {
                    "status": "new-upload",
                    "uploadId": upload.upload_id,
                    "localPartCount": 0,
                    "remotePartCount": 0,
                    "reconciledAt": _utc_iso(),
                }
            part_num = 1
            try:
                res1 = store.upload_part(upload, part_num, first_chunk, checksum_sha256=hashlib.sha256(first_chunk).hexdigest())
                provider1, progress1 = _upload_result_part(res1, part_num, first_chunk)
                upload.parts = [provider1]
                if progress_state is not None:
                    progress_state["parts"].append(progress1)
                    progress_state["nextOffset"] = len(first_chunk)
                if on_part is not None:
                    on_part(part_num, str(progress1["etag"]), len(first_chunk))

                part_num += 1
                res2 = store.upload_part(upload, part_num, second_chunk, checksum_sha256=hashlib.sha256(second_chunk).hexdigest())
                provider2, progress2 = _upload_result_part(res2, part_num, second_chunk)
                upload.parts.append(provider2)
                if progress_state is not None:
                    progress_state["parts"].append(progress2)
                    progress_state["nextOffset"] = int(progress_state["nextOffset"]) + len(second_chunk)
                if on_part is not None:
                    on_part(part_num, str(progress2["etag"]), len(second_chunk))

                for chunk in stream:
                    part_num += 1
                    hasher.update(chunk)
                    bytes_transferred += len(chunk)
                    result = store.upload_part(upload, part_num, chunk, checksum_sha256=hashlib.sha256(chunk).hexdigest())
                    provider_part, progress_part = _upload_result_part(result, part_num, chunk)
                    upload.parts.append(provider_part)
                    if progress_state is not None:
                        progress_state["parts"].append(progress_part)
                        progress_state["nextOffset"] = int(progress_state["nextOffset"]) + len(chunk)
                    if on_part is not None:
                        on_part(part_num, str(progress_part["etag"]), len(chunk))

                calc_digest = hasher.hexdigest()
                if calc_digest != expected_digest:
                    store.abort_multipart(upload)
                    raise AppError(
                        f"source component corrupt: ciphertext transfer digest mismatch: calculated={calc_digest}, expected={expected_digest}",
                        code=ErrorCode.INTERNAL,
                        status=500,
                    )
                upload.expected_size = bytes_transferred
                store.complete_multipart_if_absent(upload)
            except Exception:
                # A durable checkpoint is intentionally left resumable on
                # transient transport failures. Untracked uploads are aborted.
                if progress_state is None:
                    store.abort_multipart(upload)
                raise

    return bytes_transferred


def quarantine_and_replace_corrupt_remote_object(
    dest_target: Any,
    dest_rel: str,
    expected_digest: str,
    source_target: Any,
    source_rel: str,
    *,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR,
) -> int:
    """Safely replace a corrupted object on destination using conditional delete + transfer."""
    if dest_target.root is not None:
        q_dir = dest_target.root / ".quarantine"
        q_dir.mkdir(parents=True, exist_ok=True)
        dp = dest_target.root / dest_rel
        if dp.is_file():
            dp.rename(q_dir / f"{expected_digest}.corrupt.{time.time_ns()}")
        return stream_ciphertext_transfer(
            source_target,
            dest_target,
            source_rel,
            dest_rel,
            expected_digest,
            traffic_class=traffic_class,
        )

    if dest_target.store is not None:
        stat = dest_target.store.stat(dest_rel)
        if stat is not None:
            # Stream corrupt bytes into quarantine in bounded memory
            q_key = f".quarantine/{expected_digest}.corrupt.{time.time_ns()}"
            stream = None
            try:
                stream = dest_target.store.get_stream(dest_rel)
                try:
                    first_chunk = next(stream)
                except StopIteration:
                    first_chunk = b""
                try:
                    second_chunk = next(stream)
                except StopIteration:
                    second_chunk = None

                if second_chunk is None:
                    dest_target.store.put_if_absent(q_key, first_chunk)
                else:
                    upload = dest_target.store.begin_multipart(q_key, checksum_sha256=stat.sha256 or stat.provider_sha256 or "")
                    try:
                        p_num = 1
                        dest_target.store.upload_part(upload, p_num, first_chunk)
                        p_num += 1
                        dest_target.store.upload_part(upload, p_num, second_chunk)
                        for chunk in stream:
                            p_num += 1
                            dest_target.store.upload_part(upload, p_num, chunk)
                        dest_target.store.complete_multipart_if_absent(upload)
                    except Exception:
                        dest_target.store.abort_multipart(upload)
                        raise
            except Exception:
                pass
            finally:
                if stream is not None and hasattr(stream, "close"):
                    stream.close()
                del stream
            # Conditional delete with expected ETag / CAS
            try:
                deleted = dest_target.store.delete_if_match(dest_rel, expected_etag=stat.etag)
                if not deleted:
                    raise AppError("conditional-delete-corrupt-object-failed: CAS mismatch", code=ErrorCode.INVALID_REQUEST, status=412)
            except Exception as exc:
                raise AppError(f"conditional-delete-corrupt-object-failed: {exc}", code=ErrorCode.INVALID_REQUEST, status=412) from exc

        return stream_ciphertext_transfer(
            source_target,
            dest_target,
            source_rel,
            dest_rel,
            expected_digest,
            traffic_class=traffic_class,
        )
    return 0



# ── Durable ReplicaRepairJob CRUD ───────────────────────────────────────────


def read_repair_job(repair_id: str) -> dict[str, Any] | None:
    path = _repair_job_path(repair_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover
        return None
    return data if isinstance(data, dict) else None


def list_repair_jobs(
    *,
    policy_id: str | None = None,
    backup_id: str | None = None,
    dest_target_id: str | None = None,
    source_target_id: str | None = None,
    phase: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not REPAIRS_DIR.is_dir():
        return []
    repairs: list[dict[str, Any]] = []
    for path in sorted(REPAIRS_DIR.glob("*.json"), reverse=True):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not str(data.get("repairId", "")):
            continue
        if policy_id and str(data.get("policyId") or "") != policy_id:
            continue
        if backup_id and str(data.get("backupId") or "") != backup_id:
            continue
        if dest_target_id and str(data.get("destTargetId") or "") != dest_target_id:
            continue
        if source_target_id and str(data.get("sourceTargetId") or "") != source_target_id:
            continue
        if phase and str(data.get("phase") or "") != phase:
            continue
        repairs.append(data)
        if len(repairs) >= limit:
            break
    return repairs


def create_repair_job(
    *,
    policy_id: str,
    backup_id: str,
    dest_target_id: str,
    source_target_id: str | None = None,
    object_set_digest: str | None = None,
    repair_id: str | None = None,
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR,
) -> dict[str, Any]:
    with _LOCK:
        REPAIRS_DIR.mkdir(parents=True, exist_ok=True)
        r_id = repair_id or f"repair_{uuid.uuid4().hex[:16]}"
        job = {
            "schemaVersion": REPAIR_JOB_SCHEMA_VERSION,
            "repairId": r_id,
            "policyId": policy_id,
            "backupId": backup_id,
            "sourceTargetId": source_target_id,
            "destTargetId": dest_target_id,
            "objectSetDigest": object_set_digest,
            "repairMode": "auto",
            "trafficClass": int(traffic_class),
            "phase": "queued",
            "components": {},
            "bytesRepaired": 0,
            "attempt": 0,
            "maxAttempts": 5,
            "nextAttemptAt": None,
            "holdId": None,
            "createdAt": _utc_iso(),
            "updatedAt": _utc_iso(),
            "error": None,
        }
        _atomic_write(_repair_job_path(r_id), job)
        return job


def _set_repair_phase(job: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
    job = dict(job)
    job["phase"] = phase
    job["updatedAt"] = _utc_iso()
    for key, value in extra.items():
        job[key] = value
    _atomic_write(_repair_job_path(str(job["repairId"])), job)
    return job


# ── Ciphertext-Plane Replica Self-Healing Engine (4.5.4) ────────────────────


def execute_replica_repair(
    *,
    policy_id: str,
    backup_id: str,
    dest_target_id: str,
    source_target_id: str | None = None,
    run_id: str | None = None,
    instance_id: str = "healer-worker",
    traffic_class: backup_transfer_budget.TrafficClass = backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR,
) -> dict[str, Any]:
    """Repair missing/corrupted replica copy purely on the ciphertext plane.

    Contract (4.5.4):
    - Age decrypt count = 0
    - Age encrypt count = 0
    - Existing committed copy repair NEVER writes Receipt v4 or Commit v4,
      NEVER increments targetGeneration, NEVER moves control/head.json.
    - Fully durable and resumable on process restart.
    """
    if backup_dr_ledger.is_logical_recovery_point_retired(policy_id, backup_id):
        return {"status": "skipped", "reason": "retired", "policyId": policy_id, "backupId": backup_id}

    r_id = run_id or f"repair_{uuid.uuid4().hex[:16]}"
    existing_job = read_repair_job(r_id)
    if existing_job is None:
        # Check open repair job for (policy, backup, dest)
        open_jobs = [
            j for j in list_repair_jobs(policy_id=policy_id, backup_id=backup_id, limit=50)
            if str(j.get("destTargetId")) == dest_target_id and str(j.get("phase")) in REPAIR_ACTIVE_PHASES
        ]
        if open_jobs:
            job = open_jobs[0]
            r_id = str(job["repairId"])
        else:
            job = create_repair_job(
                policy_id=policy_id,
                backup_id=backup_id,
                dest_target_id=dest_target_id,
                source_target_id=source_target_id,
                repair_id=r_id,
                traffic_class=traffic_class,
            )
    else:
        job = existing_job

    return execute_repair_job_instance(r_id, instance_id=instance_id, requested_source_target_id=source_target_id)


def _compute_repair_backoff_seconds(attempt: int) -> int:
    base = [5, 15, 45, 120, 300]
    idx = min(max(0, attempt - 1), len(base) - 1)
    return base[idx]


def execute_repair_job_instance(
    repair_id: str,
    *,
    instance_id: str = "healer-worker",
    requested_source_target_id: str | None = None,
) -> dict[str, Any]:
    """Execute or resume an individual durable ReplicaRepairJob."""
    start_time = time.monotonic()
    job = read_repair_job(repair_id)
    if job is None:
        raise AppError("ReplicaRepairJob not found", code=ErrorCode.NOT_FOUND, status=404)
    if str(job.get("phase") or "") in REPAIR_TERMINAL_PHASES:
        return {"status": "success" if job.get("phase") == "healthy" else str(job.get("phase")), "repairId": repair_id, "job": job}

    policy_id = str(job["policyId"])
    backup_id = str(job["backupId"])
    dest_target_id = str(job["destTargetId"])
    source_target_id = job.get("sourceTargetId") or requested_source_target_id
    try:
        transfer_class = backup_transfer_budget.TrafficClass(int(job.get("trafficClass", 2)))
    except ValueError:
        transfer_class = backup_transfer_budget.TrafficClass.P2_REQUIRED_REPAIR
    attempt = int(job.get("attempt") or 0) + 1
    max_attempts = int(job.get("maxAttempts") or 5)

    if attempt > max_attempts:
        job = _set_repair_phase(job, "failed-terminal", error="max-attempts-exceeded", attempt=attempt)
        raise AppError("ReplicaRepairJob max attempts exceeded", code=ErrorCode.INVALID_REQUEST, status=409)

    # 1. Resolve source
    job = _set_repair_phase(job, "selecting-source", attempt=attempt)
    if not source_target_id:
        copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
        healthy = [
            c for c in copies
            if str(c.get("targetId")) != dest_target_id and c.get("recoverable") and c.get("state") == "healthy"
        ]
        if not healthy:
            job = _set_repair_phase(job, "failed-terminal", error="no-healthy-source-copy")
            raise AppError(
                f"No healthy source copy available to repair {dest_target_id} for {backup_id}",
                code=ErrorCode.NOT_FOUND,
                status=404,
            )
        source_target_id = str(healthy[0]["targetId"])
        job = _set_repair_phase(job, "selecting-source", sourceTargetId=source_target_id)

    source_target = backup_publish.resolve_target(source_target_id)
    dest_target = backup_publish.resolve_target(dest_target_id)

    # 2. Acquire target-side source hold
    job = _set_repair_phase(job, "acquiring-source-hold")
    try:
        hold = acquire_source_hold(
            source_target_id,
            policy_id,
            backup_id,
            holder_id=repair_id,
            target_root=source_target.root,
            target_store=source_target.store,
            object_set_digest=job.get("objectSetDigest"),
        )
    except Exception as exc:
        backoff = _compute_repair_backoff_seconds(attempt)
        next_at = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=backoff))
        job = _set_repair_phase(job, "retry-wait", error=f"acquire-hold-failed: {exc}", nextAttemptAt=next_at)
        raise

    job = _set_repair_phase(job, "validating-source-control", holdId=hold.hold_id)

    try:
        # Validate source control plane
        s_status, source_receipt, _ = authenticate_recovery_copy(
            source_target,
            policy_id,
            backup_id,
            expected_object_set_digest=job.get("objectSetDigest"),
        )
        if s_status != "authenticated" or source_receipt is None:
            c_at = str((source_receipt or {}).get("createdAt") or _utc_iso())
            backup_dr_ledger.record_logical_recovery_copy(
                target_id=source_target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                committed_at=c_at,
                state="degraded" if s_status == "corrupt" else "quarantined",
                recoverable=False,
                last_verified_at=_utc_iso(),
            )
            job = _set_repair_phase(
                job,
                "selecting-source",
                sourceTargetId=None,
                error=f"source-control-plane-{s_status}",
            )
            if s_status == "missing" or source_receipt is None:
                raise AppError(f"Source receipt missing on target {source_target_id}; cannot repair", code=ErrorCode.NOT_FOUND, status=404)
            raise AppError(f"Source control plane {s_status}; rejected", code=ErrorCode.INVALID_REQUEST, status=409)

        # 3. Acquire WriterLease on destination
        fencing = backup_scheduler.allocate_fencing_token()
        writer = backup_writer_lease.TargetWriterLease(
            dest_target.root,
            store=dest_target.store if dest_target.root is None else None,
            target_id=dest_target_id,
            owner_run_id=repair_id,
            owner_instance_id=instance_id,
            fencing_token=fencing,
        )
        try:
            writer.acquire()
        except Exception as exc:
            backoff = _compute_repair_backoff_seconds(attempt)
            next_at = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=backoff))
            job = _set_repair_phase(job, "retry-wait", error=f"writer-lease-contention: {exc}", nextAttemptAt=next_at)
            raise

        try:
            objects = list(source_receipt.get("objects") or [])
            if not objects and source_receipt.get("objectDigest"):
                objects = [{"digest": str(source_receipt.get("objectDigest")), "size": int(source_receipt.get("size") or 0)}]

            # 4. Check destination to determine repair mode: provision vs heal-existing
            job = _set_repair_phase(job, "scanning-destination")
            d_status, _, _ = authenticate_recovery_copy(
                dest_target,
                policy_id,
                backup_id,
                expected_object_set_digest=str(source_receipt.get("objectSetDigest") or source_receipt.get("objectDigest") or ""),
            )

            if d_status == "authenticated":
                repair_mode = "heal-existing"
            elif d_status == "missing":
                repair_mode = "provision"
            else:
                # Conflicting or corrupt control plane on destination
                c_at = str((source_receipt or {}).get("createdAt") or _utc_iso())
                backup_dr_ledger.record_logical_recovery_copy(
                    target_id=dest_target_id,
                    policy_id=policy_id,
                    backup_id=backup_id,
                    committed_at=c_at,
                    state="quarantined",
                    recoverable=False,
                    last_verified_at=_utc_iso(),
                )
                job = _set_repair_phase(job, "quarantined", error=f"destination-control-plane-{d_status}")
                raise AppError(
                    f"Destination control plane {d_status}; copy quarantined and cannot heal",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )

            job = _set_repair_phase(job, "scanning-destination", repairMode=repair_mode)

            # Component tracking checkpoint
            components_state = dict(job.get("components") or {})
            for obj in objects:
                digest = str(obj.get("digest") or "")
                if not digest:
                    continue
                if digest not in components_state:
                    components_state[digest] = {
                        "digest": digest,
                        "size": int(obj.get("size") or 0),
                        "state": "pending",
                        "transferredBytes": 0,
                    }

            job = _set_repair_phase(job, "transferring-components", components=components_state)
            bytes_transferred = int(job.get("bytesRepaired") or 0)

            # 5. Stream and verify components
            for obj in objects:
                digest = str(obj.get("digest") or "")
                if not digest:
                    continue
                c_state = components_state.get(digest, {})
                if c_state.get("state") == "verified":
                    continue

                comp_rel = f"objects/{digest[:2]}/{digest[2:4]}/{digest}.age"
                control_rel = f"control/{digest}.age"
                sha256_rel = f"objects/sha256/{digest[:2]}/{digest}.age"
                fn = str(source_receipt.get("filename") or "")

                # Check if component is in objects/, control/, sha256/ or filename on source
                source_rel = comp_rel
                if source_target.root is not None:
                    if (source_target.root / comp_rel).is_file():
                        source_rel = comp_rel
                    elif (source_target.root / control_rel).is_file():
                        source_rel = control_rel
                    elif (source_target.root / sha256_rel).is_file():
                        source_rel = sha256_rel
                    elif fn and (source_target.root / fn).is_file():
                        source_rel = fn
                elif source_target.store is not None:
                    if source_target.store.stat(comp_rel) is not None:
                        source_rel = comp_rel
                    elif source_target.store.stat(control_rel) is not None:
                        source_rel = control_rel
                    elif source_target.store.stat(sha256_rel) is not None:
                        source_rel = sha256_rel
                    elif fn and source_target.store.stat(fn) is not None:
                        source_rel = fn

                target_rel = source_rel

                # Check destination current state without whole-object memory buffering
                dest_valid, dest_corrupt = _verify_destination_component(dest_target, target_rel, digest)

                if dest_valid:
                    components_state[digest]["state"] = "verified"
                    job = _set_repair_phase(job, "transferring-components", components=components_state)
                    continue

                # Transfer
                if dest_corrupt:
                    trans_bytes = quarantine_and_replace_corrupt_remote_object(
                        dest_target,
                        target_rel,
                        digest,
                        source_target,
                        source_rel,
                        traffic_class=transfer_class,
                    )
                else:
                    def _checkpoint_multipart_part(part_number: int, etag: str, part_bytes: int) -> None:
                        nonlocal job
                        assert job is not None
                        components_state[digest]["lastCheckpointPart"] = part_number
                        components_state[digest]["lastCheckpointEtag"] = etag
                        components_state[digest]["lastCheckpointPartBytes"] = part_bytes
                        job = _set_repair_phase(
                            job,
                            "transferring-components",
                            components=components_state,
                            bytesRepaired=bytes_transferred,
                        )
                        hold.renew()

                    trans_bytes = stream_ciphertext_transfer(
                        source_target,
                        dest_target,
                        source_rel,
                        target_rel,
                        digest,
                        progress_state=components_state[digest],
                        on_part=_checkpoint_multipart_part,
                        traffic_class=transfer_class,
                    )

                bytes_transferred += trans_bytes
                components_state[digest]["state"] = "verified"
                components_state[digest]["transferredBytes"] = trans_bytes

                # Per-component progress checkpoint
                job = _set_repair_phase(
                    job,
                    "transferring-components",
                    components=components_state,
                    bytesRepaired=bytes_transferred,
                )

                # CAS protection renewal (raises RepairLeaseLostError immediately on lost lease)
                hold.renew()

            job = _set_repair_phase(job, "verifying-components")

            # 6. Finalizing phase
            job = _set_repair_phase(job, "finalizing")

            if repair_mode == "provision":
                # Missing copy: generate target-local Receipt v4 and Commit v4
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

                schedule_slot = f"repair/{backup_id}"
                gen, prev_hash = _next_generation_and_previous(dest_target)
                commit = {
                    "schemaVersion": 4,
                    "targetGeneration": gen,
                    "previousCommitHash": prev_hash,
                    "fencingToken": fencing,
                    "runId": repair_id,
                    "policyId": policy_id,
                    "scheduleSlot": schedule_slot,
                    "slotDigest": hashlib.sha256(f"{policy_id}:{schedule_slot}".encode("utf-8")).hexdigest(),
                    "backupId": backup_id,
                    "committedAt": _utc_iso(),
                    "receiptDigest": dest_receipt_digest,
                    "storageProtocol": "object-set-v1" if dest_receipt.get("objectSetDigest") else backup_object_set.WHOLE_AGE_V1,
                    "objectSetDigest": dest_receipt.get("objectSetDigest"),
                    "controlObjectDigest": dest_receipt.get("controlObjectDigest"),
                }
                commit["commitHash"] = backup_publish._commit_hash(commit)

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
            else:
                # In-Place Committed Copy Healing: DO NOT write Receipt v4, Commit v4, or touch head.json
                pass

            # 7. Update DR ledger evidence
            committed_at_str = _utc_iso()
            backup_dr_ledger.record_logical_recovery_copy(
                target_id=dest_target_id,
                policy_id=policy_id,
                backup_id=backup_id,
                committed_at=committed_at_str,
                object_set_digest=str(source_receipt.get("objectSetDigest") or "") or None,
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

            job = _set_repair_phase(
                job,
                "healthy",
                bytesRepaired=bytes_transferred,
                durationMs=duration_ms,
                error=None,
            )

            return {
                "status": "success",
                "repairId": repair_id,
                "policyId": policy_id,
                "backupId": backup_id,
                "sourceTargetId": source_target_id,
                "destTargetId": dest_target_id,
                "repairMode": repair_mode,
                "bytesRepaired": bytes_transferred,
                "durationMs": duration_ms,
            }
        except RepairLeaseLostError as exc:
            backoff = _compute_repair_backoff_seconds(attempt)
            next_at = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=backoff))
            job = _set_repair_phase(job, "retry-wait", error=str(exc), nextAttemptAt=next_at)
            raise
        except Exception as exc:
            err_msg = str(exc)
            if "source component corrupt" in err_msg.casefold():
                backup_dr_ledger.record_logical_recovery_copy(
                    target_id=source_target_id,
                    policy_id=policy_id,
                    backup_id=backup_id,
                    committed_at=_utc_iso(),
                    state="degraded",
                    recoverable=False,
                    last_verified_at=_utc_iso(),
                )
                job = _set_repair_phase(job, "selecting-source", sourceTargetId=None, error=err_msg)
            elif "cas mismatch" in err_msg.casefold():
                job = _set_repair_phase(job, "scanning-destination", error=err_msg)
            else:
                if attempt >= max_attempts:
                    job = _set_repair_phase(job, "failed-terminal", error=err_msg)
                else:
                    backoff = _compute_repair_backoff_seconds(attempt)
                    next_at = _utc_iso(datetime.now(tz=timezone.utc) + timedelta(seconds=backoff))
                    job = _set_repair_phase(job, "retry-wait", error=err_msg, nextAttemptAt=next_at)
            raise
        finally:
            writer.release()
    finally:
        hold.release()


def process_pending_repairs(*, instance_id: str = "healer-worker", limit: int = 5, now: datetime | None = None) -> dict[str, int]:
    """Drain and resume active/queued ReplicaRepairJobs respecting backoff and attempt limits."""
    current = now or datetime.now(tz=timezone.utc)
    pending = []
    for j in list_repair_jobs(limit=200):
        phase = str(j.get("phase") or "")
        if phase not in REPAIR_ACTIVE_PHASES and phase != "queued":
            continue
        next_at = _parse_iso(j.get("nextAttemptAt"))
        if next_at is not None and current < next_at:
            continue
        max_attempts = int(j.get("maxAttempts") or 5)
        attempt = int(j.get("attempt") or 0)
        if attempt >= max_attempts and phase in {"retry-wait", "queued"}:
            _set_repair_phase(j, "failed-terminal", error="max-attempts-exceeded")
            continue
        pending.append(j)

    processed = succeeded = failed = 0
    for job in pending[:limit]:
        r_id = str(job["repairId"])
        try:
            res = execute_repair_job_instance(r_id, instance_id=instance_id)
            processed += 1
            if res.get("status") == "success":
                succeeded += 1
            else:
                failed += 1
        except Exception:
            processed += 1
            failed += 1
    return {"processed": processed, "succeeded": succeeded, "failed": failed}


# ── Autonomous Desired-State Reconciler ─────────────────────────────────────


def _load_cursors() -> dict[str, Any]:
    if not CURSORS_PATH.is_file():
        return {}
    try:
        return json.loads(CURSORS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cursors(cursors: dict[str, Any]) -> None:
    _atomic_write(CURSORS_PATH, cursors)


def reconcile_policy_replicas(
    policy_id: str,
    *,
    instance_id: str = "reconciler-worker",
    max_points: int = 20,
    max_repairs: int = 2,
) -> dict[str, Any]:
    """Autonomous desired-state reconciler under bounded workload limits with keyset cursor."""
    from deepseek_infra.infra.workspace import backup_policies

    policy = backup_policies.get_policy(policy_id)
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"status": "skipped", "reason": "replication-disabled", "policyId": policy_id}

    target_entries = list(replication.get("targets") or [])
    if not target_entries:
        return {"status": "noop", "policyId": policy_id}

    expected_targets = [str(e["targetId"]) for e in target_entries if isinstance(e, dict) and e.get("targetId")]
    configured_primary = str(policy.get("primaryTargetId") or policy.get("targetId") or "managed-local")
    if configured_primary not in expected_targets:
        expected_targets.append(configured_primary)

    if not expected_targets:
        return {"status": "noop", "policyId": policy_id}

    cursors = _load_cursors()
    p_cursor = cursors.get(policy_id) or {}
    after_committed_at = p_cursor.get("afterCommittedAt")
    after_logical_id = p_cursor.get("afterLogicalId")

    all_copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=500)
    # Group by backupId
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for c in all_copies:
        by_backup.setdefault(str(c["backupId"]), []).append(c)

    def _point_sort_key(b_id: str) -> tuple[str, str]:
        c_list = by_backup[b_id]
        cat = min((str(c.get("committedAt") or "") for c in c_list if c.get("committedAt")), default="")
        return (cat, b_id)

    sorted_backup_ids = sorted(by_backup.keys(), key=_point_sort_key)

    # Keyset cursor filtering
    filtered_backup_ids: list[str] = []
    if after_committed_at or after_logical_id:
        target_tuple = (str(after_committed_at or ""), str(after_logical_id or ""))
        for b_id in sorted_backup_ids:
            if _point_sort_key(b_id) > target_tuple:
                filtered_backup_ids.append(b_id)
    else:
        filtered_backup_ids = list(sorted_backup_ids)

    wrapped = False
    if not filtered_backup_ids and sorted_backup_ids:
        filtered_backup_ids = list(sorted_backup_ids)
        wrapped = True

    scanned = 0
    repairs_triggered = 0
    repairs_succeeded = 0
    repairs_failed = 0
    last_scanned_backup_id: str | None = None
    last_scanned_committed_at: str | None = None

    for backup_id in filtered_backup_ids:
        if scanned >= max_points:
            break
        copy_list = by_backup[backup_id]
        if backup_dr_ledger.is_logical_recovery_point_retired(policy_id, backup_id):
            continue
        scanned += 1
        last_scanned_backup_id = backup_id
        last_scanned_committed_at = str(copy_list[0].get("committedAt") or "")

        existing_targets = {str(c["targetId"]): c for c in copy_list}
        healthy_sources = [
            c for c in copy_list
            if c.get("recoverable") and c.get("state") == "healthy"
        ]
        if not healthy_sources:
            continue
        source_target_id = str(healthy_sources[0]["targetId"])

        for dest_tid in expected_targets:
            if repairs_triggered >= max_repairs:
                break
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

    if last_scanned_backup_id is not None:
        cursors[policy_id] = {
            "lastReconciledAt": _utc_iso(),
            "afterCommittedAt": last_scanned_committed_at,
            "afterLogicalId": last_scanned_backup_id,
            "scannedPoints": scanned,
            "repairsTriggered": repairs_triggered,
            "wrappedAround": wrapped,
        }
        _save_cursors(cursors)

    return {
        "status": "completed",
        "policyId": policy_id,
        "scannedPoints": scanned,
        "repairsTriggered": repairs_triggered,
        "repairsSucceeded": repairs_succeeded,
        "repairsFailed": repairs_failed,
        "wrappedAround": wrapped,
    }


# ── Durable ReplicaRebalanceJob CRUD & Runner ─────────────────────────────────


def _set_rebalance_phase(job: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
    with _LOCK:
        job["phase"] = phase
        job["updatedAt"] = _utc_iso()
        for k, v in extra.items():
            job[k] = v
        _atomic_write(_rebalance_job_path(str(job["jobId"])), job)
        return job


def read_rebalance_job(job_id: str) -> dict[str, Any] | None:
    path = _rebalance_job_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_rebalance_jobs(
    *,
    policy_id: str | None = None,
    backup_id: str | None = None,
    dest_target_id: str | None = None,
    source_target_id: str | None = None,
    phase: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if not REBALANCE_DIR.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for path in sorted(REBALANCE_DIR.glob("*.json"), reverse=True):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not str(data.get("jobId", "")):
            continue
        if policy_id and str(data.get("policyId") or "") != policy_id:
            continue
        if backup_id and str(data.get("backupId") or "") != backup_id:
            continue
        if dest_target_id and str(data.get("destTargetId") or "") != dest_target_id:
            continue
        if source_target_id and str(data.get("sourceTargetId") or "") != source_target_id:
            continue
        if phase and str(data.get("phase") or "") != phase:
            continue
        jobs.append(data)
        if len(jobs) >= limit:
            break
    return jobs


def create_rebalance_job(
    *,
    policy_id: str,
    backup_id: str,
    dest_target_id: str,
    source_target_id: str,
    reason: str = "failure-domain-rebalance",
    prune_source_after: bool = False,
) -> dict[str, Any]:
    with _LOCK:
        REBALANCE_DIR.mkdir(parents=True, exist_ok=True)
        # Check if active rebalance job already exists
        existing = list_rebalance_jobs(
            policy_id=policy_id,
            backup_id=backup_id,
            dest_target_id=dest_target_id,
            limit=10,
        )
        for job in existing:
            if job.get("phase") not in {"complete", "failed"}:
                return job

        job_id = f"rebalance_{uuid.uuid4().hex[:16]}"
        now_str = _utc_iso()
        body: dict[str, Any] = {
            "schemaVersion": REBALANCE_JOB_SCHEMA_VERSION,
            "jobId": job_id,
            "policyId": policy_id,
            "backupId": backup_id,
            "sourceTargetId": source_target_id,
            "destTargetId": dest_target_id,
            "reason": reason,
            "pruneSourceAfter": prune_source_after,
            "phase": "pending",
            "bytesTransferred": 0,
            "createdAt": now_str,
            "updatedAt": now_str,
        }
        _atomic_write(_rebalance_job_path(job_id), body)
        return body


def simulate_copy_removal(
    policy_id: str,
    backup_id: str,
    target_id: str,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Simulate post-deletion topology invariants before removing a recovery copy."""
    from deepseek_infra.infra.workspace import backup_policies, backup_targets

    if policy is None:
        try:
            policy = backup_policies.get_policy(policy_id)
        except Exception:
            policy = {}

    repl = (policy or {}).get("replication") or {}
    placement = (policy or {}).get("placement") or {}
    min_copies = int(repl.get("minCommittedCopies") or 1)
    min_fd = int(repl.get("minFailureDomains") or 1)
    max_copies_per_fd = placement.get("maxCopiesPerFailureDomain") or repl.get("maxCopiesPerFailureDomain")

    all_target_records = {t["targetId"]: t for t in backup_targets.list_targets()}
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
    healthy_before = [c for c in copies if c.get("recoverable") and c.get("state") == "healthy"]
    healthy_after = [c for c in healthy_before if str(c.get("targetId")) != target_id]

    fd_before = {
        str((all_target_records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default")
        for c in healthy_before
    }
    fd_after = {
        str((all_target_records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default")
        for c in healthy_after
    }

    counts_by_fd_after: dict[str, int] = {}
    for c in healthy_after:
        fd_name = str((all_target_records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default")
        counts_by_fd_after[fd_name] = counts_by_fd_after.get(fd_name, 0) + 1

    policy_safe = len(healthy_after) >= min_copies and len(fd_after) >= min_fd
    if max_copies_per_fd is not None and int(max_copies_per_fd) > 0:
        if any(cnt > int(max_copies_per_fd) for cnt in counts_by_fd_after.values()):
            policy_safe = False

    protected_by_hold = is_source_held(target_id, policy_id, backup_id)

    return {
        "healthyCopiesBefore": len(healthy_before),
        "healthyCopiesAfter": len(healthy_after),
        "failureDomainsBefore": len(fd_before),
        "failureDomainsAfter": len(fd_after),
        "copiesInEachDomainAfter": counts_by_fd_after,
        "policySafe": policy_safe and not protected_by_hold,
        "protectedByHold": protected_by_hold,
        "targetId": target_id,
        "backupId": backup_id,
    }


def is_inside_maintenance_window(
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Check if current time falls within the configured policy maintenance window."""
    placement = (policy or {}).get("placement") or {}
    mw = placement.get("maintenanceWindow")
    if not mw or not isinstance(mw, dict):
        return True

    from datetime import timezone
    from zoneinfo import ZoneInfo

    tz_str = str(mw.get("timezone") or "UTC")
    tz: Any
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = timezone.utc

    current = (now or datetime.now(tz=timezone.utc)).astimezone(tz)
    start_str = str(mw.get("start") or "00:00")
    end_str = str(mw.get("end") or "23:59")

    try:
        s_h, s_m = [int(x) for x in start_str.split(":")]
        e_h, e_m = [int(x) for x in end_str.split(":")]
        cur_mins = current.hour * 60 + current.minute
        start_mins = s_h * 60 + s_m
        end_mins = e_h * 60 + e_m

        if start_mins <= end_mins:
            return start_mins <= cur_mins <= end_mins
        else:
            # Wraps around midnight
            return cur_mins >= start_mins or cur_mins <= end_mins
    except Exception:
        return True


def execute_rebalance_job(
    job_id: str,
    *,
    instance_id: str = "rebalance-worker",
) -> dict[str, Any]:
    """Execute a replica rebalance job: copy ciphertext, authenticate, record ledger, optionally prune."""
    job = read_rebalance_job(job_id)
    if job is None:
        raise AppError("rebalance job not found", code=ErrorCode.NOT_FOUND, status=404)

    policy_id = str(job["policyId"])
    backup_id = str(job["backupId"])
    source_target_id = str(job["sourceTargetId"])
    dest_target_id = str(job["destTargetId"])

    job = _set_rebalance_phase(job, "transferring")

    try:
        # Use execute_replica_repair to safely transfer and provision on dest
        repair_res = execute_replica_repair(
            policy_id=policy_id,
            backup_id=backup_id,
            dest_target_id=dest_target_id,
            source_target_id=source_target_id,
            instance_id=instance_id,
            traffic_class=backup_transfer_budget.TrafficClass.P5_REBALANCE_DRAIN,
        )
        if repair_res.get("status") != "success":
            raise AppError(f"Rebalance transfer failed: {repair_res.get('error')}", code=ErrorCode.INTERNAL, status=500)

        job = _set_rebalance_phase(
            job,
            "verifying",
            bytesTransferred=int(repair_res.get("bytesRepaired") or 0),
        )

        dest_target = backup_publish.resolve_target(dest_target_id)
        d_status, d_receipt, d_commit = authenticate_committed_copy(dest_target, policy_id, backup_id)
        if d_status != "authenticated" or d_receipt is None or d_commit is None:
            raise AppError(f"Rebalance destination copy failed authentication: {d_status}", code=ErrorCode.INVALID_REQUEST, status=409)

        job = _set_rebalance_phase(job, "committed")

        # Check if source copy should be pruned using post-delete simulation
        if job.get("pruneSourceAfter"):
            sim = simulate_copy_removal(policy_id, backup_id, source_target_id)
            if sim.get("policySafe") and not sim.get("protectedByHold"):
                job = _set_rebalance_phase(job, "pruning_source")
                # Mark retired in ledger
                copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
                backup_dr_ledger.record_logical_recovery_copy(
                    target_id=source_target_id,
                    policy_id=policy_id,
                    backup_id=backup_id,
                    committed_at=str(copies[0].get("committedAt") if copies else _utc_iso()),
                    state="retired",
                    recoverable=False,
                    last_verified_at=_utc_iso(),
                )

        job = _set_rebalance_phase(job, "complete")
        return {"status": "success", "jobId": job_id, "job": job}
    except Exception as exc:
        job = _set_rebalance_phase(job, "failed", error=str(exc))
        return {"status": "failed", "jobId": job_id, "error": str(exc), "job": job}


def process_pending_rebalances(
    *,
    instance_id: str = "rebalance-worker",
    limit: int = 5,
) -> dict[str, int]:
    pending = list_rebalance_jobs(phase="pending", limit=limit)
    succeeded = 0
    failed = 0
    for job in pending:
        res = execute_rebalance_job(str(job["jobId"]), instance_id=instance_id)
        if res.get("status") == "success":
            succeeded += 1
        else:
            failed += 1
    return {"processed": len(pending), "succeeded": succeeded, "failed": failed}


def rebalance_policy_replicas(
    policy_id: str,
    *,
    instance_id: str = "rebalance-worker",
    max_jobs: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rebalance replicas across failure domains, handle capacity watermarks, and migrate away from draining targets."""
    from deepseek_infra.infra.workspace import backup_policies, backup_targets

    policy = backup_policies.get_policy(policy_id)
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"status": "skipped", "reason": "replication-disabled"}

    if not is_inside_maintenance_window(policy, now=now):
        return {"status": "skipped", "reason": "outside-maintenance-window"}

    min_fd = int(replication.get("minFailureDomains") or 1)
    placement = policy.get("placement") or {}
    max_copies_per_fd = placement.get("maxCopiesPerFailureDomain") or replication.get("maxCopiesPerFailureDomain")
    soft_watermark = float(placement.get("softWatermarkPercent") or 80.0)

    target_entries = list(replication.get("targets") or [])
    all_target_records = {t["targetId"]: t for t in backup_targets.list_targets()}

    # Get active healthy target IDs
    active_targets = [
        str(e["targetId"]) for e in target_entries
        if isinstance(e, dict) and e.get("targetId")
        and (all_target_records.get(str(e["targetId"])) or {}).get("drainState") != "draining"
    ]

    all_copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=200)
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for c in all_copies:
        by_backup.setdefault(str(c["backupId"]), []).append(c)

    jobs_created = 0
    for backup_id, copy_list in by_backup.items():
        if jobs_created >= max_jobs:
            break
        healthy = [c for c in copy_list if c.get("recoverable") and c.get("state") == "healthy"]
        if not healthy:
            continue
        healthy_target_ids = {str(c["targetId"]) for c in healthy}
        current_fds = {
            str((all_target_records.get(tid) or {}).get("failureDomain") or "default")
            for tid in healthy_target_ids
        }

        # Check for draining targets among healthy copies
        has_draining = any(
            (all_target_records.get(tid) or {}).get("drainState") == "draining"
            for tid in healthy_target_ids
        )

        # Check for high capacity utilization (proactive capacity rebalance)
        has_capacity_pressure = False
        constrained_source_tid = None
        for tid in healthy_target_ids:
            cap = backup_targets.probe_target_capacity(tid)
            free_pct = cap.get("freePercent")
            if free_pct is not None and (100.0 - float(free_pct)) >= soft_watermark:
                has_capacity_pressure = True
                constrained_source_tid = tid
                break

        # If failure domains < min_fd or draining or capacity pressure: find candidate target
        needs_rebalance = (len(current_fds) < min_fd) or has_draining or has_capacity_pressure
        if needs_rebalance:
            for cand_tid in active_targets:
                if cand_tid not in healthy_target_ids:
                    cand_fd = str((all_target_records.get(cand_tid) or {}).get("failureDomain") or "default")

                    # Check maxCopiesPerFailureDomain constraint on candidate
                    existing_in_fd = sum(
                        1 for tid in healthy_target_ids
                        if str((all_target_records.get(tid) or {}).get("failureDomain") or "default") == cand_fd
                    )
                    if max_copies_per_fd is not None and int(max_copies_per_fd) > 0:
                        if existing_in_fd + 1 > int(max_copies_per_fd):
                            continue

                    # Check candidate capacity admission
                    cand_cap = backup_targets.probe_target_capacity(cand_tid)
                    cand_free_pct = cand_cap.get("freePercent")
                    if cand_free_pct is not None and (100.0 - float(cand_free_pct)) >= soft_watermark:
                        continue

                    if (
                        (cand_fd not in current_fds)
                        or has_draining
                        or has_capacity_pressure
                    ):
                        src_tid = constrained_source_tid or str(healthy[0]["targetId"])
                        reason = (
                            "drain-migration"
                            if has_draining
                            else (
                                "proactive-capacity-rebalance"
                                if has_capacity_pressure
                                else "failure-domain-diversity"
                            )
                        )
                        job = create_rebalance_job(
                            policy_id=policy_id,
                            backup_id=backup_id,
                            dest_target_id=cand_tid,
                            source_target_id=src_tid,
                            reason=reason,
                            prune_source_after=(has_draining or has_capacity_pressure),
                        )
                        execute_rebalance_job(str(job["jobId"]), instance_id=instance_id)
                        jobs_created += 1
                        break

    return {"status": "completed", "jobsCreated": jobs_created}



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
