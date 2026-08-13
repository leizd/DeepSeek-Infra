from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepseek_infra.infra.workspace import backup_component_cache, backup_dr_readiness, backup_incremental, backups
from deepseek_infra.infra.workspace import backup_publish
from deepseek_infra.infra.workspace.backup_target_store import ListPage, ObjectMeta
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


def _receipt_bytes(backup_id: str, *, created_at: str = "2026-08-13T11:00:00Z") -> bytes:
    return (
        json.dumps(
            {
                "backupId": backup_id,
                "policyId": "policy-a",
                "snapshotKind": "full",
                "createdAt": created_at,
                "creationVerified": True,
                "size": 100,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _commit_marker(backup_id: str, receipt: bytes, *, generation: int = 1, previous: str | None = None) -> dict[str, Any]:
    marker = {
        "schemaVersion": 4,
        "policyId": "policy-a",
        "scheduleSlot": f"slot-{generation}",
        "runId": f"run-{generation}",
        "fencingToken": generation,
        "backupId": backup_id,
        "receiptDigest": hashlib.sha256(receipt).hexdigest(),
        "targetGeneration": generation,
        "previousCommitHash": previous or backup_publish.GENESIS_COMMIT_HASH,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    return marker


def test_readiness_normalizes_invalid_inputs_and_rejects_broken_chains() -> None:
    assert backup_dr_readiness._parse_time(None) is None
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time("2026-08-13T12:00:00") is None
    assert backup_dr_readiness._nonnegative(True) == 0
    assert backup_dr_readiness._nonnegative(-1) == 0

    broken = {
        "backupId": "child",
        "targetId": "target-a",
        "policyId": "policy-a",
        "snapshotKind": "incremental",
        "parentBackupId": "missing",
        "createdAt": "2026-08-13T11:00:00Z",
        "creationVerified": True,
    }
    report = backup_dr_readiness.aggregate_readiness(
        catalog_records=[broken],
        committed_points={("target-a", "child")},
        stage_samples=[],
        drill_records=[{"completedAt": "2026-08-14T00:00:00Z", "result": "success"}],
        target_health={},
        index_health={},
        cache_health={"status": "error"},
        now=NOW,
    )
    assert report["status"] == "error"
    assert report["recoveryPoint"]["reason"] == "no-committed-recoverable-point"
    assert report["rtoEstimate"]["reason"] == "recovery-point-unavailable"
    assert report["drill"]["reason"] == "no-evidence"


def test_rto_requires_recovery_workload_even_with_complete_stage_samples() -> None:
    record = {
        "backupId": "full",
        "targetId": "target-a",
        "policyId": "policy-a",
        "snapshotKind": "full",
        "createdAt": "2026-08-13T11:00:00Z",
        "creationVerified": True,
        "size": 0,
        "logicalBytes": 0,
    }
    report = backup_dr_readiness.aggregate_readiness(
        catalog_records=[record],
        committed_points={("target-a", "full")},
        stage_samples=[_sample(stage, byte_count=100, duration_ms=10) for stage in backup_dr_readiness.REQUIRED_RTO_STAGES],
        drill_records=[],
        target_health={"target-a": {"status": "ok"}},
        index_health={("target-a", "policy-a"): {"status": "ok"}},
        cache_health={"status": "ok"},
        now=NOW,
    )
    assert report["rtoEstimate"]["reason"] == "recovery-point-workload-unavailable"
    assert report["rtoEstimate"]["missingWorkload"] == ["ciphertextBytes", "logicalBytes"]


def test_commit_chain_rejects_forks_and_merges_only_catalog_governance(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = _receipt_bytes("backup-1")
    first = _commit_marker("backup-1", receipt)
    fork = _commit_marker("backup-2", _receipt_bytes("backup-2"))
    accepted, valid = backup_dr_readiness._validated_commit_chain([first, fork])
    assert accepted == []
    assert valid is False

    monkeypatch.setattr(
        backup_dr_readiness.backup_catalog,
        "catalog_state",
        lambda _root: {
            "backup-1": {
                "backupId": "backup-1",
                "pinned": True,
                "scrubOk": True,
                "ciphertextScrubbedAt": "2026-08-13T11:30:00Z",
                "size": 999,
            }
        },
    )
    merged = backup_dr_readiness._merge_validated_receipt(
        json.loads(receipt),
        backup_dr_readiness.backup_catalog.catalog_state(Path("unused"))["backup-1"],
        target_id="target-a",
    )
    assert merged["pinned"] is True
    assert merged["scrubOk"] is True
    assert merged["size"] == 100
    assert merged["targetId"] == "target-a"


class _ReadOnlyStore:
    def __init__(self, objects: dict[str, bytes], pages: list[tuple[str, ...]]) -> None:
        self.objects = objects
        self.pages = pages

    def list_objects(self, _prefix: str, *, cursor: str | None = None) -> ListPage:
        index = int(cursor or 0)
        keys = self.pages[index]
        next_cursor = str(index + 1) if index + 1 < len(self.pages) else None
        return ListPage(tuple(ObjectMeta(key=key, size=len(self.objects.get(key, b"")), etag=f'etag-{key}') for key in keys), next_cursor)

    def get_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)


def test_remote_store_commit_reader_pages_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = _receipt_bytes("backup-1")
    marker = _commit_marker("backup-1", receipt)
    marker_key = "commits/policy-a/one.json"
    store = _ReadOnlyStore(
        {marker_key: json.dumps(marker).encode(), "receipts/backup-1.json": receipt},
        [(marker_key,), ()],
    )
    monkeypatch.setattr(backup_dr_readiness.backup_catalog, "catalog_state_store", lambda _store: {})
    records, committed, healthy = backup_dr_readiness._commit_records_for_store(store, "remote-a")
    assert healthy is True
    assert records[0]["targetId"] == "remote-a"
    assert committed == {("remote-a", "backup-1")}

    store.objects["receipts/backup-1.json"] = b"not-json"
    records, committed, healthy = backup_dr_readiness._commit_records_for_store(store, "remote-a")
    assert records == []
    assert committed == set()
    assert healthy is False

    bad_store = _ReadOnlyStore({"commits/policy-a/not-json": b"x"}, [("commits/policy-a/not-json",)])
    records, committed, healthy = backup_dr_readiness._commit_records_for_store(bad_store, "remote-a")
    assert (records, committed, healthy) == ([], set(), False)


def _insert_lineage(connection: sqlite3.Connection, backup_id: str = "backup-1") -> None:
    connection.execute(
        """
        INSERT INTO snapshot_lineages (
            target_id, policy_id, backup_id, parent_backup_id, base_backup_id,
            chain_depth, root_digest, committed_at, logical_bytes
        ) VALUES ('target-a', 'policy-a', ?, NULL, ?, 0, 'root-a', ?, 4096)
        """,
        (backup_id, backup_id, "2026-08-13T11:00:00Z"),
    )


def test_index_health_reads_healthy_stale_and_mismatch_states(tmp_settings: Path) -> None:
    record = {"targetId": "target-a", "policyId": "policy-a", "backupId": "backup-1"}
    with backup_incremental._connect() as connection:
        _insert_lineage(connection)
        connection.execute(
            "INSERT INTO current_effective_heads (target_id, policy_id, backup_id, root_digest) VALUES (?, ?, ?, ?)",
            ("target-a", "policy-a", "backup-1", "root-a"),
        )
        connection.commit()
    health = backup_dr_readiness._read_index([record], NOW)
    assert health[("target-a", "policy-a")]["status"] == "ok"
    assert record["logicalBytes"] == 4096

    backup_incremental._health_marker_path("target-a", "policy-a").parent.mkdir(parents=True, exist_ok=True)
    backup_incremental._health_marker_path("target-a", "policy-a").write_text("stale", encoding="utf-8")
    assert backup_dr_readiness._read_index([record], NOW)[("target-a", "policy-a")]["reason"] == "stale-marker"
    backup_incremental._health_marker_path("target-a", "policy-a").unlink()

    with sqlite3.connect(backup_incremental.INDEX_DB) as connection:
        connection.execute("UPDATE current_effective_heads SET backup_id = 'other'")
        connection.commit()
    assert backup_dr_readiness._read_index([record], NOW)[("target-a", "policy-a")]["reason"] == "head-mismatch"


def test_index_health_handles_missing_unreadable_and_unindexed_scopes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = {"targetId": "target-a", "policyId": "policy-a", "backupId": "missing"}
    assert backup_dr_readiness._read_index([record], NOW)[("target-a", "policy-a")]["reason"] == "not-initialized"
    backup_incremental.INDEX_DB.parent.mkdir(parents=True)
    backup_incremental.INDEX_DB.write_text("not-sqlite", encoding="utf-8")
    assert backup_dr_readiness._read_index([record], NOW)[("target-a", "policy-a")]["reason"] == "index-unreadable"

    backup_incremental.INDEX_DB.unlink()
    with backup_incremental._connect() as connection:
        connection.commit()
    assert backup_dr_readiness._read_index([record], NOW)[("target-a", "policy-a")]["reason"] == "scope-not-indexed"

    real_connect = sqlite3.connect

    def fail_connect(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise sqlite3.Error("unreadable")

    monkeypatch.setattr(backup_dr_readiness.sqlite3, "connect", fail_connect)
    assert backup_dr_readiness._read_index([record], NOW)[("target-a", "policy-a")]["reason"] == "index-unreadable"
    monkeypatch.setattr(backup_dr_readiness.sqlite3, "connect", real_connect)


def test_cache_health_and_local_evidence_scanners_are_read_only(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert backup_dr_readiness._cache_health(NOW)["reason"] == "not-initialized"
    digest = hashlib.sha256(b"ciphertext").hexdigest()
    cache = backup_component_cache.ComponentCache()
    cache.fetch(digest, len(b"ciphertext"), lambda _offset: iter((b"ciphertext",)))
    cache.partial_path(digest).write_bytes(b"partial")
    cache.pin("restore-a", [digest])
    health = backup_dr_readiness._cache_health(NOW)
    assert health["status"] == "ok"
    assert health["entries"] == 1
    assert health["partialFiles"] == 1
    assert health["pinnedEntries"] == 1

    monkeypatch.setattr(backup_component_cache, "DEFAULT_QUOTA_BYTES", 1)
    assert backup_dr_readiness._cache_health(NOW)["status"] == "warning"
    (backup_component_cache.CACHE_DIR / "pins" / "restore-a.json").write_text("{}", encoding="utf-8")
    assert backup_dr_readiness._cache_health(NOW)["reason"] == "pin-metadata-invalid"

    session = backups.RESTORE_DIR / "restore-a"
    session.mkdir(parents=True)
    (session / "remote-fetch.json").write_text(
        json.dumps(
            {
                "recoveryTelemetry": {
                    "samples": [
                        _sample("transfer", byte_count=10, duration_ms=1),
                        {"stage": "unknown"},
                        "bad",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (session / "drill-result.json").write_text(json.dumps({"result": "success", "completedAt": "2026-08-13T11:00:00Z"}), encoding="utf-8")
    malformed = backups.RESTORE_DIR / "restore-b"
    malformed.mkdir()
    (malformed / "remote-fetch.json").write_text("bad", encoding="utf-8")
    (malformed / "drill-result.json").write_text("[]", encoding="utf-8")
    assert [sample["stage"] for sample in backup_dr_readiness._stage_samples()] == ["transfer"]
    assert backup_dr_readiness._drill_records() == [{"result": "success", "completedAt": "2026-08-13T11:00:00Z"}]


def test_root_reader_and_readiness_status_fail_closed_on_unreadable_targets(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_settings / "bad-target"
    (root / "commits" / "policy-a").mkdir(parents=True)
    (root / "commits" / "policy-a" / "bad.json").write_text("bad", encoding="utf-8")
    monkeypatch.setattr(backup_dr_readiness.backup_catalog, "catalog_state", lambda _root: (_ for _ in ()).throw(OSError("bad")))
    assert backup_dr_readiness._commit_records_for_root(root, "target-a") == ([], set(), False)

    monkeypatch.setattr(
        backup_dr_readiness.backup_targets,
        "list_targets",
        lambda: [
            {"targetId": "missing-path", "kind": "filesystem", "lastProbe": {"scheduledBackupReady": False, "status": "failed"}},
            {"targetId": "remote-a", "kind": "s3", "lastProbe": {"scheduledBackupReady": True, "probedAt": "2026-08-13T11:00:00Z"}},
        ],
    )
    monkeypatch.setattr(backup_dr_readiness.backup_targets, "open_target_store", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")))
    report = backup_dr_readiness.readiness_status(now=NOW)
    assert report["health"]["target"]["status"] in {"unavailable", "error"}
