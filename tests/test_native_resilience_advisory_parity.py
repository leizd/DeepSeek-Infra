from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_dr_ledger,
    backup_policies,
    backup_targets,
)
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_resilience_advisory_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    route_src = (RUST_GATEWAY_SRC / "resilience_advisory_routes.rs").read_text(
        encoding="utf-8"
    )
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(resilience_advisory_routes::router())" in lib_src
    assert '"/api/workspace/resilience/forecast"' in route_src
    assert '"/api/workspace/resilience/optimizer"' in route_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("get", "/api/workspace/resilience/forecast") not in python_only
    assert ("get", "/api/workspace/resilience/optimizer") not in python_only


def test_resilience_advisory_python_oracle_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *args, **kwargs: None)
    fast_dir = tmp_settings / "target-fast"
    slow_dir = tmp_settings / "target-slow"
    fast_dir.mkdir(parents=True, exist_ok=True)
    slow_dir.mkdir(parents=True, exist_ok=True)
    fast = backup_targets.init_target(fast_dir, label="Fast Target")
    slow = backup_targets.init_target(slow_dir, label="Slow Target")
    fast_id = str(fast["targetId"])
    slow_id = str(slow["targetId"])
    backup_control.upsert_target({**fast, "quotaBytes": 10_000})

    backup_policies.create_policy(
        {
            "policyId": "pol_slow",
            "name": "Slow Policy",
            "targetId": slow_id,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        }
    )
    for idx, dur in enumerate((1000, 2000), start=1):
        backup_dr_ledger.record_drill_evidence(
            drill_id=f"df{idx}",
            target_id=fast_id,
            policy_id="pol_fast",
            backup_id=f"bf{idx}",
            observed_at=f"2026-11-17T0{idx}:00:00Z",
            result="success",
            duration_ms=dur,
        )
    for idx, dur in enumerate((9000, 10000), start=1):
        backup_dr_ledger.record_drill_evidence(
            drill_id=f"ds{idx}",
            target_id=slow_id,
            policy_id="pol_slow",
            backup_id=f"bs{idx}",
            observed_at=f"2026-11-17T0{idx + 2}:00:00Z",
            result="success",
            duration_ms=dur,
        )

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    fc = client.get("/api/workspace/resilience/forecast", headers=headers)
    assert fc.status_code == 200
    fc_body = fc.json()
    expected_ids = ["managed-local", *sorted((fast_id, slow_id))]
    assert [t["targetId"] for t in fc_body["targets"]] == expected_ids
    by_id = {t["targetId"]: t for t in fc_body["targets"]}
    assert by_id["managed-local"]["status"] == "unconstrained"
    assert by_id[slow_id]["status"] == "unconstrained"
    assert by_id[fast_id]["totalBytes"] == 10_000
    assert set(by_id[fast_id]["forecast"]) == {"7d", "30d", "90d"}

    opt = client.get("/api/workspace/resilience/optimizer", headers=headers)
    assert opt.status_code == 200
    opt_body = opt.json()
    assert opt_body["optimizerVersion"] == 1
    assert opt_body["targetPerformance"][fast_id]["p90LatencySeconds"] == 2.0
    assert opt_body["targetPerformance"][slow_id]["p90LatencySeconds"] == 10.0
    assert len(opt_body["recommendations"]) == 1
    assert opt_body["recommendations"][0]["policyId"] == "pol_slow"
    assert opt_body["recommendations"][0]["currentPrimaryTarget"] == slow_id
    assert opt_body["recommendations"][0]["recommendedTarget"] == fast_id


def test_rust_gateway_sources_avoid_path_ref_bug_and_unstable_let_chains() -> None:
    import re

    tests_dir = REPO_ROOT / "rust/crates/deepseek-gateway/tests"
    for path in sorted(tests_dir.glob("*.rs")):
        text = path.read_text(encoding="utf-8")
        if "fn build_app(root: &std::path::Path)" in text:
            fn_start = text.index("fn build_app(root: &std::path::Path)")
            fn_end = text.find("\n}", fn_start + 1)
            fn_body = text[fn_start:] if fn_end == -1 else text[fn_start:fn_end]
            assert not re.search(r"(?<![A-Za-z0-9_])root\.path\(\)", fn_body), (
                f"{path.name} calls .path() on &std::path::Path inside build_app"
            )

    for path in sorted((REPO_ROOT / "rust/crates").rglob("*.rs")):
        text = path.read_text(encoding="utf-8")
        for idx, line in enumerate(text.splitlines(), start=1):
            assert not re.search(r"\b(if|while)\s+let\b.*&&", line), (
                f"{path}:{idx} uses unstable rustc 1.85 let_chains syntax"
            )
