from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_s5_read_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    route_src = (RUST_GATEWAY_SRC / "backup_restore_read_routes.rs").read_text(
        encoding="utf-8"
    )
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(backup_restore_read_routes::router())" in lib_src
    assert '"/api/workspace/backups/:backup_id"' in route_src
    assert '"/api/workspace/backups/:backup_id/download"' in route_src
    assert '"/api/workspace/restores"' in route_src
    assert '"/api/workspace/restores/:restore_id"' in route_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/workspace/backups/{backup_id}") not in python_only
    assert ("get", "/api/workspace/backups/{backup_id}/download") not in python_only
    assert ("get", "/api/workspace/restores") not in python_only
    assert ("get", "/api/workspace/restores/{restore_id}") not in python_only
    assert ("delete", "/api/workspace/backups/{backup_id}") not in python_only
    assert ("delete", "/api/workspace/restores/{restore_id}") not in python_only


def test_s5_read_python_oracle_responses(tmp_settings: Path) -> None:
    backups_dir = tmp_settings / ".backups"
    staging_dir = tmp_settings / ".restore-staging"
    (backups_dir / "sessions" / "backup_ready1").mkdir(parents=True, exist_ok=True)
    (backups_dir / "sessions" / "backup_pending1").mkdir(parents=True, exist_ok=True)

    zip_path = backups_dir / "workspace.dsibackup"
    zip_path.write_bytes(b"PK\x03\x04zip-bytes")
    age_path = backups_dir / "workspace.dsibackup.age"
    age_path.write_bytes(b"age-bytes")

    (backups_dir / "sessions" / "backup_ready1" / "session.json").write_text(
        json.dumps(
            {
                "backupId": "backup_ready1",
                "phase": "ready",
                "path": str(zip_path),
                "context": {"secret": "omit"},
            }
        ),
        encoding="utf-8",
    )
    (backups_dir / "sessions" / "backup_pending1" / "session.json").write_text(
        json.dumps(
            {
                "backupId": "backup_pending1",
                "phase": "created",
                "path": str(age_path),
            }
        ),
        encoding="utf-8",
    )

    r1 = staging_dir / "restore_aaa111"
    (r1 / "verified" / "frontend").mkdir(parents=True, exist_ok=True)
    (r1 / "verified" / "frontend" / "state.json").write_text(
        '{"tab":"chat"}', encoding="utf-8"
    )
    (r1 / "plan.json").write_text(
        json.dumps(
            {
                "restoreId": "restore_aaa111",
                "encrypted": True,
                "secretArmedAt": 1.0,
                "manifest": {"backupId": "backup_src1"},
            }
        ),
        encoding="utf-8",
    )
    (r1 / "transaction.json").write_text(
        json.dumps(
            {
                "phase": "complete",
                "contributors": [
                    {"id": "memory", "swapped": True},
                    {"id": "rag", "swapped": False},
                ],
                "identityMap": {"old1": "new1"},
                "operations": [{"op": "swap"}],
            }
        ),
        encoding="utf-8",
    )
    (r1 / "remote-fetch.json").write_text('{"phase":"done"}', encoding="utf-8")

    r2 = staging_dir / "restore_bbb222"
    r2.mkdir(parents=True, exist_ok=True)
    (r2 / "remote-fetch.json").write_text(
        json.dumps(
            {
                "restoreId": "restore_bbb222",
                "phase": "fetching",
                "encrypted": False,
            }
        ),
        encoding="utf-8",
    )

    r3 = staging_dir / "restore_ccc333"
    r3.mkdir(parents=True, exist_ok=True)
    (r3 / "plan.json").write_text("{bad json", encoding="utf-8")

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    get_b = client.get("/api/workspace/backups/backup_ready1", headers=headers)
    assert get_b.status_code == 200
    assert get_b.json() == {"ok": True, "backupId": "backup_ready1", "phase": "ready"}

    dl_b = client.get("/api/workspace/backups/backup_ready1/download", headers=headers)
    assert dl_b.status_code == 200
    assert dl_b.content == b"PK\x03\x04zip-bytes"
    assert dl_b.headers["cache-control"] == "no-store"
    assert dl_b.headers["content-type"].startswith(
        "application/vnd.deepseek-infra.backup+zip"
    )
    assert "workspace.dsibackup" in dl_b.headers["content-disposition"]

    pending_dl = client.get(
        "/api/workspace/backups/backup_pending1/download", headers=headers
    )
    assert pending_dl.status_code == 409
    assert pending_dl.json() == {
        "error": "Backup is not ready",
        "code": "invalid_request",
    }

    list_r = client.get("/api/workspace/restores", headers=headers)
    assert list_r.status_code == 200
    assert list_r.json() == {
        "ok": True,
        "restores": [
            {
                "ok": True,
                "phase": "complete",
                "restoreId": "restore_aaa111",
                "secretState": "not-required",
                "applied": ["memory"],
                "restoredIdentities": [
                    {
                        "originalId": "old1",
                        "restoredId": "new1",
                        "reason": "collision",
                        "sourceBackupId": "backup_src1",
                    }
                ],
            },
            {
                "ok": True,
                "restoreId": "restore_bbb222",
                "phase": "fetching",
                "encrypted": False,
                "secretState": "not-required",
                "applied": [],
                "restoredIdentities": [],
            },
            {
                "restoreId": "restore_ccc333",
                "phase": "recovery-required",
                "compatible": False,
            },
        ],
    }

    get_r1 = client.get("/api/workspace/restores/restore_aaa111", headers=headers)
    assert get_r1.status_code == 200
    assert get_r1.json()["frontend"] == {"tab": "chat"}
    assert get_r1.json()["remoteRestorePhase"] == "done"

    get_r2 = client.get("/api/workspace/restores/restore_bbb222", headers=headers)
    assert get_r2.status_code == 200
    assert get_r2.json() == {
        "ok": True,
        "restoreId": "restore_bbb222",
        "phase": "fetching",
        "encrypted": False,
        "secretState": "not-required",
    }

    get_r3 = client.get("/api/workspace/restores/restore_ccc333", headers=headers)
    assert get_r3.status_code == 400
    assert get_r3.json() == {
        "error": "Invalid backup metadata: plan.json",
        "code": "invalid_payload",
    }
