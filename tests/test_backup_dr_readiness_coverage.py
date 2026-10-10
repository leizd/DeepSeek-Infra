"""Coverage tests for edge cases in backup_dr_readiness and backup_recovery_keeper."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_component_cache,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_publish,
    backup_recovery_keeper,
    backup_targets,
)


def test_dr_readiness_cache_health_branches(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    # Not initialized
    assert backup_dr_readiness._cache_health(now)["status"] == "unavailable"

    # Initialized with corrupted pin
    root = backup_component_cache.CACHE_DIR
    pins_dir = root / "pins"
    pins_dir.mkdir(parents=True, exist_ok=True)
    (pins_dir / "bad.json").write_text("invalid json", encoding="utf-8")
    assert backup_dr_readiness._cache_health(now)["reason"] == "pin-metadata-invalid"

    # Valid pin and partial files
    (pins_dir / "bad.json").unlink()
    valid_pin = {"schemaVersion": 1, "digests": ["a" * 64]}
    (pins_dir / "good.json").write_text(json.dumps(valid_pin), encoding="utf-8")
    partial_dir = root / "sha256" / "aa"
    partial_dir.mkdir(parents=True, exist_ok=True)
    (partial_dir / "test.partial").write_bytes(b"partial content")
    (partial_dir / "test.age").write_bytes(b"content")

    health = backup_dr_readiness._cache_health(now)
    assert health["status"] == "ok"
    assert health["entries"] == 1
    assert health["partialFiles"] == 1
    assert health["pinnedEntries"] == 1


def test_evaluate_scope_readiness_target_unhealthy_and_overdue(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    target_id = "target_overdue"
    policy_id = "policy_overdue"

    # Target unhealthy
    backup_dr_ledger.record_target_evidence(
        target_id=target_id,
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=False,
        status="error",
        reason="probe-failed",
    )
    backup_dr_ledger.record_recovery_point(
        target_id=target_id,
        policy_id=policy_id,
        backup_id="bk1",
        committed_at="2026-08-15T00:00:00Z",
        snapshot_kind="full",
        ciphertext_bytes=1000,
        logical_bytes=2000,
        recoverable=True,
    )

    res = backup_dr_readiness.evaluate_scope_readiness(
        target_id,
        policy_id,
        recovery_objectives={
            "maxScrubAgeSeconds": 10,
            "maxDrillAgeSeconds": 10,
        },
        now=now,
    )
    assert res["status"] == "blocked"
    assert "target-unhealthy" in res["reasons"]
    assert "scrub-overdue" in res["reasons"] or "no-policy-scrub-evidence" in res["reasons"]
    assert "drill-overdue" in res["reasons"] or "no-policy-drill-evidence" in res["reasons"]


def test_dr_readiness_commit_records_helpers(tmp_settings: Path) -> None:
    target_id = "target_test"
    root = tmp_settings / "target_root"
    commits_dir = root / "commits" / "policy_a"
    receipts_dir = root / "receipts"
    commits_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)

    # Missing receipts dir
    assert backup_dr_readiness._commit_records_for_root(tmp_settings / "nonexistent", target_id) == ([], set(), True)

    # Valid commit & receipt
    c_data = {
        "schemaVersion": 4,
        "backupId": "bk_c1",
        "policyId": "policy_a",
        "committedAt": "2026-08-15T01:00:00Z",
    }
    c_data["commitHash"] = backup_publish._commit_hash(c_data)
    (commits_dir / "c1.json").write_text(json.dumps(c_data), encoding="utf-8")

    r_data = {
        "backupId": "bk_c1",
        "policyId": "policy_a",
        "snapshotKind": "full",
        "size": 100,
    }
    (receipts_dir / "bk_c1.json").write_text(json.dumps(r_data), encoding="utf-8")

    records, committed, healthy = backup_dr_readiness._commit_records_for_root(root, target_id)
    assert healthy is True
    assert len(records) == 1
    assert (target_id, "bk_c1") in committed

    # Validated commit chain
    m1 = {"schemaVersion": 4, "backupId": "b1"}
    m1["commitHash"] = backup_publish._commit_hash(m1)
    m2 = {"schemaVersion": 4, "backupId": "b2", "parentCommitHash": m1["commitHash"]}
    m2["commitHash"] = backup_publish._commit_hash(m2)

    accepted, valid = backup_dr_readiness._validated_commit_chain([m1, m2])
    assert valid is True
    assert len(accepted) == 2

    # Empty markers
    assert backup_dr_readiness._validated_commit_chain([]) == ([], True)

    # Invalid first parent
    bad_first = {"schemaVersion": 4, "backupId": "b1", "parentCommitHash": "non-null"}
    bad_first["commitHash"] = backup_publish._commit_hash(bad_first)
    assert backup_dr_readiness._validated_commit_chain([bad_first]) == ([], False)


def test_recovery_keeper_single_hold_renew(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_root = tmp_settings / ".restore-staging"
    restore_id = "restore_single_hold"
    session_dir = restore_root / restore_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "remote-fetch.json"

    # Session with holdKey directly
    session_data = {
        "restoreId": restore_id,
        "phase": "fetching",
        "targetId": "target_s3",
        "backupId": "bk_001",
        "holdKey": "holds/single.json",
        "holds": [],
    }
    session_file.write_text(json.dumps(session_data), encoding="utf-8")

    mock_store = object()
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: mock_store)

    from deepseek_infra.infra.workspace import backup_recovery_lease
    renew_calls: list[str] = []

    def fake_renew(store: Any, sess: dict[str, Any], **k: Any) -> bool:
        renew_calls.append(str(sess.get("restoreId") or ""))
        return True

    monkeypatch.setattr(backup_recovery_lease, "renew_session", fake_renew)

    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["scanned"] == 1
    assert summary["renewed"] == 1
    assert len(renew_calls) == 1
