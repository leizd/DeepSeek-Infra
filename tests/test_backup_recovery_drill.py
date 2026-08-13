from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_crypto, backup_recovery_drill, backup_remote_restore, backups
import deepseek_infra.web.routes.backup_governance as governance
from deepseek_infra.web.routes.backup_governance import create_backup_governance_router


def _write_session(restore_id: str, *, phase: str = "controls-fetched") -> dict[str, Any]:
    root = backups.RESTORE_DIR / restore_id
    root.mkdir(parents=True)
    session = {
        "schemaVersion": 4,
        "restoreId": restore_id,
        "targetId": "target-a",
        "backupId": "backup-a",
        "phase": phase,
        "storageProtocol": "object-set-v1",
        "chain": [
            {
                "backupId": "backup-a",
                "control": {"expectedBytes": 10, "downloadedBytes": 10, "ciphertextPath": str(root / "control.age")},
                "requiredComponents": [{"expectedBytes": 20, "downloadedBytes": 20, "ciphertextPath": str(root / "payload.age")}],
            }
        ],
    }
    backup_remote_restore._atomic_write_json(root / "remote-fetch.json", session)
    return session


def _workspace_bytes() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for root in (config.PROJECTS_DIR, config.MEMORY_DIR, config.FILE_CACHE_DIR):
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    result[f"{root.name}/{path.relative_to(root)}"] = path.read_bytes()
    return result


def test_recovery_drill_reuses_production_path_scrubs_plaintext_and_preserves_live_workspace(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore_id = "restore_drillsuccess"
    _write_session(restore_id)
    root = backups.RESTORE_DIR / restore_id
    (root / "control.age").write_bytes(b"ciphertext-control")
    (root / "payload.age").write_bytes(b"ciphertext-payload")
    config.PROJECTS_DIR.mkdir(parents=True)
    (config.PROJECTS_DIR / "live.json").write_bytes(b'{"live":true}')
    before = _workspace_bytes()
    calls: list[str] = []

    def preflight(value: str, *, client: Any = None) -> dict[str, Any]:
        del client
        calls.append(f"preflight:{value}")
        return {"ready": True, "phase": "preflighted"}

    def fetch(value: str, *, client: Any = None) -> dict[str, Any]:
        del client
        calls.append(f"fetch:{value}")
        return {"restoreId": value, "phase": "components-fetched"}

    def materialize(value: str, *, kind: str, secret: bytearray, client: Any = None, _drill: bool = False) -> dict[str, Any]:
        del client, kind, secret
        assert _drill is True
        calls.append(f"materialize:{value}")
        tree = root / "extracted"
        (tree / "payload" / "projects").mkdir(parents=True)
        (tree / "payload" / "projects" / "restored.json").write_text('{"restored":true}', encoding="utf-8")
        (root / "control-decrypted-0.zip").write_bytes(b"plaintext-control")
        (root / "metadata-0").mkdir()
        (root / "metadata-0" / "manifest.json").write_bytes(b"plaintext-metadata")
        return {
            "restoreId": value,
            "phase": "materialized",
            "tree": str(tree),
            "manifest": {
                "files": [{"size": 17}],
                "contributors": [{"id": "projects"}],
            },
        }

    def inspect(value: str, tree: Path, *, protection: str, ciphertext_sha256: str | None = None) -> dict[str, Any]:
        del protection, ciphertext_sha256
        calls.append(f"inspect:{value}")
        assert tree == root / "extracted"
        (root / "plan.json").write_text('{"phase":"inspected"}', encoding="utf-8")
        return {"compatible": True, "operations": [{"files": 1}], "phase": "inspected"}

    released: list[str] = []
    monkeypatch.setattr(backup_remote_restore, "preflight_restore_session", preflight)
    monkeypatch.setattr(backup_remote_restore, "fetch_restore_session", fetch)
    monkeypatch.setattr(backup_remote_restore, "materialize_restore_session", materialize)
    monkeypatch.setattr(backups, "inspect_verified_restore_tree", inspect)
    monkeypatch.setattr(backups, "prepare_restore", lambda *_args, **_kwargs: pytest.fail("Drill must not prepare live restore"))
    monkeypatch.setattr(backup_remote_restore, "_release_session_holds", lambda value: released.append(str(value["restoreId"])))
    backup_crypto.put_secret(restore_id, "passphrase", "drill-secret")

    result = backup_recovery_drill.run_recovery_drill(restore_id)

    assert result["result"] == "success"
    assert result["chainLength"] == 1
    assert result["components"] == 2
    assert result["ciphertextBytes"] == 30
    assert result["logicalBytes"] == 17
    assert result["verifiedContributors"] == 1
    assert calls == [
        f"preflight:{restore_id}",
        f"fetch:{restore_id}",
        f"materialize:{restore_id}",
        f"inspect:{restore_id}",
    ]
    assert released == [restore_id]
    assert _workspace_bytes() == before
    assert not backup_crypto.has_secret(restore_id)
    assert not (root / "plan.json").exists()
    assert not (root / "extracted").exists()
    assert not (root / "metadata-0").exists()
    assert not (root / "control-decrypted-0.zip").exists()
    assert (root / "control.age").is_file()
    assert (root / "payload.age").is_file()
    persisted = json.loads((root / "drill-result.json").read_text(encoding="utf-8"))
    assert persisted == result
    assert backup_remote_restore.read_restore_session(restore_id)["drillOnly"] is True  # type: ignore[index]
    assert backup_recovery_drill.run_recovery_drill(restore_id) == result


def test_recovery_drill_failure_is_redacted_and_still_cleans_up(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore_drillfailure"
    _write_session(restore_id, phase="controls-fetched")
    root = backups.RESTORE_DIR / restore_id
    (root / "extracted").mkdir()
    (root / "extracted" / "secret.txt").write_text("private", encoding="utf-8")
    config.MEMORY_DIR.mkdir(parents=True)
    (config.MEMORY_DIR / "live.json").write_bytes(b'{"must":"remain"}')
    before = _workspace_bytes()
    backup_crypto.put_secret(restore_id, "passphrase", "secret-value")
    released: list[str] = []
    monkeypatch.setattr(backup_remote_restore, "preflight_restore_session", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(backup_remote_restore, "fetch_restore_session", lambda *_args, **_kwargs: {"phase": "components-fetched"})
    monkeypatch.setattr(
        backup_remote_restore,
        "materialize_restore_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("secret-value C:/private/project digest=" + "a" * 64)),
    )
    monkeypatch.setattr(backup_remote_restore, "_release_session_holds", lambda value: released.append(str(value["restoreId"])))

    with pytest.raises(AppError, match="Recovery Drill failed"):
        backup_recovery_drill.run_recovery_drill(restore_id)

    persisted = json.loads((root / "drill-result.json").read_text(encoding="utf-8"))
    assert persisted["result"] == "failed"
    assert persisted["failureCode"] == "drill-validation-failed"
    assert (persisted["chainLength"], persisted["components"], persisted["ciphertextBytes"]) == (1, 2, 30)
    serialized = json.dumps(persisted)
    for forbidden in ("secret-value", "private/project", "a" * 64):
        assert forbidden not in serialized
    assert not (root / "extracted").exists()
    assert not backup_crypto.has_secret(restore_id)
    assert released == [restore_id]
    assert _workspace_bytes() == before


def test_recovery_drill_reports_running_and_rejects_a_concurrent_run(tmp_settings: Path) -> None:
    restore_id = "restore_drillrunning"
    _write_session(restore_id)
    backup_crypto.put_secret(restore_id, "passphrase", "drill-secret")
    root = backups.RESTORE_DIR / restore_id

    with backup_recovery_drill._exclusive_drill_lock(root):
        assert backup_recovery_drill._claim(root, restore_id) is None
        assert backup_recovery_drill.get_recovery_drill(restore_id)["result"] == "running"
        with pytest.raises(AppError, match="Recovery Drill is already running") as captured:
            backup_recovery_drill.run_recovery_drill(restore_id)

    assert captured.value.status == 409
    backup_crypto.clear_secret(restore_id)


def test_recovery_drill_routes_are_authenticated_and_server_owned(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = {"schemaVersion": 1, "restoreId": "restore_drillroute", "result": "success"}
    calls: list[str] = []
    auth = Mock()
    monkeypatch.setattr(governance, "require_api_auth", auth)
    def run_drill(restore_id: str) -> dict[str, Any]:
        calls.append(restore_id)
        return result

    monkeypatch.setattr(governance.backup_recovery_drill, "run_recovery_drill", run_drill)
    monkeypatch.setattr(governance.backup_recovery_drill, "get_recovery_drill", lambda _restore_id: result)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.to_response(), status_code=exc.status)

    app.include_router(create_backup_governance_router())
    client = TestClient(app)

    created = client.post("/api/workspace/disaster-recovery/drills", json={"restoreId": "restore_drillroute"})
    fetched = client.get("/api/workspace/disaster-recovery/drills/restore_drillroute")
    invalid = client.post(
        "/api/workspace/disaster-recovery/drills",
        json={"restoreId": "restore_drillroute", "secret": "forbidden", "root": "C:/live"},
    )

    assert created.status_code == fetched.status_code == 200
    assert created.json() == fetched.json() == result
    assert calls == ["restore_drillroute"]
    assert invalid.status_code == 400
    assert auth.call_count == 3
