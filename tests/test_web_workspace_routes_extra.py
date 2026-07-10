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
import deepseek_infra.web.routes.workspace as workspace_routes
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
