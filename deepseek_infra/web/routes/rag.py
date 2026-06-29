"""RAG reindex, citation verification, and evaluation routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


@dataclass(frozen=True)
class RagRouteDeps:
    rebuild_local_rag_index: Callable[[], dict[str, Any]]
    verify_local_rag_citation: Callable[[str, str], dict[str, Any]]
    evaluate_local_rag_recall: Callable[[list[dict[str, Any]], int], dict[str, Any]]


def create_rag_router(deps: RagRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/rag/reindex")
    async def api_rag_reindex(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "reindex").strip().lower()
        if action not in {"reindex", "rebuild"}:
            raise AppError("Unsupported RAG action", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(deps.rebuild_local_rag_index())

    @router.post("/api/rag/verify-citation")
    async def api_rag_verify_citation(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        item_id = str(payload.get("itemId") or "").strip()
        snippet = str(payload.get("snippet") or "")
        if not item_id:
            raise AppError("itemId is required", code=ErrorCode.INVALID_PAYLOAD)
        return json_response({"ok": True, "citation": deps.verify_local_rag_citation(item_id, snippet)})

    @router.post("/api/rag/eval")
    async def api_rag_eval(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise AppError("cases must be a list", code=ErrorCode.INVALID_PAYLOAD)
        k = payload.get("k")
        k_value = int(k) if isinstance(k, int) and k > 0 else 5
        return json_response({"ok": True, "eval": deps.evaluate_local_rag_recall(cases, k_value)})

    return router
