"""Extra tests for workspace routes to cover edge cases."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backups, mutation_gate
import deepseek_infra.web.routes.workspace as workspace_routes
from deepseek_infra.web import server as server_module
from deepseek_infra.web.routes.workspace import WorkspaceRouteDeps, create_workspace_router


async def _read_multipart_files(_request: Any) -> tuple[list[dict[str, Any]], bool, str]:
    return [], False, ""


async def _read_multipart_with_files(_request: Any) -> tuple[list[dict[str, Any]], bool, str]:
    return [{"filename": "f.txt", "content_type": "text/plain", "data": b"data"}], False, ""

def _app_with_router(router: Any) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(router)
    return app


@pytest.fixture
def client(tmp_settings: Path) -> Iterator[TestClient]:
    deps = WorkspaceRouteDeps(read_multipart_files=_read_multipart_files)
    app = _app_with_router(create_workspace_router(deps))
    with patch("deepseek_infra.web.routes.workspace.require_api_auth", lambda request: None):
        yield TestClient(app)


@pytest.fixture
def client_with_files(tmp_settings: Path) -> Iterator[TestClient]:
    deps = WorkspaceRouteDeps(read_multipart_files=_read_multipart_with_files)
    app = _app_with_router(create_workspace_router(deps))
    with patch("deepseek_infra.web.routes.workspace.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_projects_create_get_rename_delete(client: TestClient) -> None:
    project_id = "proj-1"
    with patch.object(workspace_routes, "create_project", return_value={"id": project_id, "name": "P1"}):
        resp = client.post("/api/projects", json={"action": "create", "name": "P1"})
    assert resp.status_code == 200

    mock_projects = SimpleNamespace(
        get_project=lambda _id: {"id": _id},
        rename_project=lambda _id, name, description=None: {"id": _id, "name": name},
        delete_project=lambda _id: _id,
    )
    with patch.object(workspace_routes, "workspace_projects", mock_projects):
        resp = client.post("/api/projects", json={"action": "get", "id": project_id})
        assert resp.status_code == 200
        resp = client.post("/api/projects", json={"action": "rename", "id": project_id, "name": "P2", "description": "desc"})
        assert resp.status_code == 200
        resp = client.post("/api/projects", json={"action": "delete", "id": project_id})
        assert resp.status_code == 200


def test_projects_action_unsupported(client: TestClient) -> None:
    resp = client.post("/api/projects", json={"action": "nope"})
    assert resp.status_code == 400


def test_backup_session_finalize_download_and_inspect(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backups.backup_crypto, "helper_path", lambda: None)
    capabilities = client.get("/api/workspace/backups/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["purpose"] == "restorable-backup"
    assert capabilities.json()["encrypted"] is False

    created = client.post(
        "/api/workspace/backups",
        json={"mode": "full", "requiresFrontendState": False},
    )
    assert created.status_code == 200
    backup_id = created.json()["backupId"]
    ready = client.post(f"/api/workspace/backups/{backup_id}/finalize", json={})
    assert ready.status_code == 200
    assert ready.json()["phase"] == "ready"
    with patch.object(Path, "read_bytes", side_effect=AssertionError("backup download must stream")):
        downloaded = client.get(f"/api/workspace/backups/{backup_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/vnd.deepseek-infra.backup")

    inspected = client.post(
        "/api/workspace/restores/inspect",
        content=downloaded.content,
        headers={"Content-Type": "application/vnd.deepseek-infra.backup+zip"},
    )
    assert inspected.status_code == 200
    assert inspected.json()["purpose"] == "restorable-backup"


def test_backup_stream_upload_stops_at_limit_and_cleans_temporary_file(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_routes.workspace_backups, "MAX_ARCHIVE_BYTES", 1)
    response = client.post(
        "/api/workspace/restores/inspect",
        content=b"xx",
        headers={
            "Content-Type": "application/vnd.deepseek-infra.backup+zip",
            "X-Backup-Filename": "large.dsibackup",
        },
    )
    assert response.status_code == 413
    upload_dir = workspace_routes.workspace_backups.RESTORE_DIR / ".uploads"
    assert not upload_dir.exists() or not list(upload_dir.iterdir())


def test_restore_transaction_routes_and_legacy_upload(client_with_files: TestClient) -> None:
    restore_id = "restore-route"
    result = {"ok": True, "restoreId": restore_id, "phase": "backend-staged"}
    with (
        patch.object(workspace_routes.workspace_backups, "inspect_archive", return_value={"ok": True, "restoreId": restore_id}),
        patch.object(workspace_routes.workspace_backups, "put_frontend_state", return_value={"ok": True}),
        patch.object(workspace_routes.workspace_backups, "get_session", return_value={"ok": True, "phase": "ready"}),
        patch.object(workspace_routes.workspace_backups, "delete_backup", return_value=True),
        patch.object(workspace_routes.workspace_backups, "list_restores", return_value={"ok": True, "restores": [result]}),
        patch.object(workspace_routes.workspace_backups, "cleanup_restores", return_value={"ok": True, "deleted": []}),
        patch.object(workspace_routes.workspace_backups, "get_restore", return_value=result),
        patch.object(workspace_routes.workspace_backups, "delete_restore", return_value=True),
        patch.object(workspace_routes.workspace_backups, "prepare_restore", return_value=result) as prepare,
        patch.object(workspace_routes.workspace_backups, "frontend_prepared", return_value={**result, "phase": "frontend-staged"}) as staged,
        patch.object(workspace_routes.workspace_backups, "commit_restore", return_value={**result, "phase": "backend-committed"}) as commit,
        patch.object(workspace_routes.workspace_backups, "complete_restore", return_value={**result, "phase": "complete"}) as complete,
        patch.object(workspace_routes.workspace_backups, "abort_restore", return_value={**result, "phase": "rolled-back"}),
        patch.object(workspace_routes.workspace_backups, "apply_restore", return_value={**result, "phase": "complete"}) as apply,
    ):
        legacy = client_with_files.post(
            "/api/workspace/restores/inspect",
            files={"backup": ("backup.dsibackup", b"data", "application/octet-stream")},
        )
        assert legacy.status_code == 200
        assert client_with_files.put("/api/workspace/backups/backup_route/frontend-state", json={"schemaVersion": 1}).status_code == 200
        assert client_with_files.get("/api/workspace/backups/backup_route").json()["phase"] == "ready"
        assert client_with_files.delete("/api/workspace/backups/backup_route").json()["deleted"] is True
        assert client_with_files.get("/api/workspace/restores").json()["restores"]
        assert client_with_files.post("/api/workspace/restores/cleanup").status_code == 200
        assert client_with_files.get(f"/api/workspace/restores/{restore_id}").status_code == 200
        assert client_with_files.delete(f"/api/workspace/restores/{restore_id}").json()["deleted"] is True
        prepared = client_with_files.post(
            f"/api/workspace/restores/{restore_id}/prepare",
            json={
                "mode": "project-copy",
                "previousEpoch": "old",
                "targetEpoch": "new",
                "ownerDocumentId": "document",
            },
        )
        assert prepared.status_code == 200
        prepare.assert_called_once_with(
            restore_id,
            mode="project-copy",
            previous_epoch="old",
            target_epoch="new",
            owner_document_id="document",
        )
        assert client_with_files.put(
            f"/api/workspace/restores/{restore_id}/frontend-prepared",
            json={"digest": "d" * 64},
        ).status_code == 200
        staged.assert_called_once_with(restore_id, digest="d" * 64)
        assert client_with_files.post(
            f"/api/workspace/restores/{restore_id}/commit",
            json={"frontendCommitted": True, "frontendDigest": "d" * 64},
        ).status_code == 200
        commit.assert_called_once_with(restore_id, frontend_committed=True, frontend_digest="d" * 64)
        assert client_with_files.post(
            f"/api/workspace/restores/{restore_id}/complete",
            json={"frontendDigest": "d" * 64},
        ).status_code == 200
        complete.assert_called_once_with(restore_id, frontend_digest="d" * 64)
        assert client_with_files.post(f"/api/workspace/restores/{restore_id}/abort").status_code == 200
        assert client_with_files.post(
            f"/api/workspace/restores/{restore_id}/apply",
            json={"mode": "replace-empty"},
        ).status_code == 200
        apply.assert_called_once_with(restore_id, mode="replace-empty")


def test_restore_upload_rejects_empty_multipart_and_digest_mismatch(client: TestClient) -> None:
    missing = client.post(
        "/api/workspace/restores/inspect",
        files={"backup": ("backup.dsibackup", b"data", "application/octet-stream")},
    )
    assert missing.status_code == 400
    empty = client.post(
        "/api/workspace/restores/inspect",
        content=b"",
        headers={"Content-Type": "application/vnd.deepseek-infra.backup+zip"},
    )
    assert empty.status_code == 400
    with patch.object(
        workspace_routes.workspace_backups,
        "inspect_archive",
        return_value={"ok": True, "archiveSha256": "wrong"},
    ):
        mismatch = client.post(
            "/api/workspace/restores/inspect",
            content=b"payload",
            headers={"Content-Type": "application/vnd.deepseek-infra.backup+zip"},
        )
    assert mismatch.status_code == 400


def test_durable_restore_fence_blocks_peer_api_mutations_but_not_reads(tmp_settings: Path) -> None:
    root = workspace_routes.workspace_backups.RESTORE_DIR.parent
    fence = {
        "schemaVersion": 1,
        "restoreId": "restore_route_fence",
        "previousEpoch": "legacy",
        "targetEpoch": "epoch-route",
        "ownerDocumentId": "owner",
        "phase": "preparing",
        "createdAt": 1,
        "expiresAt": 2**63 - 1,
    }
    mutation_gate.write_fence(fence, root)
    try:
        with TestClient(server_module.create_app(), base_url="http://127.0.0.1") as live:
            headers = {"Cookie": f"auth_token={server_module.settings.auth.token}"}
            blocked = live.post("/api/projects", json={"action": "create", "name": "blocked"}, headers=headers)
            assert blocked.status_code == 423
            readable = live.get("/api/workspace/projects", headers=headers)
            assert readable.status_code == 200
    finally:
        mutation_gate.clear_fence("restore_route_fence", root)


def test_project_files_no_files(client: TestClient) -> None:
    resp = client.post("/api/project-files?projectId=p1")
    assert resp.status_code == 400


def test_project_files_with_files(client_with_files: TestClient) -> None:
    with patch.object(workspace_routes, "add_project_files", return_value=[{"fileId": "a" * 32}]):
        resp = client_with_files.post("/api/project-files?projectId=p1")
    assert resp.status_code == 200


def test_workspace_home(client: TestClient) -> None:
    with patch.object(workspace_routes.workspace_home, "workspace_home", return_value={"projects": []}):
        resp = client.get("/api/workspace/home?limit=5")
    assert resp.status_code == 200


def test_workspace_projects_crud(client: TestClient) -> None:
    project_id = "wp1"
    with patch.object(workspace_routes.workspace_projects, "create_project", return_value={"id": project_id, "name": "WP1"}), \
         patch.object(workspace_routes.workspace_projects, "get_project", return_value={"id": project_id}), \
         patch.object(workspace_routes.workspace_projects, "list_projects", return_value=[]), \
         patch.object(workspace_routes.workspace_projects, "rename_project", return_value={"id": project_id, "name": "WP2"}), \
         patch.object(workspace_routes.workspace_projects, "delete_project", return_value=project_id):
        resp = client.post("/api/workspace/projects", json={"name": "WP1", "description": "desc"})
        assert resp.status_code == 200

        resp = client.get(f"/api/workspace/projects/{project_id}")
        assert resp.status_code == 200

        resp = client.get("/api/workspace/projects")
        assert resp.status_code == 200

        resp = client.patch(f"/api/workspace/projects/{project_id}", json={"name": "WP2", "description": "d2"})
        assert resp.status_code == 200

        resp = client.delete(f"/api/workspace/projects/{project_id}")
        assert resp.status_code == 200


def test_workspace_project_skills(client: TestClient) -> None:
    with patch.object(workspace_routes, "project_skill_binding", return_value={"enabledSkills": []}), \
         patch.object(workspace_routes, "set_project_skill_binding", return_value={"enabledSkills": ["s1"]}):
        resp = client.get("/api/workspace/projects/p1/skills")
        assert resp.status_code == 200

        resp = client.patch("/api/workspace/projects/p1/skills", json={"enabledSkills": ["s1"], "defaultSkill": "s1", "enabledPacks": [{"packId": "p1"}], "enabledPackVersions": [{"packId": "p1", "version": "1"}]})
        assert resp.status_code == 200


def test_workspace_project_pack_install(client: TestClient) -> None:
    with patch.object(workspace_routes, "enable_pack_for_project", return_value={"enabledSkills": ["s1"]}):
        resp = client.post("/api/workspace/projects/p1/skill-packs/pack1/install", json={"version": "1.0.0"})
    assert resp.status_code == 200


def test_workspace_project_skill_runs(client: TestClient) -> None:
    with patch.object(workspace_routes, "list_project_skill_runs", return_value=[]):
        resp = client.get("/api/workspace/projects/p1/skill-runs?limit=10")
    assert resp.status_code == 200


def test_workspace_project_skill_analytics(client: TestClient) -> None:
    with patch.object(workspace_routes.skill_analytics, "analytics_summary", return_value={"projectId": "p1"}):
        resp = client.get("/api/workspace/projects/p1/skill-analytics?days=7")
    assert resp.status_code == 200


def test_workspace_project_provenance(client: TestClient) -> None:
    with patch.object(workspace_routes.workspace_provenance, "project_provenance", return_value={"events": []}):
        resp = client.get("/api/workspace/projects/p1/provenance")
    assert resp.status_code == 200


def test_workspace_project_conversations(client: TestClient) -> None:
    with patch.object(workspace_routes.workspace_projects, "list_project_conversations", return_value=[]), \
         patch.object(workspace_routes.workspace_projects, "upsert_project_conversation", return_value={"id": "c1"}):
        resp = client.get("/api/workspace/projects/p1/conversations")
        assert resp.status_code == 200

        resp = client.post("/api/workspace/projects/p1/conversations", json={"title": "T"})
        assert resp.status_code == 200


def test_workspace_saved_items_crud(client: TestClient) -> None:
    with patch.object(workspace_routes.workspace_saved_items, "list_saved_items", return_value=[]), \
         patch.object(workspace_routes.workspace_saved_items, "create_saved_item", return_value={"id": "i1"}), \
         patch.object(workspace_routes.workspace_saved_items, "update_saved_item", return_value={"id": "i1"}), \
         patch.object(workspace_routes.workspace_saved_items, "delete_saved_item", return_value="i1"):
        resp = client.get("/api/workspace/projects/p1/saved-items?type=note&tags=a,b")
        assert resp.status_code == 200

        resp = client.post("/api/workspace/projects/p1/saved-items", json={"type": "note", "title": "T", "content": "C", "sourceRef": {"x": 1}, "tags": ["a"], "purpose": "ref"})
        assert resp.status_code == 200

        resp = client.patch("/api/workspace/projects/p1/saved-items/i1", json={"title": "T2"})
        assert resp.status_code == 200

        resp = client.delete("/api/workspace/projects/p1/saved-items/i1")
        assert resp.status_code == 200


def test_workspace_artifacts_crud(client: TestClient) -> None:
    with patch.object(workspace_routes.workspace_artifacts, "list_artifacts", return_value=[]), \
         patch.object(workspace_routes.workspace_artifacts, "register_artifact", return_value={"artifactId": "a1"}), \
         patch.object(workspace_routes.workspace_artifacts, "add_artifact_version", return_value={"artifactId": "a1"}), \
         patch.object(workspace_routes.workspace_artifacts, "update_artifact", return_value={"artifactId": "a1"}), \
         patch.object(workspace_routes.workspace_artifacts, "delete_artifact", return_value="a1"):
        resp = client.get("/api/workspace/projects/p1/artifacts")
        assert resp.status_code == 200

        resp = client.post("/api/workspace/projects/p1/artifacts", json={"type": "doc", "title": "T", "path": "p", "source": {"x": 1}})
        assert resp.status_code == 200

        resp = client.patch("/api/workspace/projects/p1/artifacts/a1", json={"path": "p2"})
        assert resp.status_code == 200

        resp = client.patch("/api/workspace/projects/p1/artifacts/a1", json={"title": "T2"})
        assert resp.status_code == 200

        resp = client.delete("/api/workspace/projects/p1/artifacts/a1")
        assert resp.status_code == 200


def test_workspace_artifact_preview_and_download(client: TestClient, tmp_settings: Path) -> None:
    with patch.object(workspace_routes.workspace_artifacts, "preview_artifact", return_value={"text": "hi"}):
        resp = client.get("/api/workspace/artifacts/a1/preview?projectId=p1")
    assert resp.status_code == 200

    download_path = tmp_settings / "artifact.txt"
    download_path.write_text("hello", encoding="utf-8")
    artifact = {"artifactId": "a1", "path": str(download_path), "filename": "artifact.txt"}
    with patch.object(workspace_routes.workspace_artifacts, "require_artifact", return_value=artifact), \
         patch.object(workspace_routes.workspace_artifacts, "artifact_path", return_value=download_path), \
         patch.object(workspace_routes.workspace_artifacts, "artifact_filename", return_value="artifact.txt"):
        resp = client.get("/api/workspace/artifacts/a1/download?projectId=p1")
    assert resp.status_code == 200


def test_workspace_artifact_download_missing(client: TestClient) -> None:
    artifact = {"artifactId": "a1", "path": "missing", "filename": "x"}
    with patch.object(workspace_routes.workspace_artifacts, "require_artifact", return_value=artifact), \
         patch.object(workspace_routes.workspace_artifacts, "artifact_path", return_value=Path("missing")):
        resp = client.get("/api/workspace/artifacts/a1/download?projectId=p1")
    assert resp.status_code == 404


def test_workspace_exports_create_and_download(client: TestClient, tmp_settings: Path) -> None:
    with patch.object(workspace_routes.workspace_exports, "create_export", return_value={"exportId": "e1"}):
        resp = client.post("/api/workspace/exports", json={"projectId": "p1"})
    assert resp.status_code == 200

    export_path = tmp_settings / "export.zip"
    export_path.write_bytes(b"zipdata")
    export = {"exportId": "e1", "filename": "export.zip"}
    with patch.object(workspace_routes.workspace_exports, "resolve_export", return_value=export), \
         patch.object(workspace_routes.workspace_exports, "export_path", return_value=export_path):
        resp = client.get("/api/workspace/exports/e1/download?projectId=p1")
    assert resp.status_code == 200


def test_workspace_export_download_missing(client: TestClient) -> None:
    export = {"exportId": "e1", "filename": "x"}
    with patch.object(workspace_routes.workspace_exports, "resolve_export", return_value=export), \
         patch.object(workspace_routes.workspace_exports, "export_path", return_value=Path("missing")):
        resp = client.get("/api/workspace/exports/e1/download?projectId=p1")
    assert resp.status_code == 404
