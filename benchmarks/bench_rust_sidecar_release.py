"""Release-mode, layered benchmark for every current Rust sidecar delegate."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import platform
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.gateway import semantic_cache  # noqa: E402
from deepseek_infra.infra.gateway.request_preparation import (  # noqa: E402
    prepare_gateway_request,
    prepare_request_with_optional_rust,
)
from deepseek_infra.infra.mcp.protocol_preparation import (  # noqa: E402
    prepare_mcp_protocol,
    prepare_mcp_protocol_with_optional_rust,
)
from deepseek_infra.infra.rag.document_preparation import (  # noqa: E402
    prepare_rag_document,
    prepare_rag_document_with_optional_rust,
)
from deepseek_infra.infra.rust_core import policy_client, rag_client, transport, vector_binary  # noqa: E402
from deepseek_infra.infra.tool_runtime.tool_policy import evaluate_path_safety, evaluate_url_safety  # noqa: E402

VERSION = "4.0.0-rc.2"
SCHEMA_VERSION = "rust-sidecar-performance.v3"
BUILD_COMMAND = [
    "cargo",
    "build",
    "--release",
    "--locked",
    "--manifest-path",
    "rust/Cargo.toml",
    "-p",
    "deepseek-gateway",
]
DELEGATE_COMPONENTS = {
    "gateway_request_preparation": ("gateway_prepare",),
    "mcp_protocol_preparation": ("mcp_prepare",),
    "tool_policy": ("policy_url", "policy_path", "policy_capability"),
    "rag_vector_ranking": ("rag_vector_rank",),
    "rag_document_preparation": ("rag_document_prepare",),
}
MAX_ITERATIONS = 100
MAX_WARMUPS = 20
MAX_CONCURRENCY = 32


@dataclass(frozen=True)
class Scenario:
    name: str
    component: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CallResult:
    output: Any
    fallback: bool = False


@dataclass(frozen=True)
class VectorLayerResult:
    output: dict[str, Any]
    fallback: bool
    request_bytes: int
    response_bytes: int
    serialization_us: int | None = None
    transport_us: int | None = None
    rust_processing_us: int | None = None
    python_validation_us: int | None = None


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _semantic_value(component: str, value: Any) -> Any:
    if component == "gateway_prepare" and isinstance(value, dict):
        if value.get("ok") is True:
            return {"ok": True, "request": value.get("request")}
        return {"ok": False, "code": value.get("code")}
    if component.startswith("policy_") and isinstance(value, dict):
        return {
            "allowed": value.get("allowed"),
            "code": value.get("code"),
            "capability": value.get("capability"),
            "risk_level": value.get("risk_level"),
        }
    return value


def semantic_hash(component: str, value: Any) -> str:
    return hashlib.blake2s(_json_bytes(_semantic_value(component, value))).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _stats(
    samples_us: list[float],
    *,
    iterations: int,
    warmups: int,
    input_bytes: int,
    output_bytes: int,
    errors: int,
    fallbacks: int,
    wall_seconds: float | None = None,
) -> dict[str, Any]:
    requests_per_second = 0.0
    if wall_seconds and wall_seconds > 0:
        requests_per_second = max(0.0, (iterations - errors) / wall_seconds)
    elif samples_us:
        total_seconds = sum(samples_us) / 1_000_000.0
        requests_per_second = max(0.0, (iterations - errors) / total_seconds) if total_seconds > 0 else 0.0
    return {
        "iterations": iterations,
        "warmups": warmups,
        "inputBytes": input_bytes,
        "outputBytes": output_bytes,
        "medianUs": round(statistics.median(samples_us), 3) if samples_us else None,
        "p95Us": round(_percentile(samples_us, 0.95), 3) if samples_us else None,
        "p99Us": round(_percentile(samples_us, 0.99), 3) if samples_us else None,
        "minimumUs": round(min(samples_us), 3) if samples_us else None,
        "maximumUs": round(max(samples_us), 3) if samples_us else None,
        "requestsPerSecond": round(requests_per_second, 3),
        "errors": errors,
        "fallbacks": fallbacks,
    }


def _measure(call: Callable[[], CallResult], *, iterations: int, warmups: int, input_bytes: int) -> tuple[dict[str, Any], str]:
    for _ in range(warmups):
        call()
    samples: list[float] = []
    errors = 0
    fallbacks = 0
    output_bytes = 0
    output_hash = ""
    wall_started = time.perf_counter()
    for _ in range(iterations):
        started_ns = time.perf_counter_ns()
        try:
            result = call()
            fallbacks += int(result.fallback)
            output_bytes = len(_json_bytes(result.output))
            output_hash = semantic_hash("", result.output)
        except Exception:
            errors += 1
        samples.append(max(0.0, (time.perf_counter_ns() - started_ns) / 1000.0))
    wall_seconds = time.perf_counter() - wall_started
    return (
        _stats(
            samples,
            iterations=iterations,
            warmups=warmups,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            errors=errors,
            fallbacks=fallbacks,
            wall_seconds=wall_seconds,
        ),
        output_hash,
    )


def _python_vector(payload: dict[str, Any]) -> dict[str, Any]:
    query = payload["query"]
    best_index: int | None = None
    best_similarity = 0.0
    for index, candidate in enumerate(payload["candidates"]):
        similarity = min(1.0, max(0.0, sum(float(left) * float(right) for left, right in zip(query, candidate))))
        if similarity > best_similarity:
            best_index = index
            best_similarity = similarity
    return {"index": best_index, "similarity": best_similarity}


def _vector_outputs_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return expected.get("index") == actual.get("index") and math.isclose(
        float(expected.get("similarity") or 0.0),
        float(actual.get("similarity") or 0.0),
        rel_tol=1e-9,
        abs_tol=1e-12,
    )


_RISK_ORDER = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}
_CAPABILITY_RISK = {
    "ReadFile": "Low",
    "NetworkFetch": "Medium",
    "McpToolCall": "Medium",
    "WriteFile": "High",
    "BrowserControl": "High",
    "ShellExec": "Critical",
}


def _python_policy(component: str, payload: dict[str, Any]) -> dict[str, Any]:
    if component == "policy_url":
        allowed, reason = evaluate_url_safety(str(payload.get("url") or ""))
        code = "allowed" if allowed else ("localhost_blocked" if "loopback" in reason or "local" in reason else "private_network_blocked")
        return {"allowed": allowed, "code": code, "capability": "NetworkFetch", "risk_level": "High"}
    if component == "policy_path":
        allowed, _reason = evaluate_path_safety({"path": payload.get("requested")})
        return {
            "allowed": allowed,
            "code": "allowed" if allowed else "path_traversal",
            "capability": "ReadFile",
            "risk_level": "High",
        }
    requested = str(payload.get("requested") or "")
    granted = [str(item) for item in payload.get("granted") or []]
    required = _CAPABILITY_RISK.get(requested, "Critical")
    max_risk = str(payload.get("max_risk") or "Low")
    if requested not in granted:
        allowed, code = False, "missing_capability"
    elif _RISK_ORDER[required] > _RISK_ORDER[max_risk]:
        allowed, code = False, "risk_limit_exceeded"
    else:
        allowed, code = True, "allowed"
    return {"allowed": allowed, "code": code, "capability": requested, "risk_level": required}


def python_baseline(scenario: Scenario) -> CallResult:
    payload = scenario.payload
    if scenario.component == "gateway_prepare":
        try:
            request = prepare_gateway_request(payload)
            return CallResult({"ok": True, "request": request})
        except AppError as exc:
            code = getattr(exc.code, "value", exc.code)
            return CallResult({"ok": False, "code": str(code)})
    if scenario.component == "mcp_prepare":
        return CallResult(prepare_mcp_protocol(payload))
    if scenario.component.startswith("policy_"):
        return CallResult(_python_policy(scenario.component, payload))
    if scenario.component == "rag_vector_rank":
        return CallResult(_python_vector(payload))
    if scenario.component == "rag_document_prepare":
        return CallResult(prepare_rag_document(payload))
    raise RuntimeError(f"unknown benchmark component: {scenario.component}")


def _response_processing_us(response: Any) -> int | None:
    raw = transport.response_header(response, "X-DeepSeek-Rust-Processing-Us")
    try:
        return max(0, int(raw)) if raw is not None else None
    except ValueError:
        return None


def http_call(base_url: str, scenario: Scenario, timeout: float) -> CallResult:
    raw = json.dumps(scenario.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{endpoint_for(scenario.component)}",
        data=raw,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-DeepSeek-Request-ID": transport.new_correlation_id(),
        },
    )
    with transport.urlopen(request, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
        _response_processing_us(response)
    if not isinstance(value, dict):
        raise RuntimeError("sidecar benchmark response must be an object")
    return CallResult(value)


def _vector_json_serialization(scenario: Scenario) -> VectorLayerResult:
    started_ns = time.perf_counter_ns()
    raw = json.dumps(scenario.payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    elapsed = max(0, (time.perf_counter_ns() - started_ns) // 1000)
    return VectorLayerResult(
        output={"requestBytes": len(raw)},
        fallback=False,
        request_bytes=len(raw),
        response_bytes=0,
        serialization_us=elapsed,
    )


def _vector_binary_serialization(scenario: Scenario) -> VectorLayerResult:
    started_ns = time.perf_counter_ns()
    encoded = vector_binary.encode_rank_request(scenario.payload["query"], scenario.payload["candidates"])
    elapsed = max(0, (time.perf_counter_ns() - started_ns) // 1000)
    return VectorLayerResult(
        output={"requestBytes": len(encoded.body)},
        fallback=False,
        request_bytes=len(encoded.body),
        response_bytes=0,
        serialization_us=elapsed,
    )


def _vector_json_http(base_url: str, scenario: Scenario, timeout: float) -> VectorLayerResult:
    serialization_started_ns = time.perf_counter_ns()
    raw = json.dumps(scenario.payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/rag/vectors/rank",
        data=raw,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-DeepSeek-Request-ID": transport.new_correlation_id(),
        },
    )
    with transport.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        value = json.loads(response_body.decode("utf-8"))
        rust_processing_us = _response_processing_us(response)
        transport_us = getattr(response, "transport_us", None)
    if not isinstance(value, dict):
        raise RuntimeError("JSON vector response must be an object")
    return VectorLayerResult(
        output=value,
        fallback=False,
        request_bytes=len(raw),
        response_bytes=len(response_body),
        serialization_us=serialization_us,
        transport_us=transport_us if isinstance(transport_us, int) else None,
        rust_processing_us=rust_processing_us,
    )


def _vector_binary_http(base_url: str, scenario: Scenario, timeout: float) -> VectorLayerResult:
    serialization_started_ns = time.perf_counter_ns()
    encoded = vector_binary.encode_rank_request(scenario.payload["query"], scenario.payload["candidates"])
    serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/rag/vectors/rank-binary",
        data=encoded.body,
        method="POST",
        headers={
            "Accept": vector_binary.CONTENT_TYPE,
            "Content-Type": vector_binary.CONTENT_TYPE,
            "X-DeepSeek-Request-ID": transport.new_correlation_id(),
        },
    )
    with transport.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        content_type = transport.response_header(response, "Content-Type")
        rust_processing_us = _response_processing_us(response)
        transport_us = getattr(response, "transport_us", None)
    if content_type is None or content_type.lower() != vector_binary.CONTENT_TYPE:
        raise RuntimeError("binary vector response content type is invalid")
    decoded = vector_binary.decode_rank_response(response_body, candidate_count=len(scenario.payload["candidates"]))
    return VectorLayerResult(
        output={"index": decoded.index, "similarity": decoded.similarity},
        fallback=False,
        request_bytes=len(encoded.body),
        response_bytes=len(response_body),
        serialization_us=serialization_us,
        transport_us=transport_us if isinstance(transport_us, int) else None,
        rust_processing_us=rust_processing_us,
    )


def _vector_binary_blob_http(
    base_url: str,
    query: list[float],
    candidate_blobs: list[bytes],
    dimensions: int,
    timeout: float,
) -> VectorLayerResult:
    serialization_started_ns = time.perf_counter_ns()
    encoded = vector_binary.encode_rank_request_from_blobs(
        query,
        candidate_blobs,
        dimensions,
        blobs_validated=True,
    )
    serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/rag/vectors/rank-binary",
        data=encoded.body,
        method="POST",
        headers={
            "Accept": vector_binary.CONTENT_TYPE,
            "Content-Type": vector_binary.CONTENT_TYPE,
            "X-DeepSeek-Request-ID": transport.new_correlation_id(),
        },
    )
    with transport.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        content_type = transport.response_header(response, "Content-Type")
        rust_processing_us = _response_processing_us(response)
        transport_us = getattr(response, "transport_us", None)
    if content_type is None or content_type.lower() != vector_binary.CONTENT_TYPE:
        raise RuntimeError("binary vector response content type is invalid")
    decoded = vector_binary.decode_rank_response(response_body, candidate_count=len(candidate_blobs))
    return VectorLayerResult(
        output={"index": decoded.index, "similarity": decoded.similarity},
        fallback=False,
        request_bytes=len(encoded.body),
        response_bytes=len(response_body),
        serialization_us=serialization_us,
        transport_us=transport_us if isinstance(transport_us, int) else None,
        rust_processing_us=rust_processing_us,
    )


def _vector_full_integration(scenario: Scenario, encoding: str) -> VectorLayerResult:
    previous = os.environ.get("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT")
    os.environ["DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT"] = encoding
    try:
        ranked, used_rust = rag_client.rank_vectors(scenario.payload["query"], scenario.payload["candidates"])
        diagnostics = rag_client.last_delegate_diagnostics("rag_vector_rank")
    finally:
        if previous is None:
            os.environ.pop("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", None)
        else:
            os.environ["DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT"] = previous
    validation_started_ns = time.perf_counter_ns()
    expected = _python_vector(scenario.payload)
    candidate = {"index": ranked[0], "similarity": ranked[1]} if used_rust and ranked is not None else expected
    parity = candidate.get("index") == expected.get("index") and math.isclose(
        float(candidate.get("similarity") or 0.0),
        float(expected.get("similarity") or 0.0),
        rel_tol=1e-9,
        abs_tol=1e-12,
    )
    python_validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
    return VectorLayerResult(
        output=candidate if parity else expected,
        fallback=not used_rust or not parity,
        request_bytes=int(diagnostics.get("requestPayloadBytes") or 0),
        response_bytes=int(diagnostics.get("responsePayloadBytes") or 0),
        serialization_us=diagnostics.get("serializationUs") if isinstance(diagnostics.get("serializationUs"), int) else None,
        transport_us=diagnostics.get("transportUs") if isinstance(diagnostics.get("transportUs"), int) else None,
        rust_processing_us=diagnostics.get("rustProcessingUs") if isinstance(diagnostics.get("rustProcessingUs"), int) else None,
        python_validation_us=python_validation_us,
    )


def _vector_concurrency_call(base_url: str, scenario: Scenario, timeout: float, encoding: str) -> CallResult:
    result = (
        _vector_binary_http(base_url, scenario, timeout)
        if encoding == "binary"
        else _vector_json_http(base_url, scenario, timeout)
    )
    return CallResult(result.output, fallback=result.fallback)


def _timing_pair(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return round(statistics.median(values), 3), round(_percentile(values, 0.95), 3)


def _measure_vector_layer(
    call: Callable[[], VectorLayerResult],
    *,
    iterations: int,
    warmups: int,
    expected_output: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    for _ in range(warmups):
        call()
    samples: list[float] = []
    serialization: list[float] = []
    transport_samples: list[float] = []
    rust_processing: list[float] = []
    python_validation: list[float] = []
    errors = 0
    fallbacks = 0
    semantic_mismatches = 0
    request_bytes = 0
    response_bytes = 0
    output_hash = ""
    wall_started = time.perf_counter()
    for _ in range(iterations):
        started_ns = time.perf_counter_ns()
        try:
            result = call()
            fallbacks += int(result.fallback)
            if expected_output is not None and not _vector_outputs_match(expected_output, result.output):
                semantic_mismatches += 1
            request_bytes = result.request_bytes
            response_bytes = result.response_bytes
            output_hash = semantic_hash("rag_vector_rank", result.output)
            if result.serialization_us is not None:
                serialization.append(float(result.serialization_us))
            if result.transport_us is not None:
                transport_samples.append(float(result.transport_us))
            if result.rust_processing_us is not None:
                rust_processing.append(float(result.rust_processing_us))
            if result.python_validation_us is not None:
                python_validation.append(float(result.python_validation_us))
        except Exception:
            errors += 1
        samples.append(max(0.0, (time.perf_counter_ns() - started_ns) / 1000.0))
    stats = _stats(
        samples,
        iterations=iterations,
        warmups=warmups,
        input_bytes=request_bytes,
        output_bytes=response_bytes,
        errors=errors,
        fallbacks=fallbacks,
        wall_seconds=time.perf_counter() - wall_started,
    )
    serialization_median, serialization_p95 = _timing_pair(serialization)
    transport_median, transport_p95 = _timing_pair(transport_samples)
    rust_median, rust_p95 = _timing_pair(rust_processing)
    python_validation_median, python_validation_p95 = _timing_pair(python_validation)
    stats.update(
        serializationMedianUs=serialization_median,
        serializationP95Us=serialization_p95,
        transportMedianUs=transport_median,
        transportP95Us=transport_p95,
        rustProcessingMedianUs=rust_median,
        rustProcessingP95Us=rust_p95,
        pythonValidationMedianUs=python_validation_median,
        pythonValidationP95Us=python_validation_p95,
        semanticMismatches=semantic_mismatches,
    )
    return stats, output_hash


def _write_embedding_database(
    path: Path,
    json_texts: list[str],
    blobs: list[bytes],
    *,
    dual_write: bool,
    mixed: bool,
    dimensions: int,
) -> None:
    with sqlite3.connect(path) as conn:
        if dual_write:
            conn.execute(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, embedding TEXT NOT NULL, "
                "embedding_blob BLOB, embedding_dimensions INTEGER NOT NULL DEFAULT 0, "
                "embedding_format TEXT NOT NULL DEFAULT '')"
            )
            rows = []
            for index, (embedding, blob) in enumerate(zip(json_texts, blobs)):
                legacy = mixed and index % 4 == 0
                rows.append(
                    (
                        index,
                        embedding,
                        None if legacy else sqlite3.Binary(blob),
                        0 if legacy else dimensions,
                        "" if legacy else semantic_cache.EMBEDDING_FORMAT_F64LE_V1,
                    )
                )
            conn.executemany("INSERT INTO items VALUES (?, ?, ?, ?, ?)", rows)
        else:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, embedding TEXT NOT NULL)")
            conn.executemany("INSERT INTO items VALUES (?, ?)", enumerate(json_texts))
        conn.commit()


def _semantic_cache_storage_comparison(
    base_url: str,
    scenario: Scenario,
    *,
    iterations: int,
    warmups: int,
    timeout: float,
) -> dict[str, Any]:
    query = scenario.payload["query"]
    source_candidates = scenario.payload["candidates"]
    dimensions = len(query)
    mixed = scenario.name == "mixed_blob_legacy_rows"
    json_texts: list[str] = []
    all_blobs: list[bytes] = []
    list_candidates: list[list[float]] = []
    for candidate in source_candidates:
        representations = semantic_cache.encode_embedding_representations(
            candidate,
            expected_dimensions=dimensions,
        )
        json_texts.append(representations.json_text)
        all_blobs.append(representations.blob)
        list_candidates.append(list(representations.values))
    storage_scenario = Scenario(
        name=scenario.name,
        component=scenario.component,
        payload={"query": query, "candidates": list_candidates},
    )
    expected = _python_vector(storage_scenario.payload)

    temp_dir = Path(tempfile.mkdtemp(prefix="semantic-cache-storage-benchmark-"))
    json_database = temp_dir / "json.sqlite3"
    dual_database = temp_dir / "dual.sqlite3"
    try:
        _write_embedding_database(
            json_database,
            json_texts,
            all_blobs,
            dual_write=False,
            mixed=False,
            dimensions=dimensions,
        )
        _write_embedding_database(
            dual_database,
            json_texts,
            all_blobs,
            dual_write=True,
            mixed=mixed,
            dimensions=dimensions,
        )
        json_database_bytes = json_database.stat().st_size
        dual_database_bytes = dual_database.stat().st_size
        with sqlite3.connect(dual_database) as conn:
            cached_dual_rows = conn.execute(
                "SELECT embedding, embedding_blob, embedding_dimensions, embedding_format FROM items ORDER BY id"
            ).fetchall()
        blob_arrays = [semantic_cache.decode_embedding_blob(blob, dimensions) for blob in all_blobs]

        def result(
            output: dict[str, Any] = expected,
            *,
            request_bytes: int = 0,
            response_bytes: int = 0,
            serialization_us: int | None = None,
            transport_us: int | None = None,
            rust_processing_us: int | None = None,
            python_validation_us: int | None = None,
            fallback: bool = False,
        ) -> VectorLayerResult:
            return VectorLayerResult(
                output=output,
                fallback=fallback,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                serialization_us=serialization_us,
                transport_us=transport_us,
                rust_processing_us=rust_processing_us,
                python_validation_us=python_validation_us,
            )

        def fetch_json() -> VectorLayerResult:
            with sqlite3.connect(json_database) as conn:
                rows = conn.execute("SELECT embedding FROM items ORDER BY id").fetchall()
            if len(rows) != len(list_candidates):
                raise RuntimeError("SQLite JSON fetch lost candidates")
            return result(request_bytes=json_database_bytes)

        def decode_json_lists() -> VectorLayerResult:
            decoded = [json.loads(value) for value in json_texts]
            if len(decoded) != len(list_candidates):
                raise RuntimeError("legacy JSON decode lost candidates")
            return result()

        def assemble_from_lists() -> VectorLayerResult:
            started_ns = time.perf_counter_ns()
            encoded = vector_binary.encode_rank_request(query, list_candidates)
            elapsed = max(0, (time.perf_counter_ns() - started_ns) // 1000)
            return result(request_bytes=len(encoded.body), serialization_us=elapsed)

        def fetch_blobs() -> VectorLayerResult:
            with sqlite3.connect(dual_database) as conn:
                rows = conn.execute(
                    "SELECT embedding_blob, embedding_dimensions, embedding_format FROM items ORDER BY id"
                ).fetchall()
            if len(rows) != len(list_candidates):
                raise RuntimeError("SQLite BLOB fetch lost candidates")
            return result(request_bytes=dual_database_bytes)

        def validated_blob_inputs(rows: list[Any]) -> tuple[list[bytes], list[Any]]:
            candidate_blobs: list[bytes] = []
            candidate_arrays: list[Any] = []
            for embedding, blob, stored_dimensions, embedding_format in rows:
                if embedding_format == semantic_cache.EMBEDDING_FORMAT_F64LE_V1:
                    values = semantic_cache.decode_embedding_blob(
                        blob,
                        stored_dimensions,
                        expected_dimensions=dimensions,
                    )
                    candidate_blobs.append(bytes(blob))
                    candidate_arrays.append(values)
                else:
                    values = json.loads(embedding)
                    representations = semantic_cache.encode_embedding_representations(
                        values,
                        expected_dimensions=dimensions,
                    )
                    candidate_blobs.append(representations.blob)
                    candidate_arrays.append(values)
            return candidate_blobs, candidate_arrays

        def validate_blobs() -> VectorLayerResult:
            candidate_blobs, candidate_arrays = validated_blob_inputs(cached_dual_rows)
            if len(candidate_blobs) != len(candidate_arrays) or len(candidate_blobs) != len(list_candidates):
                raise RuntimeError("BLOB validation lost candidates")
            return result()

        def assemble_from_blobs() -> VectorLayerResult:
            started_ns = time.perf_counter_ns()
            encoded = vector_binary.encode_rank_request_from_blobs(
                query,
                all_blobs,
                dimensions,
                blobs_validated=True,
            )
            elapsed = max(0, (time.perf_counter_ns() - started_ns) // 1000)
            return result(request_bytes=len(encoded.body), serialization_us=elapsed)

        def full_from_json() -> VectorLayerResult:
            with sqlite3.connect(json_database) as conn:
                candidates = [json.loads(row[0]) for row in conn.execute("SELECT embedding FROM items ORDER BY id")]
            previous = os.environ.get("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT")
            os.environ["DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT"] = "binary"
            try:
                ranked, used_rust = rag_client.rank_vectors(query, candidates)
                diagnostics = rag_client.last_delegate_diagnostics("rag_vector_rank")
            finally:
                if previous is None:
                    os.environ.pop("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", None)
                else:
                    os.environ["DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT"] = previous
            validation_started_ns = time.perf_counter_ns()
            expected_result = _python_vector({"query": query, "candidates": candidates})
            output = {"index": ranked[0], "similarity": ranked[1]} if ranked is not None else expected_result
            parity = used_rust and _vector_outputs_match(expected_result, output)
            validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
            return result(
                output if parity else expected_result,
                request_bytes=int(diagnostics.get("requestPayloadBytes") or 0),
                response_bytes=int(diagnostics.get("responsePayloadBytes") or 0),
                serialization_us=diagnostics.get("serializationUs") if isinstance(diagnostics.get("serializationUs"), int) else None,
                transport_us=diagnostics.get("transportUs") if isinstance(diagnostics.get("transportUs"), int) else None,
                rust_processing_us=diagnostics.get("rustProcessingUs") if isinstance(diagnostics.get("rustProcessingUs"), int) else None,
                python_validation_us=validation_us,
                fallback=not parity,
            )

        def full_from_blobs() -> VectorLayerResult:
            with sqlite3.connect(dual_database) as conn:
                rows = conn.execute(
                    "SELECT embedding, embedding_blob, embedding_dimensions, embedding_format FROM items ORDER BY id"
                ).fetchall()
            candidate_blobs, candidate_arrays = validated_blob_inputs(rows)
            previous = os.environ.get("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT")
            os.environ["DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT"] = "binary"
            try:
                ranked, used_rust = rag_client.rank_vectors_from_blobs(
                    query,
                    candidate_blobs,
                    dimensions=dimensions,
                    blobs_validated=True,
                )
                diagnostics = rag_client.last_delegate_diagnostics("rag_vector_rank")
            finally:
                if previous is None:
                    os.environ.pop("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", None)
                else:
                    os.environ["DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT"] = previous
            validation_started_ns = time.perf_counter_ns()
            expected_result = _python_vector({"query": query, "candidates": candidate_arrays})
            output = {"index": ranked[0], "similarity": ranked[1]} if ranked is not None else expected_result
            parity = used_rust and _vector_outputs_match(expected_result, output)
            validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
            return result(
                output if parity else expected_result,
                request_bytes=int(diagnostics.get("requestPayloadBytes") or 0),
                response_bytes=int(diagnostics.get("responsePayloadBytes") or 0),
                serialization_us=diagnostics.get("serializationUs") if isinstance(diagnostics.get("serializationUs"), int) else None,
                transport_us=diagnostics.get("transportUs") if isinstance(diagnostics.get("transportUs"), int) else None,
                rust_processing_us=diagnostics.get("rustProcessingUs") if isinstance(diagnostics.get("rustProcessingUs"), int) else None,
                python_validation_us=validation_us,
                fallback=not parity,
            )

        def python_from_json() -> VectorLayerResult:
            candidates = [json.loads(value) for value in json_texts]
            return result(_python_vector({"query": query, "candidates": candidates}))

        def python_from_blob_arrays() -> VectorLayerResult:
            return result(_python_vector({"query": query, "candidates": blob_arrays}))

        calls: dict[str, Callable[[], VectorLayerResult]] = {
            "sqliteJsonFetch": fetch_json,
            "legacyJsonDecode": decode_json_lists,
            "listBinaryAssembly": assemble_from_lists,
            "sqliteBlobFetch": fetch_blobs,
            "blobValidation": validate_blobs,
            "directBlobAssembly": assemble_from_blobs,
            "warmBinaryHttpFromLists": lambda: _vector_binary_http(base_url, storage_scenario, timeout),
            "warmBinaryHttpFromBlobs": lambda: _vector_binary_blob_http(
                base_url,
                query,
                all_blobs,
                dimensions,
                timeout,
            ),
            "fullShadowIntegrationFromJson": full_from_json,
            "fullShadowIntegrationFromBlobs": full_from_blobs,
            "pythonDirectFromJson": python_from_json,
            "pythonDirectFromBlobArrays": python_from_blob_arrays,
        }
        layers: dict[str, dict[str, Any]] = {}
        for name, call in calls.items():
            layer, _ = _measure_vector_layer(
                call,
                iterations=iterations,
                warmups=warmups,
                expected_output=expected,
            )
            layers[name] = layer

        list_request = vector_binary.encode_rank_request(query, list_candidates).body
        blob_request = vector_binary.encode_rank_request_from_blobs(
            query,
            all_blobs,
            dimensions,
            blobs_validated=True,
        ).body
        legacy_assembly_us = float(layers["legacyJsonDecode"]["medianUs"]) + float(
            layers["listBinaryAssembly"]["medianUs"]
        )
        blob_assembly_us = float(layers["directBlobAssembly"]["medianUs"])
        zero_errors = all(not layer.get("errors") for layer in layers.values())
        zero_fallbacks = all(not layer.get("fallbacks") for layer in layers.values())
        valid_blob_candidates = len(list_candidates) - (len(list_candidates) + 3) // 4 if mixed else len(list_candidates)
        legacy_candidates = len(list_candidates) - valid_blob_candidates
        increase = dual_database_bytes - json_database_bytes
        return {
            "layers": layers,
            "semanticParity": all(not layer.get("semanticMismatches") for layer in layers.values()),
            "databaseBytes": {
                "jsonOnly": json_database_bytes,
                "dualWrite": dual_database_bytes,
                "increase": increase,
                "increasePercent": round((increase / json_database_bytes) * 100.0, 3) if json_database_bytes else 0.0,
            },
            "candidateStorage": {
                "blobCandidates": valid_blob_candidates,
                "legacyCandidates": legacy_candidates,
                "mixed": mixed,
            },
            "timingBreakdownUs": {
                "fetchUs": {
                    "json": layers["sqliteJsonFetch"]["medianUs"],
                    "blob": layers["sqliteBlobFetch"]["medianUs"],
                },
                "legacyDecodeUs": layers["legacyJsonDecode"]["medianUs"],
                "blobValidationUs": layers["blobValidation"]["medianUs"],
                "payloadAssemblyUs": {
                    "list": layers["listBinaryAssembly"]["medianUs"],
                    "blob": layers["directBlobAssembly"]["medianUs"],
                },
                "transportUs": {
                    "lists": layers["warmBinaryHttpFromLists"]["transportMedianUs"],
                    "blobs": layers["warmBinaryHttpFromBlobs"]["transportMedianUs"],
                },
                "rustProcessingUs": {
                    "lists": layers["warmBinaryHttpFromLists"]["rustProcessingMedianUs"],
                    "blobs": layers["warmBinaryHttpFromBlobs"]["rustProcessingMedianUs"],
                },
                "pythonValidationUs": {
                    "json": layers["fullShadowIntegrationFromJson"]["pythonValidationMedianUs"],
                    "blob": layers["fullShadowIntegrationFromBlobs"]["pythonValidationMedianUs"],
                },
                "totalUs": {
                    "json": layers["fullShadowIntegrationFromJson"]["medianUs"],
                    "blob": layers["fullShadowIntegrationFromBlobs"]["medianUs"],
                },
            },
            "gates": {
                "requestBytesIdentical": bytes(list_request) == bytes(blob_request),
                "directBlobPathAvoidsJsonLoads": True,
                "directBlobPathAvoidsCandidateListOfLists": True,
                "zeroErrors": zero_errors,
                "zeroUnexpectedFallbacks": zero_fallbacks,
                "blobAssemblyFasterThanLegacyJsonListAssembly": blob_assembly_us < legacy_assembly_us,
            },
            "redaction": {"vectorValuesStored": False},
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def full_integration(scenario: Scenario) -> CallResult:
    payload = scenario.payload
    if scenario.component == "gateway_prepare":
        try:
            gateway_decision = prepare_request_with_optional_rust(payload)
            return CallResult(
                {"ok": True, "request": gateway_decision.request},
                fallback=bool(gateway_decision.diagnostics.get("fallback")),
            )
        except AppError as exc:
            code = getattr(exc.code, "value", exc.code)
            return CallResult({"ok": False, "code": str(code)})
    if scenario.component == "mcp_prepare":
        mcp_decision = prepare_mcp_protocol_with_optional_rust(payload)
        return CallResult(mcp_decision.preparation, fallback=bool(mcp_decision.diagnostics.get("fallback")))
    if scenario.component == "rag_document_prepare":
        document_decision = prepare_rag_document_with_optional_rust(payload)
        return CallResult(document_decision.preparation, fallback=bool(document_decision.diagnostics.get("fallback")))
    if scenario.component == "rag_vector_rank":
        ranked, used_rust = rag_client.rank_vectors(payload["query"], payload["candidates"])
        expected = _python_vector(payload)
        candidate = {"index": ranked[0], "similarity": ranked[1]} if used_rust and ranked is not None else expected
        parity = _vector_outputs_match(expected, candidate)
        return CallResult(candidate if parity else expected, fallback=not used_rust or not parity)
    if scenario.component == "policy_url":
        result = policy_client.check_url(str(payload["url"]))
    elif scenario.component == "policy_path":
        result = policy_client.check_path(str(payload["root"]), str(payload["requested"]))
    else:
        result = policy_client.check_capability(
            str(payload["requested"]),
            [str(item) for item in payload["granted"]],
            str(payload["max_risk"]),
        )
    candidate = {
        "allowed": result.allowed,
        "code": result.code,
        "capability": result.capability,
        "risk_level": result.risk_level,
    }
    expected = _python_policy(scenario.component, payload)
    parity = semantic_hash(scenario.component, candidate) == semantic_hash(scenario.component, expected)
    return CallResult(candidate if parity else expected, fallback=not result.ok or not parity)


def endpoint_for(component: str) -> str:
    return {
        "gateway_prepare": "/gateway/request/prepare",
        "mcp_prepare": "/mcp/request/prepare",
        "policy_url": "/policy/url",
        "policy_path": "/policy/path",
        "policy_capability": "/policy/capability",
        "rag_vector_rank": "/rag/vectors/rank",
        "rag_document_prepare": "/rag/documents/prepare",
    }[component]


def _release_binary(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return ROOT / "rust" / "target" / "release" / f"{name}{suffix}"


def build_release() -> None:
    subprocess.run(BUILD_COMMAND, cwd=ROOT, check=True, timeout=900)
    for name in ("deepseek-gateway", "sidecar_core_bench"):
        if not _release_binary(name).is_file():
            raise RuntimeError(f"release binary was not produced: {name}")


def pure_rust(core_binary: Path, scenario: Scenario, *, iterations: int, warmups: int, timeout: float) -> tuple[dict[str, Any], str]:
    command = {
        "component": scenario.component,
        "payload": scenario.payload,
        "warmups": warmups,
        "iterations": iterations,
    }
    completed = subprocess.run(
        [str(core_binary)],
        cwd=ROOT,
        input=json.dumps(command, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=timeout,
    )
    value = json.loads(completed.stdout)
    if value.get("profile") != "release":
        raise RuntimeError("pure Rust benchmark helper is not a release binary")
    samples = [float(item) for item in value.get("samplesUs") or []]
    stats = _stats(
        samples,
        iterations=iterations,
        warmups=warmups,
        input_bytes=len(_json_bytes(scenario.payload)),
        output_bytes=int(value.get("outputBytes") or 0),
        errors=int(value.get("errors") or 0),
        fallbacks=0,
    )
    return stats, str(value.get("semanticHash") or "")


def _available_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


class SidecarProcess:
    def __init__(self, binary: Path, timeout: float) -> None:
        self.binary = binary
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.base_url = ""
        self.launch_us = 0
        self.health_ready_us = 0

    def start(self) -> None:
        port = _available_port()
        self.base_url = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
        env.setdefault("RUST_LOG", "deepseek_gateway=info")
        started_ns = time.perf_counter_ns()
        self.process = subprocess.Popen(
            [str(self.binary)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.launch_us = max(0, (time.perf_counter_ns() - started_ns) // 1000)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("release Rust sidecar exited before health readiness")
            try:
                with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=0.5) as response:  # noqa: S310 - local process
                    if response.status == 200:
                        self.health_ready_us = max(0, (time.perf_counter_ns() - started_ns) // 1000)
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        raise RuntimeError("release Rust sidecar did not become healthy before timeout")

    def close(self) -> None:
        transport.reset_persistent_clients()
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def __enter__(self) -> "SidecarProcess":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@contextmanager
def rust_environment(base_url: str) -> Iterator[None]:
    names = {
        "DEEPSEEK_RUST_GATEWAY": "1",
        "DEEPSEEK_RUST_MCP": "1",
        "DEEPSEEK_RUST_POLICY": "1",
        "DEEPSEEK_RUST_RAG": "1",
        "DEEPSEEK_RUST_RAG_DOCUMENT_PREP": "1",
        "DEEPSEEK_RUST_GATEWAY_FALLBACK": "1",
        "DEEPSEEK_RUST_MCP_FALLBACK": "1",
        "DEEPSEEK_RUST_RAG_FALLBACK": "1",
        "DEEPSEEK_RUST_POLICY_FAILURE_MODE": "fallback",
        "DEEPSEEK_RUST_GATEWAY_URL": base_url,
    }
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    try:
        yield
    finally:
        transport.reset_persistent_clients()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _vector_payload(candidates: int, dimensions: int, *, tie: bool = False) -> dict[str, Any]:
    if tie:
        query = [1.0] + [0.0] * (dimensions - 1)
        tie_candidates = [[0.9] + [0.0] * (dimensions - 1) for _ in range(candidates)]
        return {"query": query, "candidates": tie_candidates}
    query = [round((((index + 29) * 12_345) % 19_999 - 9_999) / 1_000_000.0, 6) for index in range(dimensions)]
    candidate_vectors: list[list[float]] = []
    for candidate_index in range(candidates):
        scale = 0.35 + (0.6 * candidate_index / max(1, candidates - 1))
        candidate_vectors.append([round(value * scale, 6) for value in query])
    return {"query": query, "candidates": candidate_vectors}


def scenarios() -> dict[str, list[Scenario]]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "synthetic benchmark tool",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
            },
        }
        for index in range(16)
    ]
    gateway = [
        Scenario("minimal_request", "gateway_prepare", {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hello"}]}),
        Scenario(
            "multi_turn_request",
            "gateway_prepare",
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn {index}"} for index in range(20)
                ],
            },
        ),
        Scenario(
            "tools_heavy_request",
            "gateway_prepare",
            {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "use tools"}], "tools": tools, "tool_choice": "auto"},
        ),
        Scenario(
            "large_multipart_content",
            "gateway_prepare",
            {
                "model": "deepseek-v4-pro",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "multipart benchmark " * 5000},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 40_000}},
                        ],
                    }
                ],
            },
        ),
        Scenario("invalid_request", "gateway_prepare", {"model": "", "messages": [{"role": "user", "content": "hello"}]}),
    ]
    mcp = [
        Scenario(
            "initialize",
            "mcp_prepare",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "benchmark", "version": "1"}},
            },
        ),
        Scenario("tools_list", "mcp_prepare", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        Scenario(
            "tools_call_small_arguments",
            "mcp_prepare",
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "synthetic", "arguments": {"value": "small"}}},
        ),
        Scenario(
            "tools_call_large_nested_arguments",
            "mcp_prepare",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "synthetic", "arguments": {"items": [{"values": list(range(100))} for _ in range(200)]}},
            },
        ),
        Scenario("invalid_json_rpc", "mcp_prepare", {"jsonrpc": "1.0", "id": 5, "method": "tools/list"}),
    ]
    policy = [
        Scenario("safe_public_url", "policy_url", {"url": "https://example.com/resource"}),
        Scenario("private_loopback_url", "policy_url", {"url": "http://127.0.0.1/private"}),
        Scenario("path_traversal", "policy_path", {"root": "/workspace", "requested": "../secret.txt"}),
        Scenario("safe_path", "policy_path", {"root": "/workspace", "requested": "project/readme.txt"}),
        Scenario("capability_allow", "policy_capability", {"requested": "ReadFile", "granted": ["ReadFile"], "max_risk": "Low"}),
        Scenario("capability_deny", "policy_capability", {"requested": "ShellExec", "granted": ["ReadFile"], "max_risk": "Critical"}),
    ]
    vectors = [
        Scenario("16_candidates_x_384_dimensions", "rag_vector_rank", _vector_payload(16, 384)),
        Scenario("128_candidates_x_768_dimensions", "rag_vector_rank", _vector_payload(128, 768)),
        Scenario("1000_candidates_x_1536_dimensions", "rag_vector_rank", _vector_payload(1000, 1536)),
        Scenario("mixed_blob_legacy_rows", "rag_vector_rank", _vector_payload(128, 768)),
        Scenario("ties_first_match", "rag_vector_rank", _vector_payload(3, 16, tie=True)),
    ]
    documents = [
        Scenario("small", "rag_document_prepare", _document_payload("short document\n" * 64, 6000, 400)),
        Scenario("medium", "rag_document_prepare", _document_payload(("medium paragraph alpha beta\n\n" * 3000)[:75_000], 6000, 400)),
        Scenario("large", "rag_document_prepare", _document_payload(("large deterministic document line\n" * 30_000)[:750_000], 6000, 400)),
        Scenario("high_overlap", "rag_document_prepare", _document_payload(("overlap line\n" * 10_000)[:120_000], 2000, 1500)),
        Scenario("cjk_heavy", "rag_document_prepare", _document_payload(("中文文档分块测试，稳定字符偏移。\n" * 10_000)[:120_000], 6000, 400)),
        Scenario("emoji_non_bmp_heavy", "rag_document_prepare", _document_payload(("🚀🙂✨ non-BMP benchmark line\n" * 10_000)[:120_000], 6000, 400)),
    ]
    return {
        "gateway_request_preparation": gateway,
        "mcp_protocol_preparation": mcp,
        "tool_policy": policy,
        "rag_vector_ranking": vectors,
        "rag_document_preparation": documents,
    }


def _document_payload(text: str, chunk_chars: int, overlap: int) -> dict[str, Any]:
    return {
        "documentId": "synthetic-benchmark-document",
        "text": text,
        "metadata": {"displayName": "synthetic.txt", "sourceType": "text/plain"},
        "chunking": {"chunkChars": chunk_chars, "chunkOverlap": overlap},
    }


def _concurrency_measure(
    call: Callable[[], CallResult],
    *,
    concurrency: int,
    iterations: int,
    warmups: int,
    input_bytes: int,
) -> dict[str, Any]:
    for _ in range(warmups):
        call()
    request_count = max(iterations, concurrency)

    def one() -> tuple[float, CallResult | None]:
        started_ns = time.perf_counter_ns()
        try:
            result = call()
        except Exception:
            result = None
        return max(0.0, (time.perf_counter_ns() - started_ns) / 1000.0), result

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        values = list(executor.map(lambda _index: one(), range(request_count)))
    wall = time.perf_counter() - started
    errors = sum(result is None for _sample, result in values)
    fallbacks = sum(bool(result and result.fallback) for _sample, result in values)
    outputs = [result.output for _sample, result in values if result is not None]
    return {
        "concurrency": concurrency,
        **_stats(
            [sample for sample, _result in values],
            iterations=request_count,
            warmups=warmups,
            input_bytes=input_bytes,
            output_bytes=len(_json_bytes(outputs[-1])) if outputs else 0,
            errors=errors,
            fallbacks=fallbacks,
            wall_seconds=wall,
        ),
        "connectionCount": transport.transport_stats().connections_created,
    }


def _machine_info(warmups: int, iterations: int, concurrency: list[int]) -> dict[str, Any]:
    rust_version = subprocess.run(["rustc", "-Vv"], cwd=ROOT, text=True, capture_output=True, check=True, timeout=30).stdout
    target = next((line.split(":", 1)[1].strip() for line in rust_version.splitlines() if line.startswith("host:")), "unknown")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True, timeout=30).stdout.strip()
    return {
        "rustProfile": "release",
        "buildCommand": BUILD_COMMAND,
        "rustVersion": rust_version.splitlines()[0] if rust_version else "unknown",
        "targetTriple": target,
        "pythonVersion": platform.python_version(),
        "operatingSystem": platform.platform(),
        "cpuLogicalCount": os.cpu_count(),
        "commitSha": commit,
        "warmupCount": warmups,
        "iterationCount": iterations,
        "concurrency": concurrency,
    }


def _stable_evidence(report: dict[str, Any]) -> dict[str, Any]:
    evidence = copy.deepcopy(report)
    machine = evidence["machine"]
    machine["operatingSystem"] = platform.system()
    machine["privacyRedacted"] = True
    evidence["artifactContainsFullMachineInfo"] = True
    return evidence


def validate_report(report: dict[str, Any]) -> None:
    if report.get("schemaVersion") != SCHEMA_VERSION or report.get("version") != VERSION:
        raise RuntimeError("benchmark report version/schema mismatch")
    if report.get("status") != "PASS":
        raise RuntimeError("benchmark report did not pass its contract gates")
    if report.get("machine", {}).get("rustProfile") != "release":
        raise RuntimeError("formal benchmark must use the release profile")
    delegates = report.get("delegates")
    if not isinstance(delegates, list) or {item.get("delegate") for item in delegates} != set(DELEGATE_COMPONENTS):
        raise RuntimeError("benchmark report does not cover all current delegates")
    for delegate in delegates:
        expected_components = set(DELEGATE_COMPONENTS[str(delegate["delegate"])])
        observed_components = {scenario.get("component") for scenario in delegate.get("scenarios") or []}
        if not expected_components.issubset(observed_components):
            raise RuntimeError(f"benchmark delegate is missing components: {delegate['delegate']}")
        if {item.get("concurrency") for item in delegate.get("concurrency") or []} != {1, 8, 32}:
            raise RuntimeError(f"benchmark delegate is missing concurrency coverage: {delegate['delegate']}")
        for scenario in delegate.get("scenarios") or []:
            layers = scenario.get("layers") or {}
            if set(layers) != {"pythonBaseline", "pureRustCore", "releaseSidecarHttp", "fullPythonIntegration"}:
                raise RuntimeError(f"benchmark scenario layers are incomplete: {scenario.get('name')}")
            if not scenario.get("semanticParity"):
                raise RuntimeError(f"benchmark semantic parity failed: {scenario.get('name')}")
            for layer in layers.values():
                if layer.get("errors") or layer.get("fallbacks"):
                    raise RuntimeError(f"benchmark errors/fallbacks are not allowed: {scenario.get('name')}")
            if delegate.get("delegate") == "rag_vector_ranking":
                comparison = scenario.get("transportComparison") or {}
                required_layers = {
                    "pythonDirect",
                    "pureRustCore",
                    "jsonSerialization",
                    "binarySerialization",
                    "warmJsonHttp",
                    "warmBinaryHttp",
                    "fullJsonIntegration",
                    "fullBinaryIntegration",
                }
                if set(comparison.get("layers") or {}) != required_layers:
                    raise RuntimeError(f"vector transport comparison is incomplete: {scenario.get('name')}")
                if not comparison.get("semanticParity"):
                    raise RuntimeError(f"vector transport semantic parity failed: {scenario.get('name')}")
                for layer in (comparison.get("layers") or {}).values():
                    if layer.get("errors") or layer.get("fallbacks"):
                        raise RuntimeError(f"vector transport errors/fallbacks are not allowed: {scenario.get('name')}")
                binary_response = comparison["layers"]["warmBinaryHttp"].get("outputBytes")
                if binary_response != vector_binary.RESPONSE_BYTES:
                    raise RuntimeError(f"binary response size is not fixed: {scenario.get('name')}")
                if scenario.get("name") == "1000_candidates_x_1536_dimensions":
                    json_bytes = comparison["layers"]["jsonSerialization"].get("inputBytes")
                    binary_bytes = comparison["layers"]["binarySerialization"].get("inputBytes")
                    if not isinstance(json_bytes, int) or not isinstance(binary_bytes, int) or binary_bytes >= json_bytes:
                        raise RuntimeError("large binary vector request is not smaller than JSON")
                if scenario.get("name") != "ties_first_match":
                    storage = scenario.get("semanticCacheStorage") or {}
                    required_storage_layers = {
                        "sqliteJsonFetch",
                        "legacyJsonDecode",
                        "listBinaryAssembly",
                        "sqliteBlobFetch",
                        "blobValidation",
                        "directBlobAssembly",
                        "warmBinaryHttpFromLists",
                        "warmBinaryHttpFromBlobs",
                        "fullShadowIntegrationFromJson",
                        "fullShadowIntegrationFromBlobs",
                        "pythonDirectFromJson",
                        "pythonDirectFromBlobArrays",
                    }
                    if set(storage.get("layers") or {}) != required_storage_layers:
                        raise RuntimeError(f"semantic-cache storage comparison is incomplete: {scenario.get('name')}")
                    if not storage.get("semanticParity"):
                        raise RuntimeError(f"semantic-cache storage parity failed: {scenario.get('name')}")
                    if any(layer.get("errors") or layer.get("fallbacks") for layer in storage["layers"].values()):
                        raise RuntimeError(f"semantic-cache storage errors/fallbacks are not allowed: {scenario.get('name')}")
                    gates = storage.get("gates") or {}
                    if not all(
                        gates.get(name)
                        for name in (
                            "requestBytesIdentical",
                            "directBlobPathAvoidsJsonLoads",
                            "directBlobPathAvoidsCandidateListOfLists",
                            "zeroErrors",
                            "zeroUnexpectedFallbacks",
                        )
                    ):
                        raise RuntimeError(f"semantic-cache storage contract gate failed: {scenario.get('name')}")
                    database_bytes = storage.get("databaseBytes") or {}
                    if int(database_bytes.get("dualWrite") or 0) <= int(database_bytes.get("jsonOnly") or 0):
                        raise RuntimeError(f"semantic-cache dual-write storage overhead is missing: {scenario.get('name')}")
                    if storage.get("redaction", {}).get("vectorValuesStored") is not False:
                        raise RuntimeError(f"semantic-cache storage report redaction failed: {scenario.get('name')}")
                    if scenario.get("name") == "1000_candidates_x_1536_dimensions" and not gates.get(
                        "blobAssemblyFasterThanLegacyJsonListAssembly"
                    ):
                        raise RuntimeError("large direct BLOB assembly is not faster than legacy JSON/list assembly")
        if delegate.get("delegate") == "rag_vector_ranking":
            comparison_concurrency = delegate.get("transportConcurrency") or {}
            if set(comparison_concurrency) != {"json", "binary"}:
                raise RuntimeError("vector transport concurrency comparison is incomplete")
            for encoding, values in comparison_concurrency.items():
                if {item.get("concurrency") for item in values} != {1, 8, 32}:
                    raise RuntimeError(f"vector {encoding} concurrency coverage is incomplete")
                if any(item.get("errors") or item.get("fallbacks") for item in values):
                    raise RuntimeError(f"vector {encoding} concurrency errors/fallbacks are not allowed")
    rendered = json.dumps(report, ensure_ascii=False).lower()
    forbidden = ("authorization", "api_key", "bearer ", "topsecret", "tool arguments", "document body")
    if any(value in rendered for value in forbidden):
        raise RuntimeError("benchmark report contains a forbidden sensitive marker")


def run_benchmark(*, iterations: int, warmups: int, concurrency: list[int], timeout: float, skip_build: bool = False) -> dict[str, Any]:
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be in 1..{MAX_ITERATIONS}")
    if not 0 <= warmups <= MAX_WARMUPS:
        raise ValueError(f"warmups must be in 0..{MAX_WARMUPS}")
    if sorted(set(concurrency)) != [1, 8, 32] or max(concurrency) > MAX_CONCURRENCY:
        raise ValueError("formal benchmark concurrency must be exactly 1,8,32")
    if not skip_build:
        build_release()
    sidecar_binary = _release_binary("deepseek-gateway")
    core_binary = _release_binary("sidecar_core_bench")
    machine = _machine_info(warmups, iterations, concurrency)
    suites = scenarios()

    with SidecarProcess(sidecar_binary, timeout) as sidecar, rust_environment(sidecar.base_url):
        cold_scenario = suites["gateway_request_preparation"][0]
        transport.reset_persistent_clients()
        first_started_ns = time.perf_counter_ns()
        first = http_call(sidecar.base_url, cold_scenario, timeout)
        first_request_us = max(0, (time.perf_counter_ns() - first_started_ns) // 1000)
        cold = {
            "component": cold_scenario.component,
            "scenario": cold_scenario.name,
            "processLaunchUs": sidecar.launch_us,
            "healthReadyUs": sidecar.health_ready_us,
            "firstRequestUs": first_request_us,
            "firstRequestSemanticHash": semantic_hash(cold_scenario.component, first.output),
            "includedInWarmResults": False,
        }

        delegate_reports: list[dict[str, Any]] = []
        for delegate, delegate_scenarios in suites.items():
            scenario_reports: list[dict[str, Any]] = []
            for scenario in delegate_scenarios:
                input_bytes = len(_json_bytes(scenario.payload))
                python_stats, _ = _measure(
                    lambda: python_baseline(scenario),
                    iterations=iterations,
                    warmups=warmups,
                    input_bytes=input_bytes,
                )
                python_output = python_baseline(scenario).output
                python_signature = semantic_hash(scenario.component, python_output)
                pure_stats, pure_signature = pure_rust(
                    core_binary,
                    scenario,
                    iterations=iterations,
                    warmups=warmups,
                    timeout=max(timeout, 120.0),
                )
                transport.reset_persistent_clients()
                http_stats, _ = _measure(
                    lambda: http_call(sidecar.base_url, scenario, timeout),
                    iterations=iterations,
                    warmups=warmups,
                    input_bytes=input_bytes,
                )
                http_output = http_call(sidecar.base_url, scenario, timeout).output
                http_signature = semantic_hash(scenario.component, http_output)
                transport.reset_persistent_clients()
                integration_stats, _ = _measure(
                    lambda: full_integration(scenario),
                    iterations=iterations,
                    warmups=warmups,
                    input_bytes=input_bytes,
                )
                integration_signature = semantic_hash(scenario.component, full_integration(scenario).output)
                signatures = {python_signature, pure_signature, http_signature, integration_signature}
                semantic_parity = len(signatures) == 1
                if scenario.component == "rag_vector_rank":
                    semantic_parity = (
                        isinstance(python_output, dict)
                        and isinstance(http_output, dict)
                        and _vector_outputs_match(python_output, http_output)
                        and len({pure_signature, http_signature, integration_signature}) == 1
                    )
                scenario_report = {
                    "name": scenario.name,
                    "component": scenario.component,
                    "inputBytes": input_bytes,
                    "layers": {
                        "pythonBaseline": python_stats,
                        "pureRustCore": pure_stats,
                        "releaseSidecarHttp": http_stats,
                        "fullPythonIntegration": integration_stats,
                    },
                    "semanticParity": semantic_parity,
                    "semanticHash": python_signature,
                }
                if scenario.component == "rag_vector_rank":
                    json_serialization, _ = _measure_vector_layer(
                        lambda: _vector_json_serialization(scenario),
                        iterations=iterations,
                        warmups=warmups,
                    )
                    binary_serialization, _ = _measure_vector_layer(
                        lambda: _vector_binary_serialization(scenario),
                        iterations=iterations,
                        warmups=warmups,
                    )
                    transport.reset_persistent_clients()
                    warm_json, warm_json_hash = _measure_vector_layer(
                        lambda: _vector_json_http(sidecar.base_url, scenario, timeout),
                        iterations=iterations,
                        warmups=warmups,
                    )
                    transport.reset_persistent_clients()
                    warm_binary, warm_binary_hash = _measure_vector_layer(
                        lambda: _vector_binary_http(sidecar.base_url, scenario, timeout),
                        iterations=iterations,
                        warmups=warmups,
                    )
                    transport.reset_persistent_clients()
                    full_json, full_json_hash = _measure_vector_layer(
                        lambda: _vector_full_integration(scenario, "json"),
                        iterations=iterations,
                        warmups=warmups,
                    )
                    transport.reset_persistent_clients()
                    full_binary, full_binary_hash = _measure_vector_layer(
                        lambda: _vector_full_integration(scenario, "binary"),
                        iterations=iterations,
                        warmups=warmups,
                    )
                    comparison_signatures = {
                        pure_signature,
                        warm_json_hash,
                        warm_binary_hash,
                        full_json_hash,
                        full_binary_hash,
                    }
                    json_request_bytes = int(json_serialization["inputBytes"])
                    binary_request_bytes = int(binary_serialization["inputBytes"])
                    reduction_percent = (
                        round((1.0 - binary_request_bytes / json_request_bytes) * 100.0, 3) if json_request_bytes else 0.0
                    )
                    scenario_report["transportComparison"] = {
                        "layers": {
                            "pythonDirect": python_stats,
                            "pureRustCore": pure_stats,
                            "jsonSerialization": json_serialization,
                            "binarySerialization": binary_serialization,
                            "warmJsonHttp": warm_json,
                            "warmBinaryHttp": warm_binary,
                            "fullJsonIntegration": full_json,
                            "fullBinaryIntegration": full_binary,
                        },
                        "semanticParity": len(comparison_signatures) == 1 and semantic_parity,
                        "jsonRequestBytes": json_request_bytes,
                        "binaryRequestBytes": binary_request_bytes,
                        "binaryResponseBytes": int(warm_binary["outputBytes"]),
                        "requestReductionPercent": reduction_percent,
                    }
                    if scenario.name != "ties_first_match":
                        transport.reset_persistent_clients()
                        scenario_report["semanticCacheStorage"] = _semantic_cache_storage_comparison(
                            sidecar.base_url,
                            scenario,
                            iterations=iterations,
                            warmups=warmups,
                            timeout=timeout,
                        )
                scenario_reports.append(scenario_report)

            representative = delegate_scenarios[0]
            concurrency_reports: list[dict[str, Any]] = []
            for level in concurrency:
                transport.reset_persistent_clients()
                concurrency_reports.append(
                    _concurrency_measure(
                        lambda: http_call(sidecar.base_url, representative, timeout),
                        concurrency=level,
                        iterations=iterations,
                        warmups=warmups,
                        input_bytes=len(_json_bytes(representative.payload)),
                    )
                )
            delegate_report: dict[str, Any] = {
                "delegate": delegate,
                "components": list(DELEGATE_COMPONENTS[delegate]),
                "scenarios": scenario_reports,
                "concurrencyScenario": representative.name,
                "concurrency": concurrency_reports,
            }
            if delegate == "rag_vector_ranking":
                binary_concurrency: list[dict[str, Any]] = []
                binary_input_bytes = len(
                    vector_binary.encode_rank_request(
                        representative.payload["query"], representative.payload["candidates"]
                    ).body
                )
                for level in concurrency:
                    transport.reset_persistent_clients()
                    binary_concurrency.append(
                        _concurrency_measure(
                            lambda: _vector_concurrency_call(sidecar.base_url, representative, timeout, "binary"),
                            concurrency=level,
                            iterations=iterations,
                            warmups=warmups,
                            input_bytes=binary_input_bytes,
                        )
                    )
                delegate_report["transportConcurrency"] = {
                    "json": concurrency_reports,
                    "binary": binary_concurrency,
                }
            delegate_reports.append(delegate_report)

        with urllib.request.urlopen(f"{sidecar.base_url}/metrics", timeout=timeout) as response:  # noqa: S310 - local sidecar
            metrics_text = response.read().decode("utf-8")

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "version": VERSION,
        "commit": machine["commitSha"],
        "status": "PASS",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "informationalOnly": True,
        "absoluteLatencyMergeGate": False,
        "machine": machine,
        "coldStart": cold,
        "delegates": delegate_reports,
        "observability": {
            "metricsEndpoint": "/metrics",
            "requiredMetricNamesPresent": all(
                name in metrics_text
                for name in (
                    "requests_total",
                    "request_duration_seconds",
                    "request_payload_bytes",
                    "response_payload_bytes",
                    "backend_errors_total",
                    "vector_rank_transport_total",
                )
            ),
            "componentAllowlist": [component for values in DELEGATE_COMPONENTS.values() for component in values],
        },
        "connectionReuse": {
            "enabled": True,
            "poolImplementation": "python_stdlib_http_client",
            "maxConnections": int(os.environ.get("DEEPSEEK_RUST_SIDECAR_MAX_CONNECTIONS", "32")),
            "forkAware": True,
            "resettable": True,
        },
        "defaults": {
            "pythonAuthoritative": True,
            "rustDelegatesEnabledByDefault": False,
            "rustSidecarInDefaultCompose": False,
            "pythonFallbackRetained": True,
            "persistenceOwner": "python",
            "vectorTransportDefault": "json",
            "automaticTransportSelection": False,
        },
        "redaction": {
            "payloadsStored": False,
            "toolArgumentsStored": False,
            "documentTextStored": False,
            "urlsOrPathsStored": False,
            "credentialsStored": False,
            "vectorValuesStored": False,
        },
    }
    validate_report(report)
    return report


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_concurrency(value: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("concurrency must be comma-separated integers") from exc
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--concurrency", type=parse_concurrency, default=[1, 8, 32])
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--artifact-out", type=Path, default=ROOT / "artifacts/rust-sidecar-performance.json")
    parser.add_argument("--evidence-out", type=Path, default=ROOT / "docs/evidence/rust-sidecar-performance-v4.0.0-rc.2.json")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(
            iterations=args.iterations,
            warmups=args.warmups,
            concurrency=args.concurrency,
            timeout=args.timeout,
            skip_build=args.skip_build,
        )
        _write(args.artifact_out, report)
        _write(args.evidence_out, _stable_evidence(report))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Rust sidecar release benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"artifact": str(args.artifact_out), "evidence": str(args.evidence_out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
