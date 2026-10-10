"""File preview and source routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from deepseek_infra.core.utils import clean_filename
from deepseek_infra.web.http_utils import content_disposition_header, json_response, require_api_auth, truthy


@dataclass(frozen=True)
class FilesRouteDeps:
    cached_file_source: Callable[[str, str | None], tuple[dict[str, Any], Path]]
    file_page_image: Callable[[str, str | None, str, str], tuple[dict[str, Any], bytes, int, int]]
    file_page_layout: Callable[[str, str | None, str], dict[str, Any]]
    file_page_search: Callable[[str, str | None, str], dict[str, Any]]
    original_file_media_type: Callable[[dict[str, Any]], str]


def create_files_router(deps: FilesRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/file-source")
    async def api_file_source(request: Request) -> Response:
        require_api_auth(request)
        file_id = request.query_params.get("fileId", "")
        project_id = request.query_params.get("projectId", "") or None
        cached, path = deps.cached_file_source(file_id, project_id)
        data = path.read_bytes()
        media_type = deps.original_file_media_type(cached)
        filename = clean_filename(str(cached.get("name") or "document"))
        disposition = "attachment" if truthy(request.query_params.get("download", "")) else "inline"
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": content_disposition_header(disposition, filename),
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type=media_type, headers=headers)

    @router.get("/api/file-page-image")
    async def api_file_page_image(request: Request) -> Response:
        require_api_auth(request)
        file_id = request.query_params.get("fileId", "")
        project_id = request.query_params.get("projectId", "") or None
        cached, data, rendered_page, page_count = deps.file_page_image(
            file_id,
            project_id,
            request.query_params.get("page", "1"),
            request.query_params.get("scale", ""),
        )
        filename = clean_filename(str(cached.get("name") or "document"))
        headers = {
            "X-File-Page": str(rendered_page),
            "X-File-Page-Count": str(page_count),
            "Content-Disposition": content_disposition_header("inline", f"{Path(filename).stem or 'document'}-page-{rendered_page}.png"),
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type="image/png", headers=headers)

    @router.get("/api/file-page-layout")
    async def api_file_page_layout(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            deps.file_page_layout(
                request.query_params.get("fileId", ""),
                request.query_params.get("projectId", "") or None,
                request.query_params.get("page", "1"),
            )
        )

    @router.get("/api/file-page-search")
    async def api_file_page_search(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            deps.file_page_search(
                request.query_params.get("fileId", ""),
                request.query_params.get("projectId", "") or None,
                request.query_params.get("query", ""),
            )
        )

    return router
