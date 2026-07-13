"""MCP JSON-RPC endpoint and external MCP tools listing routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.mcp.protocol_preparation import (
    log_mcp_protocol_diagnostics,
    prepare_mcp_protocol_with_optional_rust,
    protocol_diagnostic_headers,
    protocol_error_response,
)
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


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
        decision = prepare_mcp_protocol_with_optional_rust(body)
        log_mcp_protocol_diagnostics(decision.diagnostics)
        headers = protocol_diagnostic_headers(decision.diagnostics)
        preparation = decision.preparation
        if preparation.get("ok") is not True:
            error_response = protocol_error_response(preparation, body)
            if error_response is None:
                return Response(status_code=202, headers=headers)
            return json_response(error_response, headers=headers)

        prepared = preparation.get("request")
        response = deps.handle_mcp_message(prepared if isinstance(prepared, dict) else body)
        if response is None:
            return Response(status_code=202, headers=headers)
        return json_response(response, headers=headers)

    return router
