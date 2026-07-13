"""HTTP client for optional Rust Gateway sidecar contracts."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import (
    DEFAULT_RUST_GATEWAY_URL,
    load_rust_flags,
    rust_gateway_url,
)

DEFAULT_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class GatewayProxyResult:
    ok: bool
    status: int
    body: Any
    error_kind: str = ""


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
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    req_headers = {"Accept": "application/json"}
    del headers  # Local auth and provider credentials are never forwarded to Rust.
    data = None
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return GatewayProxyResult(ok=True, status=response.status, body={}, error_kind="rust_empty_response")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return GatewayProxyResult(ok=False, status=response.status, body=str(exc), error_kind="rust_malformed_json")
            if not isinstance(body, dict):
                return GatewayProxyResult(ok=False, status=response.status, body=body, error_kind="rust_invalid_shape")
            return GatewayProxyResult(ok=True, status=response.status, body=body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return GatewayProxyResult(ok=False, status=exc.code, body=body, error_kind="rust_http_error")
    except urllib.error.URLError as exc:
        reason = "rust_backend_timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "rust_backend_unavailable"
        return GatewayProxyResult(ok=False, status=0, body=str(exc), error_kind=reason)
    except (TimeoutError, socket.timeout) as exc:
        return GatewayProxyResult(ok=False, status=0, body=str(exc), error_kind="rust_backend_timeout")
    except Exception as exc:
        return GatewayProxyResult(ok=False, status=0, body=str(exc), error_kind="rust_backend_unavailable")


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
