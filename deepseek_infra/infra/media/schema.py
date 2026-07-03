"""Schema normalization for first-class media workspace objects."""

from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace.schema import (
    new_id,
    normalize_source_ref,
    normalize_title,
    redact_sensitive_text,
    utc_now,
    validate_project_id,
    validate_workspace_id,
)

MEDIA_TYPES = {"image", "pdf", "audio", "video", "webpage", "screenshot"}
MEDIA_STATUSES = {"pending", "processing", "ready", "failed"}
SEGMENT_TYPES = {"ocr_text", "caption", "transcript", "frame", "page_text", "webpage_text"}
MAX_SEGMENT_TEXT_CHARS = 120_000
MAX_TITLE_CHARS = 160
MAX_MEDIA_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_MEDIA_UPLOADS_PER_REQUEST = 20
ALLOWED_MEDIA_MIME_PREFIXES = ("image/", "audio/", "video/")
ALLOWED_MEDIA_MIME_TYPES = {"application/pdf", "text/html", "application/xhtml+xml"}


def new_media_id() -> str:
    return new_id("media")


def new_segment_id() -> str:
    return new_id("seg")


def validate_media_id(media_id: str) -> str:
    return validate_workspace_id(media_id, label="media id")


def validate_segment_id(segment_id: str) -> str:
    return validate_workspace_id(segment_id, label="segment id")


def normalize_media_type(value: Any, *, mime_type: str = "", filename: str = "") -> str:
    candidate = str(value or "").strip().lower()
    if candidate in MEDIA_TYPES:
        return candidate
    guessed = media_type_from_mime(mime_type, filename=filename)
    if guessed:
        return guessed
    raise AppError("Unsupported media type", code=ErrorCode.INVALID_PAYLOAD, status=400)


def media_type_from_mime(mime_type: str, *, filename: str = "") -> str:
    content_type = str(mime_type or "").split(";", 1)[0].strip().lower()
    suffix = PurePosixPath(str(filename or "")).suffix.lower()
    if content_type.startswith("image/"):
        return "image"
    if content_type == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if content_type.startswith("audio/") or suffix in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return "audio"
    if content_type.startswith("video/") or suffix in {".mp4", ".mov", ".webm", ".mkv", ".avi"}:
        return "video"
    if content_type in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
        return "webpage"
    return ""


def guess_mime_type(filename: str, default: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(str(filename or ""))[0] or default


def normalize_mime_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def validate_media_mime_type(value: Any, *, filename: str = "") -> str:
    mime_type = normalize_mime_type(value) or guess_mime_type(filename)
    if mime_type in ALLOWED_MEDIA_MIME_TYPES or any(mime_type.startswith(prefix) for prefix in ALLOWED_MEDIA_MIME_PREFIXES):
        return mime_type
    raise AppError("Unsupported media MIME type", code=ErrorCode.INVALID_PAYLOAD, status=400)


def validate_media_upload_size(size: int) -> int:
    safe_size = max(0, int(size))
    if safe_size <= 0:
        raise AppError("Media source is empty", code=ErrorCode.INVALID_PAYLOAD, status=400)
    if safe_size > MAX_MEDIA_UPLOAD_BYTES:
        raise AppError("Media upload exceeds the 50 MB limit", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    return safe_size


def normalize_status(value: Any, default: str = "pending") -> str:
    status = str(value or default).strip().lower()
    if status not in MEDIA_STATUSES:
        raise AppError("Unsupported media status", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return status


def normalize_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = re.sub(r"[^A-Za-z0-9_.:-]", "", str(key or ""))[:80]
        if not safe_key:
            continue
        if isinstance(item, dict):
            nested = normalize_metadata(item)
            if nested:
                result[safe_key] = nested
        elif isinstance(item, list):
            result[safe_key] = [normalize_metadata(child) if isinstance(child, dict) else _compact_scalar(child) for child in item[:200]]
        else:
            result[safe_key] = _compact_scalar(item)
    return result


def _compact_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)[:MAX_SEGMENT_TEXT_CHARS]


def normalize_media_record(value: dict[str, Any]) -> dict[str, Any]:
    media_id = validate_media_id(str(value.get("mediaId") or ""))
    project_id = str(value.get("projectId") or "").strip()
    if project_id:
        project_id = validate_project_id(project_id)
    mime_type = normalize_mime_type(value.get("mimeType"))
    media_type = normalize_media_type(value.get("type"), mime_type=mime_type, filename=str(value.get("title") or ""))
    created_at = str(value.get("createdAt") or utc_now())
    updated_at = str(value.get("updatedAt") or created_at)
    return {
        "mediaId": media_id,
        "projectId": project_id,
        "type": media_type,
        "title": normalize_title(value.get("title"), default="Untitled media"),
        "mimeType": mime_type or guess_mime_type(str(value.get("path") or "")),
        "path": normalize_media_path(value.get("path")),
        "source": normalize_source_ref(value.get("source")),
        "status": normalize_status(value.get("status"), default="pending"),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "metadata": normalize_metadata(value.get("metadata")),
    }


def normalize_media_path(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        return ""
    if raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:/", raw):
        raise AppError("Media path must be relative to the media library", code=ErrorCode.INVALID_PAYLOAD, status=400)
    parts = [part for part in PurePosixPath(raw).parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise AppError("Media path must not escape the media library", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return PurePosixPath(*parts).as_posix() if parts else ""


def normalize_segment(value: dict[str, Any], *, media_id: str, fallback_index: int = 0) -> dict[str, Any]:
    segment_type = str(value.get("type") or "page_text").strip().lower()
    if segment_type not in SEGMENT_TYPES:
        raise AppError("Unsupported media segment type", code=ErrorCode.INVALID_PAYLOAD, status=400)
    segment_id = str(value.get("segmentId") or "").strip()
    if segment_id:
        segment_id = validate_segment_id(segment_id)
    else:
        segment_id = new_segment_id()
    text = redact_sensitive_text(str(value.get("text") or ""))[:MAX_SEGMENT_TEXT_CHARS]
    segment: dict[str, Any] = {
        "segmentId": segment_id,
        "mediaId": validate_media_id(media_id),
        "type": segment_type,
        "text": text,
        "confidence": normalize_confidence(value.get("confidence")),
        "index": int(raw_index if (raw_index := value.get("index")) is not None else fallback_index),
    }
    if value.get("page") is not None:
        raw_page = value.get("page")
        segment["page"] = max(1, int(raw_page or 1))
    time_range = normalize_time_range(value.get("timeRange"))
    if time_range:
        segment["timeRange"] = time_range
    frame_path = normalize_media_path(value.get("framePath"))
    if frame_path:
        segment["framePath"] = frame_path
    citation = value.get("citation")
    if isinstance(citation, dict):
        segment["citation"] = normalize_source_ref(citation)
    return segment


def normalize_confidence(value: Any) -> float:
    if value is None or value == "":
        return 1.0
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 1.0


def normalize_time_range(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) < 2:
        return []
    try:
        start = max(0.0, float(value[0]))
        end = max(start, float(value[1]))
    except (TypeError, ValueError):
        return []
    return [round(start, 3), round(end, 3)]
