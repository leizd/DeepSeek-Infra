from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.infra.native_runtime.authority import (
    RUST_DATA_DOMAINS,
    PythonWriterMechanicallyDeniedError,
)
from deepseek_infra.infra.workspace import (
    backup_drain,
    backup_replication,
    backup_write_continuity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_LIB = REPO_ROOT / "rust/crates/deepseek-gateway/src/lib.rs"
OWNERSHIP_CONTRACT = REPO_ROOT / "release/native_runtime_ownership_v1.json"


def test_python_s4b_writers_denied_under_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    for store_id in (
        "backup_drain_store",
        "backup_write_continuity_store",
        "backup_replication_store",
    ):
        assert store_id in RUST_DATA_DOMAINS

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_drain_store"):
        backup_drain.start_target_drain("target_missing", reason="test")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_drain_store"):
        backup_drain.cancel_target_drain("target_missing", reason="test")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_write_continuity_store"):
        backup_write_continuity.save_write_continuity_state("pol-1", {"policyId": "pol-1"})

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_write_continuity_store"):
        backup_write_continuity.promote_primary_target("pol-1", "target_x")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_replication_store"):
        backup_replication._atomic_write(backup_replication.REBALANCE_DIR / "j.json", {"jobId": "j"})

    assert not backup_drain.DRAIN_DIR.exists()
    assert not backup_write_continuity.CONTINUITY_DIR.exists()
    assert not backup_replication.REBALANCE_DIR.exists()


def test_python_s4b_writers_allowed_under_python_and_go_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    for mode in ("python_authoritative", "go_authoritative"):
        monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", mode)
        backup_write_continuity.save_write_continuity_state(
            f"pol-{mode}",
            {"schemaVersion": 1, "policyId": f"pol-{mode}", "failoverEpoch": 0},
        )
        assert (backup_write_continuity.CONTINUITY_DIR / f"pol-{mode}.json").is_file()
        backup_replication._atomic_write(
            backup_replication.REBALANCE_DIR / f"job-{mode}.json",
            {"jobId": f"job-{mode}", "policyId": "pol-1", "phase": "pending"},
        )
        assert (backup_replication.REBALANCE_DIR / f"job-{mode}.json").is_file()


def test_rust_s4a_s4b_routes_and_declared_domains_alignment() -> None:
    lib_source = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    ownership = json.loads(OWNERSHIP_CONTRACT.read_text(encoding="utf-8"))
    domain_ids = {item["id"] for item in ownership["domains"]}

    for store_id in (
        "backup_drain_store",
        "backup_write_continuity_store",
        "backup_replication_store",
    ):
        assert f'"{store_id}"' in lib_source
        assert store_id in domain_ids

    checks = {
        "backup_recovery_identity_routes.rs": ["/api/workspace/backups/recovery-identities"],
        "backup_policy_routes.rs": ["/api/workspace/backup-policies/:policy_id"],
        "backup_drain_routes.rs": [
            "/api/workspace/backup-targets/:target_id/drain",
            "/api/workspace/backup-targets/:target_id/drain/cancel",
            "NATIVE_BACKUP_DRAIN_WRITE_NOT_OWNED",
        ],
        "backup_continuity_routes.rs": [
            "/api/workspace/backup-policies/:policy_id/continuity",
            "/api/workspace/backup-policies/:policy_id/promote-primary",
            "NATIVE_BACKUP_WRITE_CONTINUITY_WRITE_NOT_OWNED",
        ],
        "backup_rebalance_routes.rs": [
            "/api/workspace/backup-rebalances",
            "NATIVE_BACKUP_REPLICATION_WRITE_NOT_OWNED",
        ],
    }
    for filename, markers in checks.items():
        text = (REPO_ROOT / "rust/crates/deepseek-gateway/src" / filename).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in text, f"{marker} missing from {filename}"
