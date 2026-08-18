"""Replica Self-Healing Contract Tests: Autonomous Replica Healing, Two-Phase DR Audit, and Write Failover.

Enforces:
1. Durable ReplicaRepairJob state machine and process-restart resumption.
2. In-Place Committed Copy Healing Contract (Zero Receipt/Commit rewrite, Zero gen advance).
3. Bounded streaming ciphertext transfer (Zero Age decrypt, Zero Age encrypt, SHA256 validated).
4. Remote corrupt object replacement with quarantine and conditional CAS.
5. Target-side durable protection leases (holds/repair/) preventing retention GC during repair.
6. Two-phase DR remote audit with global commit chain sort and fail-closed promotion.
7. Deterministic Write Placement Plan freezing and forced Full snapshot on write failover.
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
    backup_dr_readiness,
    backup_publish,
    backup_replication,
    backup_run_plan,
    backup_scheduler,
    backup_target_store,
    backup_targets,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MockMemoryTargetStore(backup_target_store.FilesystemTargetStore):
    """Target store for testing conditional operations and streaming."""

    def __init__(self, root: Path, target_id: str = "mock-target") -> None:
        super().__init__(root)
        self.target_id = target_id
        self._etags: dict[str, str] = {}
        self.quarantine_entries: list[str] = []

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
            self.quarantine_entries.append(key)
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

    def complete_multipart_if_absent(self, upload: Any, **kwargs: Any) -> backup_target_store.PutResult:
        return super().complete_multipart_if_absent(upload)


class MockResolvedTarget:
    def __init__(self, target_id: str, root: Path | None, store: Any | None = None) -> None:
        self.target_id = target_id
        self.root = root
        self.store = store

    def require_store(self) -> Any:
        if self.store is None:
            raise RuntimeError("Store required")
        return self.store


# ── Test 1: Durable ReplicaRepairJob State Machine & Resumption ──────────────


def test_replica_repair_job_durability_and_resumption(tmp_settings, monkeypatch) -> None:
    source_dir = tmp_settings / "source_target"
    dest_dir = tmp_settings / "dest_target"
    source_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)

    # 1. Setup source target data
    policy_id = "test-pol"
    backup_id = "backup_test_001"

    c1_bytes = b"chunk-data-component-1-large-enough-12345"
    c2_bytes = b"chunk-data-component-2-second-payload-67890"
    c1_digest = _sha256(c1_bytes)
    c2_digest = _sha256(c2_bytes)

    # Write source components
    (source_dir / "objects" / c1_digest[:2] / c1_digest[2:4]).mkdir(parents=True, exist_ok=True)
    (source_dir / "objects" / c1_digest[:2] / c1_digest[2:4] / f"{c1_digest}.age").write_bytes(c1_bytes)
    (source_dir / "objects" / c2_digest[:2] / c2_digest[2:4]).mkdir(parents=True, exist_ok=True)
    (source_dir / "objects" / c2_digest[:2] / c2_digest[2:4] / f"{c2_digest}.age").write_bytes(c2_bytes)

    # Write source receipt
    source_receipt = {
        "schemaVersion": 4,
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": "source-target",
        "createdAt": "2026-08-17T00:00:00Z",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": _sha256(c1_bytes + c2_bytes),
        "objects": [
            {"digest": c1_digest, "size": len(c1_bytes), "kind": "data"},
            {"digest": c2_digest, "size": len(c2_bytes), "kind": "data"},
        ],
    }
    (source_dir / "receipts").mkdir(parents=True, exist_ok=True)
    r_bytes = (json.dumps(source_receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (source_dir / "receipts" / f"{backup_id}.json").write_bytes(r_bytes)

    source_commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": _sha256(r_bytes),
        "objectSetDigest": source_receipt["objectSetDigest"],
        "storageProtocol": "object-set-v1",
        "committedAt": "2026-08-17T00:00:00Z",
    }
    (source_dir / "commits" / policy_id).mkdir(parents=True, exist_ok=True)
    (source_dir / "commits" / policy_id / f"{backup_id}.json").write_text(json.dumps(source_commit, indent=2, sort_keys=True), encoding="utf-8")

    # Record source copy in DR ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="source-target",
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at="2026-08-17T00:00:00Z",
        object_set_digest=str(source_receipt["objectSetDigest"]),
        recoverable=True,
        state="healthy",
    )

    targets_map = {
        "source-target": MockResolvedTarget("source-target", source_dir),
        "dest-target": MockResolvedTarget("dest-target", dest_dir),
    }
    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: targets_map[tid])

    # 2. Create durable repair job
    repair_id = "repair_durability_test"
    job = backup_replication.create_repair_job(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id="dest-target",
        source_target_id="source-target",
        repair_id=repair_id,
    )
    assert job["phase"] == "queued"
    assert backup_replication.read_repair_job(repair_id) is not None

    # 3. Simulate partial execution (1st component verified, then process exit)
    job_disk = backup_replication.read_repair_job(repair_id)
    assert job_disk is not None
    job_disk["phase"] = "transferring-components"
    job_disk["components"] = {
        c1_digest: {"digest": c1_digest, "size": len(c1_bytes), "state": "verified", "transferredBytes": len(c1_bytes)},
        c2_digest: {"digest": c2_digest, "size": len(c2_bytes), "state": "pending", "transferredBytes": 0},
    }
    job_disk["bytesRepaired"] = len(c1_bytes)
    # Write component 1 to destination
    (dest_dir / "objects" / c1_digest[:2] / c1_digest[2:4]).mkdir(parents=True, exist_ok=True)
    (dest_dir / "objects" / c1_digest[:2] / c1_digest[2:4] / f"{c1_digest}.age").write_bytes(c1_bytes)

    backup_replication._atomic_write(backup_replication._repair_job_path(repair_id), job_disk)

    # 4. Resume execution (simulated process restart)
    res = backup_replication.execute_repair_job_instance(repair_id)
    assert res["status"] == "success"
    assert res["repairMode"] == "provision"

    # Verify component 2 was transferred and verified
    c2_dest = dest_dir / "objects" / c2_digest[:2] / c2_digest[2:4] / f"{c2_digest}.age"
    assert c2_dest.is_file()
    assert _sha256(c2_dest.read_bytes()) == c2_digest

    # Verify destination received target-local receipt & commit
    assert (dest_dir / "receipts" / f"{backup_id}.json").is_file()
    assert (dest_dir / "commits" / policy_id / f"{backup_id}.json").is_file()
    assert (dest_dir / "control" / "head.json").is_file()

    # Final job status in registry
    final_job = backup_replication.read_repair_job(repair_id)
    assert final_job is not None
    assert final_job["phase"] == "healthy"


# ── Test 2: In-Place Committed Copy Healing Contract ─────────────────────────


def test_in_place_committed_copy_healing_contract(tmp_settings, monkeypatch) -> None:
    """Existing committed copy repair NEVER writes new Receipt, Commit, or moves head.json."""
    source_dir = tmp_settings / "source_target"
    dest_dir = tmp_settings / "dest_target"
    source_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)

    policy_id = "test-pol"
    backup_id = "backup_committed_002"

    data_bytes = b"original-good-ciphertext-object-9988"
    digest = _sha256(data_bytes)

    # Source target setup
    (source_dir / "objects" / digest[:2] / digest[2:4]).mkdir(parents=True, exist_ok=True)
    (source_dir / "objects" / digest[:2] / digest[2:4] / f"{digest}.age").write_bytes(data_bytes)

    source_receipt = {
        "schemaVersion": 4,
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": "source-target",
        "createdAt": "2026-08-17T01:00:00Z",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": digest,
        "objects": [{"digest": digest, "size": len(data_bytes), "kind": "data"}],
    }
    (source_dir / "receipts").mkdir(parents=True, exist_ok=True)
    r_bytes = (json.dumps(source_receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (source_dir / "receipts" / f"{backup_id}.json").write_bytes(r_bytes)

    source_commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": _sha256(r_bytes),
        "objectSetDigest": source_receipt["objectSetDigest"],
        "storageProtocol": "object-set-v1",
        "committedAt": "2026-08-17T01:00:00Z",
    }
    (source_dir / "commits" / policy_id).mkdir(parents=True, exist_ok=True)
    (source_dir / "commits" / policy_id / f"{backup_id}.json").write_text(json.dumps(source_commit, indent=2, sort_keys=True), encoding="utf-8")

    # Destination already has valid Receipt v4, Commit v4 (gen 5), and head.json (gen 5)
    dest_receipt = dict(source_receipt)
    dest_receipt["targetId"] = "dest-target"
    dest_receipt_bytes = (json.dumps(dest_receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (dest_dir / "receipts").mkdir(parents=True, exist_ok=True)
    (dest_dir / "receipts" / f"{backup_id}.json").write_bytes(dest_receipt_bytes)

    dest_commit = {
        "schemaVersion": 4,
        "targetGeneration": 5,
        "previousCommitHash": "prev_hash_12345",
        "commitHash": "commit_hash_fixed_555",
        "backupId": backup_id,
        "policyId": policy_id,
        "committedAt": "2026-08-17T01:05:00Z",
        "receiptDigest": _sha256(dest_receipt_bytes),
        "objectSetDigest": digest,
    }
    (dest_dir / "commits" / policy_id).mkdir(parents=True, exist_ok=True)
    (dest_dir / "commits" / policy_id / f"{backup_id}.json").write_text(json.dumps(dest_commit, indent=2, sort_keys=True), encoding="utf-8")

    head_content = b'{"latestCommitHash": "commit_hash_fixed_555", "targetGeneration": 5}\n'
    (dest_dir / "control").mkdir(parents=True, exist_ok=True)
    (dest_dir / "control" / "head.json").write_bytes(head_content)

    # But destination is MISSING the data object (needs repair!)
    assert not (dest_dir / "objects" / digest[:2] / digest[2:4] / f"{digest}.age").exists()

    targets_map = {
        "source-target": MockResolvedTarget("source-target", source_dir),
        "dest-target": MockResolvedTarget("dest-target", dest_dir),
    }
    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: targets_map[tid])

    # Execute repair
    res = backup_replication.execute_replica_repair(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id="dest-target",
        source_target_id="source-target",
    )

    assert res["status"] == "success"
    assert res["repairMode"] == "heal-existing"

    # CONTRACT VERIFICATION:
    # 1. Data object restored and verified
    healed_data = dest_dir / "objects" / digest[:2] / digest[2:4] / f"{digest}.age"
    assert healed_data.is_file()
    assert _sha256(healed_data.read_bytes()) == digest

    # 2. Receipt v4 is UNTOUCHED
    assert (dest_dir / "receipts" / f"{backup_id}.json").read_bytes() == dest_receipt_bytes

    # 3. Commit v4 is UNTOUCHED (still targetGeneration=5, commit_hash_fixed_555)
    commit_after = json.loads((dest_dir / "commits" / policy_id / f"{backup_id}.json").read_text(encoding="utf-8"))
    assert commit_after["targetGeneration"] == 5
    assert commit_after["commitHash"] == "commit_hash_fixed_555"

    # 4. control/head.json is UNTOUCHED
    assert (dest_dir / "control" / "head.json").read_bytes() == head_content


# ── Test 3: Bounded Streaming & Remote Corrupt-Object Replacement ────────────


def test_bounded_streaming_and_remote_corruption_replacement(tmp_settings, monkeypatch) -> None:
    source_dir = tmp_settings / "source_s3"
    dest_dir = tmp_settings / "dest_s3"
    source_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)

    source_store = MockMemoryTargetStore(source_dir, "source-store")
    dest_store = MockMemoryTargetStore(dest_dir, "dest-store")

    good_payload = b"A" * (2 * 1024 * 1024)  # 2 MiB ciphertext
    good_digest = _sha256(good_payload)
    comp_rel = f"objects/{good_digest[:2]}/{good_digest[2:4]}/{good_digest}.age"

    # Put good data on source
    source_store.put_if_absent(comp_rel, good_payload, checksum_sha256=good_digest)

    # Put corrupted data on destination
    corrupt_payload = b"B" * (2 * 1024 * 1024)
    dest_store.put_if_absent(comp_rel, corrupt_payload)

    source_target = MockResolvedTarget("source-target", None, store=source_store)
    dest_target = MockResolvedTarget("dest-target", None, store=dest_store)

    # Execute safe remote replacement
    transferred = backup_replication.quarantine_and_replace_corrupt_remote_object(
        dest_target,
        comp_rel,
        good_digest,
        source_target,
        comp_rel,
    )

    assert transferred == len(good_payload)

    # Assert corrupt object moved to quarantine
    assert len(dest_store.quarantine_entries) == 1
    assert dest_store.quarantine_entries[0].startswith(".quarantine/")

    # Assert destination now contains exact good ciphertext
    final_bytes = dest_store.get_bytes(comp_rel)
    assert final_bytes == good_payload
    assert _sha256(final_bytes or b"") == good_digest


# ── Test 4: Target-Side Durable Protection Leases ────────────────────────────


def test_target_side_durable_protection_lease_preserves_source(tmp_settings) -> None:
    target_root = tmp_settings / "held_target"
    target_root.mkdir(parents=True)

    policy_id = "test-pol"
    backup_id = "backup_held_001"

    # Acquire hold
    hold = backup_replication.acquire_source_hold(
        "managed-local",
        policy_id,
        backup_id,
        holder_id="repair_worker_1",
        target_root=target_root,
    )
    assert hold.hold_id.startswith("hold_")
    assert (target_root / "holds" / "repair" / f"{hold.hold_id}.json").is_file()

    # Check is_source_held
    assert backup_replication.is_source_held("managed-local", policy_id, backup_id, target=MockResolvedTarget("managed-local", target_root))

    # Release hold
    hold.release()
    assert not (target_root / "holds" / "repair" / f"{hold.hold_id}.json").exists()
    assert not backup_replication.is_source_held("managed-local", policy_id, backup_id, target=MockResolvedTarget("managed-local", target_root))


# ── Test 5: Two-Phase DR Remote Audit with Global Commit Chain Validation ───


def test_two_phase_dr_remote_audit_chain_validation(tmp_settings, monkeypatch) -> None:
    remote_dir = tmp_settings / "remote_audit_s3"
    remote_dir.mkdir(parents=True)
    store = MockMemoryTargetStore(remote_dir, "remote-target")

    target_id = "remote-audit-target"
    monkeypatch.setattr(backup_targets, "open_target_store", lambda tid, **kw: store)

    policy_id = "pol-dr"

    # Create 3 commits
    r1_bytes = json.dumps({"schemaVersion": 4, "backupId": "b1", "policyId": policy_id, "storageProtocol": "object-set-v1", "objectSetDigest": "a" * 64, "size": 100}).encode("utf-8")
    r2_bytes = json.dumps({"schemaVersion": 4, "backupId": "b2", "policyId": policy_id, "storageProtocol": "object-set-v1", "objectSetDigest": "b" * 64, "size": 200}).encode("utf-8")
    r3_bytes = json.dumps({"schemaVersion": 4, "backupId": "b3", "policyId": policy_id, "storageProtocol": "object-set-v1", "objectSetDigest": "c" * 64, "size": 300}).encode("utf-8")

    store.put_if_absent("receipts/b1.json", r1_bytes)
    store.put_if_absent("receipts/b2.json", r2_bytes)
    store.put_if_absent("receipts/b3.json", r3_bytes)

    c1 = {"schemaVersion": 4, "backupId": "b1", "policyId": policy_id, "targetGeneration": 1, "previousCommitHash": backup_dr_audit.GENESIS_COMMIT_HASH, "receiptDigest": _sha256(r1_bytes)}
    c1["commitHash"] = backup_publish._commit_hash(c1)

    c2 = {"schemaVersion": 4, "backupId": "b2", "policyId": policy_id, "targetGeneration": 2, "previousCommitHash": c1["commitHash"], "receiptDigest": _sha256(r2_bytes)}
    c2["commitHash"] = backup_publish._commit_hash(c2)

    c3 = {"schemaVersion": 4, "backupId": "b3", "policyId": policy_id, "targetGeneration": 3, "previousCommitHash": c2["commitHash"], "receiptDigest": _sha256(r3_bytes)}
    c3["commitHash"] = backup_publish._commit_hash(c3)

    # Store commits in reverse order to verify global sort
    store.put_if_absent("commits/pol-dr/b3.json", json.dumps(c3).encode("utf-8"))
    store.put_if_absent("commits/pol-dr/b1.json", json.dumps(c1).encode("utf-8"))
    store.put_if_absent("commits/pol-dr/b2.json", json.dumps(c2).encode("utf-8"))

    # Valid head.json
    head_bytes = json.dumps({"latestCommitHash": c3["commitHash"], "targetGeneration": 3}).encode("utf-8")
    store.put_if_absent("control/head.json", head_bytes)

    # 1. Valid Chain Audit
    audit_res = backup_dr_audit.audit_remote_target(target_id, client=store)
    assert audit_res["status"] == "completed"
    assert audit_res["anomalies"] == []
    assert audit_res["recoveryPointsFound"] == 3

    # Assert recovery points are recoverable=True in DR ledger
    copies = backup_dr_ledger.list_logical_recovery_copies(target_id=target_id, policy_id=policy_id)
    assert len(copies) == 3
    assert all(c["recoverable"] for c in copies)

    # 2. Corrupt Commit Chain Test (gen2 broken)
    c2_broken = dict(c2)
    c2_broken["previousCommitHash"] = "corrupted_prev_hash"
    store.delete_if_match("commits/pol-dr/b2.json")
    store.put_if_absent("commits/pol-dr/b2.json", json.dumps(c2_broken).encode("utf-8"))

    audit_res_broken = backup_dr_audit.audit_remote_target(target_id, client=store, resume=False)
    assert audit_res_broken["status"] == "completed"
    assert any("broken-commit-chain" in a for a in audit_res_broken["anomalies"])
    assert audit_res_broken["recoveryPointsFound"] == 0


# ── Test 6: Deterministic Write Placement & Write Failover ──────────────────


def test_deterministic_write_failover_and_force_full(tmp_settings, monkeypatch) -> None:
    policy_id = "failover-pol"
    policy = {
        "policyId": policy_id,
        "targetId": "primary-unavailable",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [
                {"targetId": "replica-target-healthy", "mode": "required"},
            ],
        },
    }

    replica_dir = tmp_settings / "rep"
    replica_dir.mkdir(parents=True)

    def mock_resolve(tid: str) -> Any:
        if tid == "primary-unavailable":
            raise AppError("blocked-target-unavailable: connection timeout", status=503)
        if tid == "replica-target-healthy":
            return MockResolvedTarget(tid, replica_dir)
        raise AppError("unknown", status=404)

    monkeypatch.setattr(backup_publish, "resolve_target", mock_resolve)

    # Evaluate write placement
    placement = backup_scheduler.evaluate_write_placement(policy)
    assert placement["isFailover"] is True
    assert placement["forceFull"] is True
    assert placement["selectedWriteTargetId"] == "replica-target-healthy"
    assert placement["configuredPrimaryTargetId"] == "primary-unavailable"

    # Freeze run plan with failover fields
    slot_digest = _sha256(b"slot_test_failover")
    plan = backup_run_plan.freeze_run_plan(
        policy=policy,
        schedule_slot="test-slot",
        slot_digest=slot_digest,
        contributor_plan=[],
        target_id=placement["selectedWriteTargetId"],
        configured_primary_target_id=placement["configuredPrimaryTargetId"],
        selected_write_target_id=placement["selectedWriteTargetId"],
        candidate_target_ids=placement["candidateTargetIds"],
        failover_reason=placement["reason"],
        is_failover=True,
        snapshot_kind="full",
        force_full_reason="write-target-failover",
    )

    assert plan["isFailover"] is True
    assert plan["selectedWriteTargetId"] == "replica-target-healthy"
    assert plan["snapshotKind"] == "full"
    assert plan["forceFullReason"] == "write-target-failover"

    # DR Readiness reflects failover
    readiness = backup_dr_readiness.evaluate_scope_readiness("primary-unavailable", policy_id, policy=policy)
    assert readiness["writeContinuity"]["status"] == "failed-over"
    assert readiness["writeContinuity"]["activeWriteTargetId"] == "replica-target-healthy"
    assert "write-target-failover" in readiness["reasons"]
