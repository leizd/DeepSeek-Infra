"""FastAPI routes, local auth enforcement, static serving, and API responses."""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import queue
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Generator
from inspect import signature
from http.cookies import SimpleCookie
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from deepseek_infra.core.config import (
    APP_VERSION,
    DEFAULT_HOST,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILE_BYTES,
    MODEL_ROUTES,
    STATIC_DIR,
    SUPPORTED_MODELS,
    TAVILY_API_KEY,
    settings,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import clean_filename, local_ip, url_with_token
from deepseek_infra.infra.agent_runtime.agent_runs import (
    TERMINAL_STATUSES,
    continue_with_plan,
    create_run as create_agent_run,
    events_after as agent_run_events_after,
    load_run as load_agent_run,
    merge_runtime_payload,
    public_run as public_agent_run,
    registry as agent_run_registry,
    rerun_agent,
    resume_run,
    start_planned_run,
)
from deepseek_infra.infra.rag.context_compressor import compress_context_payload
from deepseek_infra.infra.gateway.deepseek_client import (
    RequestCancelled,
    call_deepseek_cascade,
    preflight_chat_payload,
    preflight_deepseek_payload,
    stream_deepseek,
)
from deepseek_infra.infra.gateway.budget_manager import budget_status as budget_status_for_scope
from deepseek_infra.infra.tool_runtime.tool_policy import read_recent_audit, tool_policy_status
from deepseek_infra.infra.gateway.model_router import cascade_requested as model_router_cascade_requested
from deepseek_infra.infra.gateway.model_router import router_status as model_router_status
from deepseek_infra.infra.gateway.openai_api import (
    openai_chat_completion,
    openai_chat_stream,
    openai_models_list,
    openai_to_internal_payload,
)
from deepseek_infra.infra.gateway.providers.registry import providers_status
from deepseek_infra.infra.observability.health import healthz, readyz
from deepseek_infra.infra.observability.metrics import render_prometheus
from deepseek_infra.infra.gateway.edge_inference import edge_inference_status, edge_unload
from deepseek_infra.infra.rag.files import (
    cached_file_source,
    cleanup_file_cache,
    extract_uploaded_file,
    file_page_image,
    file_page_layout,
    file_page_search,
    file_page_text,
    file_reader_window,
    load_cached_file,
)
from deepseek_infra.infra.tool_runtime.generated_files import download_descriptor, resolve_generated_file, save_generated_file_to_downloads
from deepseek_infra.infra.rag.local_rag import evaluate_recall as evaluate_local_rag_recall
from deepseek_infra.infra.rag.local_rag import rebuild_index as rebuild_local_rag_index
from deepseek_infra.infra.rag.local_rag import status as local_rag_status
from deepseek_infra.infra.rag.local_rag import verify_citation as verify_local_rag_citation
from deepseek_infra.infra.data.memory import (
    clear_memories,
    delete_memories_by_query,
    delete_memory_by_id,
    detect_memory_conflicts,
    load_memories,
    normalize_memory_category,
    normalize_memory_scope,
    upsert_memory,
)
from deepseek_infra.infra.agent_runtime.multi_agent import stream_multi_agent
from deepseek_infra.infra.observability.observability import get_trace, list_traces, trace_status
from deepseek_infra.infra.data.projects import add_project_files, create_project, delete_project, list_projects
from deepseek_infra.infra.data.reminders import create_reminder, delete_reminder, due_reminders, load_reminders
from deepseek_infra.infra.gateway.resiliency import gateway_status
from deepseek_infra.infra.gateway.semantic_cache import clear as clear_semantic_cache
from deepseek_infra.infra.gateway.semantic_cache import status as semantic_cache_status
from deepseek_infra.infra.gateway.title_generator import generate_title_payload
from deepseek_infra.infra.tool_runtime.tools import fetch_url

logger = logging.getLogger("deepseek_infra.server")

AUTH_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
MAX_MULTIPART_FIELD_BYTES = 4_096
MAX_MULTIPART_FILES = 8
MAX_MULTIPART_PARTS = MAX_MULTIPART_FILES + 4
MULTIPART_MEMORY_LIMIT = 4 * 1024 * 1024
MULTIPART_SPOOL_LIMIT = 1024 * 1024
MULTIPART_IMPORT_ERROR = "Multipart parser dependency is not installed or is shadowed by an incompatible package. Run pip install -r requirements.txt."
SHARE_TARGET_TTL_SECONDS = 30 * 60
MAX_SHARE_FIELD_CHARS = 12_000
STREAM_MEDIA_TYPE = "application/x-ndjson; charset=utf-8"
_SHARE_TARGET_LOCK = threading.RLock()
_SHARE_TARGETS: dict[str, tuple[float, dict[str, Any]]] = {}


def multipart_module_issue(candidate: Any) -> str | None:
    parse_options_header = getattr(candidate, "parse_options_header", None)
    parser = getattr(candidate, "MultipartParser", None)
    if not callable(parse_options_header) or not callable(parser):
        missing = [
            name
            for name, value in (("parse_options_header", parse_options_header), ("MultipartParser", parser))
            if not callable(value)
        ]
        return f"incompatible multipart module missing callable {', '.join(missing)}"
    try:
        parameters = signature(parser).parameters
    except (TypeError, ValueError):
        return "MultipartParser signature could not be inspected"
    required = {
        "content_length",
        "strict",
        "header_limit",
        "headersize_limit",
        "part_limit",
        "partsize_limit",
        "spool_limit",
        "memory_limit",
        "disk_limit",
    }
    missing_parameters = sorted(required.difference(parameters))
    if missing_parameters:
        return f"MultipartParser missing parameters: {', '.join(missing_parameters)}"
    return None


def supported_multipart_module(candidate: Any) -> bool:
    return multipart_module_issue(candidate) is None


def load_multipart_module() -> ModuleType | None:
    try:
        import multipart as candidate
    except ModuleNotFoundError:  # pragma: no cover - exercised only before installing requirements
        logger.warning("multipart_dependency_missing")
        return None
    issue = multipart_module_issue(candidate)
    if issue:
        logger.warning("multipart_dependency_incompatible", extra={"detail": issue})
        return None
    return candidate


multipart_module = load_multipart_module()


def create_app() -> FastAPI:
    api = FastAPI(title="DeepSeek Infra", version=APP_VERSION)

    @api.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        apply_common_headers(response, request.url.path)
        return response

    @api.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return json_response(exc.to_response(), status=exc.status)

    @api.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_route_error", extra={"path": request.url.path})
        return json_response({"error": "Server error", "code": ErrorCode.INTERNAL.value}, status=500)

    @api.options("/{path:path}")
    async def options_route(request: Request, path: str = "") -> Response:
        headers = {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }
        allowed_origin = allowed_cors_origin(request.headers.get("Origin", ""), request_port(request))
        if allowed_origin:
            headers["Access-Control-Allow-Origin"] = allowed_origin
            headers["Vary"] = "Origin"
        return Response(status_code=204, headers=headers)

    @api.get("/api/config")
    async def api_config(request: Request) -> JSONResponse:
        require_api_auth(request)
        port = request_port(request)
        computer_url = f"http://127.0.0.1:{port}"
        phone_url = f"http://{local_ip()}:{port}"
        if settings.auth.enabled:
            computer_url = url_with_token(computer_url + "/", settings.auth.token)
            phone_url = url_with_token(phone_url + "/", settings.auth.token)
        return json_response(
            {
                "version": APP_VERSION,
                "hasServerKey": bool(settings.deepseek_api_key),
                "hasSearch": bool(TAVILY_API_KEY),
                "defaultModel": settings.default_model,
                "models": list(SUPPORTED_MODELS),
                "modelRoutes": dict(MODEL_ROUTES),
                "searchModes": ["off", "auto", "on"],
                "uploadLimits": {
                    "fileMaxBytes": MAX_UPLOAD_FILE_BYTES,
                    "requestMaxBytes": MAX_UPLOAD_BYTES,
                    "maxFiles": MAX_MULTIPART_FILES,
                },
                "ocr": {"enabled": settings.ocr.enabled, "mode": settings.ocr.mode, "localOnly": False},
                "edgeInference": edge_inference_status(),
                "localRag": local_rag_status(),
                "tracing": trace_status(),
                "semanticCache": semantic_cache_status(),
                "gateway": gateway_status(),
                "providers": providers_status(),
                "modelRouter": model_router_status(),
                "budget": budget_status_for_scope("global"),
                "toolPolicy": tool_policy_status(),
                "computerUrl": computer_url,
                "phoneUrl": phone_url,
            }
        )

    @api.get("/api/rag/status")
    async def api_rag_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "localRag": local_rag_status()})

    @api.get("/api/budget")
    async def api_budget(request: Request) -> JSONResponse:
        require_api_auth(request)
        scope = str(request.query_params.get("scope") or "global").strip() or "global"
        return json_response({"ok": True, "budget": budget_status_for_scope(scope)})

    @api.get("/api/tool-policy")
    async def api_tool_policy(request: Request) -> JSONResponse:
        require_api_auth(request)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        return json_response({"ok": True, "toolPolicy": tool_policy_status(), "audit": read_recent_audit(limit)})

    @api.post("/api/rag/reindex")
    async def api_rag_reindex(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "reindex").strip().lower()
        if action not in {"reindex", "rebuild"}:
            raise AppError("Unsupported RAG action", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(rebuild_local_rag_index())

    @api.post("/api/rag/verify-citation")
    async def api_rag_verify_citation(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        item_id = str(payload.get("itemId") or "").strip()
        snippet = str(payload.get("snippet") or "")
        if not item_id:
            raise AppError("itemId is required", code=ErrorCode.INVALID_PAYLOAD)
        return json_response({"ok": True, "citation": verify_local_rag_citation(item_id, snippet)})

    @api.post("/api/rag/eval")
    async def api_rag_eval(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise AppError("cases must be a list", code=ErrorCode.INVALID_PAYLOAD)
        k = payload.get("k")
        k_value = int(k) if isinstance(k, int) and k > 0 else 5
        return json_response({"ok": True, "eval": evaluate_local_rag_recall(cases, k=k_value)})

    @api.get("/api/traces")
    async def api_traces(request: Request) -> JSONResponse:
        require_api_auth(request)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        return json_response({"ok": True, "tracing": trace_status(), "traces": list_traces(limit)})

    @api.get("/api/traces/{trace_id}")
    async def api_trace_detail(request: Request, trace_id: str) -> JSONResponse:
        require_api_auth(request)
        trace = get_trace(trace_id)
        if trace is None:
            raise AppError("Trace not found", code=ErrorCode.NOT_FOUND, status=404)
        return json_response({"ok": True, "trace": trace})

    @api.get("/api/semantic-cache/status")
    async def api_semantic_cache_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "semanticCache": semantic_cache_status()})

    @api.post("/api/semantic-cache")
    async def api_semantic_cache_action(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "status").strip().lower()
        if action == "clear":
            return json_response(clear_semantic_cache())
        if action == "status":
            return json_response({"ok": True, "semanticCache": semantic_cache_status()})
        raise AppError("Unsupported semantic cache action", code=ErrorCode.INVALID_PAYLOAD)

    @api.get("/api/gateway/status")
    async def api_gateway_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "gateway": gateway_status()})

    @api.get("/api/edge/status")
    async def api_edge_status(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "edgeInference": edge_inference_status()})

    @api.post("/api/edge/reload")
    async def api_edge_reload(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        action = str(payload.get("action") or "unload").strip().lower()
        if action not in {"unload", "reload"}:
            raise AppError("Unsupported edge action", code=ErrorCode.INVALID_PAYLOAD)
        return json_response(edge_unload())

    @api.get("/api/memory")
    async def api_memory_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"memories": load_memories()})

    @api.get("/api/share-target")
    async def api_share_target(request: Request) -> JSONResponse:
        require_api_auth(request)
        share_id = request.query_params.get("id", "")
        payload = pop_share_target_payload(share_id)
        if payload is None:
            raise AppError("Shared content expired", code=ErrorCode.NOT_FOUND, status=404)
        return json_response({"ok": True, "share": payload})

    @api.get("/api/download")
    async def api_download(request: Request) -> Response:
        require_api_auth(request)
        file_id = request.query_params.get("id", "")
        path = resolve_generated_file(file_id)
        if path is None:
            raise AppError("File does not exist or has expired", code=ErrorCode.NOT_FOUND, status=404)
        data = path.read_bytes()
        media_type, download_name = download_descriptor(path)
        disposition = "inline" if path.suffix.lower() == ".svg" and truthy(request.query_params.get("inline", "")) else "attachment"
        headers = {"Content-Disposition": f'{disposition}; filename="{download_name}"', "Cache-Control": "no-store"}
        return Response(content=data, media_type=media_type, headers=headers)

    @api.get("/api/file-source")
    async def api_file_source(request: Request) -> Response:
        require_api_auth(request)
        file_id = request.query_params.get("fileId", "")
        project_id = request.query_params.get("projectId", "") or None
        cached, path = cached_file_source(file_id, project_id=project_id)
        data = path.read_bytes()
        media_type = original_file_media_type(cached)
        filename = clean_filename(str(cached.get("name") or "document"))
        disposition = "attachment" if truthy(request.query_params.get("download", "")) else "inline"
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": content_disposition_header(disposition, filename),
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type=media_type, headers=headers)

    @api.get("/api/file-page-image")
    async def api_file_page_image(request: Request) -> Response:
        require_api_auth(request)
        file_id = request.query_params.get("fileId", "")
        project_id = request.query_params.get("projectId", "") or None
        cached, data, rendered_page, page_count = file_page_image(
            file_id,
            project_id=project_id,
            page=request.query_params.get("page", "1"),
            scale=request.query_params.get("scale", ""),
        )
        filename = clean_filename(str(cached.get("name") or "document"))
        headers = {
            "X-File-Page": str(rendered_page),
            "X-File-Page-Count": str(page_count),
            "Content-Disposition": content_disposition_header("inline", f"{Path(filename).stem or 'document'}-page-{rendered_page}.png"),
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type="image/png", headers=headers)

    @api.get("/api/file-page-layout")
    async def api_file_page_layout(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            file_page_layout(
                request.query_params.get("fileId", ""),
                project_id=request.query_params.get("projectId", "") or None,
                page=request.query_params.get("page", "1"),
            )
        )

    @api.get("/api/file-page-search")
    async def api_file_page_search(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            file_page_search(
                request.query_params.get("fileId", ""),
                project_id=request.query_params.get("projectId", "") or None,
                query=request.query_params.get("query", ""),
            )
        )

    @api.post("/api/auth/logout")
    async def api_auth_logout(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True}, headers={"Set-Cookie": expired_auth_cookie_header()})

    @api.post("/api/conversations/search")
    async def api_conversation_search(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(conversation_search(await read_json_body(request)))

    @api.post("/api/download-save")
    async def api_download_save(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        result = save_generated_file_to_downloads(str(payload.get("id") or ""), filename=str(payload.get("filename") or ""))
        return json_response(result)

    @api.post("/api/file-text")
    async def api_file_text(request: Request) -> JSONResponse:
        require_api_auth(request)
        files, ocr_enabled, ocr_api_key = await read_multipart_files(request)
        if not files:
            raise AppError("No file uploaded", code=ErrorCode.INVALID_PAYLOAD)
        extracted_files = []
        errors = []
        for file_info in files:
            try:
                extracted_files.append(
                    extract_uploaded_file(
                        file_info["filename"],
                        file_info["content_type"],
                        file_info["data"],
                        ocr_enabled=ocr_enabled,
                        ocr_api_key=ocr_api_key,
                    )
                )
            except AppError as exc:
                errors.append({"name": file_info["filename"], "error": str(exc), "code": exc.code.value, "status": exc.status})
        if not extracted_files and errors:
            error_code = ErrorCode(str(errors[0].get("code") or ErrorCode.INVALID_PAYLOAD.value))
            raise AppError(errors[0]["error"], code=error_code, status=int(errors[0].get("status") or 400))
        cleanup_file_cache()
        return json_response({"files": extracted_files, "errors": errors, "file": extracted_files[0] if extracted_files else None})

    @api.post("/api/project-files")
    async def api_project_files(request: Request) -> JSONResponse:
        require_api_auth(request)
        project_id = request.query_params.get("projectId", "")
        files, ocr_enabled, ocr_api_key = await read_multipart_files(request)
        if not files:
            raise AppError("No file uploaded", code=ErrorCode.INVALID_PAYLOAD)
        documents = add_project_files(project_id, files, ocr_enabled=ocr_enabled, ocr_api_key=ocr_api_key)
        return json_response({"ok": True, "documents": documents})

    @api.post("/api/file-chunk")
    async def api_file_chunk(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        file_id = str(payload.get("fileId") or "")
        project_id = str(payload.get("projectId") or "") or None
        try:
            chunk_index = max(0, int(payload.get("chunkIndex") or 0) - 1)
        except (TypeError, ValueError) as exc:
            raise AppError("Invalid chunk index", code=ErrorCode.INVALID_PAYLOAD, status=400) from exc
        cached = load_cached_file(file_id, project_id=project_id)
        raw_chunks = cached.get("chunks")
        chunks: list[Any] = raw_chunks if isinstance(raw_chunks, list) else []
        if chunk_index >= len(chunks):
            raise AppError("Chunk not found", code=ErrorCode.NOT_FOUND, status=404)
        chunk = chunks[chunk_index]
        if not isinstance(chunk, dict):
            raise AppError("Chunk not found", code=ErrorCode.NOT_FOUND, status=404)
        return json_response(
            {
                "file": {"name": cached.get("name"), "kind": cached.get("kind"), "fileId": file_id, "projectId": project_id or ""},
                "chunk": chunk,
            }
        )

    @api.post("/api/file-reader")
    async def api_file_reader(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        return json_response(
            file_reader_window(
                str(payload.get("fileId") or ""),
                project_id=str(payload.get("projectId") or "") or None,
                chunk_start=payload.get("chunkStart") or 1,
                chunk_count=payload.get("chunkCount") or 6,
            )
        )

    @api.post("/api/file-page-text")
    async def api_file_page_text(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        return json_response(
            file_page_text(
                str(payload.get("fileId") or ""),
                project_id=str(payload.get("projectId") or "") or None,
                page=payload.get("page") or 1,
            )
        )

    @api.post("/api/fetch-url")
    async def api_fetch_url(request: Request) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request)
        return json_response({"ok": True, "page": fetch_url(str(payload.get("url") or ""))})

    @api.post("/api/compress-context")
    async def api_context_compress(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(compress_context_payload(await read_json_body(request)))

    @api.post("/api/title")
    async def api_title(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(generate_title_payload(await read_json_body(request)))

    @api.post("/api/memory")
    async def api_memory(request: Request) -> JSONResponse:
        require_api_auth(request)
        result = memory_action(await read_json_body(request))
        status = int(result.pop("_status", 200))
        return json_response(result, status=status)

    @api.post("/api/projects")
    async def api_projects(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(project_action(await read_json_body(request)))

    @api.post("/api/reminders")
    async def api_reminders(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(reminder_action(await read_json_body(request)))

    @api.post("/api/reminders/due")
    async def api_due_reminders(request: Request) -> JSONResponse:
        require_api_auth(request)
        await read_json_body(request)
        return json_response({"reminders": due_reminders()})

    @api.post("/api/chat")
    async def api_chat(request: Request) -> Response:
        require_api_auth(request)
        payload = await read_json_body(request, max_bytes=16_000_000)
        payload = {**payload, "localBaseUrl": request_base_url(request)}
        if payload.get("stream"):
            preflight_chat_payload(payload)
            return StreamingResponse(
                chat_event_stream(payload),
                media_type=STREAM_MEDIA_TYPE,
                headers={"X-Accel-Buffering": "no"},
            )
        # Non-stream chat goes through the cascade entry, which transparently runs
        # plain call_deepseek unless the request opted into cascade inference.
        return json_response(call_deepseek_cascade(payload))

    @api.post("/v1/chat/completions")
    async def v1_chat_completions(request: Request) -> Response:
        """OpenAI-compatible chat completions over the local DeepSeek runtime."""
        require_api_auth(request)
        body = await read_json_body(request, max_bytes=16_000_000)
        payload = openai_to_internal_payload(body, local_base_url=request_base_url(request))
        model = str(payload["model"])
        if payload.get("stream"):
            return StreamingResponse(
                openai_chat_stream(payload, model),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        return json_response(openai_chat_completion(payload, model))

    @api.get("/v1/models")
    async def v1_models(request: Request) -> JSONResponse:
        """OpenAI-compatible model listing from the configured catalog."""
        require_api_auth(request)
        return json_response(openai_models_list())

    @api.get("/healthz")
    async def healthz_route() -> JSONResponse:
        """Liveness probe (unauthenticated)."""
        return json_response(healthz())

    @api.get("/readyz")
    async def readyz_route() -> JSONResponse:
        """Readiness probe (unauthenticated)."""
        return json_response(readyz())

    @api.get("/metrics")
    async def metrics_route() -> Response:
        """Prometheus metrics (unauthenticated; bind to 127.0.0.1 by default)."""
        return Response(content=render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @api.post("/api/agent-runs")
    async def api_agent_runs_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        body = await read_json_body(request)
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise AppError("payload must be an object", code=ErrorCode.INVALID_PAYLOAD)
        payload["agentMode"] = True
        preflight_deepseek_payload(payload)
        confirm_plan = bool(body.get("confirmPlan"))
        agent_preset = str(body.get("agentPreset") or "full")
        run = create_agent_run(
            payload,
            confirm_plan=confirm_plan,
            agent_preset=agent_preset,
            conversation_id=str(body.get("conversationId") or ""),
            message_id=str(body.get("messageId") or ""),
        )
        agent_run_registry.ensure_started(
            run["runId"],
            start_planned_run,
            run["runId"],
            payload,
            confirm_plan=confirm_plan,
            agent_preset=agent_preset,
        )
        return json_response({"ok": True, "runId": run["runId"], "run": run}, status=201)

    @api.post("/api/agent-runs/{run_id}/{action}")
    async def api_agent_runs_action(request: Request, run_id: str, action: str) -> JSONResponse:
        require_api_auth(request)
        body = await read_json_body(request)
        stored = load_agent_run(run_id)
        stored_payload = stored.get("requestPayload")
        runtime_payload = merge_runtime_payload(stored_payload if isinstance(stored_payload, dict) else {}, body.get("payload"))
        runtime_payload["agentMode"] = True
        preflight_deepseek_payload(runtime_payload)
        if action == "plan":
            if str(stored.get("status") or "") != "awaiting_plan":
                raise AppError("Agent run is not awaiting plan confirmation", code=ErrorCode.INVALID_PAYLOAD, status=409)
            plan = body.get("plan")
            if plan is not None and not isinstance(plan, list):
                raise AppError("plan must be a list", code=ErrorCode.INVALID_PAYLOAD)
            started = agent_run_registry.ensure_started(run_id, continue_with_plan, run_id, runtime_payload, plan)
            return json_response({"ok": True, "started": started, "run": public_agent_run(load_agent_run(run_id))})
        if action == "rerun":
            status = str(stored.get("status") or "")
            if status in {"created", "planning", "running"}:
                raise AppError("Agent run is still running", code=ErrorCode.INVALID_PAYLOAD, status=409)
            agent_id = str(body.get("agentId") or "").strip()
            if not agent_id:
                raise AppError("agentId is required", code=ErrorCode.INVALID_PAYLOAD)
            started = agent_run_registry.ensure_started(
                run_id,
                rerun_agent,
                run_id,
                runtime_payload,
                agent_id=agent_id,
                resynthesize=body.get("resynthesize") is not False,
            )
            return json_response({"ok": True, "started": started, "run": public_agent_run(load_agent_run(run_id))})
        if action == "resume":
            status = str(stored.get("status") or "")
            if status in {"created", "planning", "running"}:
                raise AppError("Agent run is still running", code=ErrorCode.INVALID_PAYLOAD, status=409)
            if status == "awaiting_plan":
                raise AppError("Confirm the plan before resuming", code=ErrorCode.INVALID_PAYLOAD, status=409)
            started = agent_run_registry.ensure_started(run_id, resume_run, run_id, runtime_payload)
            return json_response({"ok": True, "started": started, "run": public_agent_run(load_agent_run(run_id))})
        raise AppError("Unsupported Agent run action", code=ErrorCode.NOT_FOUND, status=404)

    @api.get("/api/agent-runs/{run_id}")
    async def api_agent_runs_detail(request: Request, run_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "run": public_agent_run(load_agent_run(run_id))})

    @api.get("/api/agent-runs/{run_id}/events")
    async def api_agent_runs_events(request: Request, run_id: str) -> JSONResponse:
        require_api_auth(request)
        after = parse_event_cursor(request.query_params.get("after", "-1"))
        return json_response({"ok": True, "events": agent_run_events_after(run_id, after)})

    @api.get("/api/agent-runs/{run_id}/stream")
    async def api_agent_runs_stream(request: Request, run_id: str) -> StreamingResponse:
        require_api_auth(request)
        after = parse_event_cursor(request.query_params.get("after", "-1"))
        return StreamingResponse(
            agent_run_event_stream(run_id, after),
            media_type=STREAM_MEDIA_TYPE,
            headers={"X-Accel-Buffering": "no"},
        )

    @api.post("/share-target")
    async def share_target_post(request: Request) -> RedirectResponse:
        require_allowed_host(request)
        fields, files = await read_multipart_form(request)
        prompt = share_target_prompt(
            title=first_form_value(fields, "title"),
            text=first_form_value(fields, "text"),
            url=first_form_value(fields, "url"),
        )
        attachments = []
        errors = []
        for file_info in files:
            try:
                attachments.append(
                    extract_uploaded_file(
                        file_info["filename"],
                        file_info["content_type"],
                        file_info["data"],
                        ocr_enabled=settings.ocr.enabled,
                        ocr_api_key=first_form_value(fields, "apiKey"),
                    )
                )
            except AppError as exc:
                errors.append({"name": file_info["filename"], "error": str(exc), "code": exc.code.value, "status": exc.status})
        if not prompt and not attachments and errors:
            prompt = "Please process this shared file. Some files could not be extracted; the import errors are attached."
        share_id = store_share_target_payload({"prompt": prompt, "attachments": attachments, "errors": errors})
        return RedirectResponse("/?" + urlencode({"share": share_id}), status_code=303)

    @api.api_route("/api/{path:path}", methods=["GET", "POST"])
    async def api_not_found(request: Request, path: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"error": "Not found", "code": ErrorCode.NOT_FOUND.value}, status=404)

    @api.api_route("/{static_path:path}", methods=["GET", "HEAD"])
    async def static_or_index(request: Request, static_path: str = "") -> Response:
        auth_response = handle_auth_token_redirect(request)
        if auth_response is not None:
            return auth_response
        static_file = resolve_static_file(request.url.path)
        if static_file is None:
            return Response("Not found", status_code=404, media_type="text/plain")
        media_type = static_media_type(static_file)
        return FileResponse(static_file, media_type=media_type)

    return api


app = create_app()


class FastAPIServer:
    """Small lifecycle adapter so the launcher can keep using serve/shutdown calls."""

    def __init__(self, app_instance: FastAPI, bind_socket: socket.socket, host: str, port: int) -> None:
        self.app = app_instance
        self.server_address = bind_socket.getsockname()
        self._socket = bind_socket
        self._stopped = threading.Event()
        self._config = uvicorn.Config(
            app_instance,
            host=host,
            port=port,
            log_level="warning",
            lifespan="off",
            access_log=False,
            # launch.bat starts us under pythonw, which has no console, so
            # sys.stdout/sys.stderr are None. uvicorn's default log formatter calls
            # sys.stdout.isatty() unless use_colors is set explicitly, which then
            # raises and surfaces as "Unable to configure formatter 'default'".
            use_colors=False,
        )
        self._server = uvicorn.Server(self._config)

    def serve_forever(self, poll_interval: float | None = None) -> None:
        try:
            self._server.run(sockets=[self._socket])
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        self._server.should_exit = True
        self._stopped.wait(timeout=5)

    def server_close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def create_server(start_port: int, host: str | None = None) -> tuple[FastAPIServer, int]:
    bind_host = host if host is not None else DEFAULT_HOST
    last_error: OSError | None = None
    for port in range(start_port, start_port + 20):
        sock: socket.socket | None = None
        try:
            sock = open_bind_socket(bind_host, port)
            actual_port = int(sock.getsockname()[1])
            return FastAPIServer(app, sock, bind_host, actual_port), actual_port
        except OSError as exc:
            last_error = exc
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            continue
    raise SystemExit(f"No available port found from {start_port} to {start_port + 19}: {last_error}")


def open_bind_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family=family, type=socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(socket.SOMAXCONN)
    sock.set_inheritable(False)
    return sock


def apply_common_headers(response: Response, path: str) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if path == "/api/file-source":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: http: https:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"


def json_response(data: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers)


def handle_auth_token_redirect(request: Request) -> Response | None:
    if request.url.path != "/" or not settings.auth.enabled:
        return None
    token = request.query_params.get("token", "")
    if not token:
        return None
    if not secrets.compare_digest(token, settings.auth.token):
        return json_response(AppError("Auth required", code=ErrorCode.UNAUTHORIZED, status=401).to_response(), status=401)
    if truthy(request.query_params.get("desktop", "")):
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            return json_response({"error": "Not found", "code": ErrorCode.NOT_FOUND.value}, status=404)
        return FileResponse(
            index_path,
            media_type="text/html; charset=utf-8",
            headers={"Set-Cookie": auth_cookie_header(settings.auth.token)},
        )
    return RedirectResponse("/", status_code=302, headers={"Set-Cookie": auth_cookie_header(settings.auth.token)})


def require_api_auth(request: Request) -> None:
    if not settings.auth.enabled:
        return
    require_allowed_host(request)
    provided = auth_token_from_headers(request.headers.get("Authorization", ""), request.headers.get("Cookie", ""))
    if not secrets.compare_digest(provided, settings.auth.token):
        raise AppError("Auth required", code=ErrorCode.UNAUTHORIZED, status=401)


def require_allowed_host(request: Request) -> None:
    host = host_without_port(request.headers.get("Host", ""))
    if host not in allowed_auth_hosts():
        raise AppError("Host not allowed", code=ErrorCode.FORBIDDEN, status=403)


async def read_json_body(request: Request, max_bytes: int = 2_000_000) -> dict[str, Any]:
    content_length = parse_content_length(request.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise AppError("Request body is empty", code=ErrorCode.INVALID_PAYLOAD)
    if content_length > max_bytes:
        raise AppError("Request body is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid JSON: {exc}", code=ErrorCode.INVALID_PAYLOAD) from exc
    if not isinstance(body, dict):
        raise AppError("Request body must be a JSON object", code=ErrorCode.INVALID_PAYLOAD)
    return body


async def read_multipart_files(request: Request) -> tuple[list[dict[str, Any]], bool, str]:
    fields, uploads = await read_multipart_form(request)
    ocr_enabled = settings.ocr.enabled
    for value in fields.get("ocrEnabled", []):
        ocr_enabled = truthy(value)
    return uploads, ocr_enabled, first_form_value(fields, "apiKey")


async def read_multipart_form(request: Request) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    content_type = request.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise AppError("Expected multipart/form-data", code=ErrorCode.INVALID_PAYLOAD)
    content_length = parse_content_length(request.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise AppError("Upload body is empty", code=ErrorCode.INVALID_PAYLOAD)
    if content_length > MAX_UPLOAD_BYTES:
        raise AppError(
            f"Upload body is too large. Maximum request size is {format_upload_limit(MAX_UPLOAD_BYTES)}.",
            code=ErrorCode.UPLOAD_TOO_LARGE,
            status=413,
        )
    if multipart_module is None or not supported_multipart_module(multipart_module):
        if multipart_module is not None:
            logger.warning("multipart_dependency_incompatible", extra={"detail": multipart_module_issue(multipart_module)})
        raise AppError(MULTIPART_IMPORT_ERROR, code=ErrorCode.INTERNAL, status=500)

    media_type, options = multipart_module.parse_options_header(content_type)
    boundary = options.get("boundary", "")
    if media_type != "multipart/form-data" or not boundary:
        raise AppError("Upload body is not multipart/form-data", code=ErrorCode.INVALID_PAYLOAD)

    raw = await request.body()
    uploads: list[dict[str, Any]] = []
    parser = multipart_module.MultipartParser(
        io.BytesIO(raw),
        boundary,
        content_length=content_length,
        strict=True,
        header_limit=8,
        headersize_limit=4_096,
        part_limit=MAX_MULTIPART_PARTS,
        partsize_limit=MAX_UPLOAD_FILE_BYTES,
        spool_limit=MULTIPART_SPOOL_LIMIT,
        memory_limit=MULTIPART_MEMORY_LIMIT,
        disk_limit=MAX_UPLOAD_BYTES,
    )
    fields: dict[str, list[str]] = {}
    try:
        for part in parser:
            try:
                if part.filename:
                    if len(uploads) >= MAX_MULTIPART_FILES:
                        raise AppError("Too many uploaded files", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
                    filename = clean_filename(part.filename or "")
                    if filename:
                        part_size = max(int(part.size or 0), len(part.raw or b""))
                        if part_size > MAX_UPLOAD_FILE_BYTES:
                            raise AppError(
                                f"File is too large. Maximum file size is {format_upload_limit(MAX_UPLOAD_FILE_BYTES)}.",
                                code=ErrorCode.UPLOAD_TOO_LARGE,
                                status=413,
                            )
                        uploads.append(
                            {
                                "filename": filename,
                                "content_type": part.content_type or "application/octet-stream",
                                "data": part.raw,
                            }
                        )
                    continue
                if part.size > MAX_MULTIPART_FIELD_BYTES:
                    raise AppError("Upload field is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
                name = str(part.name or "").strip()
                if name:
                    fields.setdefault(name, []).append(str(part.value or "")[:MAX_SHARE_FIELD_CHARS])
            finally:
                part.close()
    except AppError:
        raise
    except Exception as exc:
        translated = translate_multipart_error(exc)
        if translated is None:
            raise
        raise translated from exc
    return fields, uploads


def encode_stream_event(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def emit_cascade_as_stream(payload: dict[str, Any], write_event: Callable[[dict[str, Any]], None]) -> None:
    result = call_deepseek_cascade(payload)
    content = str(result.get("content") or "")
    reasoning = str(result.get("reasoning") or "")
    if reasoning:
        write_event({"type": "reasoning", "text": reasoning})
    if content:
        write_event({"type": "content", "text": content})
    write_event(
        {
            "type": "done",
            "id": result.get("id"),
            "model": result.get("model"),
            "content": content,
            "reasoning": reasoning,
            "usage": result.get("usage") or {},
            "search": result.get("search"),
            "memorySuggestions": result.get("memorySuggestions") or [],
            "diagnostics": result.get("diagnostics") or {},
        }
    )


def chat_event_stream(payload: dict[str, Any]) -> Generator[bytes, None, None]:
    cancel_event = threading.Event()
    events: queue.Queue[bytes | None] = queue.Queue()

    def write_event(data: dict[str, Any]) -> None:
        if cancel_event.is_set():
            raise RequestCancelled()
        events.put(encode_stream_event(data))

    def worker() -> None:
        try:
            if payload.get("agentMode") is True:
                stream_multi_agent(payload, write_event, cancel_event=cancel_event)
            elif model_router_cascade_requested(payload):
                # Cascade is non-stream (draft → gate → refine); replay its final
                # result as stream events so the streaming UI works unchanged.
                emit_cascade_as_stream(payload, write_event)
            else:
                stream_deepseek(payload, write_event, cancel_event=cancel_event)
        except RequestCancelled:
            return
        except Exception:
            logger.exception("chat_stream_error")
            events.put(encode_stream_event({"type": "error", "error": "Server error", "code": ErrorCode.INTERNAL.value}))
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, name="deepseek-chat-stream", daemon=True)
    thread.start()
    try:
        while True:
            item = events.get()
            if item is None:
                break
            yield item
    finally:
        cancel_event.set()


def agent_run_event_stream(run_id: str, after: int) -> Generator[bytes, None, None]:
    cursor = after
    try:
        while True:
            events = agent_run_events_after(run_id, cursor)
            for event in events:
                yield encode_stream_event(event)
                cursor = max(cursor, int(event.get("index", cursor)))
            run = load_agent_run(run_id)
            status = str(run.get("status") or "")
            if status in {*TERMINAL_STATUSES, "awaiting_plan"} and int(run.get("nextIndex") or 0) - 1 <= cursor:
                break
            agent_run_registry.wait_for_event(run_id)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return


def memory_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        return {"memories": load_memories()}
    if action == "clear":
        return {"ok": True, "deleted": clear_memories()}
    if action == "add":
        content = str(payload.get("content") or "").strip()
        category = normalize_memory_category(payload.get("category"), content)
        scope = normalize_memory_scope(payload.get("scope") or "global")
        pinned = bool(payload.get("pinned"))
        replace_ids = payload.get("replaceIds")
        replace_id_list = [str(item) for item in replace_ids] if isinstance(replace_ids, list) else []
        conflicts = detect_memory_conflicts(content, category=category, scope=scope)
        unresolved_conflicts = [item for item in conflicts if str(item.get("id") or "") not in set(replace_id_list)]
        if unresolved_conflicts:
            return {
                "error": "Memory conflicts with an existing item",
                "code": ErrorCode.MEMORY_CONFLICT.value,
                "conflicts": unresolved_conflicts,
                "_status": 409,
            }
        item = upsert_memory(content, category=category, scope=scope, source="manual", pinned=pinned, replace_ids=replace_id_list)
        return {"ok": True, "memory": item}
    if action == "delete":
        query = str(payload.get("query") or "").strip()
        scope = normalize_memory_scope(payload.get("scope") or "global")
        scopes = ["global", scope] if scope != "global" else ["global"]
        return {"ok": True, "deleted": delete_memories_by_query(query, scopes=scopes)}
    if action == "deletebyid":
        memory_id = str(payload.get("id") or "").strip()
        return {"ok": True, "deleted": delete_memory_by_id(memory_id)}
    raise AppError("Unsupported memory action", code=ErrorCode.INVALID_PAYLOAD)


def project_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        return {"projects": list_projects()}
    if action == "create":
        return {"ok": True, "project": create_project(str(payload.get("name") or ""))}
    if action == "delete":
        return {"ok": True, "deleted": delete_project(str(payload.get("id") or ""))}
    raise AppError("Unsupported project action", code=ErrorCode.INVALID_PAYLOAD)


def reminder_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "list").strip().lower()
    if action == "list":
        return {"reminders": load_reminders()}
    if action == "create":
        return {"ok": True, "reminder": create_reminder(payload)}
    if action == "delete":
        return {"ok": True, "deleted": delete_reminder(str(payload.get("id") or ""))}
    raise AppError("Unsupported reminder action", code=ErrorCode.INVALID_PAYLOAD)


def conversation_search(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip().lower()
    conversations = payload.get("conversations")
    if not query:
        return {"results": []}
    if not isinstance(conversations, list):
        raise AppError("conversations must be a list", code=ErrorCode.INVALID_PAYLOAD)

    results: list[dict[str, Any]] = []
    for conversation in conversations[:200]:
        if not isinstance(conversation, dict):
            continue
        matches = conversation_search_matches(conversation, query)
        if matches:
            results.append(
                {
                    "id": str(conversation.get("id") or ""),
                    "title": str(conversation.get("title") or "New conversation")[:160],
                    "updatedAt": conversation.get("updatedAt"),
                    "favorite": bool(conversation.get("favorite")),
                    "tags": conversation_tags(conversation),
                    "matches": matches[:5],
                }
            )
    return {"results": results[:50]}


def resolve_static_file(raw_path: str) -> Path | None:
    path = unquote(raw_path)
    if path == "/" or path == "":
        return STATIC_DIR / "index.html"
    parts = [part for part in path.split("/") if part and part not in {".", ".."}]
    if not parts:
        return STATIC_DIR / "index.html"
    static_root = STATIC_DIR.resolve()
    candidate = (static_root / Path(*parts)).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def static_media_type(path: Path) -> str:
    mapping = {
        ".css": "text/css",
        ".ico": "image/x-icon",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".webmanifest": "application/manifest+json",
        ".woff2": "font/woff2",
    }
    return mapping.get(path.suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def request_port(request: Request) -> int:
    server = request.scope.get("server")
    if isinstance(server, tuple) and len(server) >= 2:
        try:
            return int(server[1])
        except (TypeError, ValueError):
            pass
    host = str(request.headers.get("Host") or "")
    try:
        parsed = urlsplit(f"http://{host}")
        return int(parsed.port or 80)
    except (TypeError, ValueError):
        return 0


def request_base_url(request: Request) -> str:
    host_header = str(request.headers.get("Host") or "").split(",", 1)[0].strip()
    if host_header and "/" not in host_header and "\\" not in host_header and host_without_port(host_header) in allowed_auth_hosts():
        return f"http://{host_header}"
    port = request_port(request)
    return f"http://127.0.0.1:{port}" if port else "http://127.0.0.1"


def parse_content_length(value: str) -> int:
    try:
        content_length = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError("Invalid Content-Length", code=ErrorCode.INVALID_PAYLOAD) from exc
    if content_length < 0:
        raise AppError("Invalid Content-Length", code=ErrorCode.INVALID_PAYLOAD)
    return content_length


def parse_agent_run_action(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "api" or parts[1] != "agent-runs":
        raise AppError("Agent run not found", code=ErrorCode.NOT_FOUND, status=404)
    run_id = parts[2]
    action = parts[3] if len(parts) > 3 else ""
    if len(parts) > 4:
        raise AppError("Agent run not found", code=ErrorCode.NOT_FOUND, status=404)
    return run_id, action


def parse_event_cursor(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def format_upload_limit(value: int) -> str:
    return f"{max(1, round(value / 1_000_000))} MB"


def first_form_value(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name) or []
    return str(values[0] if values else "").strip()[:MAX_SHARE_FIELD_CHARS]


def share_target_prompt(*, title: str, text: str, url: str) -> str:
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if url:
        parts.append(f"URL: {url}")
    if text:
        parts.append(text)
    if not parts:
        return ""
    return "Please help me process this shared content:\n\n" + "\n\n".join(parts)


def cleanup_share_target_payloads(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - SHARE_TARGET_TTL_SECONDS
    expired = [share_id for share_id, (created_at, _) in _SHARE_TARGETS.items() if created_at < cutoff]
    for share_id in expired:
        _SHARE_TARGETS.pop(share_id, None)


def store_share_target_payload(payload: dict[str, Any]) -> str:
    with _SHARE_TARGET_LOCK:
        cleanup_share_target_payloads()
        share_id = secrets.token_urlsafe(12)
        _SHARE_TARGETS[share_id] = (time.time(), payload)
        return share_id


def pop_share_target_payload(share_id: str) -> dict[str, Any] | None:
    value = str(share_id or "").strip()
    if not value:
        return None
    with _SHARE_TARGET_LOCK:
        cleanup_share_target_payloads()
        item = _SHARE_TARGETS.pop(value, None)
    return item[1] if item else None


def auth_cookie_header(token: str) -> str:
    cookie = SimpleCookie()
    cookie["auth_token"] = token
    cookie["auth_token"]["path"] = "/"
    cookie["auth_token"]["samesite"] = "Strict"
    cookie["auth_token"]["httponly"] = True
    cookie["auth_token"]["max-age"] = str(AUTH_COOKIE_MAX_AGE_SECONDS)
    return cookie.output(header="").strip()


def expired_auth_cookie_header() -> str:
    cookie = SimpleCookie()
    cookie["auth_token"] = ""
    cookie["auth_token"]["path"] = "/"
    cookie["auth_token"]["samesite"] = "Strict"
    cookie["auth_token"]["httponly"] = True
    cookie["auth_token"]["max-age"] = "0"
    return cookie.output(header="").strip()


def translate_multipart_error(exc: Exception) -> AppError | None:
    status = int(getattr(exc, "http_status", 0) or 0)
    if status == 0:
        return None
    code = ErrorCode.UPLOAD_TOO_LARGE if status == 413 else ErrorCode.INVALID_PAYLOAD
    message = str(exc) if status == 413 else "Invalid multipart upload"
    return AppError(message, code=code, status=status)


def conversation_tags(conversation: dict[str, Any]) -> list[str]:
    tags = conversation.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(tag).strip()[:32] for tag in tags if str(tag or "").strip()][:12]


def conversation_search_matches(conversation: dict[str, Any], query: str) -> list[dict[str, Any]]:
    haystacks: list[tuple[str, str, str]] = [
        ("title", "", str(conversation.get("title") or "")),
        ("tag", "", " ".join(conversation_tags(conversation))),
    ]
    messages = conversation.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            haystacks.append(
                (
                    "message",
                    str(message.get("id") or ""),
                    f"{message.get('role') or ''} {message.get('content') or ''} {message.get('reasoning') or ''}",
                )
            )

    matches: list[dict[str, Any]] = []
    for kind, message_id, text in haystacks:
        value = str(text or "")
        index = value.lower().find(query)
        if index < 0:
            continue
        start = max(0, index - 48)
        end = min(len(value), index + len(query) + 96)
        matches.append({"kind": kind, "messageId": message_id, "snippet": value[start:end].strip()})
    return matches


def host_without_port(value: str) -> str:
    host = value.strip().split(",", 1)[0].strip()
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[").lower()
    return host.split(":", 1)[0].lower()


def allowed_auth_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    configured_host = settings.default_host.strip().lower()
    if configured_host and configured_host != "0.0.0.0":
        hosts.add(configured_host)
    lan_ip = local_ip()
    if lan_ip:
        hosts.add(lan_ip.lower())
    hosts.update(host_without_port(host) for host in settings.auth.allowed_hosts)
    return {host for host in hosts if host}


def allowed_cors_origin(origin: str, port: int) -> str:
    origin = origin.strip()
    if not origin or port <= 0:
        return ""
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "http" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
        return ""
    if origin_port is None:
        origin_port = 80
    if origin_port != port:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in allowed_auth_hosts():
        return ""
    return origin


def original_file_media_type(cached: dict[str, Any]) -> str:
    kind = str(cached.get("kind") or "").lower()
    raw_type = str(cached.get("type") or "").split(";", 1)[0].strip().lower()
    if kind == "pdf" or raw_type == "application/pdf":
        return "application/pdf"
    if kind == "image" and raw_type.startswith("image/") and raw_type not in {"image/svg+xml"}:
        return raw_type
    if raw_type.startswith("text/") and raw_type != "text/html":
        return f"{raw_type}; charset=utf-8"
    if kind in {"txt", "text", "md", "csv", "json", "xml", "log", "py", "js", "ts", "css"}:
        return "text/plain; charset=utf-8"
    if raw_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        return raw_type
    return "application/octet-stream"


def content_disposition_header(disposition: str, filename: str) -> str:
    safe_name = clean_filename(filename)
    ascii_name = safe_name.encode("ascii", errors="ignore").decode("ascii") or "document"
    ascii_name = ascii_name.replace('"', "")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe_name)}'


def auth_token_from_headers(authorization: str, cookie_header: str) -> str:
    prefix = "bearer "
    authorization = authorization.strip()
    if authorization.lower().startswith(prefix):
        return authorization[len(prefix) :].strip()

    if cookie_header:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get("auth_token")
        if morsel is not None:
            return morsel.value
    return ""


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def redact_sensitive_query(value: str) -> str:
    placeholders: list[tuple[str, str]] = []

    def replace_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlsplit(url)
        if not parsed.query:
            return url
        query = parse_qs(parsed.query, keep_blank_values=True)
        if "token" not in query:
            return url
        query["token"] = ["[redacted]"]
        redacted = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment))
        placeholder = f"__deepseek_redacted_url_{len(placeholders)}__"
        placeholders.append((placeholder, redacted))
        return placeholder

    value = re.sub(r"https?://[^\s\"']+", replace_url, value)

    def replace_path(match: re.Match[str]) -> str:
        path = match.group(0)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if "token" not in query:
            return path
        query["token"] = ["[redacted]"]
        return urlunsplit(("", "", parsed.path, urlencode(query, doseq=True), parsed.fragment))

    value = re.sub(r"/[^\s\"']*\?[^ \t\"']*token=[^ \t\"']*", replace_path, value)
    for placeholder, redacted in placeholders:
        value = value.replace(placeholder, redacted)
    return value
