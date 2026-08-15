"""Explicit resumable remote disaster recovery audit (4.5.1 Gate I).

Performs paged reconciliation of remote objects against local DR Evidence Ledger.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_dr_ledger, backup_publish, backup_targets, backups
from deepseek_infra.infra.workspace.backup_target_store import read_json


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def audit_remote_target(
    target_id: str,
    *,
    client: Any | None = None,
    page_size: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Execute paged audit of remote target objects and update DR Evidence Ledger."""
    started_at = _utc_iso()
    anomalies: list[str] = []
    audited_count = 0
    recovery_points_found = 0
    next_cursor: str | None = None

    if target_id == "managed-local":
        # Filesystem audit of local backup directory
        root = backups.BACKUP_DIR
        commits_dir = root / "commits"
        receipts_dir = root / "receipts"
        if commits_dir.is_dir():
            for c_path in sorted(commits_dir.glob("*/*.json")):
                audited_count += 1
                try:
                    commit_data = json.loads(c_path.read_text(encoding="utf-8"))
                    backup_id = str(commit_data.get("backupId") or "")
                    r_path = receipts_dir / f"{backup_id}.json"
                    receipt_data = json.loads(r_path.read_text(encoding="utf-8")) if r_path.is_file() else {}
                    if backup_id:
                        logical_bytes = int(receipt_data.get("logicalBytes") or receipt_data.get("size") or 0)
                        ciphertext_bytes = int(receipt_data.get("size") or 0)
                        backup_dr_ledger.record_recovery_point(
                            target_id="managed-local",
                            policy_id=str(receipt_data.get("policyId") or ""),
                            backup_id=backup_id,
                            committed_at=str(commit_data.get("committedAt") or receipt_data.get("createdAt") or _utc_iso()),
                            snapshot_kind=str(receipt_data.get("snapshotKind") or "full"),
                            parent_backup_id=receipt_data.get("parentBackupId"),
                            chain_digest=str(commit_data.get("commitHash") or ""),
                            chain_length=int(receipt_data.get("chainLength") or 1),
                            ciphertext_bytes=ciphertext_bytes,
                            logical_bytes=logical_bytes,
                            recoverable=True,
                            verified_at=_utc_iso(),
                            storage_protocol=str(receipt_data.get("storageProtocol") or ""),
                            metadata=receipt_data,
                        )
                        recovery_points_found += 1
                except Exception as exc:
                    anomalies.append(f"malformed-commit-{c_path.name}: {exc}")
    else:
        try:
            store = backup_targets.open_target_store(target_id, write_intent=False, client=client)
        except AppError as exc:
            backup_dr_ledger.record_audit_evidence(
                target_id=target_id,
                observed_at=started_at,
                result="failed",
                anomalies_count=1,
                details={"error": str(exc)},
            )
            raise

        page = store.list_objects("commits/", cursor=cursor, limit=page_size)
        next_cursor = page.cursor
        for meta in page.objects:
            audited_count += 1
            if not str(meta.key).endswith(".json"):
                continue
            commit = read_json(store, meta.key)
            if not isinstance(commit, dict) or not backup_publish.commit_marker_valid(commit):
                anomalies.append(f"invalid-commit-marker:{meta.key}")
                continue
            backup_id = str(commit.get("backupId") or "")
            if not backup_id:
                continue
            r_key = f"receipts/{backup_id}.json"
            receipt = read_json(store, r_key) or {}
            logical_bytes = int(receipt.get("logicalBytes") or receipt.get("size") or 0)
            ciphertext_bytes = int(receipt.get("size") or 0)
            backup_dr_ledger.record_recovery_point(
                target_id=target_id,
                policy_id=str(receipt.get("policyId") or ""),
                backup_id=backup_id,
                committed_at=str(commit.get("committedAt") or receipt.get("createdAt") or _utc_iso()),
                snapshot_kind=str(receipt.get("snapshotKind") or "full"),
                parent_backup_id=receipt.get("parentBackupId"),
                chain_digest=str(commit.get("commitHash") or ""),
                chain_length=int(receipt.get("chainLength") or 1),
                ciphertext_bytes=ciphertext_bytes,
                logical_bytes=logical_bytes,
                recoverable=True,
                verified_at=_utc_iso(),
                storage_protocol=str(receipt.get("storageProtocol") or ""),
                metadata=receipt,
            )
            recovery_points_found += 1

    status = "completed" if not next_cursor else "in-progress"
    result = {
        "targetId": target_id,
        "status": status,
        "cursor": next_cursor,
        "objectsAudited": audited_count,
        "recoveryPointsFound": recovery_points_found,
        "anomalies": anomalies,
        "auditedAt": started_at,
    }

    backup_dr_ledger.record_audit_evidence(
        target_id=target_id,
        observed_at=started_at,
        result="success" if not anomalies else "warning",
        anomalies_count=len(anomalies),
        details=result,
    )
    return result
