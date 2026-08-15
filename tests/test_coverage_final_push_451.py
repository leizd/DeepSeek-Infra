"""Final push test coverage for 4.5.1 modules (dr_readiness, recovery_drill, targets)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_policies,
    backup_publish,
    backup_recovery_credential,
    backup_recovery_drill,
    backup_targets,
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
    assert "No recovery point found" in str(exc_info.value)


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
