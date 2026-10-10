"""Edge inference reload route."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


@dataclass(frozen=True)
class EdgeRouteDeps:
    edge_unload: Callable[[], dict[str, Any]]
    edge_route_preview: Callable[[dict[str, Any]], dict[str, Any]]


def create_edge_router(deps: EdgeRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/edge/reload")
    async def api_edge_reload(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "unload").strip().lower()
        if action not in {"unload", "reload"}:
            raise AppError("Unsupported edge action", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(deps.edge_unload())

    @router.post("/api/edge/route-preview")
    async def api_edge_route_preview(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        return json_response(deps.edge_route_preview(payload))

    return router
