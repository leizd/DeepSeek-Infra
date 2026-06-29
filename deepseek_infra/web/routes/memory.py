"""Memory list, upsert, delete, and conflict detection routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth


@dataclass(frozen=True)
class MemoryRouteDeps:
    load_memories: Callable[[], list[dict[str, Any]]]
    clear_memories: Callable[[], int]
    normalize_memory_category: Callable[[object, str], str]
    normalize_memory_scope: Callable[[object], str]
    detect_memory_conflicts: Callable[[str, str, str], list[dict[str, Any]]]
    upsert_memory: Callable[..., dict[str, Any]]
    delete_memories_by_query: Callable[[str, list[str]], int]
    delete_memory_by_id: Callable[[str], int]


def create_memory_router(deps: MemoryRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/memory")
    async def api_memory_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"memories": deps.load_memories()})

    @router.post("/api/memory")
    async def api_memory(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "add").strip().lower()
        if action == "list":
            return json_response({"memories": deps.load_memories()})
        if action == "clear":
            return json_response({"ok": True, "deleted": deps.clear_memories()})
        if action == "add":
            content = str(payload.get("content") or "").strip()
            category = deps.normalize_memory_category(payload.get("category"), content)
            scope = deps.normalize_memory_scope(payload.get("scope") or "global")
            pinned = bool(payload.get("pinned"))
            replace_ids = payload.get("replaceIds")
            replace_id_list = [str(item) for item in replace_ids] if isinstance(replace_ids, list) else []
            conflicts = deps.detect_memory_conflicts(content, category, scope)
            unresolved_conflicts = [item for item in conflicts if str(item.get("id") or "") not in set(replace_id_list)]
            if unresolved_conflicts:
                return json_response(
                    {
                        "error": "Memory conflicts with an existing item",
                        "code": ErrorCode.MEMORY_CONFLICT.value,
                        "conflicts": unresolved_conflicts,
                    },
                    status=409,
                )
            item = deps.upsert_memory(content, category=category, scope=scope, source="manual", pinned=pinned, replace_ids=replace_id_list)
            return json_response({"ok": True, "memory": item})
        if action == "delete":
            query = str(payload.get("query") or "").strip()
            scope = deps.normalize_memory_scope(payload.get("scope") or "global")
            scopes = ["global", scope] if scope != "global" else ["global"]
            return json_response({"ok": True, "deleted": deps.delete_memories_by_query(query, scopes)})
        if action == "deletebyid":
            memory_id = str(payload.get("id") or "").strip()
            return json_response({"ok": True, "deleted": deps.delete_memory_by_id(memory_id)})
        raise AppError("Unsupported memory action", code=ErrorCode.INVALID_PAYLOAD)

    @router.delete("/api/memory/{memory_id}")
    async def api_memory_delete_by_id(request: Request, memory_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "deleted": deps.delete_memory_by_id(memory_id)})

    @router.post("/api/memory/conflicts")
    async def api_memory_conflicts(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        content = str(payload.get("content") or "").strip()
        category = deps.normalize_memory_category(payload.get("category"), content)
        scope = deps.normalize_memory_scope(payload.get("scope") or "global")
        conflicts = deps.detect_memory_conflicts(content, category, scope)
        return json_response({"ok": True, "conflicts": conflicts})

    return router
