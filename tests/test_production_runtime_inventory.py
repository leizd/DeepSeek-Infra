"""The production-entry inventory matches the ownership matrix and live sources."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "release" / "production_runtime_inventory_v1.json"


def test_production_runtime_inventory_matches_sources() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "production_runtime_inventory.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    inventory = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    ownership = json.loads((ROOT / "release" / "native_runtime_ownership_v1.json").read_text(encoding="utf-8"))
    python_ids = [
        item["id"]
        for item in ownership["domains"]
        if item.get("production", True) is True and item["current_owner"] == "python"
    ]
    rust_ids = [
        item["id"]
        for item in ownership["domains"]
        if item.get("production", True) is True and item["current_owner"] == "rust"
    ]
    assert inventory["current_production_authority"] == "python"
    assert inventory["ownership_domains_current_owner_python"] == python_ids
    assert inventory["ownership_domains_current_owner_rust"] == rust_ids
    assert inventory["ownership_domains_current_owner_go"] == []
    assert inventory["http_routes_only_on_python"]
    legacy_ids = {item["id"] for item in inventory["legacy_production_entries"]}
    assert {
        "python_http_server",
        "launch_desktop_server_mobile",
        "android_python_bridge",
        "backup_worker",
        "federation_node",
    } <= legacy_ids
    native_ids = {item["id"] for item in inventory["native_or_gap_entries"]}
    assert "stateless_mcp_server" in native_ids
    stateless = next(item for item in inventory["native_or_gap_entries"] if item["id"] == "stateless_mcp_server")
    assert stateless["executor"] == "rust"
    assert stateless["marker_present"] is True
    assert all(item["marker_present"] for item in inventory["legacy_production_entries"])
    android = next(item for item in inventory["native_or_gap_entries"] if item["id"] == "android_activity")
    assert android["packaged_native_binary"] is False
