from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.native_runtime.authority import (
    RUST_DATA_DOMAINS,
    PythonWriterMechanicallyDeniedError,
)
from deepseek_infra.infra.workspace import backup_remote_restore, backups
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_s5_write_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    read_src = (RUST_GATEWAY_SRC / "backup_restore_read_routes.rs").read_text(
        encoding="utf-8"
    )
    write_src = (RUST_GATEWAY_SRC / "backup_restore_write_routes.rs").read_text(
        encoding="utf-8"
    )
    archive_src = (RUST_GATEWAY_SRC / "backup_archive_routes.rs").read_text(
        encoding="utf-8"
    )
    tx_src = (RUST_GATEWAY_SRC / "backup_restore_tx_routes.rs").read_text(
        encoding="utf-8"
    )
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(backup_restore_write_routes::router())" in lib_src
    assert ".merge(backup_restore_tx_routes::router())" in lib_src
    assert ".merge(backup_archive_routes::router())" in lib_src
    assert '"restore_staging_store"' in lib_src
    assert '"/api/workspace/backups"' in write_src
    assert '"/api/workspace/backups/:backup_id/secret"' in write_src
    assert '"/api/workspace/backups/:backup_id/frontend-state"' in write_src
    assert '"/api/workspace/backups/:backup_id/finalize"' in archive_src
    assert '"/api/workspace/restores/inspect"' in archive_src
    assert '"/api/workspace/restores/:restore_id/unlock"' in archive_src
    assert '"/api/workspace/restores/:restore_id/prepare"' in tx_src
    assert '"/api/workspace/restores/:restore_id/frontend-prepared"' in tx_src
    assert '"/api/workspace/restores/:restore_id/commit"' in tx_src
    assert '"/api/workspace/restores/:restore_id/complete"' in tx_src
    assert '"/api/workspace/restores/:restore_id/abort"' in tx_src
    assert '"/api/workspace/restores/:restore_id/pause"' in tx_src
    assert '"/api/workspace/restores/:restore_id/resume"' in tx_src
    assert '"/api/workspace/restores/:restore_id/apply"' in tx_src
    assert '"/api/workspace/restores/:restore_id/secret"' in write_src
    assert '"/api/workspace/restores/cleanup"' in write_src
    assert "dispatch_delete_backup" in read_src
    assert "dispatch_delete_restore" in read_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    for method, path in (
        ("post", "/api/workspace/backups"),
        ("put", "/api/workspace/backups/{backup_id}/secret"),
        ("put", "/api/workspace/backups/{backup_id}/frontend-state"),
        ("post", "/api/workspace/backups/{backup_id}/finalize"),
        ("delete", "/api/workspace/backups/{backup_id}"),
        ("post", "/api/workspace/restores/inspect"),
        ("post", "/api/workspace/restores/{restore_id}/unlock"),
        ("post", "/api/workspace/restores/{restore_id}/prepare"),
        ("put", "/api/workspace/restores/{restore_id}/frontend-prepared"),
        ("post", "/api/workspace/restores/{restore_id}/commit"),
        ("post", "/api/workspace/restores/{restore_id}/complete"),
        ("post", "/api/workspace/restores/{restore_id}/abort"),
        ("post", "/api/workspace/restores/{restore_id}/pause"),
        ("post", "/api/workspace/restores/{restore_id}/resume"),
        ("post", "/api/workspace/restores/{restore_id}/apply"),
        ("put", "/api/workspace/restores/{restore_id}/secret"),
        ("delete", "/api/workspace/restores/{restore_id}"),
        ("post", "/api/workspace/restores/cleanup"),
    ):
        assert (method, path) not in python_only


def test_python_restore_staging_store_writers_denied_under_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    assert "restore_staging_store" in RUST_DATA_DOMAINS
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.create_session({"mode": "full", "coveragePolicy": "best-effort"})

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.put_session_secret(
            "backup_missing", {"kind": "passphrase", "secret": "hunter2"}
        )

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.put_frontend_state("backup_missing", {"schemaVersion": 1})

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.delete_backup("backup_missing")

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.delete_restore("restore_missing")

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.cleanup_restores()

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.inspect_archive(b"dummy")

    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backups.unlock_restore("restore_missing")

    for action in (
        lambda: backups.prepare_restore("restore_missing"),
        lambda: backups.frontend_prepared("restore_missing", digest="a" * 64),
        lambda: backups.commit_restore("restore_missing"),
        lambda: backups.complete_restore("restore_missing"),
        lambda: backups.abort_restore("restore_missing"),
    ):
        with pytest.raises(
            PythonWriterMechanicallyDeniedError, match="restore_staging_store"
        ):
            action()

    remote_dir = tmp_settings / ".restore-staging" / "restore_remote"
    remote_dir.mkdir(parents=True, exist_ok=True)
    remote_path = remote_dir / "remote-fetch.json"
    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backup_remote_restore.advance_federated_phase("restore_remote", "committing")
    with pytest.raises(
        PythonWriterMechanicallyDeniedError, match="restore_staging_store"
    ):
        backup_remote_restore._atomic_write_json(remote_path, {"restoreId": "restore_remote"})

    assert not (tmp_settings / ".backups").exists()


def test_s5_write_python_oracle_responses(tmp_settings: Path) -> None:
    (tmp_settings / ".memory").mkdir(parents=True, exist_ok=True)
    (tmp_settings / ".memory" / "memories.json").write_bytes(b'{"items":[]}')

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    created = client.post(
        "/api/workspace/backups",
        json={"mode": "full", "coveragePolicy": "best-effort"},
        headers=headers,
    )
    assert created.status_code == 200
    body = created.json()
    backup_id = body["backupId"]
    assert body["phase"] == "preparing"
    assert body["estimatedBytes"] == 12

    sec = client.put(
        f"/api/workspace/backups/{backup_id}/secret",
        json={"kind": "passphrase", "secret": "hunter2"},
        headers=headers,
    )
    assert sec.status_code == 200
    assert sec.json()["sessionId"] == backup_id
    assert sec.json()["expiresInSeconds"] == 300

    fe_body = {
        "schemaVersion": 1,
        "exportedAt": "2026-11-17T00:00:00Z",
        "items": [],
    }
    digest = hashlib.sha256(
        json.dumps(
            fe_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    fe = client.put(
        f"/api/workspace/backups/{backup_id}/frontend-state",
        json={**fe_body, "digest": digest},
        headers=headers,
    )
    assert fe.status_code == 200
    assert fe.json() == {"ok": True, "backupId": backup_id, "digest": digest}

    deleted_b = client.delete(f"/api/workspace/backups/{backup_id}", headers=headers)
    assert deleted_b.status_code == 200
    assert deleted_b.json() == {"ok": True, "deleted": True}

    old_restore = tmp_settings / ".restore-staging" / "restore_old1"
    old_restore.mkdir(parents=True, exist_ok=True)
    (old_restore / "plan.json").write_text(
        json.dumps(
            {
                "restoreId": "restore_old1",
                "phase": "complete",
                "completedAt": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    cleanup = client.post("/api/workspace/restores/cleanup", headers=headers)
    assert cleanup.status_code == 200
    assert cleanup.json() == {"ok": True, "deleted": ["restore_old1"]}

    cur_restore = tmp_settings / ".restore-staging" / "restore_cur1"
    cur_restore.mkdir(parents=True, exist_ok=True)
    (cur_restore / "plan.json").write_text(
        json.dumps(
            {
                "restoreId": "restore_cur1",
                "phase": "inspected",
                "encrypted": True,
            }
        ),
        encoding="utf-8",
    )
    r_sec = client.put(
        "/api/workspace/restores/restore_cur1/secret",
        json={"kind": "passphrase", "secret": "hunter2"},
        headers=headers,
    )
    assert r_sec.status_code == 200
    assert r_sec.json()["sessionId"] == "restore_cur1"

    del_r = client.delete("/api/workspace/restores/restore_cur1", headers=headers)
    assert del_r.status_code == 200
    assert del_r.json() == {"ok": True, "deleted": True}


def test_s5_archive_finalize_inspect_unlock_oracle(tmp_settings: Path) -> None:
    proj_dir = tmp_settings / ".projects" / "p1"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "project.json").write_text('{"id":"p1","name":"Oracle"}', encoding="utf-8")

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    created = client.post(
        "/api/workspace/backups",
        json={"mode": "full", "requiresFrontendState": False, "coveragePolicy": "best-effort"},
        headers=headers,
    )
    assert created.status_code == 200
    backup_id = created.json()["backupId"]

    finalized = client.post(
        f"/api/workspace/backups/{backup_id}/finalize",
        json={},
        headers=headers,
    )
    assert finalized.status_code == 200
    fin = finalized.json()
    assert fin["ok"] is True
    assert fin["phase"] == "ready"
    assert fin["filename"].endswith(".dsibackup")
    assert fin["protection"] == {"mode": "none"}

    # Idempotent finalize.
    second = client.post(
        f"/api/workspace/backups/{backup_id}/finalize",
        json={},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["phase"] == "ready"

    dl = client.get(f"/api/workspace/backups/{backup_id}/download", headers=headers)
    assert dl.status_code == 200

    inspected = client.post(
        "/api/workspace/restores/inspect",
        content=dl.content,
        headers={**headers, "Content-Type": "application/vnd.deepseek-infra.backup+zip"},
    )
    assert inspected.status_code == 200
    plan = inspected.json()
    assert plan["ok"] is True
    assert plan["phase"] == "inspected"
    assert plan["encrypted"] is False
    assert plan["compatible"] is True
    restore_id = plan["restoreId"]

    unlocked = client.post(f"/api/workspace/restores/{restore_id}/unlock", headers=headers)
    assert unlocked.status_code == 200
    assert unlocked.json()["phase"] == "inspected"

    # Non-existent restore unlock returns 404.
    missing = client.post("/api/workspace/restores/restore_missing/unlock", headers=headers)
    assert missing.status_code == 404
