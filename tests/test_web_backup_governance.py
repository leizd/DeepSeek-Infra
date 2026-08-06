"""Tests for the scheduled backup governance API."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_catalog, backup_crypto, backups
import deepseek_infra.web.routes.backup_governance as governance
from deepseek_infra.web.routes.backup_governance import create_backup_governance_router


RECIPIENT_A = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


def _app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_backup_governance_router())
    return app


@pytest.fixture
def client(tmp_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    prefix = b"age-encryption.org/v1\n"

    def encrypt_stream(target: Path, write_plaintext: object, *, mode: str, secret: object = None, recipients: tuple[str, ...] = (), cancel_event: object = None) -> None:
        import io

        buffer = io.BytesIO()
        write_plaintext(buffer)  # type: ignore[operator]
        target.write_bytes(prefix + bytes(buffer.getbuffer())[::-1])

    def decrypt_file(source: Path, target: Path, *, kind: str, secret: bytearray, cancel_event: object = None) -> None:
        raw = source.read_bytes()
        assert raw.startswith(prefix)
        target.write_bytes(raw[len(prefix):][::-1])

    monkeypatch.setattr(backup_crypto, "encrypt_stream", encrypt_stream)
    monkeypatch.setattr(backup_crypto, "decrypt_file", decrypt_file)
    monkeypatch.setattr(backup_crypto, "generate_identity", lambda: {"identity": "AGE-SECRET-KEY-1EPH", "recipient": "age1eph"})
    monkeypatch.setattr(backup_crypto, "inspect_header", lambda _path: {"age": True})
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))
    config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('{"items":[]}', encoding="utf-8")
    with patch.object(governance, "require_api_auth", lambda request: None):
        yield TestClient(_app())


def _policy_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "name": "nightly",
        "enabled": True,
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
        "frontendMirror": {"mode": "excluded"},
        "protection": {"mode": "age-recipient", "recipients": [RECIPIENT_A]},
        "targetId": "managed-local",
    }
    payload.update(overrides)
    return payload


def test_policy_crud_and_run_flow(client: TestClient) -> None:
    created = client.post("/api/workspace/backup-policies", json=_policy_payload())
    assert created.status_code == 200, created.text
    policy = created.json()
    policy_id = policy["policyId"]

    listing = client.get("/api/workspace/backup-policies")
    assert listing.status_code == 200
    assert any(item["policyId"] == policy_id for item in listing.json()["policies"])
    assert listing.json()["nextRuns"][policy_id]["timezone"] == "UTC"

    patched = client.patch(f"/api/workspace/backup-policies/{policy_id}", json={"name": "renamed", "enabled": False})
    assert patched.status_code == 200
    assert patched.json()["name"] == "renamed"

    outcome = client.post(f"/api/workspace/backup-policies/{policy_id}/run")
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["phase"] == "complete"
    backup_id = outcome.json()["backupId"]

    runs = client.get("/api/workspace/backup-runs", params={"policyId": policy_id})
    assert runs.status_code == 200
    assert runs.json()["runs"][0]["phase"] == "complete"

    catalog = client.get("/api/workspace/backup-catalog")
    assert catalog.status_code == 200
    body = catalog.json()
    assert body["chainValid"] is True
    assert [item["backupId"] for item in body["backups"]] == [backup_id]
    assert body["integrity"] == {"orphans": [], "missing": []}

    pinned = client.post(f"/api/workspace/backup-catalog/{backup_id}/pin")
    assert pinned.status_code == 200
    assert backup_catalog.catalog_state(backups.BACKUP_DIR)[backup_id]["pinned"] is True
    client.delete(f"/api/workspace/backup-catalog/{backup_id}/pin")

    scrubbed = client.post(f"/api/workspace/backups/{backup_id}/scrub")
    assert scrubbed.status_code == 200
    assert scrubbed.json()["ok"] is True

    bad_unlock = client.post(f"/api/workspace/backups/{backup_id}/verify-unlock", json={"identity": "wrong"})
    assert bad_unlock.status_code == 400
    unlocked = client.post(f"/api/workspace/backups/{backup_id}/verify-unlock", json={"identity": "AGE-SECRET-KEY-1USER"})
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["ok"] is True

    preview = client.post("/api/workspace/retention/preview", json={"policyId": policy_id})
    assert preview.status_code == 200
    assert "keep" in preview.json()
    applied = client.post("/api/workspace/retention/apply", json={"policyId": policy_id})
    assert applied.status_code == 200

    deleted = client.delete(f"/api/workspace/backup-policies/{policy_id}")
    assert deleted.status_code == 200


def test_policy_validation_errors(client: TestClient) -> None:
    bad = client.post("/api/workspace/backup-policies", json=_policy_payload(protection={"mode": "passphrase"}))
    assert bad.status_code == 400
    missing = client.get("/api/workspace/backup-catalog", params={"targetId": "target_missing"})
    assert missing.status_code in {404, 409}
    missing_preview = client.post("/api/workspace/retention/preview", json={"policyId": "policy_missing"})
    assert missing_preview.status_code == 404


def test_target_lifecycle(client: TestClient, tmp_path: Path) -> None:
    directory = tmp_path / "usb"
    directory.mkdir()
    created = client.post("/api/workspace/backup-targets", json={"path": str(directory), "label": "USB"})
    assert created.status_code == 200, created.text
    target_id = created.json()["targetId"]

    listing = client.get("/api/workspace/backup-targets")
    assert any(item["targetId"] == target_id for item in listing.json()["targets"])

    probe = client.post(f"/api/workspace/backup-targets/{target_id}/probe")
    assert probe.status_code == 200
    assert probe.json()["ready"] is True

    unsafe = client.post("/api/workspace/backup-targets", json={"path": str(config.ROOT)})
    assert unsafe.status_code == 400

    deleted = client.delete(f"/api/workspace/backup-targets/{target_id}")
    assert deleted.status_code == 200
    gone = client.post(f"/api/workspace/backup-targets/{target_id}/probe")
    assert gone.json()["ready"] is False


def test_scrub_missing_backup(client: TestClient) -> None:
    resp = client.post("/api/workspace/backups/backup_missing/scrub")
    assert resp.status_code == 404
