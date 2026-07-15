from __future__ import annotations

import json
from pathlib import Path

from deepseek_infra.infra.rag.document_preparation import prepare_rag_document_json
from deepseek_infra.infra.rag import document_preparation as preparation_contract
from scripts import check_rag_document_preparation_parity as parity

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "rag" / "document_preparation_cases.json"


def test_document_preparation_fixture_has_at_least_120_unique_cases() -> None:
    cases = parity.load_cases(FIXTURE)
    names = [case["name"] for case in cases]
    assert len(cases) == 125
    assert len(names) == len(set(names))
    assert any(case.get("generate") == "document_too_large" for case in cases)
    assert any(case.get("generate") == "request_too_large" for case in cases)
    assert any(str(case["name"]).startswith("unicode_") for case in cases)
    assert any(str(case["name"]).startswith("metadata_") for case in cases)


def test_parity_runner_accepts_contract_identical_results_without_exposing_text() -> None:
    cases = [
        case
        for case in parity.load_cases(FIXTURE)
        if case.get("generate") not in {"document_too_large", "request_too_large"}
    ]

    def python_equivalent(_base_url: str, raw: bytes, _timeout: float) -> dict[str, object]:
        return prepare_rag_document_json(raw)

    report = parity.run_parity("http://sidecar.invalid", cases, request_fn=python_equivalent)
    encoded = json.dumps(report, ensure_ascii=False)
    assert report["ok"] is True
    assert report["summary"] == {"passed": len(cases), "total": len(cases)}
    assert "first paragraph" not in encoded.lower()
    assert "authorization" not in encoded.lower()


def test_parity_runner_detects_semantic_divergence() -> None:
    case = next(case for case in parity.load_cases(FIXTURE) if case["name"] == "basic_short_text")

    def divergent(_base_url: str, raw: bytes, _timeout: float) -> dict[str, object]:
        result = prepare_rag_document_json(raw)
        document = result["document"]
        assert isinstance(document, dict)
        document["contentHash"] = "0" * 24
        return result

    report = parity.run_parity("http://sidecar.invalid", [case], request_fn=divergent)
    assert report["ok"] is False
    assert report["summary"] == {"passed": 0, "total": 1}


def test_parity_runner_ignores_only_natural_language_error_message() -> None:
    cases = [case for case in parity.load_cases(FIXTURE) if case.get("expect", {}).get("ok") is False][:1]

    def equivalent_error(_base_url: str, raw: bytes, _timeout: float) -> dict[str, object]:
        result = prepare_rag_document_json(raw)
        return {**result, "message": "different Rust wording"}

    report = parity.run_parity("http://sidecar.invalid", cases, request_fn=equivalent_error)
    assert report["ok"] is True


def test_generated_limit_and_invalid_json_cases_match_python_contract(monkeypatch) -> None:
    request_case = {"generate": "request_too_large"}
    monkeypatch.setattr(parity, "RAG_DOCUMENT_MAX_REQUEST_BYTES", 8)
    monkeypatch.setattr(preparation_contract, "RAG_DOCUMENT_MAX_REQUEST_BYTES", 8)
    assert prepare_rag_document_json(parity.raw_case(request_case))["code"] == "request_too_large"
    assert prepare_rag_document_json(parity.raw_case({"generate": "invalid_json"}))["code"] == "invalid_request"


def test_report_writer_keeps_only_redacted_summaries(tmp_path: Path) -> None:
    report = {
        "ok": True,
        "summary": {"passed": 1, "total": 1},
        "cases": [{"name": "safe", "passed": True, "python": {"fingerprint": "a"}, "rust": {"fingerprint": "a"}}],
    }
    output = tmp_path / "report.json"
    parity.write_report(output, report)
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_contract_sources_and_ci_wiring_exist() -> None:
    python_source = (ROOT / "deepseek_infra/infra/rag/document_preparation.py").read_text(encoding="utf-8")
    rust_source = (ROOT / "rust/crates/deepseek-rag/src/document_preparation.rs").read_text(encoding="utf-8")
    gateway_source = (ROOT / "rust/crates/deepseek-gateway/src/lib.rs").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "DEEPSEEK_RUST_RAG_DOCUMENT_PREP" in python_source
    assert "open(" not in rust_source
    assert "std::fs" not in rust_source
    assert "sqlite" not in rust_source.lower()
    assert '"/rag/documents/prepare"' in gateway_source
    assert "rag-document-preparation-parity:" in workflow
    assert "docs/evidence/rag-document-preparation-parity-v4.0.0.json" in workflow
