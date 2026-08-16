"""Extra branch coverage for 4.5.3 CI gate (Python 3.10/3.11/3.12 >=95.00%)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_policies,
    backup_recovery_class,
    backup_replication,
    backup_targets,
)
from deepseek_infra.web.routes import backup_governance


def test_replication_job_listing_and_filtering(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    rep_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)

    # Empty dir
    assert backup_replication.list_jobs() == []

    # Corrupted / invalid files
    (rep_dir / ".hidden.json").write_text("{}", encoding="utf-8")
    (rep_dir / "bad.json").write_text("not json", encoding="utf-8")
    (rep_dir / "list.json").write_text("[]", encoding="utf-8")

    job1 = {
        "jobId": "j1",
        "policyId": "pol1",
        "backupId": "b1",
        "mode": "required",
        "phase": "queued",
        "slotDigest": "slot1",
    }
    job2 = {
        "jobId": "j2",
        "policyId": "pol1",
        "backupId": "b2",
        "mode": "required",
        "phase": "retry-wait",
        "slotDigest": "slot2",
    }
    job3 = {
        "jobId": "j3",
        "policyId": "pol2",
        "backupId": "b3",
        "mode": "optional",
        "phase": "committed",
        "slotDigest": "slot3",
    }
    (rep_dir / "job1.json").write_text(json.dumps(job1), encoding="utf-8")
    (rep_dir / "job2.json").write_text(json.dumps(job2), encoding="utf-8")
    (rep_dir / "job3.json").write_text(json.dumps(job3), encoding="utf-8")

    assert len(backup_replication.list_jobs()) == 3
    assert len(backup_replication.list_jobs(policy_id="pol1")) == 2
    assert len(backup_replication.list_jobs(backup_id="b2")) == 1
    assert len(backup_replication.list_jobs(phase="queued")) == 1
    assert len(backup_replication.list_jobs(limit=1)) == 1

    assert backup_replication.has_open_required_jobs(policy_id="pol1") is True
    assert backup_replication.has_open_required_jobs(policy_id="pol1", slot_digest="slot1") is True
    assert backup_replication.has_open_required_jobs(policy_id="pol1", slot_digest="other-slot") is False
    assert backup_replication.has_open_required_jobs(policy_id="pol2") is False


def test_enqueue_replica_jobs_variations(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    rep_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)

    # Disabled replication
    assert backup_replication.enqueue_replica_jobs(
        policy={"policyId": "p1", "replication": {"enabled": False}},
        primary_target_id="managed-local",
        backup_id="b1",
        package=None,
        run_id="r1",
        schedule_slot="2026-08-16T12:00:00Z",
        slot_digest="s1",
    ) == []

    # Empty targets
    assert backup_replication.enqueue_replica_jobs(
        policy={"policyId": "p1", "replication": {"enabled": True, "targets": []}},
        primary_target_id="managed-local",
        backup_id="b1",
        package=None,
        run_id="r1",
        schedule_slot="2026-08-16T12:00:00Z",
        slot_digest="s1",
    ) == []

    # Valid enqueue with primary receipt
    policy = {
        "policyId": "p1",
        "replication": {
            "enabled": True,
            "targets": [
                {"targetId": "replica-1", "mode": "required", "maxAttempts": 3},
                "not-a-dict",
            ],
        },
    }
    jobs = backup_replication.enqueue_replica_jobs(
        policy=policy,
        primary_target_id="managed-local",
        backup_id="b1",
        package=None,
        run_id="r1",
        schedule_slot="2026-08-16T12:00:00Z",
        slot_digest="s1",
        primary_receipt={"objectSetDigest": "os1", "objectDigest": "ctrl1", "objects": []},
    )
    assert len(jobs) == 1
    assert jobs[0]["replicaTargetId"] == "replica-1"


def test_replication_compliance_and_lag_evaluations(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *_a, **_k: None)
    p_path = tmp_settings / "p_target"
    r_path = tmp_settings / "r_target"
    p_path.mkdir(parents=True, exist_ok=True)
    r_path.mkdir(parents=True, exist_ok=True)

    backup_targets.init_target(p_path, label="Primary")
    r_t = backup_targets.init_target(r_path, label="Replica")
    r_id = r_t["targetId"]

    # Policy disabled replication compliance
    res = backup_replication.replication_compliance(policy={"replication": {"enabled": False}}, backup_id="b1")
    assert res["enabled"] is False
    assert res["compliance"] == "healthy"

    # Policy with empty targets
    res2 = backup_replication.replication_compliance(policy={"replication": {"enabled": True, "targets": []}}, backup_id="b1")
    assert res2["compliance"] in {"healthy", "degraded"}

    # Calculate lag when policy missing
    lag_res = backup_replication.calculate_replica_lag("missing-pol", r_id)
    assert lag_res["status"] == "no-primary"


def test_source_holds_and_catalogs(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)
    monkeypatch.setattr(backup_replication, "HOLDS_DIR", rep_dir / "holds")

    hold = backup_replication.acquire_source_hold("src-1", "pol-1", "b-1", "tester")
    assert hold.target_id == "src-1"
    assert backup_replication.is_source_held("src-1", "pol-1", "b-1") is True
    assert backup_replication.is_source_held("src-1", "pol-1", "b-2") is False

    hold.release()
    assert backup_replication.is_source_held("src-1", "pol-1", "b-1") is False


def test_policy_target_bindings_validation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *_a, **_k: None)
    p_path = tmp_settings / "p_target"
    r_path = tmp_settings / "r_target"
    p_path.mkdir(parents=True, exist_ok=True)
    r_path.mkdir(parents=True, exist_ok=True)

    p_t = backup_targets.init_target(p_path, label="Primary")
    r_t = backup_targets.init_target(r_path, label="Replica")
    p_id = p_t["targetId"]
    r_id = r_t["targetId"]

    # Valid policy
    valid_pol = {
        "policyId": "pol-ok",
        "name": "OK Policy",
        "targetId": p_id,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": r_id, "mode": "required"}],
        },
    }
    backup_policies.validate_target_bindings(valid_pol)

    # Invalid primary
    invalid_p = dict(valid_pol, targetId="missing-p")
    with pytest.raises(AppError) as exc:
        backup_policies.validate_target_bindings(invalid_p)
    assert "Unregistered primary targetId" in str(exc.value)

    # Invalid replica
    invalid_r = dict(valid_pol, replication={"enabled": True, "targets": [{"targetId": "missing-r"}]})
    with pytest.raises(AppError) as exc2:
        backup_policies.validate_target_bindings(invalid_r)
    assert "Unregistered replica targetId" in str(exc2.value)


def test_recovery_class_calibration(tmp_settings: Path) -> None:
    # Empty samples calibration
    res = backup_recovery_class.calibrate_rto(target_id="target-1")
    assert "planningHeuristic" in res
    assert res["isSla"] is False


def test_governance_router_endpoints_comprehensive(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _req: None)
    app = FastAPI()
    router = backup_governance.create_backup_governance_router()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    # Capabilities
    res = client.get("/api/workspace/backup-target-capabilities")
    assert res.status_code == 200

    # Policies list
    res_pol = client.get("/api/workspace/backup-policies")
    assert res_pol.status_code == 200
