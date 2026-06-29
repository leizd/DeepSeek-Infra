"""A2A agent mesh routes (discovery, task lifecycle, streaming)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web.http_utils import json_response, read_json_body, request_base_url, require_api_auth


@dataclass(frozen=True)
class A2ARouteDeps:
    a2a_enabled: Callable[[], bool]
    agent_card: Callable[..., dict[str, Any]]
    agent_cards: Callable[..., list[dict[str, Any]]]
    handle_a2a_message: Callable[..., dict[str, Any] | None]
    is_stream_request: Callable[[dict[str, Any]], bool]
    stream_message_events: Callable[..., Any]


def create_a2a_router(deps: A2ARouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/.well-known/agent-card.json")
    async def well_known_agent_card(request: Request) -> JSONResponse:
        if not deps.a2a_enabled():
            raise AppError("A2A mesh is disabled", code=ErrorCode.FORBIDDEN, status=403)
        return json_response(deps.agent_card("orchestrator", base_url=request_base_url(request)))

    @router.get("/a2a/agents")
    async def a2a_agents(request: Request) -> JSONResponse:
        require_api_auth(request)
        if not deps.a2a_enabled():
            raise AppError("A2A mesh is disabled", code=ErrorCode.FORBIDDEN, status=403)
        return json_response({"ok": True, "agents": deps.agent_cards(base_url=request_base_url(request))})

    async def _a2a_rpc(request: Request, agent_id: str) -> Response:
        require_api_auth(request)
        if not deps.a2a_enabled():
            raise AppError("A2A mesh is disabled", code=ErrorCode.FORBIDDEN, status=403)
        body = await read_json_body(request)
        if deps.is_stream_request(body):
            return StreamingResponse(
                deps.stream_message_events(body, agent_id=agent_id),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        response = deps.handle_a2a_message(body, agent_id=agent_id, base_url=request_base_url(request))
        return json_response(response if response is not None else {})

    @router.post("/a2a")
    async def a2a_endpoint(request: Request) -> Response:
        return await _a2a_rpc(request, "orchestrator")

    @router.post("/a2a/agents/{agent_id}")
    async def a2a_agent_endpoint(request: Request, agent_id: str) -> Response:
        return await _a2a_rpc(request, agent_id)

    return router
