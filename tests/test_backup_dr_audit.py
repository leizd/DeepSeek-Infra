"""Unit tests for Resumable Remote DR Audit (Recovery Assurance Gate I)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_targets,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta


def test_audit_managed_local(tmp_settings: Path) -> None:
    # Set up some fake commits and receipts in managed-local
    root = backups.BACKUP_DIR
    commits_dir = root / "commits" / "policy_a"
    receipts_dir = root / "receipts"
    commits_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)

    commit_file = commits_dir / "20260815T000000Z.json"
    commit_data = {
        "commitHash": "hash_local_1",
        "backupId": "bk_local_1",
        "policyId": "policy_a",
        "committedAt": "2026-08-15T00:00:00Z",
    }
    commit_file.write_text(json.dumps(commit_data), encoding="utf-8")

    receipt_file = receipts_dir / "bk_local_1.json"
    receipt_data = {
        "backupId": "bk_local_1",
        "policyId": "policy_a",
        "targetId": "managed-local",
        "snapshotKind": "full",
        "chainLength": 1,
        "size": 5000,
        "logicalBytes": 12000,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "a" * 64,
        "objects": [{"digest": "b" * 64, "size": 5000}],
        "createdAt": "2026-08-15T00:00:00Z",
    }
    receipt_file.write_text(json.dumps(receipt_data), encoding="utf-8")
    payload = root / "objects" / "sha256" / "bb" / f"{'b' * 64}.age"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"present-for-audit")

    # Add a malformed commit to test anomaly handling
    bad_commit = commits_dir / "bad.json"
    bad_commit.write_text("not-json", encoding="utf-8")

    result = backup_dr_audit.audit_remote_target("managed-local")
    assert result["targetId"] == "managed-local"
    assert result["status"] == "completed"
    assert result["recoveryPointsFound"] == 1
    assert len(result["anomalies"]) == 1

    # Verify ledger was populated
    pt, _ = backup_dr_ledger.get_latest_recoverable_point("managed-local", "policy_a")
    assert pt is not None
    assert pt["backupId"] == "bk_local_1"
    assert pt["logicalBytes"] == 12000


class _MockStore:
    def __init__(self, objects: dict[str, bytes], cursor_map: dict[str | None, tuple[list[ObjectMeta], str | None]]) -> None:
        self.objects = objects
        self.cursor_map = cursor_map

    def list_objects(self, prefix: str, cursor: str | None = None, limit: int = 100) -> ListPage:
        items, nxt = self.cursor_map.get(cursor, ([], None))
        return ListPage(tuple(items), nxt)

    def get_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)


def test_audit_remote_target_paged_and_anomalies(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    from deepseek_infra.infra.workspace import backup_publish

    target_id = "target_rem_1"

    def _receipt_bytes(receipt: dict[str, Any]) -> bytes:
        return (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    receipt_1 = {
        "backupId": "bk1",
        "policyId": "p1",
        "targetId": target_id,
        "snapshotKind": "full",
        "chainLength": 1,
        "size": 200,
        "logicalBytes": 400,
    }
    receipt_1_bytes = _receipt_bytes(receipt_1)
    commit_1 = {
        "schemaVersion": 4,
        "backupId": "bk1",
        "policyId": "p1",
        "committedAt": "2026-08-15T01:00:00Z",
        "receiptDigest": hashlib.sha256(receipt_1_bytes).hexdigest(),
    }
    commit_1["commitHash"] = backup_publish._commit_hash(commit_1)

    receipt_2 = {
        "backupId": "bk2",
        "policyId": "p1",
        "targetId": target_id,
        "snapshotKind": "incremental",
        "parentBackupId": "bk1",
        "chainLength": 2,
        "size": 300,
        "logicalBytes": 600,
    }
    receipt_2_bytes = _receipt_bytes(receipt_2)
    commit_2 = {
        "schemaVersion": 4,
        "backupId": "bk2",
        "policyId": "p1",
        "parentCommitHash": commit_1["commitHash"],
        "committedAt": "2026-08-15T02:00:00Z",
        "receiptDigest": hashlib.sha256(receipt_2_bytes).hexdigest(),
    }
    commit_2["commitHash"] = backup_publish._commit_hash(commit_2)

    objects = {
        "commits/p1/c1.json": json.dumps(commit_1).encode("utf-8"),
        "receipts/bk1.json": receipt_1_bytes,
        "commits/p1/c2.json": json.dumps(commit_2).encode("utf-8"),
        "receipts/bk2.json": receipt_2_bytes,
        "commits/p1/invalid.json": b"bad-json",
        "commits/p1/other.txt": b"ignored",
    }
    cursor_map = {
        None: ([ObjectMeta(key="commits/p1/c1.json", size=10, etag="e1"), ObjectMeta(key="commits/p1/other.txt", size=1, etag="e0")], "cur2"),
        "cur2": ([ObjectMeta(key="commits/p1/c2.json", size=10, etag="e2"), ObjectMeta(key="commits/p1/invalid.json", size=1, etag="e3")], None),
    }
    store = _MockStore(objects, cursor_map)

    monkeypatch.setattr(backup_targets, "open_target_store", lambda *args, **kwargs: store)
    monkeypatch.setattr(backup_publish, "commit_marker_valid", lambda m: isinstance(m, dict) and bool(m.get("backupId")))

    # First page
    res1 = backup_dr_audit.audit_remote_target(target_id, cursor=None)
    assert res1["status"] == "in-progress"
    assert res1["cursor"] == "cur2"
    assert res1["recoveryPointsFound"] == 1
    assert res1["auditId"]

    # Second page resumes durable job
    res2 = backup_dr_audit.audit_remote_target(target_id, audit_id=res1["auditId"])
    assert res2["status"] == "completed"
    assert res2["cursor"] is None
    assert res2["recoveryPointsFound"] == 2
    assert res2["auditId"] == res1["auditId"]
    assert any("malformed" in a or "invalid" in a for a in res2["anomalies"])


def test_audit_remote_target_open_error(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open(*args: Any, **kwargs: Any) -> Any:
        raise AppError("Target store unreachable", code=ErrorCode.INTERNAL)

    monkeypatch.setattr(backup_targets, "open_target_store", fake_open)

    with pytest.raises(AppError):
        backup_dr_audit.audit_remote_target("target_fail")
