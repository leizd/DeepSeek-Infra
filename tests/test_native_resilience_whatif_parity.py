"""Slice 7a parity tests for native `POST /api/workspace/resilience/whatif`."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_authority_provider,
    backup_control_authority,
    backup_dr_ledger,
    backup_policies,
    backup_targets,
    resilience_capacity_history,
    resilience_cost_model,
    resilience_forecast_registry,
)
from deepseek_infra.web.server import create_server


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_whatif_python_oracle_matches_rust_contract_and_inventory(tmp_settings: Path, monkeypatch: Any) -> None:
    # Isolate target outside runtime root & monkeypatched temp root.
    base = tmp_settings.parent
    fake_tmp = base / "sys_tmp"
    fake_tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(fake_tmp))

    target_dir = base / "ext_targets" / "target_a"
    target_dir.mkdir(parents=True, exist_ok=True)
    anchor_dir = base / "anchor_0"
    anchor_dir.mkdir(parents=True, exist_ok=True)

    backup_control_authority.configure_authority_anchor_roots([anchor_dir])
    backup_authority_provider.sync_provider_from_legacy_globals()
    try:
        backup_targets.register_filesystem_target(
            "target_a",
            path=str(target_dir),
            label="Target A",
            failure_domain="fd-a",
        )
        (target_dir / "sample.enc").write_bytes(b"hello-world")
        backup_policies.create_policy(
            {
                "policyId": "policy_a",
                "name": "Policy A",
                "targetId": "target_a",
                "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
                "replication": {"enabled": True, "minCommittedCopies": 1, "minFailureDomains": 1},
                "placement": {"minFreeBytes": 1000},
            }
        )
        for day in range(4):
            obs = resilience_capacity_history.record_capacity_observation(
                "target_a",
                used_bytes=1024**3 + day * 1024,
                free_bytes=50 * 1024**3 - day * 1024,
                total_bytes=100 * 1024**3,
                observed_at=datetime(2026, 11, 10 + day, 0, 0, 0, tzinfo=timezone.utc),
                target_incarnation="inc-1",
                capacity_revision="rev-1",
            )
        resilience_forecast_registry.ensure_active_forecasts(obs, horizons=(30, 90))
        resilience_cost_model.put_price_catalog(
            {
                "priceCatalogVersion": 1,
                "targets": {"target_a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01}},
            }
        )
        backup_dr_ledger.record_recovery_point(
            target_id="target_a",
            policy_id="policy_a",
            backup_id="backup_1",
            committed_at="2026-11-18T01:00:00Z",
            snapshot_kind="full",
            chain_length=1,
            ciphertext_bytes=100,
            logical_bytes=100,
            recoverable=True,
        )
        backup_dr_ledger.record_logical_recovery_copy(
            target_id="target_a",
            policy_id="policy_a",
            backup_id="backup_1",
            committed_at="2026-11-18T01:00:00Z",
            recoverable=True,
            role="primary",
            state="healthy",
        )

        server, _ = create_server(0, host="127.0.0.1")
        try:
            client = TestClient(server.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
            resp = client.post(
                "/api/workspace/resilience/whatif",
                headers={"Authorization": f"Bearer {config.settings.auth.token}"},
                json={
                    "candidate": {
                        "policyId": "policy_a",
                        "targetId": "target_a",
                        "operation": "KEEP",
                        "storedBytes": 1024**3,
                    }
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "OK"
            assert body["cost"]["monthlyCost"] == 0.02
            assert body["simulation"]["preStateDigests"] == body["simulation"]["postStateDigests"]
            assert body["simulation"]["storageInventoryBefore"][0]["objectCount"] == 2
            assert body["whatIfDigest"] == _digest({k: v for k, v in body.items() if k != "whatIfDigest"})
        finally:
            server.server_close()
    finally:
        backup_control_authority.configure_authority_anchor_roots(None)
        backup_control_authority.configure_authority_anchor_stores(None)
        backup_authority_provider.reset_authority_replica_provider()


def test_whatif_route_is_registered_in_native_rust_gateway() -> None:
    repo = Path(__file__).resolve().parents[1]
    lib_rs = (repo / "rust" / "crates" / "deepseek-gateway" / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "mod resilience_whatif_routes;" in lib_rs
    assert ".merge(resilience_whatif_routes::router())" in lib_rs

    route_rs = (
        repo / "rust" / "crates" / "deepseek-gateway" / "src" / "resilience_whatif_routes.rs"
    ).read_text(encoding="utf-8")
    assert '"/api/workspace/resilience/whatif"' in route_rs
    assert "what-if accepts candidate only; present truth is server-controlled" in route_rs

    inventory = json.loads(
        (repo / "release" / "production_runtime_inventory_v1.json").read_text(encoding="utf-8")
    )
    remaining = inventory.get("http_routes_only_on_python") or []
    assert all(item.get("path") != "/api/workspace/resilience/whatif" for item in remaining)
