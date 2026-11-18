from __future__ import annotations

import json
from pathlib import Path

from deepseek_infra.infra.native_runtime.authority import RUST_DATA_DOMAINS

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_LIB = REPO_ROOT / "rust/crates/deepseek-gateway/src/lib.rs"
RUST_POLICY_RUN_ROUTES = REPO_ROOT / "rust/crates/deepseek-gateway/src/backup_policy_run_routes.rs"
OWNERSHIP_CONTRACT = REPO_ROOT / "release/native_runtime_ownership_v1.json"


def test_rust_s4_manual_run_route_and_declared_domains_alignment() -> None:
    lib_source = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    run_source = RUST_POLICY_RUN_ROUTES.read_text(encoding="utf-8")
    ownership = json.loads(OWNERSHIP_CONTRACT.read_text(encoding="utf-8"))
    domain_ids = {item["id"] for item in ownership["domains"]}

    assert "backup_policy_run_routes::router()" in lib_source
    assert "/api/workspace/backup-policies/:policy_id/run" in run_source

    for store_id, err_code in (
        ("backup_catalog_store", "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED"),
        ("backup_run_plan_store", "NATIVE_BACKUP_RUN_PLAN_WRITE_NOT_OWNED"),
        ("backup_spool_store", "NATIVE_BACKUP_SPOOL_WRITE_NOT_OWNED"),
        ("backup_index_store", "NATIVE_BACKUP_INDEX_WRITE_NOT_OWNED"),
        ("backup_dr_ledger_store", "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED"),
        ("backup_retention_store", "NATIVE_BACKUP_RETENTION_WRITE_NOT_OWNED"),
    ):
        assert store_id in RUST_DATA_DOMAINS
        assert store_id in domain_ids
        assert f'"{store_id}"' in lib_source
        assert store_id in run_source
        assert err_code in run_source
