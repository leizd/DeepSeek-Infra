"""Final push test coverage for disaster recovery modules (dr_readiness, recovery_drill, targets, keeper, class, audit, creds)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_readiness,
    backup_policies,
    backup_publish,
    backup_recovery_class,
    backup_recovery_credential,
    backup_recovery_drill,
    backup_recovery_keeper,
    backup_targets,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta


def test_scheduled_drill_missing_credentials_and_recovery_points(tmp_settings: Path) -> None:
    # 1. Blocked drill due to missing credential
    backup_policies.create_policy(
        {
            "policyId": "p_drill_test",
            "name": "Drill Policy",
            "targetId": "target_drill",
            "recoveryDrill": {
                "enabled": True,
                "credentialRef": "missing_ref",
            },
        }
    )
    res_blocked = backup_recovery_drill.execute_scheduled_drill("p_drill_test")
    assert res_blocked["status"] == "blocked"
    assert res_blocked["reason"] == "unlock-required"

    # 2. Credential present but no recovery points in ledger
    prov = backup_recovery_credential.InMemoryCredentialProvider()
    prov.set_secret("valid_ref", "secret_key_123")
    backup_recovery_credential.set_default_credential_provider(prov)

    backup_policies.create_policy(
        {
            "policyId": "p_drill_no_rp",
            "name": "Drill Policy No RP",
            "targetId": "target_no_rp",
            "recoveryDrill": {
                "enabled": True,
                "credentialRef": "valid_ref",
            },
        }
    )
    with pytest.raises(AppError) as exc_info:
        backup_recovery_drill.execute_scheduled_drill("p_drill_no_rp")
    assert "recovery point" in str(exc_info.value).lower()


def test_dr_readiness_store_commit_records_paging_and_anomalies(tmp_settings: Path) -> None:
    target_id = "target_paged"
    m1 = {
        "schemaVersion": 4,
        "backupId": "bk1",
        "policyId": "p1",
        "committedAt": "2026-08-15T01:00:00Z",
    }
    m1["commitHash"] = backup_publish._commit_hash(m1)

    objects = {
        "commits/p1/c1.json": json.dumps(m1).encode("utf-8"),
        "commits/p1/bad.json": b"invalid json",
        "commits/p1/other.txt": b"ignored",
        "receipts/bk1.json": json.dumps({"backupId": "bk1", "size": 100}).encode("utf-8"),
    }

    class PagedStore:
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            if cursor is None:
                return ListPage(
                    objects=(
                        ObjectMeta(key="commits/p1/c1.json", size=10, etag="e1"),
                        ObjectMeta(key="commits/p1/bad.json", size=5, etag="e2"),
                    ),
                    cursor="cur2",
                )
            return ListPage(
                objects=(
                    ObjectMeta(key="commits/p1/other.txt", size=1, etag="e3"),
                ),
                cursor=None,
            )

        def get_bytes(self, key: str) -> bytes:
            return objects[key]

        def get_object(self, key: str) -> bytes:
            return objects[key]

    records, committed, healthy = backup_dr_readiness._commit_records_for_store(PagedStore(), target_id)
    assert len(records) == 1
    assert healthy is False  # due to bad.json
    assert (target_id, "bk1") in committed


def test_dr_readiness_validated_chain_gaps() -> None:
    # Gap in parentCommitHash
    m1 = {"schemaVersion": 4, "backupId": "b1"}
    m1["commitHash"] = backup_publish._commit_hash(m1)
    m2 = {"schemaVersion": 4, "backupId": "b2", "parentCommitHash": "hash_mismatch"}
    m2["commitHash"] = backup_publish._commit_hash(m2)

    accepted, valid = backup_dr_readiness._validated_commit_chain([m1, m2])
    assert valid is False
    assert accepted == []


def test_backup_targets_open_target_store(tmp_settings: Path) -> None:
    # Open managed-local target store
    store = backup_targets.open_target_store("managed-local")
    assert store is not None


def test_recovery_keeper_daemon_and_health(tmp_settings: Path) -> None:
    health = backup_recovery_keeper.get_recovery_lease_health()
    assert health["status"] == "ok"

    keeper = backup_recovery_keeper.RecoveryLeaseKeeper(tick_interval_seconds=0.1)
    assert keeper.is_running is False
    keeper.start()
    assert keeper.is_running is True
    keeper.step()
    keeper.stop()
    assert keeper.is_running is False

    glob = backup_recovery_keeper.get_global_recovery_keeper()
    assert glob is not None

    assert backup_recovery_keeper._parse_iso("invalid-iso") is None
    assert backup_recovery_keeper._parse_iso(None) is None
    assert isinstance(backup_recovery_keeper._parse_iso("2026-08-15T12:00:00Z"), datetime)


def test_recovery_class_classification_and_calibration() -> None:
    # Classify buckets
    assert backup_recovery_class.size_bucket(500) == "small"
    assert backup_recovery_class.size_bucket(20 * 1024 * 1024) == "medium"
    assert backup_recovery_class.size_bucket(200 * 1024 * 1024) == "large"

    assert backup_recovery_class.chain_depth_bucket(2) == "shallow"
    assert backup_recovery_class.chain_depth_bucket(6) == "moderate"
    assert backup_recovery_class.chain_depth_bucket(15) == "deep"

    rc = backup_recovery_class.classify_recovery(
        target_kind="filesystem",
        logical_bytes=150 * 1024 * 1024,
        chain_length=12,
    )
    assert rc.size_category == "large"
    assert rc.chain_depth == "deep"
    assert "filesystem" in rc.tag
    assert str(rc) == rc.tag
    d = rc.to_dict()
    assert d["sizeBucket"] == "large"

    # Calibration with percentile and multiple samples
    samples = [
        {"stage": "transfer", "bytes": 10_000_000, "durationMs": 200, "recoveryClass": rc.to_dict()}
        for _ in range(12)
    ] + [
        {"stage": "crypto", "bytes": 10_000_000, "durationMs": 100, "recoveryClass": rc.to_dict()}
        for _ in range(12)
    ] + [
        {"stage": "materialize", "bytes": 10_000_000, "durationMs": 50, "recoveryClass": rc.to_dict()}
        for _ in range(12)
    ]
    cal = backup_recovery_class.calibrate_rto(samples, logical_bytes=10_000_000, recovery_class=rc)
    assert cal["confidence"] == "high"
    assert cal["isSla"] is False
    assert cal["p50Seconds"] > 0
    assert cal["p90Seconds"] >= cal["p50Seconds"]


def test_dr_audit_managed_local_target(tmp_settings: Path) -> None:
    root = backups.BACKUP_DIR
    commits_dir = root / "commits" / "pol1"
    commits_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir = root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    commit_data = {
        "schemaVersion": 4,
        "backupId": "bk_local_audit",
        "policyId": "pol1",
        "committedAt": "2026-08-15T02:00:00Z",
    }
    commit_data["commitHash"] = backup_publish._commit_hash(commit_data)
    (commits_dir / "c1.json").write_text(json.dumps(commit_data), encoding="utf-8")

    receipt_data = {
        "backupId": "bk_local_audit",
        "policyId": "pol1",
        "size": 5000,
        "logicalBytes": 12000,
        "chainLength": 1,
        "createdAt": "2026-08-15T02:00:00Z",
    }
    (receipts_dir / "bk_local_audit.json").write_text(json.dumps(receipt_data), encoding="utf-8")

    res = backup_dr_audit.audit_remote_target("managed-local")
    assert res["status"] == "completed"
    assert res["recoveryPointsFound"] >= 1
    assert res["objectsAudited"] >= 1


def test_in_memory_recovery_credential_provider() -> None:
    provider = backup_recovery_credential.InMemoryCredentialProvider()
    provider.set_secret("my_cred", "my_secret_val")
    assert provider.has_credential("my_cred") is True
    assert provider.has_credential("nonexistent") is False

    with provider.open_secret("my_cred") as sec_bytes:
        assert sec_bytes.decode("utf-8") == "my_secret_val"

    sec_bytes_manual = provider.acquire_secret_bytes("my_cred")
    assert sec_bytes_manual.decode("utf-8") == "my_secret_val"
    backup_recovery_credential.zeroize(sec_bytes_manual)
    assert sec_bytes_manual == bytearray(len(sec_bytes_manual))
