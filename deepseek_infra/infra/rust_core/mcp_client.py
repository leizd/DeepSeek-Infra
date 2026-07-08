"""HTTP proxy client for the Rust MCP sidecar."""

from __future__ import annotations

import json
import os
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
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_ms: int | None = None,
) -> McpProxyResult:
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    req_headers = {"Accept": "application/json"}
    if headers and "Authorization" in headers:
        req_headers["Authorization"] = headers["Authorization"]
    data = None
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return McpProxyResult(ok=True, status=response.status, body={})
            return McpProxyResult(
                ok=True, status=response.status, body=json.loads(raw.decode("utf-8"))
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return McpProxyResult(ok=False, status=exc.code, body=body)
    except Exception as exc:
        return McpProxyResult(ok=False, status=0, body=str(exc))


def proxy_mcp_to_rust(
    payload: dict[str, Any], headers: dict[str, str] | None = None
) -> McpProxyResult:
    if not _rust_mcp_enabled():
        return McpProxyResult(
            ok=False, status=0, body={"error": "Rust MCP is disabled"}
        )
    return _request("POST", "/mcp", payload=payload, headers=headers)


def rust_mcp_enabled() -> bool:
    return _rust_mcp_enabled()


def fallback_to_python_enabled() -> bool:
    return _fallback_enabled()
