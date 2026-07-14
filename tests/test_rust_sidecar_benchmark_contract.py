from __future__ import annotations

import json
import platform
import re
import time
from pathlib import Path
from typing import Any

import pytest

from benchmarks import bench_rust_sidecar_release as benchmark
from deepseek_infra.infra.rag.document_preparation import prepare_rag_document

ROOT = Path(__file__).resolve().parents[1]
TIMING_FIELDS = {
    "pythonPreparationUs",
    "serializationUs",
    "transportUs",
    "rustProcessingUs",
    "pythonValidationUs",
    "totalDelegateUs",
}


def _layer() -> dict[str, Any]:
    return {
        "iterations": 1,
        "warmups": 1,
        "inputBytes": 1,
        "outputBytes": 1,
        "medianUs": 1.0,
        "p95Us": 1.0,
        "p99Us": 1.0,
        "minimumUs": 1.0,
        "maximumUs": 1.0,
        "requestsPerSecond": 1.0,
        "errors": 0,
        "fallbacks": 0,
    }


def _report() -> dict[str, Any]:
    delegates = []
    for delegate, components in benchmark.DELEGATE_COMPONENTS.items():
        scenarios = []
        for component in components:
            scenarios.append(
                {
                    "name": f"synthetic_{component}",
                    "component": component,
                    "layers": {
                        "pythonBaseline": _layer(),
                        "pureRustCore": _layer(),
                        "releaseSidecarHttp": _layer(),
                        "fullPythonIntegration": _layer(),
                    },
                    "semanticParity": True,
                }
            )
        delegates.append(
            {
                "delegate": delegate,
                "components": list(components),
                "scenarios": scenarios,
                "concurrency": [{"concurrency": value} for value in (1, 8, 32)],
            }
        )
    return {
        "schemaVersion": benchmark.SCHEMA_VERSION,
        "version": benchmark.VERSION,
        "machine": {"rustProfile": "release", "operatingSystem": "full machine detail"},
        "delegates": delegates,
    }


def test_release_binary_is_used() -> None:
    assert benchmark.BUILD_COMMAND == [
        "cargo",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        "rust/Cargo.toml",
        "-p",
        "deepseek-gateway",
    ]
    dockerfile = (ROOT / "rust/Dockerfile").read_text(encoding="utf-8")
    assert "cargo build" in dockerfile and "--release" in dockerfile and "target/release/deepseek-gateway" in dockerfile


def test_benchmark_warmup_is_excluded() -> None:
    calls = 0

    def measured() -> benchmark.CallResult:
        nonlocal calls
        calls += 1
        return benchmark.CallResult({"ok": True})

    stats, _signature = benchmark._measure(measured, iterations=3, warmups=2, input_bytes=2)
    assert calls == 5
    assert stats["iterations"] == 3
    assert stats["warmups"] == 2


def test_cold_and_warm_results_are_separate() -> None:
    source = (ROOT / "benchmarks/bench_rust_sidecar_release.py").read_text(encoding="utf-8")
    assert '"coldStart": cold' in source
    assert '"includedInWarmResults": False' in source
    assert source.count("subprocess.Popen(") == 1


def test_all_current_delegates_are_reported() -> None:
    report = _report()
    benchmark.validate_report(report)
    assert set(benchmark.DELEGATE_COMPONENTS) == {
        "gateway_request_preparation",
        "mcp_protocol_preparation",
        "tool_policy",
        "rag_vector_ranking",
        "rag_document_preparation",
    }


def test_benchmark_report_redacts_sensitive_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report()
    monkeypatch.setattr(platform, "system", lambda: "TestOS")
    evidence = benchmark._stable_evidence(report)
    rendered = json.dumps(evidence, ensure_ascii=False).lower()
    assert evidence["machine"]["operatingSystem"] == "TestOS"
    assert evidence["machine"]["privacyRedacted"] is True
    assert "full machine detail" not in rendered
    assert "arguments" not in rendered
    assert "documenttext" not in rendered


def test_metrics_labels_are_bounded_and_do_not_contain_user_data() -> None:
    source = (ROOT / "rust/crates/deepseek-gateway/src/observability.rs").read_text(encoding="utf-8")
    assert all(f'"{component}"' in source for values in benchmark.DELEGATE_COMPONENTS.values() for component in values)
    assert "component_for_path" in source
    for forbidden_label in ("model", "method", "tool", "document_id", "hostname", "request_id"):
        assert not re.search(rf"\{{[^}}]*{forbidden_label}=", source)
    assert "payload_bytes" in source and "response_bytes" in source and "stable_error_code" in source
    policy_source = (ROOT / "rust/crates/deepseek-gateway/src/policy_routes.rs").read_text(encoding="utf-8")
    assert "policy_target =" not in policy_source
    assert "audit = %" not in policy_source


def test_diagnostics_do_not_change_parity_and_rust_time_is_not_trusted() -> None:
    base = {"ok": True, "request": {"model": "deepseek-v4-pro"}}
    timed = {**base, "diagnostics": {"rustProcessingUs": 1}}
    assert benchmark.semantic_hash("gateway_prepare", base) == benchmark.semantic_hash("gateway_prepare", timed)
    for relative in (
        "deepseek_infra/infra/gateway/request_preparation.py",
        "deepseek_infra/infra/mcp/protocol_preparation.py",
        "deepseek_infra/infra/rag/document_preparation.py",
        "deepseek_infra/infra/gateway/semantic_cache.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert not re.search(r"if[^\n]*rustProcessingUs|rustProcessingUs[^\n]*(?:==|!=|<=|>=|<|>)", source)


def _median_runtime(call: Any, rounds: int = 3) -> float:
    values = []
    for _ in range(rounds):
        started = time.perf_counter()
        call()
        values.append(time.perf_counter() - started)
    return sorted(values)[len(values) // 2]


def test_vector_ranking_scaling_is_consistent_with_candidate_dimension_product() -> None:
    small = benchmark._vector_payload(64, 128)
    twice_candidates = benchmark._vector_payload(128, 128)
    twice_dimensions = benchmark._vector_payload(64, 256)
    small_time = max(_median_runtime(lambda: benchmark._python_vector(small)), 1e-9)
    candidate_ratio = _median_runtime(lambda: benchmark._python_vector(twice_candidates)) / small_time
    dimension_ratio = _median_runtime(lambda: benchmark._python_vector(twice_dimensions)) / small_time
    assert candidate_ratio < 8.0
    assert dimension_ratio < 8.0


def test_document_preparation_has_bounded_scaling_and_overlap() -> None:
    small = benchmark._document_payload("alpha beta\n" * 2000, 1000, 100)
    large = benchmark._document_payload("alpha beta\n" * 4000, 1000, 100)
    small_time = max(_median_runtime(lambda: prepare_rag_document(small)), 1e-9)
    assert _median_runtime(lambda: prepare_rag_document(large)) / small_time < 10.0
    overlap = benchmark._document_payload("x\n" * 20_000, 200, 199)
    result = prepare_rag_document(overlap)
    assert result["ok"] is True
    assert 0 < len(result["chunks"]) <= len(overlap["text"])


def test_request_limits_and_invalid_errors_are_bounded_before_parse() -> None:
    gateway_source = (ROOT / "rust/crates/deepseek-gateway/src/lib.rs").read_text(encoding="utf-8")
    endpoint = gateway_source[gateway_source.index("async fn gateway_request_prepare") : gateway_source.index("async fn mcp_protocol_prepare")]
    assert endpoint.index("body.len()") < endpoint.index("serde_json::from_slice")
    invalid = prepare_rag_document({"documentId": "x", "text": "x", "chunking": {"chunkChars": 0, "chunkOverlap": 0}})
    assert len(benchmark._json_bytes(invalid)) < 1000


def test_required_timing_fields_are_present_in_delegate_diagnostics_sources() -> None:
    sources = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "deepseek_infra/infra/gateway/request_preparation.py",
            "deepseek_infra/infra/mcp/protocol_preparation.py",
            "deepseek_infra/infra/rag/document_preparation.py",
            "deepseek_infra/infra/rust_core/policy_client.py",
            "deepseek_infra/infra/rust_core/rag_client.py",
        )
    )
    assert all(field in sources for field in TIMING_FIELDS)
