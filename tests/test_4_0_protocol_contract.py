from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "release" / "4_0_protocol_contract.json"
EXPECTED_ENDPOINTS = {
    ("GET", "/healthz"),
    ("GET", "/metrics"),
    ("POST", "/gateway/request/prepare"),
    ("POST", "/mcp/request/prepare"),
    ("POST", "/policy/url"),
    ("POST", "/policy/path"),
    ("POST", "/policy/capability"),
    ("POST", "/rag/vectors/rank"),
    ("POST", "/rag/vectors/rank-binary"),
    ("POST", "/rag/documents/prepare"),
}


def _contract() -> dict[str, object]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _endpoints() -> list[dict[str, object]]:
    value = _contract()["endpoints"]
    assert isinstance(value, list)
    return value


def test_protocol_contract_freezes_exact_rc2_inventory() -> None:
    data = _contract()
    assert data["schema_version"] == 1
    assert data["version"] == "4.0.0-rc.2"
    assert data["status"] == "frozen"
    endpoints = _endpoints()
    assert {(str(item["method"]), str(item["path"])) for item in endpoints} == EXPECTED_ENDPOINTS
    assert len(endpoints) == len(EXPECTED_ENDPOINTS)


def test_every_endpoint_records_schema_media_limits_errors_and_ownership() -> None:
    required = {
        "request_schema_version",
        "response_schema_version",
        "request_content_type",
        "response_content_type",
        "max_payload_bytes",
        "stable_error_codes",
        "fallback_owner",
        "business_owner",
        "stability",
    }
    for endpoint in _endpoints():
        assert required <= endpoint.keys()
        assert isinstance(endpoint["max_payload_bytes"], int) and endpoint["max_payload_bytes"] >= 0
        assert isinstance(endpoint["stable_error_codes"], list)
        assert endpoint["stability"] in {"public", "internal_stable", "experimental"}
        assert endpoint["fallback_owner"] in {"python", "not_applicable"}


def test_frozen_routes_exist_in_the_rust_router() -> None:
    router = (ROOT / "rust" / "crates" / "deepseek-gateway" / "src" / "lib.rs").read_text(encoding="utf-8")
    policy = (ROOT / "rust" / "crates" / "deepseek-gateway" / "src" / "policy_routes.rs").read_text(encoding="utf-8")
    source = router + "\n" + policy
    for _method, path in EXPECTED_ENDPOINTS:
        assert f'"{path}"' in source


def test_binary_magic_and_limits_match_python_and_rust_sources() -> None:
    data = _contract()
    binary = data["binary_protocol"]
    assert isinstance(binary, dict)
    assert binary == {
        "request_magic": "DSVRNK01",
        "response_magic": "DSVRSP01",
        "byte_order": "little-endian",
        "response_bytes": 24,
    }
    rust = (ROOT / "rust" / "crates" / "deepseek-rag" / "src" / "vector_binary.rs").read_text(encoding="utf-8")
    python = (ROOT / "deepseek_infra" / "infra" / "rust_core" / "vector_binary.py").read_text(encoding="utf-8")
    for magic in ("DSVRNK01", "DSVRSP01"):
        assert magic in rust and magic in python
    endpoint = next(item for item in _endpoints() if item["path"] == "/rag/vectors/rank-binary")
    assert endpoint["max_payload_bytes"] == 12_800_016
    assert endpoint["request_schema_version"] == "DSVRNK01"
    assert endpoint["response_schema_version"] == "DSVRSP01"


def test_protocol_freeze_preserves_python_business_ownership() -> None:
    for endpoint in _endpoints():
        path = str(endpoint["path"])
        if path not in {"/healthz", "/metrics"}:
            assert endpoint["fallback_owner"] == "python"
            assert str(endpoint["business_owner"]).startswith("python_")
    binary = next(item for item in _endpoints() if item["path"] == "/rag/vectors/rank-binary")
    assert binary["stability"] == "experimental"
