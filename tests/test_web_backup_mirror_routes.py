"""Tests for the sealed frontend backup mirror routes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backup_policies
import deepseek_infra.web.routes.workspace as workspace_routes
from deepseek_infra.web.routes.workspace import WorkspaceRouteDeps, create_workspace_router


async def _read_multipart_files(_request: Any) -> tuple[list[dict[str, Any]], bool, str]:
    return [], False, ""


def _app_with_router(router: Any) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(router)
    return app


@pytest.fixture
def client(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(b"age:" + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        target.write_bytes(source.read_bytes()[4:][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1X", "recipient": "age1x"})
    deps = WorkspaceRouteDeps(read_multipart_files=_read_multipart_files)
    app = _app_with_router(create_workspace_router(deps))
    with patch.object(workspace_routes, "require_api_auth", lambda request: None):
        yield TestClient(app)


def _envelope() -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemaVersion": 1,
        "sourceVersion": "4.4.4",
        "createdAt": 1,
        "conversations": [],
        "conflicts": [],
    }
    body["digest"] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return body


def test_mirror_put_get_and_list(client: TestClient) -> None:
    backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": "nightly",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
            "targetId": "managed-local",
        }
    )
    resp = client.put(
        "/api/workspace/backup-mirrors/mirror_main/frontend",
        json={"sourceEpoch": "epoch-1", "acknowledgedAt": "2026-01-01T00:00:00Z", "envelope": _envelope()},
    )
    assert resp.status_code == 200, resp.text
    metadata = resp.json()
    assert metadata["profileId"] == "mirror_main"
    assert metadata["creationVerified"] is True

    status = client.get("/api/workspace/backup-mirrors/mirror_main")
    assert status.status_code == 200
    assert status.json()["status"] == "current"

    listing = client.get("/api/workspace/backup-mirrors")
    assert listing.status_code == 200
    assert [item["profileId"] for item in listing.json()["mirrors"]] == ["mirror_main"]


def test_mirror_put_rejects_bad_envelope(client: TestClient) -> None:
    envelope = _envelope()
    envelope["digest"] = "0" * 64
    resp = client.put("/api/workspace/backup-mirrors/mirror_main/frontend", json={"sourceEpoch": "epoch-1", "envelope": envelope})
    assert resp.status_code == 400
    missing_epoch = client.put("/api/workspace/backup-mirrors/mirror_main/frontend", json={"envelope": _envelope()})
    assert missing_epoch.status_code == 400


def test_mirror_status_missing(client: TestClient) -> None:
    resp = client.get("/api/workspace/backup-mirrors/mirror_missing")
    assert resp.status_code == 200
    assert resp.json()["status"] == "missing"
