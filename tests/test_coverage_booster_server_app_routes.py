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

    # 6. Healthz, Readyz, and Metrics
    resp = client.get("/healthz")
    assert resp.status_code == 200

    resp = client.get("/readyz")
    assert resp.status_code == 200

    resp = client.get("/metrics")
    assert resp.status_code == 200

    # 7. Reminders due
    resp = client.post("/api/reminders/due", json={}, headers=headers)
    assert resp.status_code == 200
    assert "reminders" in resp.json()

    # 8. Compress context
    resp = client.post("/api/compress-context", json={"apiKey": "sk-fake", "messages": []}, headers=headers)
    assert resp.status_code == 200

    # 9. original_file_media_type
    assert server.original_file_media_type({"kind": "pdf"}) == "application/pdf"
    assert server.original_file_media_type({"kind": "image", "type": "image/png"}) == "image/png"
    assert server.original_file_media_type({"kind": "md"}) == "text/plain; charset=utf-8"
    assert server.original_file_media_type({"kind": "unknown"}) == "application/octet-stream"

    # 10. redact_sensitive_query
    redacted_url = server.redact_sensitive_query("https://example.com/api?token=secret123&foo=bar")
    assert "%5Bredacted%5D" in redacted_url
    redacted_path = server.redact_sensitive_query("/api/path?token=secret456")
    assert "%5Bredacted%5D" in redacted_path
