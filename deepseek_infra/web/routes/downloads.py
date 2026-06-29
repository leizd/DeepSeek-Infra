"""Generated file download routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web.http_utils import require_api_auth, truthy


@dataclass(frozen=True)
class DownloadsRouteDeps:
    resolve_generated_file: Callable[[str], Path | None]
    download_descriptor: Callable[[Path], tuple[str, str]]


def create_downloads_router(deps: DownloadsRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/download")
    async def api_download(request: Request) -> Response:
        require_api_auth(request)
        file_id = request.query_params.get("id", "")
        path = deps.resolve_generated_file(file_id)
        if path is None:
            raise AppError("File does not exist or has expired", code=ErrorCode.NOT_FOUND, status=404)
        data = path.read_bytes()
        media_type, download_name = deps.download_descriptor(path)
        disposition = "inline" if path.suffix.lower() == ".svg" and truthy(request.query_params.get("inline", "")) else "attachment"
        headers = {"Content-Disposition": f'{disposition}; filename="{download_name}"', "Cache-Control": "no-store"}
        return Response(content=data, media_type=media_type, headers=headers)

    return router
