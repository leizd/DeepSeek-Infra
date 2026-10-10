"""HTTP route registration for trace listing, detail, export, and routed React pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.observability.export import export_trace, redact_trace_for_response
from deepseek_infra.infra.observability.observability import get_trace, list_traces, trace_status

RequireAuth = Callable[[Request], None]
TraceGetter = Callable[[str], dict[str, Any] | None]
TraceExporter = Callable[[str], dict[str, Any] | None]
TraceLister = Callable[[int | None], list[dict[str, Any]]]
TraceStatus = Callable[[], dict[str, Any]]
FrontendIndex = Callable[[], Path]


def register_trace_routes(
    api: FastAPI,
    *,
    require_api_auth: RequireAuth,
    frontend_index_path: FrontendIndex,
    get_trace_fn: TraceGetter = get_trace,
    export_trace_fn: TraceExporter = export_trace,
    list_traces_fn: TraceLister = list_traces,
    trace_status_fn: TraceStatus = trace_status,
) -> None:
    @api.get("/api/traces")
    async def api_traces(request: Request) -> JSONResponse:
        require_api_auth(request)
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            limit = 50
        traces = [redact_trace_for_response(trace) for trace in list_traces_fn(limit)]
        return JSONResponse({"ok": True, "tracing": trace_status_fn(), "traces": traces})

    @api.get("/api/traces/{trace_id}")
    async def api_trace_detail(request: Request, trace_id: str) -> JSONResponse:
        require_api_auth(request)
        trace = get_trace_fn(trace_id)
        if trace is None:
            raise AppError("Trace not found", code=ErrorCode.NOT_FOUND, status=404)
        return JSONResponse({"ok": True, "trace": redact_trace_for_response(trace)})

    @api.get("/api/traces/{trace_id}/export.json")
    async def api_trace_export(request: Request, trace_id: str) -> JSONResponse:
        require_api_auth(request)
        trace = export_trace_fn(trace_id)
        if trace is None:
            raise AppError("Trace not found", code=ErrorCode.NOT_FOUND, status=404)
        filename = export_filename(trace_id)
        return JSONResponse(trace, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @api.get("/trace/{trace_id}")
    async def trace_standalone_page(request: Request, trace_id: str) -> Response:
        require_api_auth(request)
        if get_trace_fn(trace_id) is None:
            raise AppError("Trace not found", code=ErrorCode.NOT_FOUND, status=404)
        return FileResponse(frontend_index_path(), media_type="text/html; charset=utf-8")


def export_filename(trace_id: str) -> str:
    safe_id = "".join(ch for ch in str(trace_id or "") if ch.isalnum() or ch in {"-", "_"})[:32]
    return f"trace-{safe_id or 'export'}.json"
