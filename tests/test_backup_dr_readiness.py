from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepseek_infra.infra.workspace import backup_component_cache, backup_dr_readiness, backup_incremental, backups
from deepseek_infra.infra.workspace import backup_publish
import deepseek_infra.web.routes.backup_governance as governance
from deepseek_infra.web.routes.backup_governance import create_backup_governance_router


UTC = timezone.utc
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _sample(stage: str, *, byte_count: int, duration_ms: int, observed_at: str = "2026-08-13T11:30:00Z") -> dict[str, Any]:
    return {
        "stage": stage,
        "result": "success",
        "bytes": byte_count,
        "durationMs": duration_ms,
        "observedAt": observed_at,
    }


def test_readiness_uses_latest_committed_recoverable_chain_and_measured_throughput() -> None:
    records = [
        {
            "backupId": "full",
            "targetId": "target-a",
            "policyId": "policy-a",
            "snapshotKind": "full",
            "createdAt": "2026-08-13T10:00:00Z",
            "creationVerified": True,
            "size": 400,
            "logicalBytes": 4_000,
            "scrubOk": True,
            "ciphertextScrubbedAt": "2026-08-13T10:30:00Z",
        },
        {
            "backupId": "incremental",
            "targetId": "target-a",
            "policyId": "policy-a",
            "snapshotKind": "incremental",
            "parentBackupId": "full",
            "createdAt": "2026-08-13T11:00:00Z",
            "creationVerified": True,
            "size": 600,
            "logicalBytes": 6_000,
            "scrubOk": False,
            "ciphertextScrubbedAt": "2026-08-13T11:15:00Z",
        },
        {
            "backupId": "orphan-newer",
            "targetId": "target-a",
            "policyId": "policy-a",
            "snapshotKind": "incremental",
            "parentBackupId": "missing",
            "createdAt": "2026-08-13T11:30:00Z",
            "creationVerified": True,
            "size": 50,
            "logicalBytes": 100,
        },
    ]
    report = backup_dr_readiness.aggregate_readiness(
        catalog_records=records,
        committed_points={("target-a", "full"), ("target-a", "incremental"), ("target-a", "orphan-newer")},
        stage_samples=[
            _sample("transfer", byte_count=100, duration_ms=1_000),
            _sample("crypto", byte_count=100, duration_ms=2_000),
            _sample("materialization", byte_count=250, duration_ms=1_000),
        ],
        drill_records=[
            {"restoreId": "drill-ok", "result": "success", "completedAt": "2026-08-13T09:00:00Z"},
            {"restoreId": "drill-failed", "result": "failed", "completedAt": "2026-08-13T10:45:00Z"},
        ],
        target_health={"target-a": {"status": "ok", "source": "persisted-target-probe", "checkedAt": "2026-08-13T11:45:00Z"}},
        index_health={
            ("target-a", "policy-a"): {"status": "ok", "source": "local-snapshot-index", "checkedAt": "2026-08-13T11:50:00Z"}
        },
        cache_health={"status": "ok", "source": "local-ciphertext-cache", "checkedAt": "2026-08-13T11:55:00Z"},
        now=NOW,
    )

    assert report["recoveryPoint"] == {
        "status": "available",
        "backupId": "incremental",
        "targetId": "target-a",
        "policyId": "policy-a",
        "snapshotKind": "incremental",
        "chainLength": 2,
        "recoveryPointAt": "2026-08-13T11:00:00Z",
        "rpoSeconds": 3_600,
        "source": "validated-commit-and-receipt",
    }
    assert report["rtoEstimate"]["status"] == "estimated"
    assert report["rtoEstimate"]["estimatedSeconds"] == 54
    assert report["rtoEstimate"]["isSla"] is False
    assert report["rtoEstimate"]["evidence"]["samplesByStage"] == {
        "crypto": 1,
        "materialization": 1,
        "transfer": 1,
    }
    assert report["scrub"]["status"] == "error"
    assert report["scrub"]["latestSuccessfulAt"] == "2026-08-13T10:30:00Z"
    assert report["drill"]["status"] == "error"
    assert report["drill"]["latestSuccessfulAt"] == "2026-08-13T09:00:00Z"


def test_readiness_reports_rto_unavailable_when_recent_stage_evidence_is_incomplete() -> None:
    report = backup_dr_readiness.aggregate_readiness(
        catalog_records=[
            {
                "backupId": "full",
                "targetId": "managed-local",
                "policyId": "policy-a",
                "snapshotKind": "full",
                "createdAt": "2026-08-13T11:00:00Z",
                "creationVerified": True,
                "size": 600,
                "logicalBytes": 6_000,
            }
        ],
        committed_points={("managed-local", "full")},
        stage_samples=[
            _sample("transfer", byte_count=100, duration_ms=1_000),
            _sample("crypto", byte_count=100, duration_ms=2_000, observed_at="2026-06-01T00:00:00Z"),
        ],
        drill_records=[],
        target_health={},
        index_health={},
        cache_health={"status": "unavailable", "reason": "not-initialized"},
        now=NOW,
    )

    assert report["rtoEstimate"] == {
        "status": "unavailable",
        "isSla": False,
        "reason": "insufficient-recent-stage-throughput",
        "missingStages": ["crypto", "materialization"],
        "evidenceWindowDays": 30,
    }
    assert report["scrub"]["status"] == "unavailable"
    assert report["drill"]["status"] == "unavailable"
    assert report["status"] == "warning"


def test_disaster_recovery_status_route_is_authenticated(monkeypatch: Any) -> None:
    expected = {"schemaVersion": 1, "status": "warning", "calculatedAt": "2026-08-13T12:00:00Z"}
    auth = Mock()
    monkeypatch.setattr(governance, "require_api_auth", auth)
    monkeypatch.setattr(governance.backup_dr_readiness, "readiness_status", lambda: expected)
    app = FastAPI()
    app.include_router(create_backup_governance_router())

    response = TestClient(app).get("/api/workspace/disaster-recovery/status")

    assert response.status_code == 200
    assert response.json() == expected
    auth.assert_called_once()


def test_readiness_status_empty_state_is_read_only(tmp_settings: Any) -> None:
    assert not backup_incremental.INDEX_DB.exists()
    assert not backup_component_cache.CACHE_DIR.exists()
    assert not backups.RESTORE_DIR.exists()

    report = backup_dr_readiness.readiness_status(now=NOW)

    assert report["recoveryPoint"]["status"] == "unavailable"
    assert report["rtoEstimate"]["status"] == "unavailable"
    assert report["health"]["target"]["status"] == "unavailable"
    assert not backup_incremental.INDEX_DB.exists()
    assert not backup_component_cache.CACHE_DIR.exists()
    assert not backups.RESTORE_DIR.exists()


def test_commit_records_require_hash_chain_and_receipt_digest(tmp_path: Any) -> None:
    root = tmp_path / "target"
    receipt = {
        "backupId": "backup-1",
        "targetId": "target-a",
        "policyId": "policy-a",
        "snapshotKind": "full",
        "createdAt": "2026-08-13T11:00:00Z",
        "creationVerified": True,
        "size": 100,
    }
    receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    receipt_path = root / "receipts" / "backup-1.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(receipt_bytes)
    marker = {
        "schemaVersion": 4,
        "policyId": "policy-a",
        "scheduleSlot": "slot-a",
        "runId": "run-a",
        "fencingToken": 1,
        "backupId": "backup-1",
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "targetGeneration": 1,
        "previousCommitHash": backup_publish.GENESIS_COMMIT_HASH,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    marker_path = root / "commits" / "policy-a" / "slot.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

    records, committed, healthy = backup_dr_readiness._commit_records_for_root(root, "target-a")

    assert healthy is True
    assert records[0]["backupId"] == "backup-1"
    assert committed == {("target-a", "backup-1")}
    receipt_path.write_text("{}", encoding="utf-8")
    records, committed, healthy = backup_dr_readiness._commit_records_for_root(root, "target-a")
    assert records == []
    assert committed == set()
    assert healthy is False
