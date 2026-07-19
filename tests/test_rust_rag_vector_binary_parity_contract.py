from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

from deepseek_infra.infra.rust_core import vector_binary

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_rag_vector_binary_parity.py"


def _module() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT), run_name="vector_binary_parity_contract")


def test_binary_parity_generator_declares_100_plus_cases() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "for seed in range(90)" in source
    assert all(
        name in source
        for name in (
            "single_candidate",
            "first_match_tie",
            "negative_similarity",
            "zero_vector",
            "dimensions_384",
            "dimensions_768",
            "dimensions_1536",
            "16_candidates_x_384_dimensions",
            "128_candidates_x_768_dimensions",
            "1000_candidates_x_1536_dimensions",
        )
    )


def test_binary_parity_malformed_contract_is_complete() -> None:
    malformed_cases = _module()["_malformed_cases"]()
    assert len(malformed_cases) >= 16
    codes = {case[3] for case in malformed_cases}
    assert {
        "invalid_content_type",
        "invalid_binary_magic",
        "invalid_binary_header",
        "invalid_dimensions",
        "invalid_candidate_count",
        "payload_length_mismatch",
        "payload_too_large",
        "non_finite_vector",
        "arithmetic_overflow",
    } <= codes


def test_binary_contract_constants_match_python_and_rust() -> None:
    rust = (ROOT / "rust/crates/deepseek-rag/src/vector_binary.rs").read_text(encoding="utf-8")
    assert 'b"DSVRNK01"' in rust
    assert 'b"DSVRSP01"' in rust
    assert vector_binary.REQUEST_MAGIC == b"DSVRNK01"
    assert vector_binary.RESPONSE_MAGIC == b"DSVRSP01"
    assert vector_binary.RESPONSE_BYTES == 24
    assert vector_binary.MAX_SCALARS == 1_600_000


def test_binary_endpoint_keeps_json_endpoint() -> None:
    gateway = (ROOT / "rust/crates/deepseek-gateway/src/lib.rs").read_text(encoding="utf-8")
    assert '.route("/rag/vectors/rank", post(rag_vectors_rank))' in gateway
    assert '.route("/rag/vectors/rank-binary", post(rag_vectors_rank_binary))' in gateway
    assert "invalid_content_type" in gateway
    rust_contract = (ROOT / "rust/crates/deepseek-rag/src/vector_binary.rs").read_text(encoding="utf-8")
    assert vector_binary.CONTENT_TYPE in rust_contract


def test_binary_decoder_validates_length_before_scalar_scan() -> None:
    source = (ROOT / "rust/crates/deepseek-rag/src/vector_binary.rs").read_text(encoding="utf-8")
    assert source.index("if body.len() != expected_bytes") < source.index("for value in scalars.chunks_exact(8)")
    assert "checked_mul" in source
    assert "checked_add" in source
    assert "Vec<Vec<f64>>" not in source


def test_binary_codec_uses_stdlib_array_without_numpy() -> None:
    source = (ROOT / "deepseek_infra/infra/rust_core/vector_binary.py").read_text(encoding="utf-8")
    assert "import array" in source
    assert "byteswap()" in source
    assert "struct.pack(\"<8sII\"" in source
    assert "numpy" not in source.lower()
    assert 'struct.pack("<d"' not in source


def test_binary_config_is_explicit_and_defaults_to_json() -> None:
    config = (ROOT / "deepseek_infra/infra/rust_core/config.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'DEFAULT_RUST_RAG_VECTOR_TRANSPORT = "json"' in config
    assert 'frozenset({"json", "binary"})' in config
    assert "DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=json" in env


def test_binary_metrics_labels_are_bounded() -> None:
    source = (ROOT / "rust/crates/deepseek-gateway/src/observability.rs").read_text(encoding="utf-8")
    assert 'const VECTOR_TRANSPORT_ENCODINGS: [&str; 2] = ["json", "binary"]' in source
    assert "vector_rank_transport_total" in source
    for forbidden in ("dimensions=", "candidate_count=", "request_id=", "vector="):
        assert forbidden not in source


def test_binary_client_has_no_json_retry_on_binary_failure() -> None:
    source = (ROOT / "deepseek_infra/infra/rust_core/rag_client.py").read_text(encoding="utf-8")
    start = source.index('if configured_transport == "binary":')
    end = source.index('    else:\n        result = _request("POST", "/rag/vectors/rank"', start)
    binary_branch = source[start:end]
    assert '"/rag/vectors/rank-binary"' in binary_branch
    assert '"/rag/vectors/rank"' not in binary_branch


def test_parity_artifact_schema_redacts_vectors() -> None:
    artifact = ROOT / "docs/evidence/rag-vector-binary-parity-v4.1.0.json"
    if not artifact.is_file():
        return
    report = json.loads(artifact.read_text(encoding="utf-8"))
    assert report["schemaVersion"] == "rag-vector-binary-parity.v1"
    assert report["validCaseCount"] >= 100
    assert report["containsVectorValues"] is False
    assert all("query" not in case and "candidates" not in case for case in report["cases"])
    assert report["binaryResponseBytes"] == 24
    large = report["largePayloads"]["1000_candidates_x_1536_dimensions"]
    assert large["binaryRequestBytes"] < large["jsonRequestBytes"]
