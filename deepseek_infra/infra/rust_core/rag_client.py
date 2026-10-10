"""HTTP proxy client for Rust-backed RAG hot paths."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

from deepseek_infra.infra.rust_core import transport, vector_binary
from deepseek_infra.infra.rust_core.config import (
    rust_gateway_url,
    rust_rag_vector_transport,
    rust_rag_vector_transport_invalid,
)

DEFAULT_RAG_TIMEOUT_MS = 3000
_delegate_state = threading.local()


@dataclass(frozen=True)
class RagProxyResult:
    ok: bool
    status: int
    body: Any
    error_kind: str = ""
    latency_ms: int = 0
    serialization_us: int | None = None
    transport_us: int | None = None
    rust_processing_us: int | None = None
    total_us: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    correlation_id: str = ""
    connection_reused: bool | None = None
    connection_count: int | None = None


def _set_delegate_diagnostics(component: str, diagnostics: dict[str, Any]) -> None:
    values = getattr(_delegate_state, "diagnostics", None)
    if not isinstance(values, dict):
        values = {}
        _delegate_state.diagnostics = values
    values[component] = dict(diagnostics)


def update_delegate_diagnostics(component: str, **values: Any) -> None:
    diagnostics = last_delegate_diagnostics(component)
    diagnostics.update(values)
    _set_delegate_diagnostics(component, diagnostics)


def last_delegate_diagnostics(component: str) -> dict[str, Any]:
    values = getattr(_delegate_state, "diagnostics", {})
    diagnostics = values.get(component, {}) if isinstance(values, dict) else {}
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


def reset_delegate_diagnostics() -> None:
    _delegate_state.diagnostics = {}


def _rust_rag_enabled() -> bool:
    from deepseek_infra.infra.rust_core.config import load_rust_flags

    return load_rust_flags().rag


def _fallback_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_RAG_FALLBACK", "1")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _timeout_ms() -> int:
    try:
        return int(os.environ.get("DEEPSEEK_RUST_RAG_TIMEOUT_MS", DEFAULT_RAG_TIMEOUT_MS))
    except ValueError:
        return DEFAULT_RAG_TIMEOUT_MS


def _request(
    method: str,
    path: str,
    payload: Any = None,
    timeout_ms: int | None = None,
) -> RagProxyResult:
    total_started_ns = time.perf_counter_ns()
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    correlation_id = transport.new_correlation_id()
    headers = {"Accept": "application/json", "X-DeepSeek-Request-ID": correlation_id}
    data = None
    serialization_us = 0
    if payload is not None:
        headers["Content-Type"] = "application/json"
        serialization_started_ns = time.perf_counter_ns()
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    started = time.perf_counter()

    def result(
        *,
        ok: bool,
        status: int,
        body: Any,
        error_kind: str = "",
        response: Any = None,
        response_bytes: int = 0,
    ) -> RagProxyResult:
        observed_transport_us = getattr(response, "transport_us", None)
        if not isinstance(observed_transport_us, int):
            observed_transport_us = max(0, int((time.perf_counter() - started) * 1_000_000))
        rust_processing_us: int | None = None
        raw_rust_us = transport.response_header(response, "X-DeepSeek-Rust-Processing-Us") if response is not None else None
        if raw_rust_us is not None:
            try:
                rust_processing_us = max(0, int(raw_rust_us))
            except ValueError:
                rust_processing_us = None
        return RagProxyResult(
            ok=ok,
            status=status,
            body=body,
            error_kind=error_kind,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            serialization_us=serialization_us,
            transport_us=observed_transport_us,
            rust_processing_us=rust_processing_us,
            total_us=max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
            request_bytes=len(data or b""),
            response_bytes=response_bytes,
            correlation_id=correlation_id,
            connection_reused=getattr(response, "connection_reused", None),
            connection_count=getattr(response, "connection_count", None),
        )

    try:
        with transport.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return result(ok=False, status=response.status, body=None, error_kind="rust_empty_response", response=response)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return result(
                    ok=False, status=response.status, body=None, error_kind="rust_malformed_json", response=response, response_bytes=len(raw)
                )
            if not isinstance(body, dict):
                return result(
                    ok=False,
                    status=response.status,
                    body=body,
                    error_kind="rust_response_not_object",
                    response=response,
                    response_bytes=len(raw),
                )
            return result(ok=True, status=response.status, body=body, response=response, response_bytes=len(raw))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            body = raw.decode("utf-8")
        except Exception:
            raw = b""
            body = str(exc)
        return result(ok=False, status=exc.code, body=body, error_kind="rust_http_failure", response=exc, response_bytes=len(raw))
    except TimeoutError:
        return result(ok=False, status=0, body=None, error_kind="rust_backend_timeout")
    except urllib.error.URLError as exc:
        kind = "rust_backend_timeout" if "timed out" in str(exc).lower() else "rust_backend_unavailable"
        return result(ok=False, status=0, body=None, error_kind=kind)
    except Exception as exc:
        return result(ok=False, status=0, body=str(exc), error_kind="rust_backend_unavailable")


def _binary_request(
    path: str,
    data: bytes | bytearray,
    *,
    serialization_us: int,
    timeout_ms: int | None = None,
) -> RagProxyResult:
    total_started_ns = time.perf_counter_ns()
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    correlation_id = transport.new_correlation_id()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": vector_binary.CONTENT_TYPE,
            "Content-Type": vector_binary.CONTENT_TYPE,
            "X-DeepSeek-Request-ID": correlation_id,
        },
    )
    started_ns = time.perf_counter_ns()

    def result(
        *,
        ok: bool,
        status: int,
        body: Any,
        error_kind: str = "",
        response: Any = None,
        response_bytes: int = 0,
    ) -> RagProxyResult:
        observed_transport_us = getattr(response, "transport_us", None)
        if not isinstance(observed_transport_us, int):
            observed_transport_us = max(0, (time.perf_counter_ns() - started_ns) // 1000)
        rust_processing_us: int | None = None
        raw_rust_us = transport.response_header(response, "X-DeepSeek-Rust-Processing-Us") if response is not None else None
        if raw_rust_us is not None:
            try:
                rust_processing_us = max(0, int(raw_rust_us))
            except ValueError:
                rust_processing_us = None
        return RagProxyResult(
            ok=ok,
            status=status,
            body=body,
            error_kind=error_kind,
            latency_ms=max(0, (time.perf_counter_ns() - started_ns) // 1_000_000),
            serialization_us=serialization_us,
            transport_us=observed_transport_us,
            rust_processing_us=rust_processing_us,
            total_us=max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
            request_bytes=len(data),
            response_bytes=response_bytes,
            correlation_id=correlation_id,
            connection_reused=getattr(response, "connection_reused", None),
            connection_count=getattr(response, "connection_count", None),
        )

    try:
        with transport.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return result(ok=False, status=response.status, body=None, error_kind="rust_empty_response", response=response)
            content_type = transport.response_header(response, "Content-Type")
            if content_type is None or content_type.strip().lower() != vector_binary.CONTENT_TYPE:
                return result(
                    ok=False,
                    status=response.status,
                    body=None,
                    error_kind="rust_binary_content_type",
                    response=response,
                    response_bytes=len(raw),
                )
            return result(ok=True, status=response.status, body=raw, response=response, response_bytes=len(raw))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return result(ok=False, status=exc.code, body=None, error_kind="rust_http_failure", response=exc, response_bytes=len(raw))
    except TimeoutError:
        return result(ok=False, status=0, body=None, error_kind="rust_backend_timeout")
    except urllib.error.URLError as exc:
        kind = "rust_backend_timeout" if "timed out" in str(exc).lower() else "rust_backend_unavailable"
        return result(ok=False, status=0, body=None, error_kind=kind)
    except Exception:
        return result(ok=False, status=0, body=None, error_kind="rust_backend_unavailable")


def _should_use_result(result: RagProxyResult) -> bool:
    if not result.ok:
        return False
    if result.status != 200:
        return False
    if not isinstance(result.body, dict):
        return False
    return True


def normalize_query(query: str) -> tuple[str | None, bool]:
    """Return (normalized, used_rust). Falls back to None if Rust fails or is disabled."""
    if not _rust_rag_enabled():
        return None, False
    result = _request("POST", "/rag/query/normalize", payload={"query": query})
    if _should_use_result(result):
        body = result.body
        normalized = body.get("normalized")
        if isinstance(normalized, str):
            return normalized, True
    if _fallback_enabled():
        return None, False
    return None, False


def score_chunks(
    query: str, chunks: list[dict[str, Any]]
) -> tuple[list[tuple[str, float]] | None, bool]:
    """Return (ranked_id_score_pairs, used_rust). Falls back to None."""
    if not _rust_rag_enabled() or not chunks:
        return None, False
    result = _request(
        "POST", "/rag/chunks/score", payload={"query": query, "chunks": chunks}
    )
    if _should_use_result(result):
        ranked = result.body.get("ranked")
        if isinstance(ranked, list):
            pairs: list[tuple[str, float]] = []
            for item in ranked:
                if isinstance(item, dict):
                    item_id = item.get("id")
                    item_score = item.get("score")
                    if isinstance(item_id, str) and isinstance(item_score, (int, float)):
                        pairs.append((item_id, float(item_score)))
            return pairs, True
    if _fallback_enabled():
        return None, False
    return None, False


def format_citation(
    source: str, start_line: int | None, end_line: int | None
) -> tuple[str | None, bool]:
    """Return (citation, used_rust). Falls back to None."""
    if not _rust_rag_enabled():
        return None, False
    payload: dict[str, Any] = {"source": source}
    if start_line is not None:
        payload["start_line"] = start_line
    if end_line is not None:
        payload["end_line"] = end_line
    result = _request("POST", "/rag/citation/format", payload=payload)
    if _should_use_result(result):
        citation = result.body.get("citation")
        if isinstance(citation, str):
            return citation, True
    if _fallback_enabled():
        return None, False
    return None, False


def rank_vectors(
    query: list[float], candidates: list[list[float]]
) -> tuple[tuple[int | None, float] | None, bool]:
    """Return ((best_index, similarity), used_rust) for semantic-cache vectors."""
    total_started_ns = time.perf_counter_ns()
    component = "rag_vector_rank"
    preparation_started_ns = time.perf_counter_ns()
    configured_transport = rust_rag_vector_transport()
    invalid_transport_config = rust_rag_vector_transport_invalid()
    preparation_us = max(0, (time.perf_counter_ns() - preparation_started_ns) // 1000)
    if not _rust_rag_enabled() or not query or not candidates:
        _set_delegate_diagnostics(
            component,
            {
                "runtime": "python",
                "fallback": False,
                "pythonPreparationUs": preparation_us,
                "serializationUs": None,
                "transportUs": None,
                "rustProcessingUs": None,
                "pythonValidationUs": None,
                "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
                "transportEncoding": "python",
                "requestPayloadBytes": 0,
                "responsePayloadBytes": 0,
                "transportConfigInvalid": invalid_transport_config,
            },
        )
        return None, False
    if configured_transport == "binary":
        serialization_started_ns = time.perf_counter_ns()
        try:
            encoded = vector_binary.encode_rank_request(query, candidates)
        except vector_binary.VectorBinaryError as exc:
            serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
            _set_delegate_diagnostics(
                component,
                {
                    "runtime": "python",
                    "fallback": True,
                    "fallbackReason": exc.code,
                    "pythonPreparationUs": preparation_us,
                    "serializationUs": serialization_us,
                    "transportUs": None,
                    "rustProcessingUs": None,
                    "pythonValidationUs": None,
                    "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
                    "transportEncoding": "binary",
                    "requestPayloadBytes": 0,
                    "responsePayloadBytes": 0,
                    "transportConfigInvalid": invalid_transport_config,
                },
            )
            return None, False
        serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
        result = _binary_request(
            "/rag/vectors/rank-binary",
            encoded.body,
            serialization_us=serialization_us,
        )
    else:
        result = _request("POST", "/rag/vectors/rank", payload={"query": query, "candidates": candidates})
    diagnostics = {
        "runtime": "python",
        "fallback": not result.ok,
        "fallbackReason": result.error_kind,
        "pythonPreparationUs": preparation_us,
        "serializationUs": result.serialization_us,
        "transportUs": result.transport_us,
        "rustProcessingUs": result.rust_processing_us,
        "pythonValidationUs": None,
        "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
        "requestBytes": result.request_bytes,
        "responseBytes": result.response_bytes,
        "requestPayloadBytes": result.request_bytes,
        "responsePayloadBytes": result.response_bytes,
        "transportEncoding": configured_transport,
        "transportConfigInvalid": invalid_transport_config,
        "connectionReused": result.connection_reused,
        "connectionCount": result.connection_count,
        "correlationId": result.correlation_id,
    }
    usable_result = result.ok and result.status == 200 and (
        isinstance(result.body, bytes) if configured_transport == "binary" else isinstance(result.body, dict)
    )
    if usable_result:
        validation_started_ns = time.perf_counter_ns()
        if configured_transport == "binary":
            try:
                decoded = vector_binary.decode_rank_response(result.body, candidate_count=len(candidates))
            except vector_binary.VectorBinaryError as exc:
                diagnostics.update(
                    fallback=True,
                    fallbackReason=exc.code,
                    pythonValidationUs=max(0, (time.perf_counter_ns() - validation_started_ns) // 1000),
                    totalDelegateUs=max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
                )
                _set_delegate_diagnostics(component, diagnostics)
                return None, False
            index = decoded.index
            similarity = decoded.similarity
        else:
            index = result.body.get("index")
            similarity = result.body.get("similarity")
        valid_index = index is None or (
            isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(candidates)
        )
        valid_similarity = (
            isinstance(similarity, (int, float))
            and not isinstance(similarity, bool)
            and math.isfinite(float(similarity))
            and 0.0 <= float(similarity) <= 1.0
        )
        validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
        diagnostics.update(
            pythonValidationUs=validation_us,
            totalDelegateUs=max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
        )
        if valid_index and valid_similarity:
            diagnostics.update(runtime="rust", fallback=False, fallbackReason="")
            _set_delegate_diagnostics(component, diagnostics)
            return (index, float(similarity)), True
        diagnostics.update(fallback=True, fallbackReason="rust_defensive_validation_failed")
    _set_delegate_diagnostics(component, diagnostics)
    if _fallback_enabled():
        return None, False
    return None, False


def rank_vectors_from_blobs(
    query: Sequence[float],
    candidate_blobs: Sequence[bytes | memoryview],
    *,
    dimensions: int,
    blobs_validated: bool = False,
) -> tuple[tuple[int | None, float] | None, bool]:
    """Rank f64le SQLite candidate buffers through the binary endpoint once."""
    total_started_ns = time.perf_counter_ns()
    component = "rag_vector_rank"
    preparation_started_ns = time.perf_counter_ns()
    configured_transport = rust_rag_vector_transport()
    invalid_transport_config = rust_rag_vector_transport_invalid()
    preparation_us = max(0, (time.perf_counter_ns() - preparation_started_ns) // 1000)
    if not _rust_rag_enabled() or not query or not candidate_blobs:
        _set_delegate_diagnostics(
            component,
            {
                "runtime": "python",
                "fallback": False,
                "pythonPreparationUs": preparation_us,
                "serializationUs": None,
                "transportUs": None,
                "rustProcessingUs": None,
                "pythonValidationUs": None,
                "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
                "transportEncoding": "python",
                "requestPayloadBytes": 0,
                "responsePayloadBytes": 0,
                "transportConfigInvalid": invalid_transport_config,
                "payloadAssemblySource": "blob",
            },
        )
        return None, False
    if configured_transport != "binary":
        _set_delegate_diagnostics(
            component,
            {
                "runtime": "python",
                "fallback": True,
                "fallbackReason": "binary_blob_transport_inactive",
                "pythonPreparationUs": preparation_us,
                "serializationUs": None,
                "transportUs": None,
                "rustProcessingUs": None,
                "pythonValidationUs": None,
                "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
                "transportEncoding": configured_transport,
                "requestPayloadBytes": 0,
                "responsePayloadBytes": 0,
                "transportConfigInvalid": invalid_transport_config,
                "payloadAssemblySource": "blob",
            },
        )
        return None, False

    serialization_started_ns = time.perf_counter_ns()
    try:
        encoded = vector_binary.encode_rank_request_from_blobs(
            query,
            candidate_blobs,
            dimensions,
            blobs_validated=blobs_validated,
        )
    except vector_binary.VectorBinaryError as exc:
        serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
        _set_delegate_diagnostics(
            component,
            {
                "runtime": "python",
                "fallback": True,
                "fallbackReason": exc.code,
                "pythonPreparationUs": preparation_us,
                "serializationUs": serialization_us,
                "transportUs": None,
                "rustProcessingUs": None,
                "pythonValidationUs": None,
                "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
                "transportEncoding": "binary",
                "requestPayloadBytes": 0,
                "responsePayloadBytes": 0,
                "transportConfigInvalid": invalid_transport_config,
                "payloadAssemblySource": "blob",
            },
        )
        return None, False
    serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    result = _binary_request(
        "/rag/vectors/rank-binary",
        encoded.body,
        serialization_us=serialization_us,
    )
    diagnostics = {
        "runtime": "python",
        "fallback": not result.ok,
        "fallbackReason": result.error_kind,
        "pythonPreparationUs": preparation_us,
        "serializationUs": result.serialization_us,
        "transportUs": result.transport_us,
        "rustProcessingUs": result.rust_processing_us,
        "pythonValidationUs": None,
        "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
        "requestBytes": result.request_bytes,
        "responseBytes": result.response_bytes,
        "requestPayloadBytes": result.request_bytes,
        "responsePayloadBytes": result.response_bytes,
        "transportEncoding": "binary",
        "transportConfigInvalid": invalid_transport_config,
        "connectionReused": result.connection_reused,
        "connectionCount": result.connection_count,
        "correlationId": result.correlation_id,
        "payloadAssemblySource": "blob",
    }
    usable_result = result.ok and result.status == 200 and isinstance(result.body, bytes)
    if usable_result:
        validation_started_ns = time.perf_counter_ns()
        try:
            decoded = vector_binary.decode_rank_response(result.body, candidate_count=len(candidate_blobs))
        except vector_binary.VectorBinaryError as exc:
            diagnostics.update(
                fallback=True,
                fallbackReason=exc.code,
                pythonValidationUs=max(0, (time.perf_counter_ns() - validation_started_ns) // 1000),
                totalDelegateUs=max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
            )
            _set_delegate_diagnostics(component, diagnostics)
            return None, False
        validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
        diagnostics.update(
            pythonValidationUs=validation_us,
            totalDelegateUs=max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
            runtime="rust",
            fallback=False,
            fallbackReason="",
        )
        _set_delegate_diagnostics(component, diagnostics)
        return (decoded.index, decoded.similarity), True
    _set_delegate_diagnostics(component, diagnostics)
    return None, False


def validate_index(chunks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Return (validation_result, used_rust). Falls back to None."""
    if not _rust_rag_enabled() or not chunks:
        return None, False
    result = _request("POST", "/rag/index/validate", payload={"chunks": chunks})
    if _should_use_result(result):
        body = result.body
        if isinstance(body, dict):
            return {"valid": bool(body.get("valid")), "error": body.get("error")}, True
    if _fallback_enabled():
        return None, False
    return None, False


def prepare_document(payload: dict[str, Any]) -> RagProxyResult:
    """Prepare already-parsed text; never forwards caller headers or credentials."""
    if not rag_document_preparation_enabled():
        return RagProxyResult(ok=False, status=0, body=None, error_kind="rust_disabled")
    return _request("POST", "/rag/documents/prepare", payload=payload)


def rag_document_preparation_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "0")
    return value.strip().lower() in ("1", "true", "yes", "on")


def rust_rag_enabled() -> bool:
    return _rust_rag_enabled()


def fallback_to_python_enabled() -> bool:
    return _fallback_enabled()
