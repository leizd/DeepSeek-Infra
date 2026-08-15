"""Targeted test coverage boosters for server FastAPI application routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.web import server


def test_server_app_endpoints(tmp_settings: Path) -> None:
    app = server.create_app()
    client = TestClient(app)
    headers = {
        "Host": "127.0.0.1",
        "Authorization": f"Bearer {settings.auth.token}",
    }

    # 1. Semantic cache endpoints
    resp = client.post("/api/semantic-cache", json={"action": "status"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.post("/api/semantic-cache", json={"action": "clear"}, headers=headers)
    assert resp.status_code == 200

    resp = client.post("/api/semantic-cache", json={"action": "unknown_action"}, headers=headers)
    assert resp.status_code == 400

    # 2. Auth logout endpoint
    resp = client.post("/api/auth/logout", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 3. Share target not found
    resp = client.get("/api/share-target?id=nonexistent", headers=headers)
    assert resp.status_code == 404

    # 4. Download save endpoint
    resp = client.post("/api/download-save", json={"id": "nonexistent"}, headers=headers)
    assert resp.status_code in {200, 400, 404}

    # 5. OPTIONS preflight route
    resp = client.options("/api/test-route", headers={"Origin": "http://localhost:8000"})
    assert resp.status_code == 204
