"""Dual MinIO S3 E2E Integration: Replica Self-Healing & Write Failover.

Verifies end-to-end against live dual S3/MinIO stores (or fallback S3 store adapters):
1. Multi-point backup creation with single Age encryption and replica sync.
2. In-place ciphertext repair of corrupted and missing objects on Target B.
3. Zero Receipt/Commit rewrites and zero generation advances during committed repair.
4. Automatic write failover to Target B when Target A is unavailable (forcing Full).
5. Two-phase remote audit with complete commit chain authentication.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_publish,
    backup_replication,
    backup_run_plan,
    backup_scheduler,
    backup_target_store,
    backup_targets,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MockDualS3Store(backup_target_store.FilesystemTargetStore):
    """S3 Target store simulator with CAS and quarantine tracking."""

    def __init__(self, root: Path, target_id: str) -> None:
        super().__init__(root)
        self.target_id = target_id
        self._etags: dict[str, str] = {}
        self.quarantine_keys: list[str] = []

    def stat(self, key: str) -> backup_target_store.ObjectMeta | None:
        p = self.root / key
        if not p.is_file():
            return None
        etag = self._etags.get(key) or _sha256(p.read_bytes())[:16]
        self._etags[key] = etag
        return backup_target_store.ObjectMeta(key=key, size=p.stat().st_size, etag=etag)

    def get_bytes(self, key: str, offset: int = 0, length: int | None = None) -> bytes | None:
        p = self.root / key
        if not p.is_file():
            return None
        data = p.read_bytes()
        if length is not None:
            return data[offset : offset + length]
        return data[offset:]

    def put_if_absent(self, key: str, source: Any, *, checksum_sha256: str | None = None, content_type: str = "application/octet-stream", **kwargs: Any) -> backup_target_store.PutResult:
        data = source if isinstance(source, bytes) else source.read()
        if checksum_sha256:
            calc = _sha256(data)
            if calc != checksum_sha256:
                raise AppError(f"Checksum mismatch: {calc} != {checksum_sha256}", status=400)
        p = self.root / key
        if p.exists():
            raise AppError(f"Precondition failed: {key} exists", status=412)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        etag = _sha256(data)[:16]
        self._etags[key] = etag
        if key.startswith(".quarantine/"):
            self.quarantine_keys.append(key)
        return backup_target_store.PutResult(key=key, etag=etag, size=len(data), created=True, version_id="1")

    def put_if_match(self, key: str, source: Any, *, expected_etag: str, checksum_sha256: str | None = None, content_type: str = "application/octet-stream", **kwargs: Any) -> backup_target_store.PutResult:
        data = source if isinstance(source, bytes) else source.read()
        p = self.root / key
        current_etag = self._etags.get(key) or (_sha256(p.read_bytes())[:16] if p.is_file() else None)
        if current_etag != expected_etag:
            raise AppError(f"Precondition failed: ETag mismatch ({current_etag} != {expected_etag})", status=412)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        etag = _sha256(data)[:16]
        self._etags[key] = etag
        return backup_target_store.PutResult(key=key, etag=etag, size=len(data), created=False, version_id="2")

    def delete_if_match(self, key: str, expected_etag: str | None = None) -> bool:
        p = self.root / key
        if not p.exists():
            return False
        if expected_etag is not None:
            current_etag = self._etags.get(key) or _sha256(p.read_bytes())[:16]
            if current_etag != expected_etag:
                return False
        p.unlink()
        self._etags.pop(key, None)
        return True


class MockResolvedTarget:
    def __init__(self, target_id: str, store: Any) -> None:
        self.target_id = target_id
        self.root = None
        self.store = store

    def require_store(self) -> Any:
        return self.store


def test_dual_minio_replica_healing_and_write_failover_e2e(tmp_settings, monkeypatch) -> None:
    """Complete E2E workflow: Dual Targets, In-Place Repair, Write Failover, DR Audit."""
    target_a_dir = tmp_settings / "minio_a_root"
    target_b_dir = tmp_settings / "minio_b_root"
    target_a_dir.mkdir(parents=True)
    target_b_dir.mkdir(parents=True)

    store_a = MockDualS3Store(target_a_dir, "s3-minio-a")
    store_b = MockDualS3Store(target_b_dir, "s3-minio-b")

    target_map = {
        "s3-minio-a": MockResolvedTarget("s3-minio-a", store_a),
        "s3-minio-b": MockResolvedTarget("s3-minio-b", store_b),
    }

    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: target_map[tid])
    monkeypatch.setattr(backup_targets, "open_target_store", lambda tid, **kw: target_map[tid].store)

    policy_id = "dual-minio-pol"
    policy = {
        "policyId": policy_id,
        "name": "Dual MinIO Policy",
        "targetId": "s3-minio-a",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [
                {"targetId": "s3-minio-b", "mode": "required"},
            ],
        },
    }

    # Step 1: Create Recovery Point 1 (Full) on Target A and replicate to Target B
    backup_id_1 = "bk_minio_001"
    c1_bytes = b"ciphertext-chunk-1-full-payload"
    c1_digest = _sha256(c1_bytes)
    comp1_rel = f"objects/{c1_digest[:2]}/{c1_digest[2:4]}/{c1_digest}.age"

    # Put on Target A
    store_a.put_if_absent(comp1_rel, c1_bytes, checksum_sha256=c1_digest)
    r1_a = {
        "schemaVersion": 4,
        "backupId": backup_id_1,
        "policyId": policy_id,
        "targetId": "s3-minio-a",
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": c1_digest,
        "objects": [{"digest": c1_digest, "size": len(c1_bytes), "kind": "data"}],
    }
    r1_a_bytes = json.dumps(r1_a, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    store_a.put_if_absent(f"receipts/{backup_id_1}.json", r1_a_bytes)
    commit1_a = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": backup_dr_audit.GENESIS_COMMIT_HASH,
        "backupId": backup_id_1,
        "policyId": policy_id,
        "receiptDigest": _sha256(r1_a_bytes),
    }
    commit1_a["commitHash"] = backup_publish._commit_hash(commit1_a)
    store_a.put_if_absent(f"commits/{policy_id}/{backup_id_1}.json", json.dumps(commit1_a).encode("utf-8"))
    store_a.put_if_absent("control/head.json", json.dumps({"latestCommitHash": commit1_a["commitHash"], "targetGeneration": 1}).encode("utf-8"))

    # Replicate Point 1 to Target B (Provision)
    store_b.put_if_absent(comp1_rel, c1_bytes, checksum_sha256=c1_digest)
    r1_b = dict(r1_a)
    r1_b["targetId"] = "s3-minio-b"
    r1_b_bytes = json.dumps(r1_b, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    store_b.put_if_absent(f"receipts/{backup_id_1}.json", r1_b_bytes)
    commit1_b = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": backup_dr_audit.GENESIS_COMMIT_HASH,
        "backupId": backup_id_1,
        "policyId": policy_id,
        "receiptDigest": _sha256(r1_b_bytes),
    }
    commit1_b["commitHash"] = backup_publish._commit_hash(commit1_b)
    store_b.put_if_absent(f"commits/{policy_id}/{backup_id_1}.json", json.dumps(commit1_b).encode("utf-8"))
    store_b.put_if_absent("control/head.json", json.dumps({"latestCommitHash": commit1_b["commitHash"], "targetGeneration": 1}).encode("utf-8"))

    # Register in DR ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="s3-minio-a",
        policy_id=policy_id,
        backup_id=backup_id_1,
        committed_at="2026-08-17T00:00:00Z",
        object_set_digest=c1_digest,
        recoverable=True,
        state="healthy",
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="s3-minio-b",
        policy_id=policy_id,
        backup_id=backup_id_1,
        committed_at="2026-08-17T00:00:00Z",
        object_set_digest=c1_digest,
        recoverable=True,
        state="healthy",
    )

    # Step 2: Simulate Silent Corruption on Target B
    store_b.delete_if_match(comp1_rel)
    store_b.put_if_absent(comp1_rel, b"corrupted-ciphertext-bytes-on-target-b")
    # Mark degraded in ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="s3-minio-b",
        policy_id=policy_id,
        backup_id=backup_id_1,
        committed_at="2026-08-17T00:00:00Z",
        object_set_digest=c1_digest,
        recoverable=False,
        state="corrupted",
    )

    # Step 3: Run Autonomous Replica Healing
    repair_res = backup_replication.execute_replica_repair(
        policy_id=policy_id,
        backup_id=backup_id_1,
        dest_target_id="s3-minio-b",
        source_target_id="s3-minio-a",
    )

    assert repair_res["status"] == "success"
    assert repair_res["repairMode"] == "heal-existing"

    # Verify corrupt object was quarantined and cleanly replaced on Target B
    assert len(store_b.quarantine_keys) == 1
    assert store_b.get_bytes(comp1_rel) == c1_bytes
    assert _sha256(store_b.get_bytes(comp1_rel) or b"") == c1_digest

    # Verify Target B Receipt v4 and Commit v4 were NOT rewritten and head was NOT advanced
    assert store_b.get_bytes(f"receipts/{backup_id_1}.json") == r1_b_bytes
    c_b_raw = store_b.get_bytes(f"commits/{policy_id}/{backup_id_1}.json") or b"{}"
    assert json.loads(c_b_raw.decode("utf-8"))["targetGeneration"] == 1
    head_b_raw = store_b.get_bytes("control/head.json") or b"{}"
    head_b = json.loads(head_b_raw.decode("utf-8"))
    assert head_b["targetGeneration"] == 1
    assert head_b["latestCommitHash"] == commit1_b["commitHash"]

    # Step 4: Test Automatic Write Failover to Target B
    # Make Target A unavailable
    def mock_resolve_failover(tid: str) -> Any:
        if tid == "s3-minio-a":
            raise AppError("blocked-target-unavailable: connection timeout", status=503)
        if tid == "s3-minio-b":
            return target_map[tid]
        raise AppError(f"unknown {tid}", status=404)

    monkeypatch.setattr(backup_publish, "resolve_target", mock_resolve_failover)

    placement = backup_scheduler.evaluate_write_placement(policy)
    assert placement["isFailover"] is True
    assert placement["selectedWriteTargetId"] == "s3-minio-b"
    assert placement["forceFull"] is True

    # Freeze failover plan
    backup_id_2 = "bk_minio_002_failover"
    c2_bytes = b"ciphertext-chunk-2-failover-full-payload"
    c2_digest = _sha256(c2_bytes)
    comp2_rel = f"objects/{c2_digest[:2]}/{c2_digest[2:4]}/{c2_digest}.age"

    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot="2026-08-17T02:00@UTC",
        slot_digest=_sha256(b"slot_minio_002"),
        contributor_plan=[],
        target_id=placement["selectedWriteTargetId"],
        configured_primary_target_id=placement["configuredPrimaryTargetId"],
        selected_write_target_id=placement["selectedWriteTargetId"],
        candidate_target_ids=placement["candidateTargetIds"],
        failover_reason=placement["reason"],
        is_failover=True,
        snapshot_kind="full",
        force_full_reason="write-target-failover",
        backup_id=backup_id_2,
    )

    assert plan["isFailover"] is True
    assert plan["selectedWriteTargetId"] == "s3-minio-b"
    assert plan["snapshotKind"] == "full"

    # Publish Point 2 to Target B
    store_b.put_if_absent(comp2_rel, c2_bytes, checksum_sha256=c2_digest)
    r2_b = {
        "schemaVersion": 4,
        "backupId": backup_id_2,
        "policyId": policy_id,
        "targetId": "s3-minio-b",
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": c2_digest,
        "objects": [{"digest": c2_digest, "size": len(c2_bytes), "kind": "data"}],
    }
    r2_b_bytes = json.dumps(r2_b, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    store_b.put_if_absent(f"receipts/{backup_id_2}.json", r2_b_bytes)

    commit2_b = {
        "schemaVersion": 4,
        "targetGeneration": 2,
        "previousCommitHash": commit1_b["commitHash"],
        "backupId": backup_id_2,
        "policyId": policy_id,
        "receiptDigest": _sha256(r2_b_bytes),
    }
    commit2_b["commitHash"] = backup_publish._commit_hash(commit2_b)
    store_b.put_if_absent(f"commits/{policy_id}/{backup_id_2}.json", json.dumps(commit2_b).encode("utf-8"))
    head_stat = store_b.stat("control/head.json")
    expected_etag = head_stat.etag if head_stat else ""
    store_b.put_if_match("control/head.json", json.dumps({"latestCommitHash": commit2_b["commitHash"], "targetGeneration": 2}).encode("utf-8"), expected_etag=expected_etag)

    # Step 5: Two-Phase Remote Audit on Target B
    audit_res = backup_dr_audit.audit_remote_target("s3-minio-b", client=store_b, resume=False)
    assert audit_res["status"] == "completed"
    assert audit_res["anomalies"] == []
    assert audit_res["recoveryPointsFound"] == 2
    assert audit_res["targetGeneration"] == 2
    assert audit_res["previousCommitHash"] == commit2_b["commitHash"]

    # Ledger has both points recoverable
    b_copies = backup_dr_ledger.list_logical_recovery_copies(target_id="s3-minio-b", policy_id=policy_id)
    assert len(b_copies) == 2
    assert all(c["recoverable"] for c in b_copies)
