from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.infra.native_runtime.authority import (
    RUST_DATA_DOMAINS,
    PythonWriterMechanicallyDeniedError,
)
from deepseek_infra.infra.workspace import (
    authority_retention,
    backup_retention,
    backups,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_LIB = REPO_ROOT / "rust/crates/deepseek-gateway/src/lib.rs"
RUST_BACKUP_RETENTION_ROUTES = REPO_ROOT / "rust/crates/deepseek-gateway/src/backup_retention_routes.rs"
RUST_AUTHORITY_RETENTION_ROUTES = REPO_ROOT / "rust/crates/deepseek-gateway/src/authority_retention_routes.rs"
OWNERSHIP_CONTRACT = REPO_ROOT / "release/native_runtime_ownership_v1.json"


def test_python_retention_writers_denied_under_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    root = backups.BACKUP_DIR
    root.mkdir(parents=True, exist_ok=True)

    assert "backup_retention_store" in RUST_DATA_DOMAINS
    assert "authority_retention_store" in RUST_DATA_DOMAINS

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_retention_store"):
        backup_retention.put_retention_policy("p-deny", {"keepLast": 2})

    retention = backup_retention.normalize_retention_policy({"keepLast": 1})
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_retention_store"):
        backup_retention.apply_retention(retention, root)

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_retention_store"):
        backup_retention.finalize_retention(retention, root)

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="authority_retention_store"):
        authority_retention.put_authority_retention_policy({"minimumGenerations": 20})

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="authority_retention_store"):
        authority_retention.execute_compaction()

    assert not (backup_retention.BACKUP_RETENTION_DIR / "p-deny.json").exists()
    assert not (authority_retention.AUTHORITY_RETENTION_DIR / "policy.json").exists()


def test_python_retention_writers_allowed_under_python_and_go_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    for mode in ("python_authoritative", "go_authoritative"):
        monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
        saved_ret = backup_retention.put_retention_policy(f"pol-{mode}", {"keepLast": 4})
        assert saved_ret["keepLast"] == 4
        saved_auth = authority_retention.put_authority_retention_policy({"minimumGenerations": 42})
        assert saved_auth["minimumGenerations"] == 42


def test_rust_s3_routes_and_declared_domains_alignment() -> None:
    lib_source = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    assert '"backup_retention_store"' in lib_source
    assert '"authority_retention_store"' in lib_source

    ownership = json.loads(OWNERSHIP_CONTRACT.read_text(encoding="utf-8"))
    domain_ids = {item["id"] for item in ownership["domains"]}
    assert "backup_retention_store" in domain_ids
    assert "authority_retention_store" in domain_ids

    backup_src = RUST_BACKUP_RETENTION_ROUTES.read_text(encoding="utf-8")
    for required in (
        "/api/workspace/retention/preview",
        "/api/workspace/retention/apply",
        "NATIVE_BACKUP_RETENTION_WRITE_NOT_OWNED",
        "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED",
        "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED",
        "retention_runs",
    ):
        assert required in backup_src

    auth_src = RUST_AUTHORITY_RETENTION_ROUTES.read_text(encoding="utf-8")
    for required in (
        "/api/workspace/authority/retention/policy",
        "/api/workspace/authority/history-snapshot",
        "/api/workspace/authority/retention/explain",
        "/api/workspace/authority/retention/plan",
        "/api/workspace/authority/retention/compact",
        "NATIVE_AUTHORITY_RETENTION_WRITE_NOT_OWNED",
        "control_authority_mutations",
    ):
        assert required in auth_src
