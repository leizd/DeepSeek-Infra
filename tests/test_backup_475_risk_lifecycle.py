"""Coverage-aware risk observation lifecycle (Gate A)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from deepseek_infra.infra.workspace import resilience_risk_observations


def _open_replica(*, backup_id: str, policy_id: str = "policy-A") -> dict[str, object]:
    return {
        "type": "REPLICA_LAG",
        "policyId": policy_id,
        "backupId": backup_id,
        "targetId": "target-A",
        "severity": "warning",
    }


def test_superseded_backup_risk_cannot_remain_open(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    first = {
        "riskDigest": "d1",
        "generatedAt": "2026-08-28T00:00:00Z",
        "coverage": {"REPLICA_LAG": {"policies": ["policy-A"], "backups": ["backup-100"], "complete": True}},
        "risks": [_open_replica(backup_id="backup-100")],
    }
    resilience_risk_observations.observe_risk_snapshot(first, now=now)
    later = {
        "riskDigest": "d2",
        "generatedAt": "2026-08-29T00:00:00Z",
        "coverage": {"REPLICA_LAG": {"policies": ["policy-A"], "backups": ["backup-101"], "complete": True}},
        "risks": [_open_replica(backup_id="backup-101")],
    }
    records = resilience_risk_observations.observe_risk_snapshot(later, now=now)
    old = resilience_risk_observations.observation_for_risk(_open_replica(backup_id="backup-100"))
    new = resilience_risk_observations.observation_for_risk(_open_replica(backup_id="backup-101"))
    assert old is not None and old["status"] == "SUPERSEDED"
    assert old["closureReason"] == "SUPERSEDED_BACKUP"
    assert new is not None and new["status"] == "OPEN"
    assert old["riskSubjectDigest"] not in {item["riskSubjectDigest"] for item in resilience_risk_observations.list_open_observations()}
    assert any(item["status"] == "SUPERSEDED" for item in records)


def test_policy_disabled_and_target_removed_are_retired(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "open-policy",
            "generatedAt": "2026-08-28T00:00:00Z",
            "coverage": {"REPLICA_LAG": {"policies": ["policy-A"], "complete": True}},
            "risks": [_open_replica(backup_id="backup-100")],
        },
        now=now,
    )
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "policy-gone",
            "generatedAt": "2026-08-29T00:00:00Z",
            "coverage": {"REPLICA_LAG": {"policies": ["policy-B"], "complete": True}},
            "risks": [],
        },
        now=now,
    )
    retired = resilience_risk_observations.observation_for_risk(_open_replica(backup_id="backup-100"))
    assert retired is not None and retired["status"] == "RETIRED"
    assert retired["closureReason"] == "POLICY_DISABLED"

    capacity = {"type": "CAPACITY_EXHAUSTION", "targetId": "target-Z", "severity": "degraded"}
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "cap-open",
            "generatedAt": "2026-08-29T01:00:00Z",
            "coverage": {"CAPACITY_EXHAUSTION": {"targets": ["target-Z"], "complete": True}},
            "risks": [capacity],
        },
        now=now,
    )
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "cap-removed",
            "generatedAt": "2026-08-29T02:00:00Z",
            "coverage": {"CAPACITY_EXHAUSTION": {"targets": ["target-A"], "complete": True}},
            "risks": [],
        },
        now=now,
    )
    removed = resilience_risk_observations.observation_for_risk(capacity)
    assert removed is not None and removed["status"] == "RETIRED"
    assert removed["closureReason"] == "TARGET_REMOVED"


def test_unknown_coverage_does_not_implicitly_clear(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    risk = _open_replica(backup_id="backup-100")
    resilience_risk_observations.observe_risk_snapshot(
        {"riskDigest": "open", "generatedAt": "2026-08-28T00:00:00Z", "risks": [risk]},
        now=now,
    )
    later = resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "partial",
            "generatedAt": "2026-08-29T00:00:00Z",
            "coverage": {"REPLICA_LAG": {"policies": ["policy-A"], "complete": False}},
            "risks": [],
        },
        now=now,
    )
    record = later[0]
    assert record["status"] == "UNKNOWN_COVERAGE"
    assert record["closureReason"] == "UNKNOWN_COVERAGE"
    assert resilience_risk_observations.list_open_observations()[0]["status"] == "UNKNOWN_COVERAGE"


def test_complete_empty_scope_is_retired_and_healthy_absent_is_cleared(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    risk = {"type": "DR_STALENESS", "severity": "warning", "policyId": "policy-dr"}
    resilience_risk_observations.observe_risk_snapshot(
        {"riskDigest": "dr-open", "generatedAt": "2026-08-28T00:00:00Z", "risks": [risk]},
        now=now,
    )
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "dr-retired",
            "generatedAt": "2026-08-29T00:00:00Z",
            "coverage": {"DR_STALENESS": {"complete": True}},
            "risks": [],
        },
        now=now,
    )
    retired = resilience_risk_observations.observation_for_risk(risk)
    assert retired is not None and retired["status"] == "RETIRED"
    assert retired["closureReason"] == "SCOPE_RETIRED"

    capacity = {"type": "CAPACITY_EXHAUSTION", "targetId": "target-A", "severity": "warning"}
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "cap-open-2",
            "generatedAt": "2026-08-29T03:00:00Z",
            "coverage": {"CAPACITY_EXHAUSTION": {"targets": ["target-A"], "complete": True}},
            "risks": [capacity],
        },
        now=now,
    )
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "cap-healthy",
            "generatedAt": "2026-08-29T04:00:00Z",
            "coverage": {"CAPACITY_EXHAUSTION": {"targets": ["target-A"], "complete": True}},
            "risks": [],
        },
        now=now,
    )
    cleared = resilience_risk_observations.observation_for_risk(capacity)
    assert cleared is not None and cleared["status"] == "CLEARED"
    assert cleared["closureReason"] == "HEALTHY"


def test_retired_risk_can_reopen_when_it_returns(tmp_settings: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    risk = _open_replica(backup_id="backup-100", policy_id="policy-gone")
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "a",
            "generatedAt": "2026-08-28T00:00:00Z",
            "coverage": {"REPLICA_LAG": {"policies": ["policy-gone"], "complete": True}},
            "risks": [risk],
        },
        now=now,
    )
    resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "b",
            "generatedAt": "2026-08-29T00:00:00Z",
            "coverage": {"REPLICA_LAG": {"policies": ["other"], "complete": True}},
            "risks": [],
        },
        now=now,
    )
    reopened = resilience_risk_observations.observe_risk_snapshot(
        {
            "riskDigest": "c",
            "generatedAt": "2026-08-29T05:00:00Z",
            "coverage": {"REPLICA_LAG": {"policies": ["policy-gone"], "complete": True}},
            "risks": [risk],
        },
        now=now,
    )
    assert reopened[0]["status"] == "REOPENED"
    assert reopened[0]["closureReason"] is None
