from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.mcp.protocol_preparation import prepare_mcp_protocol_json
from scripts import check_mcp_protocol_parity as parity


ROOT = Path(__file__).resolve().parents[1]


def test_shared_fixture_has_at_least_seventy_unique_deterministic_cases() -> None:
    cases = parity.load_fixture()

    assert len(cases) >= 70
    assert len(cases) == 105
    names = [case.get("name") for case in cases]
    assert all(isinstance(name, str) and name for name in names)
    assert len(names) == len(set(names))
    assert all(isinstance(case.get("expect"), dict) for case in cases)


def test_python_reference_matches_all_fixture_expectations() -> None:
    for case in parity.load_fixture():
        result = prepare_mcp_protocol_json(parity.raw_case(case))
        assert parity._matches_expectation(result, case["expect"]), case["name"]


def test_parity_runner_compares_normalized_protocol_and_stable_error_categories() -> None:
    def python_backed_rust(_base_url: str, raw: bytes, _timeout: float) -> dict[str, object]:
        result = prepare_mcp_protocol_json(raw)
        if result.get("ok") is not True:
            result = {**result, "message": "Rust wording intentionally differs"}
        return result

    report = parity.run_parity("http://sidecar", parity.load_fixture(), request_fn=python_backed_rust)

    assert report["ok"] is True
    assert report["summary"] == {"passed": 105, "total": 105}


def test_generated_fixture_edges_cover_size_depth_and_redacted_reports() -> None:
    by_name = {case["name"]: case for case in parity.load_fixture()}
    oversized = parity.raw_case(by_name["oversized_request"])
    nested = parity.raw_case(by_name["excessive_nesting"])

    assert len(oversized) > 2_000_000
    assert prepare_mcp_protocol_json(oversized)["code"] == "request_too_large"
    assert prepare_mcp_protocol_json(nested)["code"] == "nesting_limit_exceeded"

    summary = parity._summary(prepare_mcp_protocol_json(parity.raw_case(by_name["tools_call_cjk_arguments"])))
    assert "arguments" not in str(summary)
    assert "Rust MCP 协议" not in str(summary)


def test_rust_mcp_source_is_protocol_preparation_only() -> None:
    handler = (ROOT / "rust" / "crates" / "deepseek-mcp" / "src" / "handler.rs").read_text(encoding="utf-8")
    registry = (ROOT / "rust" / "crates" / "deepseek-mcp" / "src" / "registry.rs").read_text(encoding="utf-8")
    preparation = (ROOT / "rust" / "crates" / "deepseek-mcp" / "src" / "protocol_preparation.rs").read_text(
        encoding="utf-8"
    )

    assert "prepare_protocol_value" in handler
    assert "call_tool(" not in handler
    assert "pub fn call_tool" not in registry
    assert '"owner": "python"' in preparation


def test_ci_and_docs_wire_the_mcp_protocol_parity_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    documentation = ROOT / "docs" / "MCP_PROTOCOL_PREPARATION_PARITY.md"

    assert "mcp-protocol-parity:" in workflow
    assert "python scripts/check_mcp_protocol_parity.py" in workflow
    assert "artifacts/mcp-protocol-parity-report.json" in workflow
    assert documentation.is_file()
