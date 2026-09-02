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
    _write_session(restore_id, phase="fetching-controls")
    root = backups.RESTORE_DIR / restore_id
    (root / "control.age").write_bytes(b"ciphertext-control")
    (root / "payload.age").write_bytes(b"ciphertext-payload")
    config.PROJECTS_DIR.mkdir(parents=True)
    (config.PROJECTS_DIR / "live.json").write_bytes(b'{"live":true}')
    before = _workspace_bytes()
    calls: list[str] = []
    workspace_digests: list[str] = []

    def preflight(value: str, *, client: Any = None) -> dict[str, Any]:
        del client
        calls.append(f"preflight:{value}")
        return {"ready": True, "phase": "preflighted"}

    def fetch(value: str, *, client: Any = None) -> dict[str, Any]:
        del client
        calls.append(f"fetch:{value}")
        phase = "controls-fetched" if sum(call.startswith("fetch:") for call in calls) == 1 else "components-fetched"
        return {"restoreId": value, "phase": phase}

    def materialize(value: str, *, kind: str, secret: bytearray, client: Any = None, _drill: bool = False) -> dict[str, Any]:
        del client, kind, secret
        assert _drill is True
        calls.append(f"materialize:{value}")
        tree = root / "extracted"
        (tree / "payload" / "projects").mkdir(parents=True)
        (tree / "payload" / "projects" / "restored.json").write_text('{"restored":true}', encoding="utf-8")
        workspace_digests.append("sha256:" + backups._tree_digest(tree))
        (root / "control-decrypted-0.zip").write_bytes(b"plaintext-control")
        (root / "metadata-0").mkdir()
        (root / "metadata-0" / "manifest.json").write_bytes(b"plaintext-metadata")
        return {
            "restoreId": value,
            "phase": "materialized",
            "tree": str(tree),
            "manifest": {
                "source": {"revision": "source-revision-a"},
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
    assert result["workspaceDigest"] == workspace_digests[0]
    assert result["sourceRevision"] == "source-revision-a"
    assert result["cleanupCompleted"] is True
    assert calls == [
        f"fetch:{restore_id}",
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
        json={"restoreId": "restore_drillroute", "secret": "forbidden", "root": "C:/live"},  # pragma: allowlist secret
    )

    assert created.status_code == fetched.status_code == 200
    assert created.json() == fetched.json() == result
    assert calls == ["restore_drillroute"]
    assert invalid.status_code == 400
    assert auth.call_count == 3


def test_recovery_drill_metadata_and_phase_guards_fail_closed(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for invalid in ("bad", "restore_bad-id", "../restore_escape"):
        with pytest.raises(AppError, match="Invalid restore id"):
            backup_recovery_drill.get_recovery_drill(invalid)
    with pytest.raises(AppError) as missing:
        backup_recovery_drill.get_recovery_drill("restore_missing")
    assert missing.value.status == 404

    restore_id = "restore_guarded"
    session = _write_session(restore_id, phase="prepared")
    root = backups.RESTORE_DIR / restore_id
    backup_crypto.put_secret(restore_id, "passphrase", "secret")
    with pytest.raises(AppError, match="pre-commit") as disallowed:
        backup_recovery_drill.run_recovery_drill(restore_id)
    assert disallowed.value.status == 409

    session["phase"] = "controls-fetched"
    backup_remote_restore._atomic_write_json(root / "remote-fetch.json", session)
    backup_crypto.clear_secret(restore_id)
    with pytest.raises(AppError, match="unlock secret"):
        backup_recovery_drill.run_recovery_drill(restore_id)

    (root / backup_recovery_drill.RESULT_NAME).write_text("[]", encoding="utf-8")
    with pytest.raises(AppError, match="metadata"):
        backup_recovery_drill.get_recovery_drill(restore_id)
    (root / backup_recovery_drill.RESULT_NAME).write_text("not-json", encoding="utf-8")
    with pytest.raises(AppError, match="metadata"):
        backup_recovery_drill.get_recovery_drill(restore_id)


def test_recovery_drill_work_and_projected_inspection_are_bounded(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore_projected"
    _write_session(restore_id)
    root = backups.RESTORE_DIR / restore_id
    tree = root / "extracted"
    tree.mkdir()
    session = {
        "chain": [
            {"expectedBytes": 5, "requiredComponents": [None, {"expectedBytes": -1}, {"expectedBytes": 7}]},
            "bad",
        ]
    }
    monkeypatch.setattr(backup_remote_restore, "restore_members", lambda _session: [session["chain"][0], {}])
    work = backup_recovery_drill._work(
        session,
        {"manifest": {"files": [None, {"size": -1}, {"size": 9}], "contributors": [None, {"id": "projects"}]}},
        {"operations": []},
    )
    assert work == {"chainLength": 2, "components": 3, "ciphertextBytes": 12, "logicalBytes": 9, "verifiedContributors": 1}

    inspected: list[str] = []

    def inspect_projected(value: str, *_args: Any, **_kwargs: Any) -> dict[str, bool]:
        inspected.append(value)
        return {"compatible": True}

    monkeypatch.setattr(
        backups,
        "inspect_projected_restore_tree",
        inspect_projected,
    )
    assert backup_recovery_drill._inspect_materialized(
        restore_id,
        {"tree": str(tree), "manifest": {}, "projection": {}},
        kind="identity",
    ) == {"compatible": True}
    assert inspected == [restore_id]
    with pytest.raises(AppError, match="outside"):
        backup_recovery_drill._inspect_materialized(
            restore_id,
            {"tree": str(root.parent), "manifest": {}},
            kind="passphrase",
        )


def test_recovery_drill_rejects_unverified_fetch_and_incompatible_tree(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix, fetch_phase, compatible in (("fetch", "fetching", True), ("compat", "components-fetched", False)):
        restore_id = f"restore_{suffix}failure"
        _write_session(restore_id)
        root = backups.RESTORE_DIR / restore_id
        tree = root / "extracted"
        backup_crypto.put_secret(restore_id, "passphrase", "secret")
        monkeypatch.setattr(backup_remote_restore, "preflight_restore_session", lambda *_args, **_kwargs: {"ready": True})
        monkeypatch.setattr(
            backup_remote_restore,
            "fetch_restore_session",
            lambda *_args, phase=fetch_phase, **_kwargs: {"phase": phase},
        )
        monkeypatch.setattr(
            backup_remote_restore,
            "materialize_restore_session",
            lambda *_args, **_kwargs: _materialized_tree(tree),
        )
        monkeypatch.setattr(backups, "inspect_verified_restore_tree", lambda *_args, value=compatible, **_kwargs: {"compatible": value})
        monkeypatch.setattr(backup_remote_restore, "_release_session_holds", lambda _session: None)
        with pytest.raises(AppError, match="Recovery Drill failed"):
            backup_recovery_drill.run_recovery_drill(restore_id)
        result = backup_recovery_drill.get_recovery_drill(restore_id)
        assert result["result"] == "failed"
        assert result["failureCode"] == "drill-validation-failed"


def test_recovery_drill_cleanup_failure_overrides_success(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore_cleanupfailure"
    _write_session(restore_id)
    root = backups.RESTORE_DIR / restore_id
    tree = root / "extracted"
    backup_crypto.put_secret(restore_id, "passphrase", "secret")
    monkeypatch.setattr(backup_remote_restore, "preflight_restore_session", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(backup_remote_restore, "fetch_restore_session", lambda *_args, **_kwargs: {"phase": "components-fetched"})

    def materialize(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        tree.mkdir()
        (tree / "plain.txt").write_text("plain", encoding="utf-8")
        return {"tree": str(tree), "manifest": {}}

    monkeypatch.setattr(backup_remote_restore, "materialize_restore_session", materialize)
    monkeypatch.setattr(backups, "inspect_verified_restore_tree", lambda *_args, **_kwargs: {"compatible": True})
    monkeypatch.setattr(backup_recovery_drill, "_scrub_plaintext", lambda _root: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(backup_remote_restore, "_release_session_holds", lambda _session: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(AppError, match="Recovery Drill failed"):
        backup_recovery_drill.run_recovery_drill(restore_id)
    result = backup_recovery_drill.get_recovery_drill(restore_id)
    assert result["failureCode"] == "drill-cleanup-failed"
    assert result["cleanupCompleted"] is False
    assert backup_recovery_drill._plaintext_remains(root) is True


def test_recovery_drill_helpers_detect_each_plaintext_shape_and_missing_session(tmp_settings: Path) -> None:
    missing_root = backups.RESTORE_DIR / "restore_absent"
    missing_root.mkdir(parents=True)
    with pytest.raises(AppError, match="session not found"):
        backup_recovery_drill._claim(missing_root, "restore_absent")

    scratch = tmp_settings / "drill-scratch"
    scratch.mkdir()
    backup_recovery_drill._scrub_directory(scratch / "missing")

    plan = scratch / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    assert backup_recovery_drill._plaintext_remains(scratch) is True
    plan.unlink()

    archive = scratch / "control-decrypted-a.zip"
    archive.write_bytes(b"plain")
    assert backup_recovery_drill._plaintext_remains(scratch) is True
    archive.unlink()

    extracted = scratch / "extracted"
    extracted.mkdir()
    (extracted / "plain.txt").write_text("plain", encoding="utf-8")
    assert backup_recovery_drill._plaintext_remains(scratch) is True
    backup_recovery_drill._scrub_directory(extracted)
    assert not extracted.exists()


def test_recovery_drill_cleanup_failure_preserves_original_failure(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore_doublefailure"
    _write_session(restore_id)
    backup_crypto.put_secret(restore_id, "passphrase", restore_id)
    monkeypatch.setattr(backup_remote_restore, "preflight_restore_session", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(
        backup_remote_restore,
        "fetch_restore_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fetch failed")),
    )
    monkeypatch.setattr(backup_recovery_drill, "_scrub_plaintext", lambda _root: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(backup_remote_restore, "_release_session_holds", lambda _session: None)

    with pytest.raises(AppError, match="Recovery Drill failed"):
        backup_recovery_drill.run_recovery_drill(restore_id)
    assert backup_recovery_drill.get_recovery_drill(restore_id)["failureCode"] == "drill-cleanup-failed"


def _materialized_tree(tree: Path) -> dict[str, Any]:
    tree.mkdir(exist_ok=True)
    return {"tree": str(tree), "manifest": {}}
