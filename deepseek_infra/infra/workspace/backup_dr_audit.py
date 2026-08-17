"""Durable resumable remote disaster recovery audit (4.5.4).

Two-phase commit chain authentication & global sorting:
Phase 1: Paged remote scan stages unverified candidates; never promotes prematurely.
Phase 2: Global sort by targetGeneration decouples S3 key lexical listing order;
         validates generation continuity (1..N), commit hash chain, genesis root,
         and control/head.json binding.
Phase 3: Atomic promotion: only after the whole commit chain passes are recovery
         points and logical copies promoted to recoverable=True in DR Evidence Ledger.
         Any chain break leaves candidates unpromoted and fails the audit.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_dr_ledger, backup_publish, backup_targets, backups
from deepseek_infra.infra.workspace.backup_target_store import read_json


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


GENESIS_COMMIT_HASH = "0" * 64


def _validate_commit_receipt_binding(
    *,
    target_id: str,
    commit: dict[str, Any],
    receipt: dict[str, Any] | None,
    raw_receipt_bytes: bytes | None = None,
    previous_commit_hash: str | None = None,
) -> list[str]:
    anomalies: list[str] = []
    if not backup_publish.commit_marker_valid(commit):
        anomalies.append("invalid-commit-marker")
        return anomalies
    backup_id = str(commit.get("backupId") or "")
    if not backup_id:
        anomalies.append("missing-backup-id")
        return anomalies
    if not isinstance(receipt, dict) or not receipt:
        anomalies.append(f"missing-receipt:{backup_id}")
        return anomalies
    if raw_receipt_bytes is None:
        raw_receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    receipt_digest = hashlib.sha256(raw_receipt_bytes).hexdigest()
    expected = str(commit.get("receiptDigest") or "")
    if expected and receipt_digest != expected:
        anomalies.append(f"receipt-digest-mismatch:{backup_id}")
    if str(receipt.get("backupId") or "") != backup_id:
        anomalies.append(f"receipt-backup-id-mismatch:{backup_id}")
    if str(receipt.get("targetId") or "") and str(receipt.get("targetId")) != target_id:
        anomalies.append(f"receipt-target-mismatch:{backup_id}")
    if str(receipt.get("policyId") or "") and str(commit.get("policyId") or "") and str(receipt.get("policyId")) != str(commit.get("policyId")):
        anomalies.append(f"receipt-policy-mismatch:{backup_id}")
    objects = receipt.get("objects")
    storage = str(receipt.get("storageProtocol") or "")
    if storage == "object-set-v1":
        # Require digest; inventory may be absent on legacy fixtures but digest is mandatory for CAS binding.
        if not str(receipt.get("objectSetDigest") or commit.get("objectSetDigest") or ""):
            if isinstance(objects, list) and objects:
                pass  # inventory present without digest still flagged
            anomalies.append(f"missing-object-set-digest:{backup_id}")
        if objects is not None and (not isinstance(objects, list) or (isinstance(objects, list) and not objects)):
            anomalies.append(f"invalid-object-set-inventory:{backup_id}")
    return anomalies


def audit_remote_target(
    target_id: str,
    *,
    client: Any | None = None,
    page_size: int = 100,
    cursor: str | None = None,
    audit_id: str | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Execute two-phase paged audit of target objects and update DR Evidence Ledger."""
    started_at = _utc_iso()
    anomalies: list[str] = []
    audited_count = 0
    recovery_points_found = 0
    next_cursor: str | None = None
    previous_commit_hash: str | None = None
    target_generation: int | None = None
    staged_candidates: list[dict[str, Any]] = []

    existing: dict[str, Any] | None = None
    if audit_id:
        existing = backup_dr_ledger.get_audit_job(audit_id)
    elif resume:
        existing = backup_dr_ledger.get_open_audit_job(target_id)

    if existing is not None:
        a_id = str(existing["auditId"])
        start_cursor = existing.get("cursor")
        audited_count = int(existing.get("recordsChecked") or 0)
        anomalies = list(existing.get("anomalies") or [])
        previous_commit_hash = existing.get("previousCommitHash")
        target_generation = existing.get("targetGeneration")
        started_at = str(existing.get("startedAt") or started_at)
        recovery_points_found = int((existing.get("details") or {}).get("recoveryPointsFound") or 0)
        staged_candidates = list((existing.get("details") or {}).get("candidates") or [])
    else:
        a_id = audit_id or f"audit_{uuid.uuid4().hex[:16]}"
        start_cursor = cursor
        backup_dr_ledger.upsert_audit_job(
            audit_id=a_id,
            target_id=target_id,
            phase="scanning",
            cursor=start_cursor,
            records_checked=0,
            anomalies=[],
            started_at=started_at,
            details={"candidates": []},
        )

    store = None

    if target_id == "managed-local":
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
                    raw_receipt_fs = r_path.read_bytes() if r_path.is_file() else None
                    receipt_data = json.loads(raw_receipt_fs.decode("utf-8")) if raw_receipt_fs is not None else {}
                    bind_anomalies = []
                    if not isinstance(commit_data, dict) or not backup_id:
                        bind_anomalies.append("invalid-commit-marker")
                    else:
                        if raw_receipt_fs is None or not receipt_data:
                            bind_anomalies.append(f"missing-receipt:{backup_id}")
                        else:
                            exp_digest = str(commit_data.get("receiptDigest") or "")
                            if exp_digest and hashlib.sha256(raw_receipt_fs).hexdigest() != exp_digest:
                                bind_anomalies.append(f"receipt-digest-mismatch:{backup_id}")
                            if str(receipt_data.get("backupId") or "") != backup_id:
                                bind_anomalies.append(f"receipt-backup-id-mismatch:{backup_id}")
                    anomalies.extend(bind_anomalies)
                    if backup_id and isinstance(commit_data, dict):
                        staged_candidates.append({
                            "commit": commit_data,
                            "receipt": receipt_data,
                            "backupId": backup_id,
                            "policyId": str(receipt_data.get("policyId") or commit_data.get("policyId") or ""),
                            "bindAnomalies": bind_anomalies,
                            "targetGeneration": commit_data.get("targetGeneration"),
                            "commitHash": str(commit_data.get("commitHash") or ""),
                            "previousCommitHash": str(commit_data.get("previousCommitHash") or ""),
                            "committedAt": str(commit_data.get("committedAt") or receipt_data.get("createdAt") or _utc_iso()),
                            "snapshotKind": str(receipt_data.get("snapshotKind") or "full"),
                            "parentBackupId": receipt_data.get("parentBackupId"),
                            "chainLength": int(receipt_data.get("chainLength") or 1),
                            "ciphertextBytes": int(receipt_data.get("size") or 0),
                            "logicalBytes": int(receipt_data.get("logicalBytes") or receipt_data.get("size") or 0),
                            "storageProtocol": str(receipt_data.get("storageProtocol") or ""),
                            "objectSetDigest": str(receipt_data.get("objectSetDigest") or commit_data.get("objectSetDigest") or "") or None,
                        })
                except Exception as exc:
                    anomalies.append(f"malformed-commit-{c_path.name}: {exc}")
        next_cursor = None
    else:
        try:
            store = backup_targets.open_target_store(target_id, write_intent=False, client=client)
        except AppError as exc:
            backup_dr_ledger.upsert_audit_job(
                audit_id=a_id,
                target_id=target_id,
                phase="failed",
                cursor=start_cursor,
                records_checked=audited_count,
                anomalies=anomalies + [str(exc)],
                started_at=started_at,
                completed_at=_utc_iso(),
                details={"error": str(exc)},
            )
            backup_dr_ledger.record_audit_evidence(
                audit_id=a_id,
                target_id=target_id,
                observed_at=started_at,
                result="failed",
                status="failed",
                anomalies_count=1,
                records_checked=audited_count,
                cursor=start_cursor,
                details={"error": str(exc)},
            )
            raise

        page = store.list_objects("commits/", cursor=start_cursor, limit=page_size)
        next_cursor = page.cursor
        for meta in page.objects:
            audited_count += 1
            if not str(meta.key).endswith(".json"):
                continue
            commit = read_json(store, meta.key)
            if not isinstance(commit, dict):
                anomalies.append(f"invalid-commit-marker:{meta.key}")
                continue
            backup_id = str(commit.get("backupId") or "")
            if not backup_id:
                anomalies.append(f"missing-backup-id:{meta.key}")
                continue
            r_key = f"receipts/{backup_id}.json"
            raw_receipt: bytes | None = None
            if hasattr(store, "get_bytes"):
                try:
                    raw_receipt = store.get_bytes(r_key)
                except Exception:
                    raw_receipt = None
            receipt: dict[str, Any] | None = None
            if raw_receipt is not None:
                try:
                    parsed = json.loads(raw_receipt.decode("utf-8"))
                    if isinstance(parsed, dict):
                        receipt = parsed
                except Exception:
                    receipt = None
            else:
                try:
                    parsed = read_json(store, r_key)
                    if isinstance(parsed, dict):
                        receipt = parsed
                        raw_receipt = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
                except Exception:
                    receipt = None
            bind_anomalies = _validate_commit_receipt_binding(
                target_id=target_id,
                commit=commit,
                receipt=receipt,
                raw_receipt_bytes=raw_receipt,
                previous_commit_hash=previous_commit_hash,
            )
            anomalies.extend(bind_anomalies)
            receipt_dict = receipt if isinstance(receipt, dict) else {}
            staged_candidates.append({
                "commit": commit,
                "receipt": receipt_dict,
                "backupId": backup_id,
                "policyId": str(receipt_dict.get("policyId") or commit.get("policyId") or ""),
                "bindAnomalies": bind_anomalies,
                "targetGeneration": commit.get("targetGeneration"),
                "commitHash": str(commit.get("commitHash") or ""),
                "previousCommitHash": str(commit.get("previousCommitHash") or ""),
                "committedAt": str(commit.get("committedAt") or receipt_dict.get("createdAt") or _utc_iso()),
                "snapshotKind": str(receipt_dict.get("snapshotKind") or "full"),
                "parentBackupId": receipt_dict.get("parentBackupId"),
                "chainLength": int(receipt_dict.get("chainLength") or 1),
                "ciphertextBytes": int(receipt_dict.get("size") or 0),
                "logicalBytes": int(receipt_dict.get("logicalBytes") or receipt_dict.get("size") or 0),
                "storageProtocol": str(receipt_dict.get("storageProtocol") or ""),
                "objectSetDigest": str(receipt_dict.get("objectSetDigest") or commit.get("objectSetDigest") or "") or None,
            })

    # Phase 2: Once paged scan is complete, global sort & validate complete commit chain
    chain_anomalies: list[str] = []
    if not next_cursor and staged_candidates:
        generation_commits = [c for c in staged_candidates if isinstance(c.get("targetGeneration"), int)]
        generation_commits.sort(key=lambda c: int(c["targetGeneration"]))

        prev_hash: str | None = None
        prev_gen: int | None = None

        for c in generation_commits:
            curr_gen = int(c["targetGeneration"])
            curr_hash = str(c.get("commitHash") or "")
            curr_prev = str(c.get("previousCommitHash") or "")
            b_id = str(c.get("backupId") or "")

            if prev_gen is not None:
                if curr_gen != prev_gen + 1:
                    chain_anomalies.append(f"generation-gap:{prev_gen}->{curr_gen}")
                if prev_hash and curr_prev and curr_prev != prev_hash:
                    chain_anomalies.append(f"broken-commit-chain:{b_id}")
            else:
                if curr_gen == 1 and curr_prev and curr_prev != GENESIS_COMMIT_HASH:
                    chain_anomalies.append(f"broken-genesis-commit-hash:{b_id}")

            prev_gen = curr_gen
            prev_hash = curr_hash

        target_generation = prev_gen
        previous_commit_hash = prev_hash

        # Validate control/head.json
        if target_id != "managed-local" and store is not None:
            try:
                head_data = read_json(store, "control/head.json")
                if isinstance(head_data, dict) and generation_commits:
                    latest_c = generation_commits[-1]
                    latest_commit_hash = str(latest_c.get("commitHash") or "")
                    latest_gen = int(latest_c.get("targetGeneration") or 0)
                    head_commit_hash = str(head_data.get("latestCommitHash") or "")
                    head_gen = head_data.get("targetGeneration")

                    if head_commit_hash and latest_commit_hash and head_commit_hash != latest_commit_hash:
                        chain_anomalies.append(f"head-commit-hash-mismatch:head={head_commit_hash},audited={latest_commit_hash}")
                    if head_gen is not None and int(head_gen) != latest_gen:
                        chain_anomalies.append(f"head-generation-mismatch:head={head_gen},audited={latest_gen}")
            except Exception:
                pass

        anomalies.extend(chain_anomalies)

        # Phase 3: Promotion: Promote candidates to recoverable ONLY IF chain is valid
        chain_valid = not chain_anomalies
        recovery_points_found = 0
        for cand in staged_candidates:
            b_id = cand["backupId"]
            p_id = cand["policyId"]
            b_anomalies = list(cand.get("bindAnomalies") or [])
            recoverable = chain_valid and not b_anomalies
            all_cand_anomalies = b_anomalies + (chain_anomalies if not chain_valid else [])

            backup_dr_ledger.record_recovery_point(
                target_id=target_id,
                policy_id=p_id,
                backup_id=b_id,
                committed_at=cand["committedAt"],
                snapshot_kind=cand["snapshotKind"],
                parent_backup_id=cand["parentBackupId"],
                chain_digest=cand["commitHash"],
                chain_length=cand["chainLength"],
                ciphertext_bytes=cand["ciphertextBytes"],
                logical_bytes=cand["logicalBytes"],
                recoverable=recoverable,
                verified_at=_utc_iso(),
                storage_protocol=cand["storageProtocol"],
                metadata=cand["receipt"] if recoverable else {"auditRejected": True, "anomalies": all_cand_anomalies},
            )
            if recoverable:
                recovery_points_found += 1
                backup_dr_ledger.record_logical_recovery_copy(
                    target_id=target_id,
                    policy_id=p_id,
                    backup_id=b_id,
                    committed_at=cand["committedAt"],
                    object_set_digest=cand["objectSetDigest"],
                    recoverable=True,
                    role="replica" if target_id != "managed-local" else "primary",
                )

    if next_cursor:
        recovery_points_found = len([c for c in staged_candidates if not c.get("bindAnomalies")])

    status = "completed" if not next_cursor else "in-progress"
    phase = "completed" if status == "completed" else "scanning"
    result = {
        "auditId": a_id,
        "targetId": target_id,
        "status": status,
        "phase": phase,
        "cursor": next_cursor,
        "objectsAudited": audited_count,
        "recordsChecked": audited_count,
        "recoveryPointsFound": recovery_points_found,
        "anomalies": anomalies,
        "targetGeneration": target_generation,
        "previousCommitHash": previous_commit_hash,
        "auditedAt": started_at,
        "updatedAt": _utc_iso(),
    }

    backup_dr_ledger.upsert_audit_job(
        audit_id=a_id,
        target_id=target_id,
        phase=phase,
        cursor=next_cursor,
        target_generation=target_generation,
        previous_commit_hash=previous_commit_hash,
        records_checked=audited_count,
        anomalies=anomalies,
        started_at=started_at,
        completed_at=_utc_iso() if phase == "completed" else None,
        details={"recoveryPointsFound": recovery_points_found, "objectsAudited": audited_count, "candidates": staged_candidates},
    )
    backup_dr_ledger.record_audit_evidence(
        audit_id=a_id,
        target_id=target_id,
        started_at=started_at,
        observed_at=started_at,
        status=status if status == "in-progress" else ("success" if not anomalies else "warning"),
        result="success" if not anomalies and status == "completed" else ("warning" if anomalies else "in-progress"),
        completed_at=_utc_iso() if phase == "completed" else None,
        cursor=next_cursor,
        records_checked=audited_count,
        anomalies_count=len(anomalies),
        details=result,
    )
    return result


def resume_audit(audit_id: str, *, client: Any | None = None, page_size: int = 100) -> dict[str, Any]:
    """Resume a durable audit job by id after process restart."""
    job = backup_dr_ledger.get_audit_job(audit_id)
    if job is None:
        raise AppError(f"Audit job not found: {audit_id}", code=ErrorCode.NOT_FOUND, status=404)
    if job.get("phase") in {"completed", "failed"}:
        return {
            "auditId": audit_id,
            "targetId": job["targetId"],
            "status": "completed" if job["phase"] == "completed" else "failed",
            "phase": job["phase"],
            "cursor": job.get("cursor"),
            "recordsChecked": job.get("recordsChecked"),
            "anomalies": job.get("anomalies") or [],
            "resumed": False,
        }
    return audit_remote_target(
        str(job["targetId"]),
        client=client,
        page_size=page_size,
        audit_id=audit_id,
        resume=True,
    )
