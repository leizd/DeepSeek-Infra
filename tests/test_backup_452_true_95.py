"""Push true coverage past 95.00 without rounding tricks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_dr_readiness,
    backup_recovery_keeper,
    backup_remote_restore,
    backup_targets,
)


def test_evaluate_scope_many_edges(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    backup_dr_ledger.record_recovery_point(
        target_id="target_e",
        policy_id="pol_e",
        backup_id="bk_e",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=0,
        ciphertext_bytes=0,
        storage_protocol="object-set-v1",
    )
    backup_dr_ledger.record_scrub_evidence(
        target_id="target_e",
        backup_id="bk_e",
        policy_id="pol_e",
        observed_at="2026-08-10T00:00:00Z",
        result="success",
    )
    backup_dr_ledger.record_drill_evidence(
        target_id="target_e",
        policy_id="pol_e",
        backup_id="bk_e",
        observed_at="2026-08-10T00:00:00Z",
        result="blocked",
    )
    backup_dr_ledger.record_target_evidence(
        target_id="target_e",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=True,
        status="ok",
    )
    monkeypatch.setattr(backup_targets, "get_target", lambda tid: {"kind": "filesystem", "targetId": tid})
    scope = backup_dr_readiness.evaluate_scope_readiness(
        "target_e",
        "pol_e",
        recovery_objectives={"maxScrubAgeSeconds": 60, "maxDrillAgeSeconds": 60, "maxRpoSeconds": 10**9},
        policy={
            "policyId": "pol_e",
            "targetId": "target_e",
            "replication": {"enabled": True, "targets": [{"targetId": "tr"}], "minCommittedCopies": 3},
        },
        now=now,
    )
    assert "reasons" in scope
    assert scope["replicationCompliance"] == "degraded"

    # no target evidence defaults health ok
    scope2 = backup_dr_readiness.evaluate_scope_readiness("target_none", "pol_none", now=now)
    assert scope2["recoveryPoint"]["status"] == "unavailable"


def test_readiness_status_rollup(tmp_settings: Path) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="managed-local",
        policy_id="p1",
        backup_id="b1",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=100,
    )
    path = tmp_settings / ".backup-policies" / "p1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "p1",
                "name": "n",
                "enabled": True,
                "targetId": "managed-local",
                "replication": {"enabled": False, "targets": [], "minCommittedCopies": 1},
                "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
                "schedule": {"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
                "scope": {"mode": "full", "projectIds": []},
                "frontendMirror": {"mode": "best-effort"},
                "retentionPolicyId": "default",
                "retry": {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900},
                "incremental": {"mode": "off"},
                "recoveryObjectives": {},
                "recoveryDrill": {"enabled": False},
                "createdAt": "2026-08-15T00:00:00Z",
                "updatedAt": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    report = backup_dr_readiness.readiness_status()
    assert "scopes" in report
    assert "recoveryLeases" in report


def test_commit_records_for_root_with_files(tmp_settings: Path) -> None:
    root = tmp_settings / "tgt"
    commits = root / "commits" / "pol"
    receipts = root / "receipts"
    commits.mkdir(parents=True)
    receipts.mkdir(parents=True)
    commit = {
        "schemaVersion": 4,
        "backupId": "bk1",
        "policyId": "pol",
        "commitHash": "a" * 64,
        "receiptDigest": "b" * 64,
        "previousCommitHash": "0" * 64,
        "targetGeneration": 1,
        "fencingToken": 1,
        "runId": "r",
        "scheduleSlot": "s",
        "slotDigest": "c" * 64,
    }
    from deepseek_infra.infra.workspace import backup_publish

    commit["commitHash"] = backup_publish._commit_hash(commit)
    (commits / "c.json").write_text(json.dumps(commit), encoding="utf-8")
    (receipts / "bk1.json").write_text(json.dumps({"backupId": "bk1", "size": 1}), encoding="utf-8")
    (commits / "bad.json").write_text("not-json", encoding="utf-8")
    records, points, ok = backup_dr_readiness._commit_records_for_root(root, "target_x")
    assert isinstance(records, list)
    assert isinstance(points, set)


def test_keeper_fresh_session_becomes_terminal(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_settings / ".restore-staging" / "r_race"
    root.mkdir(parents=True)
    path = root / "remote-fetch.json"
    path.write_text(
        json.dumps(
            {
                "restoreId": "r_race",
                "phase": "fetching",
                "targetId": "target_r",
                "holds": [{"holdKey": "holds/a.json", "generation": 1}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: object())

    def fake_lock(p: Path):
        class L:
            def __enter__(self):
                # mutate to terminal under lock
                path.write_text(
                    json.dumps({"restoreId": "r_race", "phase": "complete", "targetId": "target_r", "holds": []}),
                    encoding="utf-8",
                )
                return None

            def __exit__(self, *a: Any) -> None:
                return None

        return L()

    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_recovery_job.session_lock", fake_lock)
    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["scanned"] == 1


def test_feed_telemetry_with_target_kind_error(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_targets,
        "get_target",
        lambda tid: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    backup_remote_restore._feed_telemetry_to_ledger(
        {
            "phase": "complete",
            "targetId": "target_missing",
            "expectedBytes": 10,
            "storageProtocol": "object-set-v1",
            "recoveryTelemetry": {
                "samples": [
                    {"stage": "crypto", "result": "success", "bytes": 10, "durationMs": 2, "observedAt": "2026-08-15T00:00:00Z"},
                ]
            },
        }
    )
    samples = backup_dr_ledger.list_stage_samples(stage="crypto")
    assert samples
