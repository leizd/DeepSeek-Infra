from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_targets,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, object_key, put_json_if_absent


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake-system-temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _stable_json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_copy_retirement_preserves_formal_history_and_writes_bound_marker(tmp_settings: Path) -> None:
    target_id = "target_retirement_history"
    policy_id = "policy_retirement_history"
    backup_id = "backup_retirement_history"
    target_root = tmp_settings / "retirement-history-target"
    backup_targets.register_filesystem_target(target_id, path=target_root)

    payload_rel = "objects/sha256/aa/aabbcc.age"
    payload_path = target_root / payload_rel
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(b"retirable-ciphertext")

    receipt = {
        "schemaVersion": 4,
        "storageProtocol": "object-set-v1",
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": "object-set-digest-1",
        "filename": payload_rel,
        "components": [{"path": payload_rel, "digest": "aabbcc"}],
    }
    receipt_bytes = _stable_json_bytes(receipt)
    receipt_path = target_root / "receipts" / f"{backup_id}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)

    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt["objectSetDigest"],
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "committedAt": "2026-08-20T00:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    commit_bytes = _stable_json_bytes(commit)
    commit_path = target_root / "commits" / policy_id / f"{backup_id}.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_bytes(commit_bytes)

    backup_dr_ledger.record_logical_recovery_copy(
        target_id=target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        committed_at="2026-08-20T00:00:00Z",
        object_set_digest=str(receipt["objectSetDigest"]),
        state="healthy",
        recoverable=True,
    )

    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, target_id, reason="drain-complete")
        result = backup_retirement.execute_copy_retirement_job(job["jobId"])

    assert result["phase"] == "reclaimed", result
    assert receipt_path.read_bytes() == receipt_bytes
    assert commit_path.read_bytes() == commit_bytes
    assert not payload_path.exists()

    marker_path = target_root / "retirements" / policy_id / f"{backup_id}.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["targetId"] == target_id
    assert marker["policyId"] == policy_id
    assert marker["backupId"] == backup_id
    assert marker["receiptDigest"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert marker["commitHash"] == commit["commitHash"]
    assert marker["objectSetDigest"] == receipt["objectSetDigest"]
    assert marker["reason"] == "drain-complete"
    assert backup_retirement.retirement_marker_valid(marker, receipt_bytes=receipt_bytes, commit=commit)


def _remote_history_fixture(target_id: str, *, with_retirement_marker: bool) -> tuple[MemoryTargetStore, str, str]:
    policy_id = f"policy_{target_id}"
    backup_id = f"backup_{target_id}"
    missing_digest = hashlib.sha256(f"missing-{target_id}".encode()).hexdigest()
    receipt = {
        "schemaVersion": 4,
        "storageProtocol": "object-set-v1",
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": hashlib.sha256(f"set-{target_id}".encode()).hexdigest(),
        "controlObjectDigest": missing_digest,
        "objects": [{"digest": missing_digest, "size": 123}],
        "createdAt": "2026-08-20T00:00:00Z",
    }
    receipt_bytes = _stable_json_bytes(receipt)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": target_id,
        "policyId": policy_id,
        "backupId": backup_id,
        "objectSetDigest": receipt["objectSetDigest"],
        "controlObjectDigest": missing_digest,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "committedAt": "2026-08-20T00:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)

    store = MemoryTargetStore()
    store.put_if_absent(f"receipts/{backup_id}.json", receipt_bytes)
    put_json_if_absent(store, f"commits/{policy_id}/0001.json", commit)
    put_json_if_absent(
        store,
        "control/head.json",
        {"schemaVersion": 1, "targetGeneration": 1, "latestCommitHash": commit["commitHash"]},
    )
    assert store.stat(object_key(missing_digest)) is None

    if with_retirement_marker:
        marker = backup_retirement.build_retirement_marker(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt_bytes=receipt_bytes,
            receipt=receipt,
            commit=commit,
            retirement_job_id="retire_audit_fixture",
            reason="retention-policy",
            retired_at="2026-08-20T01:00:00Z",
        )
        put_json_if_absent(store, backup_retirement.retirement_marker_key(policy_id, backup_id), marker)
    return store, policy_id, backup_id


def test_audit_distinguishes_governed_retirement_from_missing_payload_corruption(tmp_settings: Path) -> None:
    retired_target = "target_audit_retired"
    retired_store, retired_policy, retired_backup = _remote_history_fixture(retired_target, with_retirement_marker=True)
    with patch.object(backup_targets, "open_target_store", return_value=retired_store):
        retired_result = backup_dr_audit.audit_remote_target(retired_target)

    assert not any(str(item).startswith("missing-payload:") for item in retired_result["anomalies"])
    retired_copy = backup_dr_ledger.list_logical_recovery_copies(
        target_id=retired_target,
        policy_id=retired_policy,
        backup_id=retired_backup,
    )[0]
    assert retired_copy["state"] == "retired"
    assert retired_copy["recoverable"] is False

    corrupt_target = "target_audit_corrupt"
    corrupt_store, corrupt_policy, corrupt_backup = _remote_history_fixture(corrupt_target, with_retirement_marker=False)
    with patch.object(backup_targets, "open_target_store", return_value=corrupt_store):
        corrupt_result = backup_dr_audit.audit_remote_target(corrupt_target)

    assert any(str(item).startswith(f"missing-payload:{corrupt_backup}:") for item in corrupt_result["anomalies"])
    corrupt_copies = backup_dr_ledger.list_logical_recovery_copies(
        target_id=corrupt_target,
        policy_id=corrupt_policy,
        backup_id=corrupt_backup,
    )
    assert not corrupt_copies or corrupt_copies[0]["recoverable"] is False


def test_copy_retirement_waits_while_rebalance_still_references_source(tmp_settings: Path) -> None:
    target_id = "target_retirement_busy"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "retirement-busy-target")
    policy_id = "policy_retirement_busy"
    backup_id = "backup_retirement_busy"
    active_job = {
        "jobId": "rebalance_busy",
        "policyId": policy_id,
        "backupId": backup_id,
        "sourceTargetId": target_id,
        "destTargetId": "target_elsewhere",
        "phase": "pending",
    }

    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        with patch.object(backup_replication, "list_rebalance_jobs", return_value=[active_job]):
            job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, target_id)
            result = backup_retirement.execute_copy_retirement_job(job["jobId"])

    assert result["phase"] == "waiting-for-dependencies"
    assert "active-job" in str(result["error"])
