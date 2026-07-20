from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import check_gateway_request_parity as parity


ROOT = Path(__file__).resolve().parents[1]


def test_shared_fixture_has_at_least_fifty_unique_deterministic_cases() -> None:
    cases = parity.load_fixture()

    assert len(cases) >= 50
    assert len(cases) == 68
    ids = [case.get("id") for case in cases]
    assert all(isinstance(case_id, str) and case_id for case_id in ids)
    assert len(ids) == len(set(ids))
    assert {case.get("group") for case in cases} == {"valid", "invalid"}
    assert all("expected_error" not in case for case in cases if case["group"] == "valid")
    assert all(isinstance(case.get("expected_error"), str) for case in cases if case["group"] == "invalid")


def test_python_reference_matches_fixture_expectations() -> None:
    for case in parity.load_fixture():
        result = parity.python_result(parity.materialize(case["request"]))
        if case["group"] == "valid":
            assert result["ok"] is True, case["id"]
        else:
            assert result == {"ok": False, "code": case["expected_error"]}, case["id"]


def test_parity_runner_compares_core_requests_and_stable_error_categories() -> None:
    messages = {
        "invalid_request": "wording may differ",
        "unsupported_model": "different Rust prose",
        "invalid_messages": "different Rust prose",
        "invalid_message_role": "different Rust prose",
        "invalid_message_content": "different Rust prose",
        "invalid_tools": "different Rust prose",
        "invalid_tool_choice": "different Rust prose",
        "invalid_temperature": "different Rust prose",
        "invalid_max_tokens": "different Rust prose",
        "request_too_large": "different Rust prose",
    }

    def python_backed_rust(_base_url: str, request: dict[str, Any], _timeout: float) -> dict[str, Any]:
        result = parity.python_result(request)
        if result["ok"] is True:
            return {**result, "diagnostics": {"runtime": "rust", "normalized": True}}
        return {**result, "message": messages[result["code"]]}

    report = parity.run_parity("http://sidecar", parity.load_fixture(), request_fn=python_backed_rust)

    assert report["ok"] is True
    assert report["summary"] == {"passed": 68, "total": 68}


def test_special_fixture_markers_cover_non_finite_size_and_depth_edges() -> None:
    by_id = {case["id"]: case for case in parity.load_fixture()}

    nan_request = parity.materialize(by_id["invalid-temperature-nan"]["request"])
    large_request = parity.materialize(by_id["invalid-request-too-large"]["request"])
    deep_request = parity.materialize(by_id["invalid-request-too-deep"]["request"])

    assert str(nan_request["temperature"]) == "nan"
    assert len(large_request["messages"][0]["content"]) == 16_000_001
    assert parity.python_result(deep_request) == {"ok": False, "code": "request_too_large"}


def test_ci_and_docs_wire_the_gateway_parity_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    documentation = ROOT / "docs" / "GATEWAY_REQUEST_PREPARATION_PARITY.md"

    assert "gateway-request-parity:" in workflow
    assert "python scripts/check_gateway_request_parity.py" in workflow
    assert "docs/evidence/gateway-request-parity-v4.2.0.json" in workflow
    assert documentation.is_file()
