from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.rag import local_rag
from scripts import check_rag_parity as parity


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _fixture() -> dict[str, list[dict[str, Any]]]:
    return parity.load_fixture()


def _expected_value(case: dict[str, Any]) -> dict[str, Any]:
    if "expected_error" in case:
        return {"error": case["expected_error"]}
    return {"value": case["expected"]}


def test_shared_fixture_has_at_least_thirty_deterministic_cases() -> None:
    fixture = _fixture()
    cases = [case for group in fixture.values() for case in group]

    assert len(cases) >= 30
    assert len(cases) == 38
    ids = [str(case.get("id") or "") for case in cases]
    assert all(ids)
    assert len(ids) == len(set(ids))
    for case in fixture["normalization_cases"] + fixture["citation_cases"]:
        assert "expected" in case or "expected_error" in case
    for case in fixture["ranking_cases"]:
        assert len(case["expected_order"]) == case["top_k"]
        assert len(case["expected_scores"]) == case["top_k"]
    for case in fixture["index_validation_cases"]:
        assert set(case["expected"]) == {"valid", "error"}


def test_python_rag_reference_matches_explicit_fixture_expectations() -> None:
    fixture = _fixture()

    for case in fixture["normalization_cases"]:
        assert parity._normalization_result(case["input"]) == _expected_value(case), case["id"]

    for case in fixture["ranking_cases"]:
        chunks = [parity._contract_chunk(chunk) for chunk in case["chunks"]]
        actual = parity._ranking_result(local_rag.python_rag_rank_chunks(case["query"], chunks), case["top_k"])
        assert actual["order"] == case["expected_order"], case["id"]
        assert parity._scores_match(actual["scores"], case["expected_scores"]), case["id"]

    for case in fixture["citation_cases"]:
        actual = parity._citation_result(case["source"], case.get("start_line"), case.get("end_line"))
        assert actual == _expected_value(case), case["id"]

    for case in fixture["index_validation_cases"]:
        chunks = [parity._contract_chunk(chunk) for chunk in case["chunks"]]
        if case.get("roundtrip") is True:
            chunks = json.loads(json.dumps(chunks, ensure_ascii=False))
        assert local_rag.python_rag_validate_index(chunks) == case["expected"], case["id"]


def _rust_error_message(category: str | None) -> str | None:
    messages: dict[str, str] = {
        "duplicate_chunk_id": "duplicate chunk id: dup",
        "empty_chunk_id": "invalid chunk : chunk id is empty",
        "empty_chunk_source": "invalid chunk a: chunk source is empty",
        "empty_chunk_text": "invalid chunk a: chunk text is empty",
        "invalid_line_range": "invalid chunk a: chunk line range is invalid",
    }
    return messages.get(category) if category is not None else None


def _python_backed_rust_request(
    _base_url: str,
    path: str,
    payload: dict[str, Any],
    _timeout: float,
) -> parity.HttpResult:
    if path == "/rag/query/normalize":
        try:
            normalized = local_rag.python_rag_normalize_query(payload["query"])
        except ValueError:
            return parity.HttpResult(400, {"error": "query is empty"}, "")
        return parity.HttpResult(200, {"normalized": normalized, "tokens": normalized.split()}, "")
    if path == "/rag/chunks/score":
        ranked = local_rag.python_rag_rank_chunks(payload["query"], payload["chunks"])
        return parity.HttpResult(200, {"ranked": [{"id": item_id, "score": score} for item_id, score in ranked]}, "")
    if path == "/rag/citation/format":
        try:
            citation = local_rag.python_rag_format_citation(
                payload["source"], payload.get("start_line"), payload.get("end_line")
            )
        except ValueError:
            return parity.HttpResult(400, {"error": "invalid line range"}, "")
        return parity.HttpResult(200, {"citation": citation}, "")
    if path == "/rag/index/validate":
        validation = local_rag.python_rag_validate_index(payload["chunks"])
        if validation["error"] == "invalid_metadata":
            return parity.HttpResult(422, {"raw": "invalid metadata"}, "")
        return parity.HttpResult(
            200,
            {"valid": validation["valid"], "error": _rust_error_message(validation["error"])},
            "",
        )
    raise AssertionError(f"unexpected path: {path}")


def test_parity_runner_reports_all_shared_cases_passed() -> None:
    report = parity.run_parity("http://sidecar", _fixture(), request_fn=_python_backed_rust_request)

    assert report["ok"] is True
    assert sum(group["passed"] for group in report["summary"].values()) == 38
    assert all(result["passed"] for result in report["cases"])


def test_ranking_divergence_reports_first_position() -> None:
    def divergent_request(base_url: str, path: str, payload: dict[str, Any], timeout: float) -> parity.HttpResult:
        result = _python_backed_rust_request(base_url, path, payload, timeout)
        if path == "/rag/chunks/score" and isinstance(result.body.get("ranked"), list):
            result.body["ranked"] = list(reversed(result.body["ranked"]))
        return result

    report = parity.run_parity("http://sidecar", _fixture(), request_fn=divergent_request)
    first_failure = next(result for result in report["cases"] if not result["passed"])

    assert report["ok"] is False
    assert first_failure["category"] == "ranking"
    assert first_failure["detail"].startswith("position 1")


def test_strict_mode_writes_failure_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parity, "wait_for_sidecar", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        parity,
        "run_parity",
        lambda *_args, **_kwargs: {
            "ok": False,
            "summary": {"normalization": {"passed": 0, "total": 1}},
            "cases": [],
        },
    )
    report_path = tmp_path / "rag-parity-report.json"

    exit_code = parity.main(["--strict", "--report", str(report_path)])

    assert exit_code == 1
    assert json.loads(report_path.read_text(encoding="utf-8"))["ok"] is False


def test_ci_runs_rag_parity_as_independent_offline_job() -> None:
    workflow = _read(".github/workflows/ci.yml")
    script = _read("scripts/check_rag_parity.py")
    default_compose = _read("docker-compose.yml")

    assert "rag-parity:" in workflow
    assert "deepseek-rust-gateway:parity" in workflow
    assert "python scripts/check_rag_parity.py" in workflow
    assert "--strict" in workflow
    assert "docs/evidence/rag-parity-v4.2.1.json" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "DEEPSEEK_RUST_RAG=1" not in default_compose
    assert "connect_db(" not in script
    assert "embedding" not in script.lower()
