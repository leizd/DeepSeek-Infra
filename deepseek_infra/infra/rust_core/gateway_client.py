"""HTTP client for optional Rust Gateway sidecar contracts."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import (
    DEFAULT_RUST_GATEWAY_URL,
    load_rust_flags,
    rust_gateway_url,
)
from deepseek_infra.infra.rust_core import transport

DEFAULT_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class GatewayProxyResult:
    ok: bool
    status: int
    body: Any
    error_kind: str = ""
    serialization_us: int | None = None
    transport_us: int | None = None
    rust_processing_us: int | None = None
    total_us: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    correlation_id: str = ""
    connection_reused: bool | None = None
    connection_count: int | None = None


def _rust_gateway_enabled() -> bool:
    return load_rust_flags().gateway


def _fallback_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_GATEWAY_FALLBACK", "1")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _timeout_ms() -> int:
    try:
        return int(os.environ.get("DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
    except ValueError:
        return DEFAULT_TIMEOUT_MS


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int | None = None,
) -> GatewayProxyResult:
    total_started_ns = time.perf_counter_ns()
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    correlation_id = transport.new_correlation_id()
    req_headers = {"Accept": "application/json", "X-DeepSeek-Request-ID": correlation_id}
    del headers  # Local auth and provider credentials are never forwarded to Rust.
    data = None
    serialization_us = 0
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
        serialization_started_ns = time.perf_counter_ns()
        data = json.dumps(payload).encode("utf-8")
        serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    transport_started_ns = time.perf_counter_ns()

    def result(
        *,
        ok: bool,
        status: int,
        body: Any,
        error_kind: str = "",
        response: Any = None,
        response_bytes: int = 0,
    ) -> GatewayProxyResult:
        observed_transport_us = getattr(response, "transport_us", None)
        if not isinstance(observed_transport_us, int):
            observed_transport_us = max(0, (time.perf_counter_ns() - transport_started_ns) // 1000)
        rust_processing_us: int | None = None
        raw_rust_us = transport.response_header(response, "X-DeepSeek-Rust-Processing-Us") if response is not None else None
        if raw_rust_us is not None:
            try:
                rust_processing_us = max(0, int(raw_rust_us))
            except ValueError:
                rust_processing_us = None
        return GatewayProxyResult(
            ok=ok,
            status=status,
            body=body,
            error_kind=error_kind,
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
                return result(ok=True, status=response.status, body={}, error_kind="rust_empty_response", response=response)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return result(
                    ok=False,
                    status=response.status,
                    body=str(exc),
                    error_kind="rust_malformed_json",
                    response=response,
                    response_bytes=len(raw),
                )
            if not isinstance(body, dict):
                return result(
                    ok=False,
                    status=response.status,
                    body=body,
                    error_kind="rust_invalid_shape",
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
        return result(ok=False, status=exc.code, body=body, error_kind="rust_http_error", response=exc, response_bytes=len(raw))
    except urllib.error.URLError as exc:
        reason = "rust_backend_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "rust_backend_unavailable"
        return result(ok=False, status=0, body=str(exc), error_kind=reason)
    except (TimeoutError, socket.timeout) as exc:
        return result(ok=False, status=0, body=str(exc), error_kind="rust_backend_timeout")
    except Exception as exc:
        return result(ok=False, status=0, body=str(exc), error_kind="rust_backend_unavailable")


def proxy_chat_to_rust(
    payload: dict[str, Any], headers: dict[str, str] | None = None
) -> GatewayProxyResult:
    if not _rust_gateway_enabled():
        return GatewayProxyResult(
            ok=False, status=0, body={"error": "Rust Gateway is disabled"}
        )
    return _request("POST", "/v1/chat/completions", payload=payload, headers=headers)


def proxy_models_to_rust(headers: dict[str, str] | None = None) -> GatewayProxyResult:
    if not _rust_gateway_enabled():
        return GatewayProxyResult(
            ok=False, status=0, body={"error": "Rust Gateway is disabled"}
        )
    return _request("GET", "/v1/models", headers=headers)


def prepare_request_with_rust(payload: dict[str, Any]) -> GatewayProxyResult:
    """Send only a credential-free request body to deterministic preparation."""
    if not _rust_gateway_enabled():
        return GatewayProxyResult(
            ok=False,
            status=0,
            body={"error": "Rust Gateway is disabled"},
            error_kind="rust_gateway_disabled",
        )
    return _request("POST", "/gateway/request/prepare", payload=payload)


def rust_gateway_enabled() -> bool:
    return _rust_gateway_enabled()


def fallback_to_python_enabled() -> bool:
    return _fallback_enabled()


def __getattr__(name: str) -> Any:
    if name == "DEFAULT_RUST_GATEWAY_URL":
        return DEFAULT_RUST_GATEWAY_URL
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
