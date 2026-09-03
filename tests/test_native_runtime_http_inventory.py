from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_rust_gateway_contains_all_canonical_public_routes() -> None:
    inv_path = ROOT / "compat" / "native-runtime" / "v1" / "http" / "rest_inventory.json"
    inv = json.loads(inv_path.read_text(encoding="utf-8"))

    gateway_rs = (ROOT / "rust" / "crates" / "deepseek-gateway" / "src" / "lib.rs").read_text(encoding="utf-8")
    for method, path in inv["public_routes"]:
        assert f'"{path}"' in gateway_rs, f"Route {path} missing from Rust gateway create_app()"

    for path in inv["eventual_rust_owner"]:
        assert f'"{path}"' in gateway_rs, f"Eventual Rust owner route {path} missing from Rust gateway"

    for path in inv["eventual_go_owner"]:
        # e.g. /api/* is proxied via /api/*path in Rust gateway
        prefix = path.rstrip("*")
        assert f'"{prefix}' in gateway_rs, f"Eventual Go owner route prefix {prefix} missing from Rust gateway"
