"""Read-only status and diagnostics routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.web.http_utils import json_response, request_port, require_api_auth


@dataclass(frozen=True)
class StatusRouteDeps:
    version: str
    settings: Any
    tavily_api_key: str
    supported_models: Sequence[str]
    model_routes: Mapping[str, str]
    max_upload_file_bytes: int
    max_upload_bytes: int
    max_multipart_files: int
    local_ip: Callable[[], str]
    url_with_token: Callable[[str, str], str]
    edge_inference_status: Callable[[], dict[str, Any]]
    local_rag_status: Callable[[], dict[str, Any]]
    trace_status: Callable[[], dict[str, Any]]
    semantic_cache_status: Callable[[], dict[str, Any]]
    gateway_status: Callable[[], dict[str, Any]]
    providers_status: Callable[[], dict[str, Any]]
    model_router_status: Callable[[], dict[str, Any]]
    budget_status: Callable[[str], dict[str, Any]]
    tool_policy_status: Callable[[], dict[str, Any]]
    read_recent_audit: Callable[[int], list[dict[str, Any]]]
    scheduler_status: Callable[[], dict[str, Any]]
    scheduler_dead_letters: Callable[[int], list[dict[str, Any]]]
    mcp_status: Callable[[], dict[str, Any]]
    a2a_status: Callable[[], dict[str, Any]]
    taint_status: Callable[[], dict[str, Any]]
    rust_status: Callable[[], dict[str, Any]]


def create_status_router(deps: StatusRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config")
    async def api_config(request: Request) -> JSONResponse:
        require_api_auth(request)
        port = request_port(request)
        computer_url = f"http://127.0.0.1:{port}"
        phone_url = f"http://{deps.local_ip()}:{port}"
        if deps.settings.auth.enabled:
            computer_url = deps.url_with_token(computer_url + "/", deps.settings.auth.token)
            phone_url = deps.url_with_token(phone_url + "/", deps.settings.auth.token)
        return json_response(
            {
                "version": deps.version,
                "hasServerKey": bool(deps.settings.deepseek_api_key),
                "hasSearch": bool(deps.tavily_api_key),
                "defaultModel": deps.settings.default_model,
                "models": list(deps.supported_models),
                "modelRoutes": dict(deps.model_routes),
                "searchModes": ["off", "auto", "on"],
                "uploadLimits": {
                    "fileMaxBytes": deps.max_upload_file_bytes,
                    "requestMaxBytes": deps.max_upload_bytes,
                    "maxFiles": deps.max_multipart_files,
                },
                "ocr": {"enabled": deps.settings.ocr.enabled, "mode": deps.settings.ocr.mode, "localOnly": False},
                "edgeInference": deps.edge_inference_status(),
                "localRag": deps.local_rag_status(),
                "tracing": deps.trace_status(),
                "semanticCache": deps.semantic_cache_status(),
                "gateway": deps.gateway_status(),
                "providers": deps.providers_status(),
                "modelRouter": deps.model_router_status(),
                "budget": deps.budget_status("global"),
                "toolPolicy": deps.tool_policy_status(),
                "mcp": deps.mcp_status(),
                "a2a": deps.a2a_status(),
                "contextTaint": deps.taint_status(),
                "computerUrl": computer_url,
                "phoneUrl": phone_url,
            }
        )

    @router.get("/api/rag/status")
    async def api_rag_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "localRag": deps.local_rag_status()})

    @router.get("/api/budget")
    async def api_budget(request: Request) -> JSONResponse:
        require_api_auth(request)
        scope = str(request.query_params.get("scope") or "global").strip() or "global"
        return json_response({"ok": True, "budget": deps.budget_status(scope)})

    @router.get("/api/tool-policy")
    async def api_tool_policy(request: Request) -> JSONResponse:
        require_api_auth(request)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        return json_response({"ok": True, "toolPolicy": deps.tool_policy_status(), "audit": deps.read_recent_audit(limit)})

    @router.get("/api/scheduler")
    async def api_scheduler(request: Request) -> JSONResponse:
        require_api_auth(request)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        return json_response({"ok": True, "scheduler": deps.scheduler_status(), "deadLetters": deps.scheduler_dead_letters(limit)})

    @router.get("/api/mcp")
    async def api_mcp_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "mcp": deps.mcp_status()})

    @router.get("/api/taint")
    async def api_taint_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "contextTaint": deps.taint_status()})

    @router.get("/api/semantic-cache/status")
    async def api_semantic_cache_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "semanticCache": deps.semantic_cache_status()})

    @router.get("/api/gateway/status")
    async def api_gateway_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "gateway": deps.gateway_status()})

    @router.get("/api/edge/status")
    async def api_edge_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "edgeInference": deps.edge_inference_status()})

    @router.get("/api/rust/status")
    async def api_rust_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "rust": deps.rust_status()})

    return router
