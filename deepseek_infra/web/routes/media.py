"""Media Library routes for multimodal workspace objects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.media import ingestion, library
from deepseek_infra.web.http_utils import json_response, read_json_body, require_api_auth, truthy


@dataclass(frozen=True)
class MediaRouteDeps:
    read_multipart_form: Callable[..., Any]


def create_media_router(deps: MediaRouteDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/media")
    async def api_media_create(request: Request) -> JSONResponse:
        require_api_auth(request)
        content_type = request.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            fields, uploads = await deps.read_multipart_form(request)
            if not uploads:
                raise AppError("No media uploaded", code=ErrorCode.INVALID_PAYLOAD)
            project_id = request.query_params.get("projectId", "") or _first(fields, "projectId")
            process = truthy(request.query_params.get("process", "")) or _truthy_field(fields, "process")
            ocr_enabled = _truthy_field(fields, "ocrEnabled")
            ocr_api_key = _first(fields, "apiKey")
            media_items = []
            for upload in uploads:
                title = _first(fields, "title") if len(uploads) == 1 else ""
                media_items.append(
                    ingestion.ingest_upload(
                        upload,
                        project_id=project_id,
                        title=title,
                        source={"kind": "upload", "refId": str(upload.get("filename") or "")},
                        process=process,
                        ocr_enabled=ocr_enabled,
                        ocr_api_key=ocr_api_key,
                    )
                )
            return json_response({"ok": True, "media": media_items[0], "mediaItems": media_items})

        payload = await read_json_body(request, max_bytes=16_000_000)
        media = ingestion.register_from_payload(payload)
        return json_response({"ok": True, "media": media})

    @router.get("/api/media")
    async def api_media_list(request: Request) -> JSONResponse:
        require_api_auth(request)
        return json_response(
            {
                "ok": True,
                "media": library.list_media(
                    project_id=str(request.query_params.get("projectId") or ""),
                    media_type=str(request.query_params.get("type") or ""),
                    status=str(request.query_params.get("status") or ""),
                ),
            }
        )

    @router.get("/api/media/{media_id}")
    async def api_media_get(request: Request, media_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "media": library.get_media(media_id)})

    @router.post("/api/media/{media_id}/process")
    async def api_media_process(request: Request, media_id: str) -> JSONResponse:
        require_api_auth(request)
        payload = await read_json_body(request) if int(request.headers.get("Content-Length") or "0") > 0 else {}
        return json_response(
            ingestion.process_media(
                media_id,
                ocr_enabled=truthy(payload.get("ocrEnabled")) if "ocrEnabled" in payload else None,
                ocr_api_key=str(payload.get("apiKey") or ""),
            )
        )

    @router.get("/api/media/{media_id}/segments")
    async def api_media_segments(request: Request, media_id: str) -> JSONResponse:
        require_api_auth(request)
        library.get_media(media_id)
        return json_response({"ok": True, "segments": library.list_segments(media_id)})

    @router.delete("/api/media/{media_id}")
    async def api_media_delete(request: Request, media_id: str) -> JSONResponse:
        require_api_auth(request)
        return json_response({"ok": True, "deleted": library.delete_media(media_id)})

    return router


def _first(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name) or []
    return str(values[0] if values else "").strip()


def _truthy_field(fields: dict[str, list[str]], name: str) -> bool:
    return any(truthy(value) for value in fields.get(name, []))
