"""MCP JSON-RPC endpoint and external MCP tools listing routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.rust_core.mcp_client import (
    fallback_to_python_enabled,
    proxy_mcp_to_rust,
    rust_mcp_enabled,
)
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


def _authorization_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    return headers


@dataclass(frozen=True)
class McpRouteDeps:
    mcp_enabled: Callable[[], bool]
    handle_mcp_message: Callable[[dict[str, Any]], dict[str, Any] | None]
    list_external_mcp_tools: Callable[[], dict[str, Any]]


def create_mcp_router(deps: McpRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/mcp/external/tools")
    async def api_external_mcp_tools(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(deps.list_external_mcp_tools())

    @router.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        require_api_auth(request)
        if not deps.mcp_enabled():
            raise AppError("MCP server is disabled", code=ErrorCode.FORBIDDEN, status=403)
        body = await read_json_body(request)
        if rust_mcp_enabled():
            result = proxy_mcp_to_rust(body, headers=_authorization_headers(request))
            if result.ok:
                if result.body is None or result.body == {}:
                    return Response(status_code=202)
                return json_response(result.body)
            if not fallback_to_python_enabled():
                raise AppError(
                    f"Rust MCP unavailable: {result.body}",
                    code=ErrorCode.UPSTREAM_FAILURE,
                    status=502,
                )
        response = deps.handle_mcp_message(body)
        if response is None:
            return Response(status_code=202)
        return json_response(response)

    return router
