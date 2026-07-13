"""Chat, title generation, conversation search, and OpenAI-compatible routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from deepseek_infra.infra.gateway.deepseek_client import (
    call_deepseek_cascade,
    preflight_chat_payload,
)
from deepseek_infra.infra.gateway.openai_api import (
    openai_chat_completion,
    openai_chat_stream,
    openai_models_list,
    openai_to_internal_payload,
)
from deepseek_infra.infra.gateway.title_generator import generate_title_payload
from deepseek_infra.web.http_utils import json_response, read_json_body, request_base_url, require_api_auth

STREAM_MEDIA_TYPE = "application/x-ndjson; charset=utf-8"


@dataclass(frozen=True)
class ChatRouteDeps:
    chat_event_stream: Callable[[dict[str, Any]], Any]
    conversation_search: Callable[[dict[str, Any]], dict[str, Any]]


def create_chat_router(deps: ChatRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/title")
    async def api_title(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(generate_title_payload(await read_json_body(request)))

    @router.post("/api/conversations/search")
    async def api_conversation_search(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(deps.conversation_search(await read_json_body(request)))

    @router.post("/api/chat")
    async def api_chat(request: Request) -> Response:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000_000)
        payload = {**payload, "localBaseUrl": request_base_url(request)}
        if payload.get("stream"):
            preflight_chat_payload(payload)
            return StreamingResponse(
                deps.chat_event_stream(payload),
                media_type=STREAM_MEDIA_TYPE,
                headers={"X-Accel-Buffering": "no"},
            )
        return json_response(call_deepseek_cascade(payload))

    @router.post("/v1/chat/completions")
    async def v1_chat_completions(request: Request) -> Response:
        require_api_auth(request)
        body = await read_json_body(request, max_bytes=16_000_000)
        payload = openai_to_internal_payload(body, local_base_url=request_base_url(request))
        if payload.get("stream"):
            return StreamingResponse(
                openai_chat_stream(payload, str(payload["model"])),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        return json_response(openai_chat_completion(payload, str(payload["model"])))

    @router.get("/v1/models")
    async def v1_models(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(openai_models_list())

    return router
