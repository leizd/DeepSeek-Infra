"""Credential-free protocol preparation client for the Rust MCP sidecar."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import rust_gateway_url

DEFAULT_MCP_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class McpProxyResult:
    ok: bool
    status: int
    body: Any
    error_kind: str = ""
    latency_ms: int = 0


def _rust_mcp_enabled() -> bool:
    from deepseek_infra.infra.rust_core.config import load_rust_flags

    return load_rust_flags().mcp


def _fallback_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_MCP_FALLBACK", "1")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _timeout_ms() -> int:
    try:
        return int(
            os.environ.get("DEEPSEEK_RUST_MCP_TIMEOUT_MS", DEFAULT_MCP_TIMEOUT_MS)
        )
    except ValueError:
        return DEFAULT_MCP_TIMEOUT_MS


def _request(
    method: str,
    path: str,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    allow_empty: bool = False,
) -> McpProxyResult:
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    req_headers = {"Accept": "application/json"}
    # MCP preparation is credential-free.  Keep the legacy parameter for API
    # compatibility, but never forward Authorization or any caller headers.
    del headers
    data = None
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    started = time.perf_counter()

    def result(
        *,
        ok: bool,
        status: int,
        body: Any,
        error_kind: str = "",
    ) -> McpProxyResult:
        return McpProxyResult(
            ok=ok,
            status=status,
            body=body,
            error_kind=error_kind,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                if allow_empty:
                    return result(ok=True, status=response.status, body={})
                return result(ok=False, status=response.status, body=None, error_kind="rust_empty_response")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return result(ok=False, status=response.status, body=None, error_kind="rust_malformed_json")
            if not isinstance(body, dict):
                return result(ok=False, status=response.status, body=body, error_kind="rust_response_not_object")
            return result(ok=True, status=response.status, body=body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return result(ok=False, status=exc.code, body=body, error_kind="rust_http_failure")
    except (TimeoutError, urllib.error.URLError) as exc:
        kind = "rust_backend_timeout" if "timed out" in str(exc).lower() or "timeout" in str(exc).lower() else "rust_backend_unavailable"
        return result(ok=False, status=0, body=None, error_kind=kind)
    except Exception:
        return result(ok=False, status=0, body=None, error_kind="rust_backend_unavailable")


def prepare_mcp_with_rust(payload: Any) -> McpProxyResult:
    """Ask Rust to prepare one message without delegating execution."""
    if not _rust_mcp_enabled():
        return McpProxyResult(
            ok=False,
            status=0,
            body=None,
            error_kind="rust_disabled",
        )
    return _request("POST", "/mcp/request/prepare", payload=payload)


def proxy_mcp_to_rust(
    payload: dict[str, Any], headers: dict[str, str] | None = None
) -> McpProxyResult:
    if not _rust_mcp_enabled():
        return McpProxyResult(
            ok=False, status=0, body={"error": "Rust MCP is disabled"}
        )
    return _request("POST", "/mcp", payload=payload, headers=headers, allow_empty=True)


def rust_mcp_enabled() -> bool:
    return _rust_mcp_enabled()


def fallback_to_python_enabled() -> bool:
    return _fallback_enabled()
