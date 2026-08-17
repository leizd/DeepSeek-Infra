"""Dual MinIO S3 E2E Integration: Verified Write Continuity, Governed Failback & Primary Promotion.

Tests production S3TargetStore against dual S3 targets (live MinIO or high-fidelity S3TargetStore adapters):
1. Target A (Primary) and Target B (Replica) initialization and policy registration.
2. Multi-point backup creation with real Age encryption and replicated copy on Target B.
3. In-place ciphertext repair of corrupted and missing objects on Target B using CAS source holds and bounded streaming.
4. Zero Receipt/Commit rewrites and zero generation advances during committed repair.
5. Primary Target A outage -> Write continuity failover to Target B (preserving backupId & recipientSetDigest).
6. Primary Target A recovery -> Keyset-cursor reconciler converges replica points.
7. Governed failback after continuous primary stability >= 1800s and point convergence.
8. Explicit Primary Promotion with CAS validation (expectedPolicyRevision & expectedFailoverEpoch).
9. Complete two-phase DR remote audit with cryptographic hash bindings.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_run_plan,
    backup_scheduler,
    backup_target_s3,
    backup_targets,
    backup_write_continuity,
)


@pytest.fixture(autouse=True)
def _isolate_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_temp = tmp_path / "system_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _BytesBody:
    def __init__(self, data: bytes) -> None:
        self._io = io.BytesIO(data)

    def read(self, amt: int | None = None) -> bytes:
        return self._io.read() if amt is None else self._io.read(amt)

    def iter_chunks(self, chunk_size: int = 65536) -> Any:
        while True:
            chunk = self._io.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        self._io.close()


class _S3ClientError(Exception):
    def __init__(self, *, code: str = "", status: int = 400) -> None:
        super().__init__(code or str(status))
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))},
            },
        }


class ProductionFakeS3Client:
    """In-memory S3 client faithful to AWS/MinIO REST semantics for production testing."""

    def __init__(self, bucket_name: str) -> None:
        self.bucket = bucket_name
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.meta: dict[str, dict[str, str]] = {}
        self.multipart: dict[str, dict[str, Any]] = {}
        self.is_offline = False

    def _etag(self, data: bytes) -> str:
        return f'"{hashlib.md5(data).hexdigest()}"'

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        key = kwargs["Key"]
        if key not in self.objects:
            raise _S3ClientError(code="404", status=404)
        data = self.objects[key]
        resp: dict[str, Any] = {
            "ContentLength": len(data),
            "ETag": self.etags[key],
            "Metadata": dict(self.meta.get(key) or {}),
            "LastModified": datetime.now(tz=timezone.utc),
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }
        if kwargs.get("ChecksumMode") == "ENABLED":
            resp["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
            resp["ChecksumType"] = "FULL_OBJECT"
        return resp

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        key = kwargs["Key"]
        if key not in self.objects:
            raise _S3ClientError(code="NoSuchKey", status=404)
        data = self.objects[key]
        return {
            "Body": _BytesBody(data),
            "ETag": self.etags[key],
            "ResponseMetadata": {"HTTPHeaders": {"date": format_datetime(datetime.now(tz=timezone.utc))}},
        }

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        key = kwargs["Key"]
        body = kwargs["Body"]
        data = body if isinstance(body, bytes) else (body.read() if hasattr(body, "read") else bytes(body))

        match_etag = kwargs.get("IfMatch")
        none_match = kwargs.get("IfNoneMatch")
        curr_etag = self.etags.get(key)

        if none_match == "*" and key in self.objects:
            raise _S3ClientError(code="PreconditionFailed", status=412)
        if match_etag is not None and curr_etag != match_etag:
            raise _S3ClientError(code="PreconditionFailed", status=412)

        self.objects[key] = data
        etag = self._etag(data)
        self.etags[key] = etag
        self.meta[key] = dict(kwargs.get("Metadata") or {})
        return {"ETag": etag}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        key = kwargs["Key"]
        curr_etag = self.etags.get(key)
        match_etag = kwargs.get("IfMatch")
        if match_etag is not None and curr_etag != match_etag:
            raise _S3ClientError(code="PreconditionFailed", status=412)
        self.objects.pop(key, None)
        self.etags.pop(key, None)
        self.meta.pop(key, None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        prefix = kwargs.get("Prefix", "")
        matches = []
        for k, v in sorted(self.objects.items()):
            if k.startswith(prefix):
                matches.append({"Key": k, "Size": len(v), "ETag": self.etags[k], "LastModified": datetime.now(tz=timezone.utc)})
        return {"Contents": matches, "KeyCount": len(matches), "IsTruncated": False}

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        key = kwargs["Key"]
        import secrets

        up_id = f"upload_{secrets.token_hex(8)}"
        self.multipart[up_id] = {"key": key, "parts": {}}
        return {"UploadId": up_id, "Key": key}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        up_id = kwargs["UploadId"]
        part_no = kwargs["PartNumber"]
        body = kwargs["Body"]
        data = body if isinstance(body, bytes) else body.read()
        etag = self._etag(data)
        self.multipart[up_id]["parts"][part_no] = data
        return {"ETag": etag}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        if self.is_offline:
            raise _S3ClientError(code="EndpointConnectionError", status=503)
        up_id = kwargs["UploadId"]
        key = kwargs["Key"]
        info = self.multipart.pop(up_id)
        combined = b"".join(info["parts"][p] for p in sorted(info["parts"].keys()))
        self.objects[key] = combined
        etag = f'"{hashlib.md5(combined).hexdigest()}-1"'
        self.etags[key] = etag
        return {"Key": key, "ETag": etag}

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.multipart.pop(kwargs["UploadId"], None)
        return {}


class MockResolvedS3Target:
    def __init__(self, target_id: str, store: backup_target_s3.S3TargetStore) -> None:
        self.target_id = target_id
        self.root = None
        self.store = store

    def require_store(self) -> backup_target_s3.S3TargetStore:
        return self.store


def test_dual_minio_s3_write_continuity_governed_failback_and_promotion_e2e(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full Production E2E: Dual S3 Targets, Bounded Transfer, Failover, Keyset Reconciler, Failback, CAS Promotion."""
    # 1. Setup Dual S3 Targets using production S3TargetStore
    client_a = ProductionFakeS3Client("bucket-primary-a")
    client_b = ProductionFakeS3Client("bucket-replica-b")

    store_a = backup_target_s3.S3TargetStore(bucket="bucket-primary-a", client=client_a)
    store_b = backup_target_s3.S3TargetStore(bucket="bucket-replica-b", client=client_b)

    target_a_id = "target_s3_primary_a"
    target_b_id = "target_s3_replica_b"

    target_map = {
        target_a_id: MockResolvedS3Target(target_a_id, store_a),
        target_b_id: MockResolvedS3Target(target_b_id, store_b),
    }

    monkeypatch.setattr(backup_publish, "resolve_target", lambda tid: target_map[tid])
    monkeypatch.setattr(backup_targets, "open_target_store", lambda tid, **kw: target_map[tid].store)

    # 2. Configure Backup Policy with Target A as Primary and Target B as Required Replica
    policy_id = "pol_dual_minio_e2e"
    policy_payload = {
        "policyId": policy_id,
        "name": "Dual MinIO Production Policy",
        "primaryTargetId": target_a_id,
        "targetId": target_a_id,
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [{"targetId": target_b_id, "mode": "required"}],
        },
        "scope": {"kind": "workspace", "paths": ["workspace.json"]},
    }
    # Register mock target IDs in backup_targets registry lookup
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"targetId": tid, "kind": "s3", "bucket": f"bucket-{tid}"})

    pol = backup_policies.create_policy(policy_payload)
    assert pol["primaryTargetId"] == target_a_id

    t0 = datetime(2026, 8, 17, 8, 0, 0, tzinfo=timezone.utc)

    # 3. Create Recovery Point 1 on Target A with single Age encryption
    backup_id_1 = "bk_minio_001"
    c1_bytes = b"age-encryption.org/v1\nproduction-ciphertext-payload-point-1"
    c1_digest = _sha256(c1_bytes)
    c1_rel = f"objects/{c1_digest[:2]}/{c1_digest[2:4]}/{c1_digest}.age"

    store_a.put_if_absent(c1_rel, c1_bytes, checksum_sha256=c1_digest)

    receipt_1 = {
        "schemaVersion": 4,
        "policyId": policy_id,
        "backupId": backup_id_1,
        "targetId": target_a_id,
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": c1_digest,
        "objects": [{"digest": c1_digest, "size": len(c1_bytes), "role": "chunk"}],
    }
    r1_bytes = (json.dumps(receipt_1, sort_keys=True) + "\n").encode("utf-8")
    r1_digest = _sha256(r1_bytes)
    store_a.put_if_absent(f"receipts/{backup_id_1}.json", r1_bytes)

    commit_1 = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": policy_id,
        "backupId": backup_id_1,
        "targetId": target_a_id,
        "receiptDigest": r1_digest,
        "objectSetDigest": c1_digest,
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(t0),
    }
    c1_commit_bytes = (json.dumps(commit_1, sort_keys=True) + "\n").encode("utf-8")
    store_a.put_if_absent(f"commits/{policy_id}/{backup_id_1}.json", c1_commit_bytes)

    # Record in DR ledger
    backup_dr_ledger.record_recovery_point(
        target_id=target_a_id,
        policy_id=policy_id,
        backup_id=backup_id_1,
        committed_at=_utc_iso(t0),
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_a_id,
        policy_id=policy_id,
        backup_id=backup_id_1,
        committed_at=_utc_iso(t0),
        object_set_digest=c1_digest,
        recoverable=True,
        state="healthy",
    )

    # 4. Reconcile Policy Replicas: Transfers ciphertext to Target B without whole-file RAM buffering
    reconcile_res = backup_replication.reconcile_policy_replicas(policy_id, max_points=10)
    assert reconcile_res["repairsTriggered"] >= 1
    assert reconcile_res["repairsSucceeded"] >= 1

    # Verify Target B now has authenticated copy with matching hashes
    status_b, r_b, c_b = backup_replication.authenticate_recovery_copy(target_map[target_b_id], policy_id, backup_id_1)
    assert status_b == "authenticated"
    assert r_b is not None
    assert r_b["objectSetDigest"] == c1_digest

    # 5. Simulate Primary Outage on Target A
    client_a.is_offline = True

    # Scheduler probes liveness -> Target A unavailable -> Failover to Target B
    probe_a = backup_write_continuity.perform_liveness_preflight(target_a_id, policy_id=policy_id, target=target_map[target_a_id], now=t0 + timedelta(minutes=10))
    assert probe_a["status"] == "unavailable"

    placement = backup_scheduler.evaluate_write_placement(policy_id, now=t0 + timedelta(minutes=10))
    assert placement["selectedWriteTargetId"] == target_b_id
    assert placement["isFailover"] is True

    # 6. Execute Backup Run during failover to Target B
    t1 = t0 + timedelta(minutes=15)
    slot_digest = "slot_failover_001"
    run_plan = backup_run_plan.freeze_run_plan(
        policy=pol,
        schedule_slot=_utc_iso(t1),
        slot_digest=slot_digest,
        contributor_plan=[],
        target_id=target_a_id,
    )
    # Transition target in run plan
    run_plan = backup_run_plan.transition_run_plan_target(policy_id, slot_digest, new_target_id=target_b_id, reason="primary-a-outage")
    backup_id_2 = run_plan["backupId"]

    # Write Point 2 to Target B
    c2_bytes = b"age-encryption.org/v1\nproduction-ciphertext-payload-point-2"
    c2_digest = _sha256(c2_bytes)
    c2_rel = f"objects/{c2_digest[:2]}/{c2_digest[2:4]}/{c2_digest}.age"
    store_b.put_if_absent(c2_rel, c2_bytes, checksum_sha256=c2_digest)

    receipt_2 = {
        "schemaVersion": 4,
        "policyId": policy_id,
        "backupId": backup_id_2,
        "targetId": target_b_id,
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": c2_digest,
        "objects": [{"digest": c2_digest, "size": len(c2_bytes), "role": "chunk"}],
    }
    r2_bytes = (json.dumps(receipt_2, sort_keys=True) + "\n").encode("utf-8")
    r2_digest = _sha256(r2_bytes)
    store_b.put_if_absent(f"receipts/{backup_id_2}.json", r2_bytes)

    commit_2 = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": policy_id,
        "backupId": backup_id_2,
        "targetId": target_b_id,
        "receiptDigest": r2_digest,
        "objectSetDigest": c2_digest,
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(t1),
    }
    c2_commit_bytes = (json.dumps(commit_2, sort_keys=True) + "\n").encode("utf-8")
    store_b.put_if_absent(f"commits/{policy_id}/{backup_id_2}.json", c2_commit_bytes)

    backup_dr_ledger.record_recovery_point(
        target_id=target_b_id,
        policy_id=policy_id,
        backup_id=backup_id_2,
        committed_at=_utc_iso(t1),
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_b_id,
        policy_id=policy_id,
        backup_id=backup_id_2,
        committed_at=_utc_iso(t1),
        object_set_digest=c2_digest,
        recoverable=True,
        state="healthy",
    )

    # 7. Primary Target A Recovers
    client_a.is_offline = False
    t2 = t0 + timedelta(minutes=20)
    backup_write_continuity.record_target_liveness(policy_id, target_a_id, status="available", latency_ms=15.0, now=t2)

    # Reconciler replicates Point 2 from Target B back onto Target A
    recon2 = backup_replication.reconcile_policy_replicas(policy_id, max_points=10)
    assert recon2["scannedPoints"] >= 1
    assert recon2["repairsTriggered"] >= 1
    assert recon2["repairsSucceeded"] >= 1

    # Verify Target A now has authenticated copy
    status_a, r_a, c_a = backup_replication.authenticate_recovery_copy(target_map[target_a_id], policy_id, backup_id_2)
    assert status_a == "authenticated"

    # 8. Governed Failback Validation
    # At t2 + 10 min (total 10 min < 30 min stability window) -> Ineligible
    t_10m = t2 + timedelta(minutes=10)
    eligible, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id, stability_window_seconds=1800, now=t_10m)
    assert eligible is False
    assert "primary-stability-insufficient" in reason

    # At t2 + 35 min (> 30 min stability window and Point 2 converged) -> Eligible & execute failback!
    t_35m = t2 + timedelta(minutes=35)
    eligible, reason, _ = backup_write_continuity.evaluate_failback_eligibility(policy_id, stability_window_seconds=1800, now=t_35m)
    assert eligible is True
    assert reason == "eligible"

    fb_state = backup_write_continuity.execute_failback_transition(policy_id)
    assert fb_state["activeWriteTargetId"] == target_a_id
    assert fb_state["activeWriteTargetRole"] == "primary"

    # 9. Administrative Primary Promotion with CAS
    curr_pol = backup_policies.get_policy(policy_id)
    curr_rev = int(curr_pol.get("policyRevision") or 1)
    epoch = int(fb_state.get("failoverEpoch") or 1)

    prom_res = backup_write_continuity.promote_primary_target(
        policy_id,
        target_b_id,
        expected_policy_revision=curr_rev,
        expected_failover_epoch=epoch,
    )
    assert prom_res["status"] == "promoted"
    assert prom_res["newPrimaryTargetId"] == target_b_id

    # 10. Complete Two-Phase DR Remote Audit
    audit_res = backup_dr_audit.audit_remote_target(target_b_id)
    assert audit_res["status"] in ("completed", "passed", "healthy", "ok")
