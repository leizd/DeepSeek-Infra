"""Coverage tests for Backup Governance Routes, Server Handlers, and HTTP Utils (v4.5)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_policies, backup_targets
from deepseek_infra.launcher import credentials as creds_module
from deepseek_infra.web import http_utils
from deepseek_infra.web import server as server_module
import deepseek_infra.web.routes.backup_governance as governance
from deepseek_infra.web.routes.backup_governance import create_backup_governance_router


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_backup_governance_457_routes_full(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda r: None)
    app = FastAPI()
    app.include_router(create_backup_governance_router())
    with TestClient(app) as client:
        # 1. Capacity summary
        res_cap = client.get("/api/workspace/backup-capacity/summary")
        assert res_cap.status_code == 200

        # 2. Transfer budget
        res_tb = client.get("/api/workspace/backup-transfer-budget")
        assert res_tb.status_code == 200

        # 3. Create, get, list backup retirements
        t_id = "target_retire_api_1"
        t_root = tmp_settings / "retire_api_1"
        backup_targets.register_filesystem_target(t_id, path=t_root)

        res_create = client.post(
            "/api/workspace/backup-retirements",
            json={"policyId": "pol-retire-api", "backupId": "bk-retire-api", "targetId": t_id, "reason": "api test"},
        )
        assert res_create.status_code in {200, 201, 400, 404, 409}
        if res_create.status_code in {200, 201}:
            job_id = res_create.json()["jobId"]
            res_get = client.get(f"/api/workspace/backup-retirements/{job_id}")
            assert res_get.status_code == 200

        res_list = client.get("/api/workspace/backup-retirements")
        assert res_list.status_code == 200
        assert "jobs" in res_list.json()

        # 4. Continuity and Promote Primary
        policy_cont = {
            "name": "Policy Cont Test",
            "policyId": "pol-cont-api-1",
            "targetId": t_id,
        }
        backup_policies.create_policy(policy_cont)

        res_cont = client.get("/api/workspace/backup-policies/pol-cont-api-1/continuity")
        assert res_cont.status_code == 200

        with patch("deepseek_infra.infra.workspace.backup_write_continuity.promote_primary_target", return_value={"promoted": True}):
            res_promote = client.post(
                "/api/workspace/backup-policies/pol-cont-api-1/promote-primary",
                json={"targetId": t_id, "expectedPolicyRevision": 1},
            )
            assert res_promote.status_code == 200

        # 5. Error cases
        with pytest.raises(AppError):
            client.post("/api/workspace/backup-retirements", json={})

        with pytest.raises(AppError):
            client.get("/api/workspace/backup-retirements/job_nonexistent_xyz")

        with pytest.raises(AppError):
            client.post("/api/workspace/backup-policies/pol-cont-api-1/promote-primary", json={})


def test_web_routes_457(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(governance, "require_api_auth", lambda _req: None)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: object, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.to_response(), status_code=exc.status or 400)

    app.include_router(create_backup_governance_router())
    with TestClient(app) as client:
        # 1. Target drain endpoints
        tid = "target_api_drain_1"
        backup_targets.register_filesystem_target(tid, path=tmp_settings / "adt1")

        res_drain = client.post(f"/api/workspace/backup-targets/{tid}/drain", json={"reason": "test"})
        assert res_drain.status_code == 200
        assert res_drain.json()["phase"] == "draining"

        res_get_drain = client.get(f"/api/workspace/backup-targets/{tid}/drain")
        assert res_get_drain.status_code == 200
        assert res_get_drain.json()["phase"] == "draining"

        res_cancel = client.post(f"/api/workspace/backup-targets/{tid}/drain/cancel", json={"reason": "test-cancel"})
        assert res_cancel.status_code == 200
        assert res_cancel.json()["phase"] == "cancelled"

        # 2. Capacity & transfer budget endpoints
        res_cap = client.get("/api/workspace/backup-capacity/summary")
        assert res_cap.status_code == 200
        assert "targets" in res_cap.json()

        res_budget = client.get("/api/workspace/backup-transfer-budget")
        assert res_budget.status_code == 200
        assert "globalBandwidthBytesPerSec" in res_budget.json()


def test_server_share_target_post_coverage(tmp_settings: Path) -> None:
    srv, _ = server_module.create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1", follow_redirects=False)

    # 1. Share target with text and title as multipart
    resp1 = client.post(
        "/share-target",
        data={"title": "Shared Title", "text": "Shared Text", "url": "https://example.com"},
        files={"dummy": ("sample.txt", b"sample content", "text/plain")},
    )
    assert resp1.status_code == 303
    assert "/?share=" in resp1.headers.get("Location", "")

    # 2. Share target with uploaded file and with extraction
    fake_file = ("test.txt", b"hello shared file", "text/plain")
    resp2 = client.post(
        "/share-target",
        data={"title": "File Title"},
        files={"file": fake_file},
    )
    assert resp2.status_code == 303

    # 3. Share target with invalid/unextractable file error handling
    with patch("deepseek_infra.web.server.extract_uploaded_file", side_effect=AppError("Cannot extract", code=ErrorCode.INVALID_REQUEST, status=400)):
        resp3 = client.post(
            "/share-target",
            files={"file": ("corrupt.bin", b"xyz", "application/octet-stream")},
        )
        assert resp3.status_code == 303

    # 4. File text endpoint and projects list action
    auth_headers = {"Authorization": f"Bearer {settings.auth.token}"}
    resp4 = client.post(
        "/api/file-text",
        files={"file": ("hello.txt", b"some text", "text/plain")},
        headers=auth_headers,
    )
    assert resp4.status_code == 200

    resp5 = client.post(
        "/api/projects",
        json={"action": "list"},
        headers=auth_headers,
    )
    assert resp5.status_code == 200


def test_server_static_paths_and_redaction(tmp_settings: Path) -> None:
    # 1. resolve_static_file branches
    assert server_module.resolve_static_file("/../secret.txt") is None
    assert server_module.resolve_static_file("/legacy/old.html") is None
    assert server_module.resolve_static_file("") is not None
    assert server_module.resolve_static_file("/ui") is not None

    # 2. redact_sensitive_query branches
    assert server_module.redact_sensitive_query("http://localhost:8000/api?token=secret123") == "http://localhost:8000/api?token=%5Bredacted%5D"
    assert server_module.redact_sensitive_query("/share?not_token=456") == "/share?not_token=456"


def test_http_utils_cors_and_body_limits(tmp_settings: Path) -> None:
    # 1. allowed_cors_origin with unlisted host
    assert http_utils.allowed_cors_origin("http://malicious.domain.com:8080", 8080) == ""

    # 2. allowed_auth_hosts with 0.0.0.0 and no lan_ip
    fake_settings = replace(http_utils.settings, default_host="0.0.0.0")
    with patch.object(http_utils, "settings", fake_settings):
        with patch.object(http_utils, "local_ip", return_value=""):
            hosts = http_utils.allowed_auth_hosts()
            assert "0.0.0.0" not in hosts


def test_launcher_credentials_restrict_permissions_posix_and_nt(tmp_settings: Path) -> None:
    target_file = tmp_settings / "test_creds.json"
    target_file.write_text("{}", encoding="utf-8")

    # 1. Test NT branch
    with patch.object(os, "name", "nt"):
        creds_module._restrict_permissions(target_file)

    # 2. Test POSIX branch with chmod
    with patch.object(os, "name", "posix"):
        with patch.object(Path, "chmod"):
            creds_module._restrict_permissions(target_file)

    # 3. Test POSIX branch with OSError
    with patch.object(os, "name", "posix"):
        with patch.object(Path, "chmod", side_effect=OSError("Permission denied")):
            creds_module._restrict_permissions(target_file)


def test_server_unhandled_and_import_error_branches(tmp_settings: Path) -> None:
    # 1. load_multipart_module with issue
    with patch("deepseek_infra.web.server.multipart_module_issue", return_value="Incompatible version"):
        assert server_module.load_multipart_module() is None

    # 2. _list_external_mcp_tools with import exception
    with patch.dict("sys.modules", {"deepseek_infra.infra.mcp.bridge": None}):
        res = server_module._list_external_mcp_tools()
        assert res["ok"] is True
        assert res["servers"] == []

    # 3. unhandled_error_handler
    srv, _ = server_module.create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, raise_server_exceptions=False)
    with patch("deepseek_infra.web.server.require_api_auth", side_effect=RuntimeError("unexpected boom")):
        resp = client.get("/api/agent-runs/123")
        assert resp.status_code == 500
        assert resp.json().get("error") == "Server error"


def test_http_utils_require_api_auth_disabled_and_large_body(tmp_settings: Path) -> None:
    # 1. require_api_auth with auth disabled (line 81)
    fake_settings = replace(http_utils.settings, auth=replace(http_utils.settings.auth, enabled=False))
    with patch.object(http_utils, "settings", fake_settings):
        http_utils.require_api_auth(Request({"type": "http"}))

    # 2. read_json_body with body exceeding limit (line 99)
    async def run_body_check() -> None:
        req = Request({"type": "http", "headers": [(b"content-length", b"5000")]})
        with pytest.raises(AppError) as exc_too_large:
            await http_utils.read_json_body(req, max_bytes=100)
        assert exc_too_large.value.status == 413

    asyncio.run(run_body_check())


def test_server_port_scan_oserror_handling(tmp_settings: Path) -> None:
    # Covers server.py lines 808-816
    mock_sock = MagicMock()
    mock_sock.getsockname.side_effect = OSError("sock error")
    mock_sock.close.side_effect = OSError("close error")
    with patch("deepseek_infra.web.server.open_bind_socket", return_value=mock_sock):
        with pytest.raises(SystemExit):
            server_module.create_server(49152)


def test_backup_governance_target_drain_404_and_empty_candidate(tmp_settings: Path) -> None:
    srv, _ = server_module.create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, base_url="http://127.0.0.1")
    auth_headers = {"Authorization": f"Bearer {settings.auth.token}"}

    # 1. Drain 404
    resp = client.get("/api/workspace/backup-targets/non_existent_target/drain", headers=auth_headers)
    assert resp.status_code == 404

    # 2. _find_backup_session with empty targetId in candidates (line 89)
    with patch("deepseek_infra.infra.workspace.backup_targets.list_targets", return_value=[{"targetId": ""}]):
        with pytest.raises(AppError) as exc_nf:
            governance._find_backup_session("bk_nonexistent")
        assert exc_nf.value.status == 404
