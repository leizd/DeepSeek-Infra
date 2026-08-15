"""Integration tests for disaster recovery and workspace backup route endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_dr_audit,
    backup_recovery_drill,
    backups,
)
from deepseek_infra.web.routes.backup_governance import create_backup_governance_router
from deepseek_infra.web.routes.workspace import WorkspaceRouteDeps, create_workspace_router


@pytest.fixture
def test_app(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.routes.workspace.require_api_auth", lambda _req: None)

    app = FastAPI()
    gov_router = create_backup_governance_router()
    app.include_router(gov_router)

    ws_deps = WorkspaceRouteDeps(read_multipart_files=Mock())
    ws_router = create_workspace_router(ws_deps)
    app.include_router(ws_router)
    return TestClient(app)


def test_disaster_recovery_status_and_audit_routes(test_app: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # GET /api/workspace/disaster-recovery/status
    res = test_app.get("/api/workspace/disaster-recovery/status")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "scopes" in data

    # POST /api/workspace/disaster-recovery/audit
    monkeypatch.setattr(
        backup_dr_audit,
        "audit_remote_target",
        lambda target_id, **k: {
            "targetId": target_id,
            "status": "completed",
            "recoveryPointsFound": 3,
            "anomalies": [],
        },
    )
    res_audit = test_app.post(
        "/api/workspace/disaster-recovery/audit",
        json={"targetId": "target_1", "pageSize": 50},
    )
    assert res_audit.status_code == 200
    assert res_audit.json()["targetId"] == "target_1"
    assert res_audit.json()["recoveryPointsFound"] == 3


def test_recovery_drill_routes(test_app: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_recovery_drill,
        "run_recovery_drill",
        lambda restore_id: {
            "restoreId": restore_id,
            "status": "success",
            "verifiedAt": "2026-08-15T00:00:00Z",
        },
    )
    res_create = test_app.post(
        "/api/workspace/disaster-recovery/drills",
        json={"restoreId": "restore_123"},
    )
    assert res_create.status_code == 200
    assert res_create.json()["restoreId"] == "restore_123"

    monkeypatch.setattr(
        backup_recovery_drill,
        "execute_scheduled_drill",
        lambda policy_id: {
            "policyId": policy_id,
            "status": "completed",
        },
    )
    res_sched = test_app.post("/api/workspace/disaster-recovery/drills/schedule/policy_prod")
    assert res_sched.status_code == 200
    assert res_sched.json()["policyId"] == "policy_prod"

    monkeypatch.setattr(
        backup_recovery_drill,
        "get_recovery_drill",
        lambda restore_id: {
            "restoreId": restore_id,
            "status": "success",
            "verifiedAt": "2026-08-15T00:00:00Z",
        },
    )
    res_get = test_app.get("/api/workspace/disaster-recovery/drills/restore_123")
    assert res_get.status_code == 200
    assert res_get.json()["restoreId"] == "restore_123"


def test_workspace_backup_identities_and_secrets(test_app: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        backup_crypto,
        "generate_identity",
        lambda: {"recipient": "age1fake", "identity": "AGE-SECRET-KEY-FAKE"},
    )
    monkeypatch.setattr(
        backup_crypto,
        "put_secret",
        lambda session_id, kind, secret: {"ok": True, "kind": kind},
    )

    # POST /api/workspace/backups/recovery-identities
    res_ident = test_app.post("/api/workspace/backups/recovery-identities")
    assert res_ident.status_code == 200
    ident_data = res_ident.json()
    assert "recipient" in ident_data
    assert "identity" in ident_data

    # Create a session to test PUT secret & GET & DELETE
    created = backups.create_session({})
    sess_id = str(created["backupId"])

    res_sec = test_app.put(
        f"/api/workspace/backups/{sess_id}/secret",
        json={"kind": "passphrase", "secret": "test-passphrase-1234"},
    )
    assert res_sec.status_code == 200

    res_get_sess = test_app.get(f"/api/workspace/backups/{sess_id}")
    assert res_get_sess.status_code == 200
    assert res_get_sess.json()["backupId"] == sess_id

    # Test restore secrets & cleanup & list & get
    rest_id = "restore_a1b2c3d4e5f60718"
    rest_dir = backups.RESTORE_DIR / rest_id
    rest_dir.mkdir(parents=True, exist_ok=True)
    (rest_dir / "transaction.json").write_text(json.dumps({"restoreId": rest_id, "phase": "created", "kind": "passphrase"}), encoding="utf-8")

    res_rest_sec = test_app.put(
        f"/api/workspace/restores/{rest_id}/secret",
        json={"kind": "passphrase", "secret": "test-passphrase-1234"},
    )
    assert res_rest_sec.status_code == 200

    res_rest_list = test_app.get("/api/workspace/restores")
    assert res_rest_list.status_code == 200

    res_rest_get = test_app.get(f"/api/workspace/restores/{rest_id}")
    assert res_rest_get.status_code == 200

    res_clean = test_app.post("/api/workspace/restores/cleanup")
    assert res_clean.status_code == 200


def test_backup_catalog_routes(test_app: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Catalog GET
    res_cat = test_app.get("/api/workspace/backup-catalog")
    assert res_cat.status_code == 200
    cat_data = res_cat.json()
    assert "backups" in cat_data
    assert "chainValid" in cat_data
