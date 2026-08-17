"""Comprehensive Coverage Booster for Autonomous Replica Self-Healing and DR Control Plane."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_dr_readiness,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_target_store,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MockTargetStore(backup_target_store.FilesystemTargetStore):
    def __init__(self, root: Path, target_id: str = "mock-target") -> None:
        super().__init__(root)
        self.target_id = target_id
        self._etags: dict[str, str] = {}
        self.deleted_keys: list[str] = []

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
        self.deleted_keys.append(key)
        return True


class MockTarget:
    def __init__(self, target_id: str, root: Path | None = None, store: Any | None = None) -> None:
        self.target_id = target_id
        self.root = root
        self.store = store

    def require_store(self) -> Any:
        return self.store


def test_source_hold_lifecycle_store_and_filesystem(tmp_settings) -> None:
    """Test source hold acquire, query, expiration, and release on store and filesystem."""
    root = tmp_settings / "t_store"
    root.mkdir(parents=True)
    store = MockTargetStore(root, "store-hold-target")
    target_mock = MockTarget("store-hold-target", store=store)

    # 1. Acquire hold with store
    hold = backup_replication.acquire_source_hold(
        target_id="store-hold-target",
        policy_id="pol-1",
        backup_id="bk-1",
        holder_id="test-holder",
        target_store=store,
        hold_seconds=60,
    )
    assert hold.hold_id
    assert backup_replication.is_source_held("store-hold-target", "pol-1", "bk-1") is True
    assert backup_replication.is_source_held("store-hold-target", "pol-1", "bk-1", target=target_mock) is True

    # Renew hold
    hold.renew(duration_seconds=120)
    assert backup_replication.is_source_held("store-hold-target", "pol-1", "bk-1") is True

    # 2. Release hold
    backup_replication.release_source_hold(hold)
    assert backup_replication.is_source_held("store-hold-target", "pol-1", "bk-1") is False

    # 3. Acquire hold with local target_root
    fs_root = tmp_settings / "fs_root"
    fs_root.mkdir(parents=True)
    fs_target_mock = MockTarget("fs-target", root=fs_root)
    hold_fs = backup_replication.acquire_source_hold(
        target_id="fs-target",
        policy_id="pol-2",
        backup_id="bk-2",
        holder_id="fs-holder",
        target_root=fs_root,
        hold_seconds=60,
    )
    assert (fs_root / "holds" / "repair" / f"{hold_fs.hold_id}.json").is_file()
    assert backup_replication.is_source_held("fs-target", "pol-2", "bk-2") is True
    assert backup_replication.is_source_held("fs-target", "pol-2", "bk-2", target=fs_target_mock) is True

    # Expired check
    future_time = datetime.now(tz=timezone.utc) + timedelta(seconds=1000)
    assert backup_replication.is_source_held("fs-target", "pol-2", "bk-2", target=fs_target_mock, now=future_time) is False

    # Release fs hold via method
    hold_fs.release()
    assert not (fs_root / "holds" / "repair" / f"{hold_fs.hold_id}.json").is_file()
    assert backup_replication.is_source_held("fs-target", "pol-2", "bk-2") is False


def test_repair_job_crud_and_listing(tmp_settings) -> None:
    """Test create, read, list, and phase transitions of ReplicaRepairJob."""
    # Non-existent job
    assert backup_replication.read_repair_job("non-existent-id") is None

    # Corrupt job json file
    bad_file = backup_replication.REPAIRS_DIR / "bad_job.json"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("invalid json content", encoding="utf-8")
    assert backup_replication.read_repair_job("bad_job") is None

    # Create valid job
    job = backup_replication.create_repair_job(
        policy_id="pol_crud",
        backup_id="bk_crud",
        dest_target_id="dest_target",
        source_target_id="src_target",
        repair_id="rep_crud_001",
    )
    assert job["repairId"] == "rep_crud_001"
    assert job["phase"] == "queued"

    read_back = backup_replication.read_repair_job("rep_crud_001")
    assert read_back is not None
    assert read_back["policyId"] == "pol_crud"

    # List jobs with filters
    jobs = backup_replication.list_repair_jobs(policy_id="pol_crud")
    assert len(jobs) >= 1
    assert any(j["repairId"] == "rep_crud_001" for j in jobs)

    jobs_bk = backup_replication.list_repair_jobs(backup_id="bk_crud")
    assert len(jobs_bk) >= 1

    jobs_dest = backup_replication.list_repair_jobs(dest_target_id="dest_target")
    assert len(jobs_dest) >= 1

    jobs_src = backup_replication.list_repair_jobs(source_target_id="src_target")
    assert len(jobs_src) >= 1

    jobs_stage = backup_replication.list_repair_jobs(phase="queued")
    assert len(jobs_stage) >= 1

    # Phase update
    updated = backup_replication._set_repair_phase(
        job,
        phase="transferring-components",
        progress={"completed": 1, "total": 2},
    )
    assert updated["phase"] == "transferring-components"
    assert updated["progress"]["completed"] == 1


def test_replication_job_crud_and_enqueue(tmp_settings) -> None:
    """Test replication job CRUD, filters, and enqueue helpers."""
    # List jobs when dir empty
    assert backup_replication.list_jobs(policy_id="none") == []

    # Write job
    j_dir = backup_replication.REPLICATION_DIR
    j_dir.mkdir(parents=True, exist_ok=True)
    (j_dir / "bad.json").write_text("corrupted", encoding="utf-8")
    assert backup_replication.list_jobs() == []

    # Enqueue replica jobs
    policy_disabled = {"policyId": "p-dis", "replication": {"enabled": False}}
    assert backup_replication.enqueue_replica_jobs(
        policy=policy_disabled,
        primary_target_id="p-target",
        backup_id="bk-1",
        package=None,
        run_id="run-1",
        schedule_slot="slot-1",
        slot_digest="dig-1",
    ) == []

    policy_empty = {"policyId": "p-emp", "replication": {"enabled": True, "targets": []}}
    assert backup_replication.enqueue_replica_jobs(
        policy=policy_empty,
        primary_target_id="p-target",
        backup_id="bk-1",
        package=None,
        run_id="run-1",
        schedule_slot="slot-1",
        slot_digest="dig-1",
    ) == []

    policy_valid = {
        "policyId": "p-val",
        "replication": {
            "enabled": True,
            "targets": [
                {"targetId": "r-target-1", "mode": "required"},
                {"targetId": "p-target", "mode": "required"},  # primary self skipped
            ],
        },
    }
    receipt = {
        "schemaVersion": 4,
        "backupId": "bk-val-1",
        "policyId": "p-val",
        "targetId": "p-target",
        "objectSetDigest": "os-dig",
        "controlObjectDigest": "ctrl-dig",
        "objects": [{"digest": "os-dig", "size": 100, "kind": "data"}],
    }
    enqueued = backup_replication.enqueue_replica_jobs(
        policy=policy_valid,
        primary_target_id="p-target",
        backup_id="bk-val-1",
        package=None,
        run_id="run-val",
        schedule_slot="slot-val",
        slot_digest="dig-val",
        primary_receipt=receipt,
    )
    assert len(enqueued) == 1
    job_id = enqueued[0]["jobId"]

    # Test read_job and list_jobs filters
    read_back = backup_replication.read_job(job_id)
    assert read_back is not None
    assert read_back["policyId"] == "p-val"

    assert backup_replication.has_open_required_jobs(policy_id="p-val") is True
    assert backup_replication.has_open_required_jobs(policy_id="p-val", slot_digest="dig-val") is True
    assert backup_replication.has_open_required_jobs(policy_id="p-val", slot_digest="diff-dig") is False


def test_quarantine_and_replace_corrupt_remote_object_branches(tmp_settings) -> None:
    """Test remote corrupt object replacement branches: non-existent corrupt object, ETag mismatch."""
    src_root = tmp_settings / "src_remote_store"
    dest_root = tmp_settings / "dest_remote_store"
    src_root.mkdir(parents=True)
    dest_root.mkdir(parents=True)

    src_store = MockTargetStore(src_root, "src_store")
    dest_store = MockTargetStore(dest_root, "dest_store")

    key = "objects/ab/cd/abcd1234.age"
    valid_bytes = b"valid-ciphertext-payload-bytes"
    valid_digest = _sha256(valid_bytes)

    # Put valid object in source
    src_store.put_if_absent(key, valid_bytes, checksum_sha256=valid_digest)

    src_target = MockTarget("src_store", store=src_store)
    dest_target = MockTarget("dest_store", store=dest_store)

    # 1. Non-existent corrupt object (direct stream transfer)
    bytes_trans = backup_replication.quarantine_and_replace_corrupt_remote_object(
        dest_target=dest_target,
        dest_rel=key,
        expected_digest=valid_digest,
        source_target=src_target,
        source_rel=key,
    )
    assert bytes_trans == len(valid_bytes)
    assert dest_store.get_bytes(key) == valid_bytes

    # 2. Corrupt object exists (quarantine + replace path)
    dest_store.delete_if_match(key)
    dest_store.put_if_absent(key, b"corrupted-bytes")

    bytes_trans2 = backup_replication.quarantine_and_replace_corrupt_remote_object(
        dest_target=dest_target,
        dest_rel=key,
        expected_digest=valid_digest,
        source_target=src_target,
        source_rel=key,
    )
    assert bytes_trans2 == len(valid_bytes)
    assert dest_store.get_bytes(key) == valid_bytes

    # 3. Source corruption causes digest mismatch
    src_store.delete_if_match(key)
    src_store.put_if_absent(key, b"corrupted-source-bytes")

    with pytest.raises(AppError) as exc_info:
        backup_replication.quarantine_and_replace_corrupt_remote_object(
            dest_target=dest_target,
            dest_rel=key,
            expected_digest=valid_digest,
            source_target=src_target,
            source_rel=key,
        )
    assert "source component corrupt" in str(exc_info.value) or "ciphertext transfer digest mismatch" in str(exc_info.value)


def test_process_pending_repairs_and_reconcile_replicas(tmp_settings, monkeypatch) -> None:
    """Test process_pending_repairs and reconcile_policy_replicas controller flows."""
    # Setup source and dest directories
    src_dir = tmp_settings / "src_box"
    dest_dir = tmp_settings / "dest_box"
    src_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)

    target_map = {
        "src-target": MockTarget("src-target", root=src_dir),
        "dest-target": MockTarget("dest-target", root=dest_dir),
    }
    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: target_map[tid])

    # Put a valid backup point in source
    p_id = "pol-auto"
    b_id = "bk-auto-1"
    chunk = b"hello-world-chunk"
    c_dig = _sha256(chunk)
    c_rel = f"objects/{c_dig[:2]}/{c_dig[2:4]}/{c_dig}.age"
    (src_dir / c_rel).parent.mkdir(parents=True, exist_ok=True)
    (src_dir / c_rel).write_bytes(chunk)

    receipt = {
        "schemaVersion": 4,
        "backupId": b_id,
        "policyId": p_id,
        "targetId": "src-target",
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": c_dig,
        "objects": [{"digest": c_dig, "size": len(chunk), "kind": "data"}],
    }
    (src_dir / "receipts").mkdir(parents=True, exist_ok=True)
    (src_dir / "receipts" / f"{b_id}.json").write_text(json.dumps(receipt), encoding="utf-8")

    # Record source copy in DR ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="src-target",
        policy_id=p_id,
        backup_id=b_id,
        committed_at="2026-08-17T00:00:00Z",
        object_set_digest=c_dig,
        recoverable=True,
        state="healthy",
    )

    # 1. Create a queued repair job and run execute_repair_job_instance
    job = backup_replication.create_repair_job(
        policy_id=p_id,
        backup_id=b_id,
        dest_target_id="dest-target",
        source_target_id="src-target",
        repair_id="rep_pending_01",
    )
    assert job["phase"] == "queued"

    res = backup_replication.execute_repair_job_instance("rep_pending_01")
    assert res["status"] == "success"

    # Also test process_pending_repairs
    _ = backup_replication.create_repair_job(
        policy_id=p_id,
        backup_id=b_id,
        dest_target_id="dest-target",
        source_target_id="src-target",
        repair_id="rep_pending_02",
    )
    processed = backup_replication.process_pending_repairs(limit=5)
    assert processed["processed"] >= 1

    # Verify dest now has the object
    assert (dest_dir / c_rel).is_file()
    assert (dest_dir / c_rel).read_bytes() == chunk

    # 2. Test reconcile_policy_replicas
    policy = {
        "policyId": p_id,
        "targetId": "src-target",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [
                {"targetId": "dest-target", "mode": "required"},
            ],
        },
    }
    from deepseek_infra.infra.workspace import backup_policies
    monkeypatch.setattr(backup_policies, "get_policy", lambda pid: policy)

    reconcile_res = backup_replication.reconcile_policy_replicas(p_id)
    assert reconcile_res["status"] == "completed"
    assert reconcile_res["scannedPoints"] >= 1

    # Test replication_compliance
    comp = backup_replication.replication_compliance(policy=policy, backup_id=b_id)
    assert comp["enabled"] is True
    assert "compliance" in comp


def test_replica_lag_calculation_branches(tmp_settings) -> None:
    """Test calculate_replica_lag for no-primary, no-replica, and calculated scenarios."""
    # 1. No primary
    lag1 = backup_replication.calculate_replica_lag("pol-lag", "rep-1", primary_target_id="p-1")
    assert lag1["status"] == "no-primary"

    # 2. Primary exists, no replica
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="p-1",
        policy_id="pol-lag",
        backup_id="bk-lag-1",
        committed_at="2026-08-17T01:00:00Z",
        recoverable=True,
    )
    lag2 = backup_replication.calculate_replica_lag("pol-lag", "rep-1", primary_target_id="p-1")
    assert lag2["status"] == "no-replica"
    assert lag2["lagRecoveryPoints"] == 999

    # 3. Both exist
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="rep-1",
        policy_id="pol-lag",
        backup_id="bk-lag-1",
        committed_at="2026-08-17T00:55:00Z",
        recoverable=True,
    )
    lag3 = backup_replication.calculate_replica_lag("pol-lag", "rep-1", primary_target_id="p-1")
    assert lag3["status"] == "calculated"
    assert lag3["lagRecoveryPoints"] == 0
    assert lag3["lagSeconds"] == 300


def test_write_failover_and_dr_readiness(tmp_settings, monkeypatch) -> None:
    """Test write placement failover and DR readiness writeContinuity block."""
    policy = {
        "policyId": "pol-stability",
        "targetId": "primary-target-unavailable",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [
                {"targetId": "replica-target-healthy", "mode": "required"},
            ],
        },
    }

    replica_dir = tmp_settings / "replica_store"
    replica_dir.mkdir(parents=True)

    def mock_resolve(tid: str) -> Any:
        if tid == "primary-target-unavailable":
            raise AppError("connection failed", status=503)
        if tid == "replica-target-healthy":
            return MockTarget(tid, root=replica_dir)
        raise AppError("unknown", status=404)

    monkeypatch.setattr(backup_publish, "resolve_target", mock_resolve)

    # Evaluate write placement
    placement = backup_scheduler.evaluate_write_placement(policy)
    assert placement["isFailover"] is True
    assert placement["selectedWriteTargetId"] == "replica-target-healthy"
    assert placement["forceFull"] is True

    # Test DR readiness writeContinuity block
    readiness = backup_dr_readiness.evaluate_scope_readiness("replica-target-healthy")
    assert "writeContinuity" in readiness
    assert "status" in readiness["writeContinuity"]


def test_backup_replication_extra_edge_cases(tmp_settings, monkeypatch) -> None:
    """Cover additional edge cases in backup_replication."""
    store_dir = tmp_settings / "extra_store"
    store_dir.mkdir(parents=True)
    store = MockTargetStore(store_dir, target_id="extra-target")
    target = MockTarget("extra-target", root=store_dir, store=store)

    # 1. SourceHold model methods and serialization
    now = datetime.now(timezone.utc)
    hold = backup_replication.SourceHold(
        hold_id="test-h-1",
        target_id="extra-target",
        policy_id="pol-1",
        backup_id="bk-1",
        holder_id="repair-job-1",
        expires_at=(now + timedelta(seconds=60)).isoformat(),
    )
    d = hold.to_dict()
    assert d["holdId"] == "test-h-1"

    # 2. Acquire, check, and release hold via store
    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: target)
    acquired = backup_replication.acquire_source_hold(
        target_id="extra-target",
        policy_id="pol-1",
        backup_id="bk-1",
        holder_id="repair-job-1",
        target_root=store_dir,
        target_store=store,
        hold_seconds=30,
    )
    assert acquired is not None
    assert backup_replication.is_source_held("extra-target", "pol-1", "bk-1")

    # Renew hold
    acquired.renew(duration_seconds=60)
    assert backup_replication.is_source_held("extra-target", "pol-1", "bk-1")

    # Release hold
    backup_replication.release_source_hold(acquired)

    # Check non-existent hold
    assert not backup_replication.is_source_held("extra-target", "pol-1", "non-existent")

    # 3. Stream ciphertext transfer with multi-chunk buffer
    data = b"X" * (16 * 1024)  # 16 KiB
    (store_dir / "source.dat").write_bytes(data)
    dest_dir = tmp_settings / "extra_dest"
    dest_dir.mkdir(parents=True)
    dest_target = MockTarget("dest-target", root=dest_dir)

    bytes_trans = backup_replication.stream_ciphertext_transfer(
        target,
        dest_target,
        "source.dat",
        "dest.dat",
        expected_digest=_sha256(data),
        chunk_size=4096,  # 4 KiB chunks -> 4 iterations
    )
    assert bytes_trans == len(data)
    assert (dest_dir / "dest.dat").read_bytes() == data

    # Corrupt source checksum mismatch
    with pytest.raises(AppError) as exc_info:
        backup_replication.stream_ciphertext_transfer(
            target,
            dest_target,
            "source.dat",
            "dest-corrupt.dat",
            expected_digest="wrong_checksum",
            chunk_size=4096,
        )
    assert "digest mismatch" in str(exc_info.value)

    # 4. Quarantine and replace corrupt remote object
    (dest_dir / "corrupt-target.dat").write_bytes(b"corrupt-content")
    replaced_bytes = backup_replication.quarantine_and_replace_corrupt_remote_object(
        dest_target,
        "corrupt-target.dat",
        _sha256(data),
        target,
        "source.dat",
    )
    assert replaced_bytes == len(data)
    assert (dest_dir / "corrupt-target.dat").read_bytes() == data


def test_backup_governance_router_booster(tmp_settings, monkeypatch) -> None:
    """Test backup governance router endpoints for recovery status and replication."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from deepseek_infra.web.routes.backup_governance import create_backup_governance_router

    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    app = FastAPI()
    app.include_router(create_backup_governance_router())
    client = TestClient(app, raise_server_exceptions=False)

    # Recovery status
    resp = client.get("/api/workspace/disaster-recovery/status")
    assert resp.status_code == 200

    # Policy list
    resp_pol = client.get("/api/workspace/backup-policies")
    assert resp_pol.status_code == 200


def test_backup_replication_repair_jobs_query_and_compliance(tmp_settings, monkeypatch) -> None:
    """Test repair job listings, non-existent reads, and replication compliance calculations."""
    # 1. Non-existent and corrupt repair jobs
    assert backup_replication.read_repair_job("non-existent-repair-job") is None

    # Corrupt repair job file
    corrupt_file = tmp_settings / ".backup-replication" / "repairs" / "repair_corrupt.json"
    corrupt_file.parent.mkdir(parents=True, exist_ok=True)
    corrupt_file.write_text("{bad json", encoding="utf-8")
    assert backup_replication.read_repair_job("repair_corrupt") is None

    # 2. Replication compliance scenarios
    # A: Disabled replication
    pol_disabled = {"policyId": "p-dis", "replication": {"enabled": False}}
    comp_dis = backup_replication.replication_compliance(policy=pol_disabled, backup_id="bk-1")
    assert comp_dis["enabled"] is False
    assert comp_dis["compliance"] == "healthy"

    # B: Enabled replication with lag threshold
    pol_lag = {
        "policyId": "p-lag",
        "targetId": "p-target",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "maxReplicaLagSeconds": 100,
            "targets": [{"targetId": "r-target", "mode": "required"}],
        },
    }
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="p-target",
        policy_id="p-lag",
        backup_id="bk-comp-1",
        committed_at="2026-08-17T02:00:00Z",
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="r-target",
        policy_id="p-lag",
        backup_id="bk-comp-1",
        committed_at="2026-08-17T01:00:00Z",
        recoverable=True,
    )
    comp_lag = backup_replication.replication_compliance(policy=pol_lag, backup_id="bk-comp-1")
    assert comp_lag["enabled"] is True
    assert comp_lag["compliance"] == "degraded"
    assert any("replica-lag-exceeded" in r for r in comp_lag["reasons"])


def test_backup_retention_copy_safety_edge_cases(tmp_settings, monkeypatch) -> None:
    """Test retention effective min copies derivation and copy safety."""
    from deepseek_infra.infra.workspace import backup_retention, backup_policies, backup_targets

    # Create policy with minCommittedCopies
    policy_id = "pol-ret-copy"
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"targetId": tid, "status": "active"})
    backup_policies.create_policy({
        "policyId": policy_id,
        "name": "Retention Policy",
        "cron": "0 0 * * *",
        "targetId": "managed-local",
        "keepLast": 5,
        "replication": {
            "enabled": True,
            "minCommittedCopies": 3,
            "targets": [
                {"targetId": "target_replica_01", "mode": "required"},
                {"targetId": "target_replica_02", "mode": "required"},
            ],
        },
    })

    # Effective min copies should be derived from policy replication
    ret = {"keepLast": 5, "minCommittedCopies": 1}
    effective = backup_retention._effective_min_copies(ret, policy_id)
    assert effective == 3

    # Fallback to retention config if policy does not exist
    effective_fallback = backup_retention._effective_min_copies(ret, "non-existent-pol")
    assert effective_fallback == 1


def test_backup_replication_repair_execution_branches(tmp_settings, monkeypatch) -> None:
    """Cover repair execution edge cases, retired skips, terminal status, and reconciler."""
    from deepseek_infra.infra.workspace import backup_dr_ledger, backup_replication, backup_policies

    # 1. Retired point skip
    monkeypatch.setattr(backup_dr_ledger, "is_logical_recovery_point_retired", lambda p, b: True)
    res_retired = backup_replication.execute_replica_repair(
        policy_id="pol-ret",
        backup_id="bk-retired",
        dest_target_id="target-r1",
    )
    assert res_retired["status"] == "skipped"
    assert res_retired["reason"] == "retired"

    # 2. Terminal job return
    monkeypatch.setattr(backup_dr_ledger, "is_logical_recovery_point_retired", lambda p, b: False)
    job_term = backup_replication.create_repair_job(
        policy_id="pol-term",
        backup_id="bk-term",
        dest_target_id="target-r1",
        repair_id="repair-term-01",
    )
    backup_replication._set_repair_phase(job_term, "healthy")
    res_term = backup_replication.execute_replica_repair(
        policy_id="pol-term",
        backup_id="bk-term",
        dest_target_id="target-r1",
        run_id="repair-term-01",
    )
    assert res_term["status"] == "success"

    # 3. No healthy source copy available -> raises 404
    backup_replication.create_repair_job(
        policy_id="pol-no-src",
        backup_id="bk-no-src",
        dest_target_id="target-r1",
        repair_id="repair-no-src-01",
    )
    monkeypatch.setattr(backup_dr_ledger, "list_logical_recovery_copies", lambda **k: [])
    with pytest.raises(AppError) as exc_info:
        backup_replication.execute_repair_job_instance("repair-no-src-01")
    assert "No healthy source copy available" in str(exc_info.value)
    assert exc_info.value.status == 404

    # 4. Replication jobs edge cases (non-existent, terminal)
    with pytest.raises(AppError) as repl_exc:
        backup_replication.execute_replication_job("non-existent-job-id")
    assert repl_exc.value.status == 404

    # Terminal replication job
    term_job = {
        "jobId": "repl_term_01",
        "policyId": "pol-1",
        "backupId": "bk-1",
        "phase": "committed",
    }
    backup_replication._atomic_write(backup_replication._job_path("repl_term_01"), term_job)
    assert backup_replication.execute_replication_job("repl_term_01")["phase"] == "committed"

    # 4. process_pending_repairs
    res_proc = backup_replication.process_pending_repairs(limit=5)
    assert "processed" in res_proc

    # 5. reconcile_policy_replicas
    # Policy with replication disabled
    monkeypatch.setattr(
        backup_policies,
        "get_policy",
        lambda pid: {"policyId": pid, "replication": {"enabled": False}},
    )
    res_recon_dis = backup_replication.reconcile_policy_replicas("p-dis")
    assert res_recon_dis["status"] == "skipped"

    # Policy with no replica targets
    monkeypatch.setattr(
        backup_policies,
        "get_policy",
        lambda pid: {"policyId": pid, "replication": {"enabled": True, "targets": []}},
    )
    res_recon_noop = backup_replication.reconcile_policy_replicas("p-noop")
    assert res_recon_noop["status"] == "noop"

    # Cursor persistence
    cursors = {"pol-1": "bk-100"}
    backup_replication._save_cursors(cursors)
    assert backup_replication._load_cursors() == cursors


def test_backup_replication_heal_existing_and_provision_modes(tmp_settings, monkeypatch) -> None:
    """Test full execution of heal-existing and provision modes with filesystem targets."""
    from deepseek_infra.infra.workspace import backup_dr_ledger, backup_replication, backup_publish

    src_root = tmp_settings / "targets" / "src_target"
    src_root.mkdir(parents=True, exist_ok=True)
    dst_root = tmp_settings / "targets" / "dst_target"
    dst_root.mkdir(parents=True, exist_ok=True)

    src_target = MockTarget("target_src", root=src_root)
    dst_target = MockTarget("target_dst", root=dst_root)

    def mock_resolve(tid: str):
        if tid == "target_src":
            return src_target
        if tid == "target_dst":
            return dst_target
        return MockTarget(tid, root=tmp_settings / tid)

    monkeypatch.setattr(backup_publish, "resolve_target", mock_resolve)
    monkeypatch.setattr(backup_dr_ledger, "is_logical_recovery_point_retired", lambda p, b: False)

    policy_id = "pol-heal-test"
    backup_id = "bk-heal-01"

    # Setup source receipt and components
    comp1_data = b"COMPONENT-CONTENT-1"
    comp1_sha = hashlib.sha256(comp1_data).hexdigest()
    comp1_path = src_root / "objects" / comp1_sha[:2] / comp1_sha[2:4] / f"{comp1_sha}.age"
    comp1_path.parent.mkdir(parents=True, exist_ok=True)
    comp1_path.write_bytes(comp1_data)

    receipt = {
        "schemaVersion": 4,
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": "target_src",
        "objects": [{"digest": comp1_sha, "size": len(comp1_data)}],
        "objectSetDigest": "obj-digest-1",
    }
    rcpt_path = src_root / "receipts" / f"{backup_id}.json"
    rcpt_path.parent.mkdir(parents=True, exist_ok=True)
    rcpt_path.write_text(json.dumps(receipt), encoding="utf-8")

    # Record recovery copy on source
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_src",
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at="2026-08-17T02:00:00Z",
        recoverable=True,
    )

    # 1. Provision mode: Destination has no receipt/commit yet
    res_prov = backup_replication.execute_replica_repair(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id="target_dst",
        source_target_id="target_src",
        run_id="repair-prov-01",
    )
    assert res_prov["status"] == "success"
    assert (dst_root / "receipts" / f"{backup_id}.json").is_file()
    assert (dst_root / "objects" / comp1_sha[:2] / comp1_sha[2:4] / f"{comp1_sha}.age").read_bytes() == comp1_data

    # 2. Heal-existing mode: Destination already has receipt & commit, but component is corrupted
    (dst_root / "objects" / comp1_sha[:2] / comp1_sha[2:4] / f"{comp1_sha}.age").write_bytes(b"CORRUPTED-DATA")
    res_heal = backup_replication.execute_replica_repair(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id="target_dst",
        source_target_id="target_src",
        run_id="repair-heal-01",
    )
    assert res_heal["status"] == "success"
    # Verify corrupted data was repaired
    assert (dst_root / "objects" / comp1_sha[:2] / comp1_sha[2:4] / f"{comp1_sha}.age").read_bytes() == comp1_data

    # 3. Missing source receipt -> 404
    (src_root / "receipts" / f"{backup_id}.json").unlink()
    backup_replication.create_repair_job(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id="target_dst",
        source_target_id="target_src",
        repair_id="repair-bad-rcpt-01",
    )
    with pytest.raises(AppError) as exc_bad_rcpt:
        backup_replication.execute_repair_job_instance("repair-bad-rcpt-01")
    assert "Source receipt missing" in str(exc_bad_rcpt.value)
    assert exc_bad_rcpt.value.status == 404


@pytest.mark.integration
def test_server_routes_booster_coverage(tmp_settings, monkeypatch) -> None:
    """Test server endpoints for agent runs, share-target, and fallback 404."""
    import http.client
    import threading
    import contextlib
    from deepseek_infra.core import config
    from deepseek_infra.web import server as server_module

    auth_token = config.settings.auth.token or "test_secret_token"
    auth_headers = {"Authorization": f"Bearer {auth_token}"}

    @contextlib.contextmanager
    def _running_srv():
        srv, _ = server_module.create_server(0, host="127.0.0.1")
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            yield srv
        finally:
            srv.shutdown()
            srv.server_close()
            th.join(timeout=5)

    def _req(srv, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        try:
            req_headers = dict(auth_headers)
            if headers:
                req_headers.update(headers)
            conn.request(method, path, body=body, headers=req_headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    with _running_srv() as srv:
        # 1. 404 fallback
        st_404, _ = _req(srv, "GET", "/api/unknown-endpoint-404-coverage")
        assert st_404 == 404

        # 2. Agent runs invalid payload
        st_bad_run, _ = _req(
            srv,
            "POST",
            "/api/agent-runs",
            body=json.dumps({"payload": "not-a-dict"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert st_bad_run in (400, 422)

        # 3. Agent runs valid payload
        monkeypatch.setattr(
            server_module,
            "create_agent_run",
            lambda p, **kw: {"runId": "run_fake_01", "status": "planned", "plan": []},
        )
        monkeypatch.setattr(
            server_module.agent_run_registry,
            "ensure_started",
            lambda *a, **kw: None,
        )
        st_good_run, res_body = _req(
            srv,
            "POST",
            "/api/agent-runs",
            body=json.dumps({
                "payload": {
                    "apiKey": "sk-fake-test-key-coverage",
                    "model": "deepseek-v4-pro",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                "confirmPlan": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert st_good_run == 201
        assert b"run_fake_01" in res_body

        # 4. Share target upload
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        body_parts = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="title"\r\n\r\n'
            f"Shared Title\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="text"\r\n\r\n'
            f"Shared Text\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        st_share, _ = _req(
            srv,
            "POST",
            "/share-target",
            body=body_parts,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert st_share == 303








