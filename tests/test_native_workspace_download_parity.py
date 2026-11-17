from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.workspace import artifacts, exports
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_workspace_download_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    art_src = (RUST_GATEWAY_SRC / "artifact_preview_routes.rs").read_text(encoding="utf-8")
    exp_src = (RUST_GATEWAY_SRC / "workspace_export_routes.rs").read_text(encoding="utf-8")
    prov_src = (RUST_GATEWAY_SRC / "workspace_provenance_routes.rs").read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(artifact_preview_routes::router())" in lib_src
    assert ".merge(workspace_export_routes::router())" in lib_src
    assert ".merge(workspace_provenance_routes::router())" in lib_src
    assert '"/api/workspace/artifacts/:artifact_id/download"' in art_src
    assert '"/api/workspace/exports/:export_id/download"' in exp_src
    assert '"/api/workspace/projects/:project_id/provenance"' in prov_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/workspace/artifacts/{artifact_id}/download") not in python_only
    assert ("get", "/api/workspace/exports/{export_id}/download") not in python_only
    assert ("get", "/api/workspace/projects/{project_id}/provenance") not in python_only


def test_workspace_download_python_oracle_headers_and_content(tmp_settings: Path) -> None:
    project = projects.create_project("Download Parity Project")
    generated = tmp_settings / ".generated"
    generated.mkdir(parents=True, exist_ok=True)
    report = generated / "parity.md"
    report.write_bytes(b"# Parity Artifact\n")
    art = artifacts.register_artifact(
        project["id"],
        artifact_type="markdown",
        title="My Report",
        path=".generated/parity.md",
    )
    exported = exports.create_export(
        {"kind": "project", "projectId": project["id"], "format": "json"}
    )
    exp = exported["export"]

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    art_resp = client.get(
        f"/api/workspace/artifacts/{art['artifactId']}/download?projectId={project['id']}",
        headers=headers,
    )
    assert art_resp.status_code == 200
    assert art_resp.content == b"# Parity Artifact\n"
    assert art_resp.headers["cache-control"] == "no-store"
    assert "My-Report.md" in art_resp.headers["content-disposition"]

    exp_resp = client.get(
        f"/api/workspace/exports/{exp['exportId']}/download?projectId={project['id']}",
        headers=headers,
    )
    assert exp_resp.status_code == 200
    assert exp_resp.headers["cache-control"] == "no-store"
    assert exp_resp.headers["content-type"].startswith("application/json")
    assert exp["filename"] in exp_resp.headers["content-disposition"]

    prov_resp = client.get(
        f"/api/workspace/projects/{project['id']}/provenance",
        headers=headers,
    )
    assert prov_resp.status_code == 200
    prov = prov_resp.json()
    assert prov["ok"] is True
    assert prov["projectId"] == project["id"]
    assert prov["summary"]["types"]["project"] == 1
    assert prov["summary"]["types"]["artifact"] == 1
    assert prov["summary"]["types"]["export"] == 1
