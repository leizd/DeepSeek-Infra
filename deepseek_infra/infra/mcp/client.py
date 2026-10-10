"""Minimal outbound MCP client (Streamable HTTP, JSON + SSE responses).

Lets the runtime *consume* external MCP servers, so local agents are not limited
to built-in tools. Deliberately small: single-message JSON-RPC over ``POST``.
As of v2.3.0 the client handles both ``application/json`` and
``text/event-stream`` (SSE) response bodies — the official MCP SDK's
Streamable HTTP transport returns SSE for every POST, so SSE parsing is required
for real third-party interop. Session id echo per the Streamable HTTP transport.
Disabled by default (``MCP_CLIENT_ENABLED``) and only talks to servers
explicitly configured in ``MCP_CLIENT_SERVERS``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError, ErrorCode

CLIENT_INFO = {"name": "deepseek-infra-client", "version": "1.0"}


def _parse_sse_jsonrpc(text: str) -> dict[str, Any] | None:
    """Extract the first JSON-RPC object from an SSE ``text/event-stream`` body.

    The official MCP SDK wraps every JSON-RPC response in an SSE ``data:`` line.
    Multi-line ``data:`` fields are concatenated before JSON decoding.
    """
    for block in text.split("\n\n"):
        data_parts: list[str] = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_parts.append(line[5:].lstrip())
        if not data_parts:
            continue
        try:
            parsed = json.loads("".join(data_parts))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


@dataclass(frozen=True, slots=True)
class MCPCallStats:
    """Transport stats from the last outbound MCP HTTP request."""

    latency_ms: int = 0
    attempts: int = 0
    retry_count: int = 0
    timeout: bool = False
    error_type: str = ""


class MCPClient:
    """One configured external MCP server connection."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str = "",
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.name = str(name or "") or self.base_url
        self.timeout_seconds = timeout_seconds or settings.mcp.client_timeout_seconds
        self.max_retries = settings.mcp.client_max_retries if max_retries is None else max(0, int(max_retries))
        self.retry_backoff_seconds = (
            settings.mcp.client_retry_backoff_seconds
            if retry_backoff_seconds is None
            else max(0.0, float(retry_backoff_seconds))
        )
        self.extra_headers: dict[str, str] = dict(extra_headers) if extra_headers else {}
        self.session_id = ""
        self.protocol_version = ""
        self._next_id = 0
        self.last_stats = MCPCallStats()

    # -- transport -----------------------------------------------------------

    def _post(self, message: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        started = time.perf_counter()
        max_attempts = max(1, int(self.max_retries) + 1)
        last_error: BaseException | None = None
        headers: dict[str, str] = {}
        raw = b""
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    raw = response.read()
                self._set_stats(started, attempt)
                break
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code < 500 or attempt >= max_attempts:
                    self._set_stats(started, attempt, error_type="http_error")
                    raise AppError(f"MCP server {self.name} returned HTTP {exc.code}", code=ErrorCode.UPSTREAM_FAILURE, status=502) from exc
                self._sleep_before_retry(attempt)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= max_attempts:
                    error_type = "timeout" if _looks_like_timeout(exc) else "unreachable"
                    self._set_stats(started, attempt, error_type=error_type)
                    raise AppError(f"MCP server {self.name} is unreachable: {exc}", code=ErrorCode.UPSTREAM_FAILURE, status=502) from exc
                self._sleep_before_retry(attempt)
        else:  # pragma: no cover - loop always breaks or raises
            self._set_stats(started, max_attempts, error_type="upstream_failure")
            raise AppError(f"MCP server {self.name} is unreachable: {last_error}", code=ErrorCode.UPSTREAM_FAILURE, status=502)
        if not raw:
            return None, headers
        content_type = headers.get("content-type", "")
        try:
            if "text/event-stream" in content_type:
                parsed = _parse_sse_jsonrpc(raw.decode("utf-8"))
            else:
                parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._set_stats(started, self.last_stats.attempts or 1, error_type="schema_error")
            raise AppError(f"MCP server {self.name} returned invalid JSON", code=ErrorCode.UPSTREAM_FAILURE, status=502) from exc
        return (parsed if isinstance(parsed, dict) else None), headers

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = float(self.retry_backoff_seconds) * max(0, attempt)
        if delay > 0:
            time.sleep(delay)

    def _set_stats(self, started: float, attempts: int, *, error_type: str = "") -> None:
        self.last_stats = MCPCallStats(
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            attempts=max(1, int(attempts or 1)),
            retry_count=max(0, int(attempts or 1) - 1),
            timeout=error_type == "timeout",
            error_type=error_type,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        headers.update(self.extra_headers)
        return headers

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        response, headers = self._post(message)
        session_id = headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        if not isinstance(response, dict):
            raise AppError(f"MCP server {self.name} returned no response for {method}", code=ErrorCode.UPSTREAM_FAILURE, status=502)
        error = response.get("error")
        if isinstance(error, dict):
            raise AppError(
                f"MCP server {self.name} error {error.get('code')}: {error.get('message')}",
                code=ErrorCode.UPSTREAM_FAILURE,
                status=502,
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method})
        except AppError:
            pass  # notifications are best-effort

    # -- MCP methods -----------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self.protocol_version = str(result.get("protocolVersion") or "")
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list")
        tools = result.get("tools")
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._rpc("tools/call", {"name": str(name or ""), "arguments": arguments or {}})


def configured_clients() -> list[MCPClient]:
    """Clients for every configured external MCP server (empty when disabled)."""
    if not settings.mcp.client_enabled:
        return []
    clients: list[MCPClient] = []
    for name, url in settings.mcp.client_servers:
        timeout = settings.mcp.client_server_timeouts.get(name, settings.mcp.client_timeout_seconds)
        clients.append(MCPClient(url, name=name, timeout_seconds=timeout))
    return clients


def _looks_like_timeout(exc: BaseException) -> bool:
    text = str(exc).lower()
    reason = getattr(exc, "reason", None)
    reason_text = str(reason or "").lower()
    return "timed out" in text or "timeout" in text or "timed out" in reason_text or "timeout" in reason_text
