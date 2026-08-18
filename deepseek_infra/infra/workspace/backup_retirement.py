"""Durable Copy Retirement and Physical Garbage Collection (4.5.7).

Provides formal CopyRetirementJob lifecycle, post-delete topology simulation,
active hold checks, and reference-counted physical GC that never deletes
ciphertext shared by multiple retained receipts.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_publish,
)

RETIREMENT_DIR = config.ROOT / ".backup-retirements"
RETIREMENT_DB = RETIREMENT_DIR / "retirements.sqlite3"
RETIREMENTS_DIR = RETIREMENT_DIR
RETIREMENTS_DB = RETIREMENT_DB

RETIREMENT_TERMINAL_PHASES = frozenset({"reclaimed", "rejected", "failed", "superseded"})


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    RETIREMENT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(RETIREMENT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS copy_retirement_jobs (
            job_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            bytes_reclaimed INTEGER DEFAULT 0,
            sim_metadata TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retirement_phase ON copy_retirement_jobs(phase)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_retirement_target ON copy_retirement_jobs(target_id, policy_id)")
    return conn


def create_copy_retirement_job(
    policy_id: str,
    backup_id: str,
    target_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a durable CopyRetirementJob in requested phase."""
    job_id = f"retire_{secrets.token_hex(8)}"
    now = _utc_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO copy_retirement_jobs(job_id, policy_id, backup_id, target_id, phase, created_at, updated_at, error, bytes_reclaimed, sim_metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, '{}')
            """,
            (job_id, policy_id, backup_id, target_id, "requested", now, now),
        )
    return get_copy_retirement_job(job_id) or {}


def get_copy_retirement_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM copy_retirement_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return {
            "jobId": row["job_id"],
            "policyId": row["policy_id"],
            "backupId": row["backup_id"],
            "targetId": row["target_id"],
            "phase": row["phase"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "error": row["error"],
            "bytesReclaimed": row["bytes_reclaimed"],
            "simMetadata": json.loads(row["sim_metadata"] or "{}"),
        }


def list_copy_retirement_jobs(
    *,
    policy_id: str | None = None,
    target_id: str | None = None,
    phase: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List copy retirement jobs matching optional filters."""
    with _connect() as conn:
        query = "SELECT * FROM copy_retirement_jobs WHERE 1=1"
        params: list[Any] = []
        if policy_id:
            query += " AND policy_id = ?"
            params.append(policy_id)
        if target_id:
            query += " AND target_id = ?"
            params.append(target_id)
        if phase:
            query += " AND phase = ?"
            params.append(phase)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "jobId": row["job_id"],
                "policyId": row["policy_id"],
                "backupId": row["backup_id"],
                "targetId": row["target_id"],
                "phase": row["phase"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "error": row["error"],
                "bytesReclaimed": row["bytes_reclaimed"],
                "simMetadata": json.loads(row["sim_metadata"] or "{}"),
            }
            for row in rows
        ]


def _update_job_phase(
    job_id: str,
    phase: str,
    *,
    error: str | None = None,
    bytes_reclaimed: int | None = None,
    sim_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utc_iso()
    with _connect() as conn:
        updates = ["phase = ?", "updated_at = ?"]
        params: list[Any] = [phase, now]
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        if bytes_reclaimed is not None:
            updates.append("bytes_reclaimed = ?")
            params.append(bytes_reclaimed)
        if sim_metadata is not None:
            updates.append("sim_metadata = ?")
            params.append(json.dumps(sim_metadata, ensure_ascii=False, sort_keys=True))
        params.append(job_id)
        conn.execute(f"UPDATE copy_retirement_jobs SET {', '.join(updates)} WHERE job_id = ?", params)
    return get_copy_retirement_job(job_id) or {}


def cancel_copy_retirement_job(job_id: str, *, reason: str = "operator-cancelled") -> dict[str, Any]:
    """Cancel a pending copy retirement job."""
    job = get_copy_retirement_job(job_id)
    if not job:
        raise AppError(f"Retirement job {job_id} not found", code=ErrorCode.NOT_FOUND, status=404)
    if job["phase"] in RETIREMENT_TERMINAL_PHASES:
        return job
    return _update_job_phase(job_id, "cancelled", error=reason)


def execute_copy_retirement_job(
    job_id: str,
    *,
    instance_id: str = "retirement-worker",
) -> dict[str, Any]:
    """Drive a CopyRetirementJob through its safety checks and GC execution."""
    job = get_copy_retirement_job(job_id)
    if not job:
        raise AppError(f"Retirement job {job_id} not found", code=ErrorCode.NOT_FOUND, status=404)

    if job["phase"] in RETIREMENT_TERMINAL_PHASES:
        return job

    policy_id = job["policyId"]
    backup_id = job["backupId"]
    target_id = job["targetId"]

    # 1. checking-topology
    _update_job_phase(job_id, "checking-topology")
    from deepseek_infra.infra.workspace import backup_replication

    sim = backup_replication.simulate_copy_removal(policy_id, backup_id, target_id)
    if not sim.get("policySafe"):
        reason = "topology-safety-constraint: removal would breach minCommittedCopies or minFailureDomains or maxCopiesPerFailureDomain"
        return _update_job_phase(job_id, "rejected", error=reason, sim_metadata=sim)

    # 2. checking-holds
    _update_job_phase(job_id, "checking-holds")
    if sim.get("protectedByHold") or backup_replication.is_source_held(target_id, policy_id, backup_id):
        reason = "copy-protected-by-hold: active drill, recovery, or lease hold on target"
        return _update_job_phase(job_id, "rejected", error=reason, sim_metadata=sim)

    # 3. retiring-control-copy
    _update_job_phase(job_id, "retiring-control-copy")
    # Mark in DR Ledger as unrecoverable / quarantined
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
    target_copy = next((c for c in copies if str(c.get("targetId")) == target_id), None)
    committed_at = str(target_copy.get("committedAt") if target_copy else _utc_iso())

    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at=committed_at,
        state="retired",
        recoverable=False,
        last_verified_at=_utc_iso(),
    )

    # 4. gc-pending -> gc-running (Physical GC with Reference Counting)
    _update_job_phase(job_id, "gc-pending")
    _update_job_phase(job_id, "gc-running")

    bytes_reclaimed = 0
    try:
        target = backup_publish.resolve_target(target_id)
        # Find which ciphertext components are owned by this backup
        r_key = f"receipts/{backup_id}.json"
        c_key = f"commits/{policy_id}/{backup_id}.json"

        raw_receipt = None
        if target.root is not None:
            cands = [
                target.root / "receipts" / f"{backup_id}.json",
                target.root / "receipts" / f"{backup_id}.receipt.json",
                target.root / "receipts" / policy_id / f"{backup_id}.receipt.json",
                target.root / "receipts" / policy_id / f"{backup_id}.json",
            ]
            for c in cands:
                if c.is_file():
                    raw_receipt = c.read_bytes()
                    break
        elif target.store is not None:
            try:
                raw_receipt = target.store.get_bytes(r_key)
            except Exception:
                raw_receipt = None

        receipt_dict: dict[str, Any] = {}
        if raw_receipt:
            try:
                receipt_dict = json.loads(raw_receipt.decode("utf-8"))
            except Exception:
                receipt_dict = {}

        components_to_check: list[str] = []
        if receipt_dict.get("filename"):
            components_to_check.append(str(receipt_dict["filename"]))
        for comp_item in receipt_dict.get("components") or []:
            if isinstance(comp_item, dict):
                digest = comp_item.get("digest")
                if digest:
                    components_to_check.append(f"ciphertext/sha256/{digest}")
                    components_to_check.append(f"objects/sha256/{digest[:2]}/{digest}.age")
                if comp_item.get("path"):
                    components_to_check.append(str(comp_item["path"]))

        # Check all other retained receipts on this target to avoid deleting shared ciphertext!
        retained_components = set()
        if target.root is not None:
            receipts_dir = target.root / "receipts"
            if receipts_dir.is_dir():
                for cand in receipts_dir.rglob("*.json"):
                    if backup_id not in cand.name:
                        try:
                            cand_data = json.loads(cand.read_text(encoding="utf-8"))
                            if isinstance(cand_data, dict):
                                if cand_data.get("filename"):
                                    retained_components.add(str(cand_data["filename"]))
                                for comp_item in cand_data.get("components") or []:
                                    if isinstance(comp_item, dict):
                                        digest = comp_item.get("digest")
                                        if digest:
                                            retained_components.add(f"ciphertext/sha256/{digest}")
                                            retained_components.add(f"objects/sha256/{digest[:2]}/{digest}.age")
                                        if comp_item.get("path"):
                                            retained_components.add(str(comp_item["path"]))
                        except Exception:
                            continue
        elif target.store is not None:
            # For remote stores, query active logical copies from DR ledger to identify live backups
            all_target_copies = backup_dr_ledger.list_logical_recovery_copies(target_id=target_id)
            for c in all_target_copies:
                if c.get("recoverable") and str(c.get("backupId")) != backup_id:
                    other_bid = str(c.get("backupId"))
                    try:
                        other_rb = target.store.get_bytes(f"receipts/{other_bid}.json")
                        if other_rb:
                            other_r = json.loads(other_rb.decode("utf-8"))
                            if isinstance(other_r, dict):
                                if other_r.get("filename"):
                                    retained_components.add(str(other_r["filename"]))
                                for comp_item in other_r.get("components") or []:
                                    if isinstance(comp_item, dict):
                                        digest = comp_item.get("digest")
                                        if digest:
                                            retained_components.add(f"ciphertext/sha256/{digest}")
                                            retained_components.add(f"objects/sha256/{digest[:2]}/{digest}.age")
                                        if comp_item.get("path"):
                                            retained_components.add(str(comp_item["path"]))
                    except Exception:
                        continue

        # Physical deletion only for components NOT referenced by other retained receipts
        for comp in components_to_check:
            if comp not in retained_components:
                if target.root is not None:
                    cp = target.root / comp
                    if cp.is_file():
                        sz = cp.stat().st_size
                        cp.unlink(missing_ok=True)
                        bytes_reclaimed += sz
                elif target.store is not None:
                    stat = target.store.stat(comp)
                    if stat is not None:
                        target.store.delete_if_match(comp, expected_etag=stat.etag)
                        bytes_reclaimed += int(stat.size or 0)

        # Delete the receipt and commit metadata from target
        if target.root is not None:
            for p in [
                target.root / "receipts" / f"{backup_id}.json",
                target.root / "receipts" / f"{backup_id}.receipt.json",
                target.root / "receipts" / policy_id / f"{backup_id}.receipt.json",
                target.root / "receipts" / policy_id / f"{backup_id}.json",
                target.root / "commits" / policy_id / f"{backup_id}.json",
                target.root / "commits" / policy_id / f"{backup_id}.commit.json",
            ]:
                p.unlink(missing_ok=True)
        elif target.store is not None:
            try:
                target.store.delete_if_match(r_key)
            except Exception:
                pass
            try:
                target.store.delete_if_match(c_key)
            except Exception:
                pass

        return _update_job_phase(job_id, "reclaimed", bytes_reclaimed=bytes_reclaimed, sim_metadata=sim)
    except Exception as exc:
        return _update_job_phase(job_id, "failed", error=str(exc))
