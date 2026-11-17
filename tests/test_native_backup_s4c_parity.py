from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.native_runtime.authority import (
    GO_CONTROL_DOMAINS,
    PythonWriterMechanicallyDeniedError,
    assert_python_writer_allowed,
)
from deepseek_infra.infra.workspace import backup_targets

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_TARGET_ROUTES = REPO_ROOT / "rust/crates/deepseek-gateway/src/backup_target_routes.rs"
RUST_GATEWAY_LIB = REPO_ROOT / "rust/crates/deepseek-gateway/src/lib.rs"
GO_PUBLIC = REPO_ROOT / "go/internal/api/public.go"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def test_python_target_domain_fenced_under_go_authoritative_and_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for domain in ("target", "target_registry"):
        assert domain in GO_CONTROL_DOMAINS
        for mode in ("go_authoritative", "python_disabled"):
            monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
            with pytest.raises(PythonWriterMechanicallyDeniedError):
                assert_python_writer_allowed(domain)
        monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
        assert_python_writer_allowed(domain)


def test_s4c_target_routes_registered_and_removed_from_python_only_inventory() -> None:
    rust_src = RUST_TARGET_ROUTES.read_text(encoding="utf-8")
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    go_public_src = GO_PUBLIC.read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(backup_target_routes::router())" in lib_src
    for route_path in (
        "/api/workspace/backup-targets",
        "/api/workspace/backup-targets/register-new",
        "/api/workspace/backup-targets/:target_id",
        "/api/workspace/backup-targets/:target_id/adopt",
        "/api/workspace/backup-targets/:target_id/activate",
        "/api/workspace/backup-targets/:target_id/probe",
    ):
        assert f'"{route_path}"' in rust_src

    assert "backupTargetsCreate" in go_public_src
    assert "backupTargetsList" in go_public_src
    assert "backupTargetsDispatch" in go_public_src

    python_only_targets = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
        if str(item.get("path", "")).startswith("/api/workspace/backup-targets")
    }
    assert python_only_targets == set()


def test_s4c_python_oracle_target_lifecycle_and_refusal_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
    tmp_path: Path,
) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))

    usb_dir = tmp_path / "usb-oracle"
    usb_dir.mkdir()
    created = backup_targets.init_target(usb_dir, label="Oracle USB")
    target_id = str(created["targetId"])
    assert target_id.startswith("target_")
    assert created["kind"] == "filesystem"
    assert created["drainState"] == "active"

    probe = backup_targets.probe_target(target_id)
    assert probe["ready"] is True
    assert probe["status"] == "ok"

    missing_probe = backup_targets.probe_target("target_missing")
    assert missing_probe["ready"] is False
    assert missing_probe["status"] == "blocked-target-unavailable"
    assert missing_probe["detail"] == "Backup target not found"

    with pytest.raises(AppError) as missing_delete:
        backup_targets.delete_target("target_missing")
    assert missing_delete.value.status == 404

    deleted = backup_targets.delete_target(target_id)
    assert deleted == {"deleted": True, "targetId": target_id}
