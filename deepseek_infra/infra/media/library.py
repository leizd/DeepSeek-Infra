"""Persistent media metadata and segment storage."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.media import schema
from deepseek_infra.infra.workspace.schema import redact_value, utc_now, validate_project_id, write_json_atomic

MEDIA_DIR = config.MEDIA_DIR
STORE_NAME = "library.json"
SEGMENT_DIR_NAME = "segments"
OBJECT_DIR_NAME = "objects"


def media_dir() -> Path:
    return MEDIA_DIR


def store_path() -> Path:
    return media_dir() / STORE_NAME


def object_dir(media_id: str) -> Path:
    return media_dir() / OBJECT_DIR_NAME / schema.validate_media_id(media_id)


def segments_path(media_id: str) -> Path:
    return media_dir() / SEGMENT_DIR_NAME / f"{schema.validate_media_id(media_id)}.json"


def media_file_path(media: dict[str, Any]) -> Path:
    raw = schema.normalize_media_path(media.get("path"))
    if not raw:
        return object_dir(str(media.get("mediaId") or ""))
    path = (media_dir() / raw).resolve()
    try:
        path.relative_to(media_dir().resolve())
    except ValueError as exc:
        raise AppError("Media file must stay inside the media library", code=ErrorCode.INVALID_PAYLOAD, status=400) from exc
    return path


def relative_media_path(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(media_dir().resolve()).as_posix()
    except ValueError as exc:
        raise AppError("Media file must stay inside the media library", code=ErrorCode.INVALID_PAYLOAD, status=400) from exc


def validate_object_media_path(path: str, *, media_id: str = "") -> str:
    safe_path = schema.normalize_media_path(path)
    if not safe_path:
        return ""
    parts = safe_path.split("/")
    expected_prefix = ["objects"]
    if media_id:
        expected_prefix.append(schema.validate_media_id(media_id))
    if parts[: len(expected_prefix)] != expected_prefix:
        raise AppError("Media source path must point to an object stored in the media library", code=ErrorCode.INVALID_PAYLOAD, status=400)
    media_file_path({"mediaId": media_id or "media_path_check", "path": safe_path})
    return safe_path


def list_media(*, project_id: str = "", media_type: str = "", status: str = "") -> list[dict[str, Any]]:
    safe_project = validate_project_id(project_id) if project_id else ""
    requested_type = schema.normalize_media_type(media_type) if media_type else ""
    requested_status = schema.normalize_status(status) if status else ""
    items = _load_store()
    result = []
    for item in items:
        if safe_project and item.get("projectId") != safe_project:
            continue
        if requested_type and item.get("type") != requested_type:
            continue
        if requested_status and item.get("status") != requested_status:
            continue
        result.append(public_media(item))
    return sorted(result, key=lambda row: str(row.get("updatedAt") or ""), reverse=True)


def get_media(media_id: str) -> dict[str, Any]:
    safe_id = schema.validate_media_id(media_id)
    for item in _load_store():
        if item.get("mediaId") == safe_id:
            return public_media(item)
    raise AppError("Media not found", code=ErrorCode.NOT_FOUND, status=404)


def register_media(
    *,
    media_id: str = "",
    project_id: str = "",
    media_type: str,
    title: str,
    mime_type: str = "",
    path: str = "",
    source: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    now = utc_now()
    record = schema.normalize_media_record(
        {
            "mediaId": media_id or schema.new_media_id(),
            "projectId": project_id,
            "type": media_type,
            "title": title,
            "mimeType": mime_type,
            "path": path,
            "source": source or {},
            "status": status,
            "createdAt": now,
            "updatedAt": now,
            "metadata": metadata or {},
        }
    )
    if record.get("path"):
        record["path"] = validate_object_media_path(str(record["path"]), media_id=str(record["mediaId"]))
    items = [item for item in _load_store() if item.get("mediaId") != record["mediaId"]]
    items.append(record)
    _write_store(items)
    return public_media(record)


def update_media(media_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    safe_id = schema.validate_media_id(media_id)
    items = _load_store()
    for index, item in enumerate(items):
        if item.get("mediaId") != safe_id:
            continue
        merged = dict(item)
        for key in ("title", "path", "source", "status", "projectId"):
            if key in patch:
                merged[key] = patch[key]
        if "metadata" in patch:
            raw_existing = merged.get("metadata")
            existing_metadata: dict[str, Any] = dict(raw_existing) if isinstance(raw_existing, dict) else {}
            raw_patch = patch.get("metadata")
            patch_metadata = raw_patch if isinstance(raw_patch, dict) else {}
            merged["metadata"] = {**existing_metadata, **patch_metadata}
        if "type" in patch:
            merged["type"] = patch["type"]
        if "mimeType" in patch:
            merged["mimeType"] = patch["mimeType"]
        merged["updatedAt"] = utc_now()
        normalized = schema.normalize_media_record(merged)
        if normalized.get("path"):
            normalized["path"] = validate_object_media_path(str(normalized["path"]), media_id=str(normalized["mediaId"]))
        items[index] = normalized
        _write_store(items)
        return public_media(normalized)
    raise AppError("Media not found", code=ErrorCode.NOT_FOUND, status=404)


def set_status(media_id: str, status: str, *, metadata_patch: dict[str, Any] | None = None) -> dict[str, Any]:
    media = get_media(media_id)
    raw_metadata = media.get("metadata")
    metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    if metadata_patch:
        metadata.update(metadata_patch)
    return update_media(media_id, {"status": status, "metadata": metadata})


def delete_media(media_id: str) -> int:
    safe_id = schema.validate_media_id(media_id)
    items = _load_store()
    remaining = [item for item in items if item.get("mediaId") != safe_id]
    if len(remaining) == len(items):
        return 0
    _write_store(remaining)
    try:
        segments_path(safe_id).unlink()
    except OSError:
        pass
    try:
        shutil.rmtree(object_dir(safe_id))
    except OSError:
        pass
    try:
        from deepseek_infra.infra.media.indexer import delete_media_index

        delete_media_index(safe_id)
    except Exception:
        pass
    return 1


def save_source_bytes(media_id: str, filename: str, data: bytes) -> str:
    schema.validate_media_upload_size(len(data))
    directory = object_dir(media_id)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename or "source.bin").suffix.lower()
    target = directory / ("source" + (suffix or ".bin"))
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return relative_media_path(target)


def save_text_source(media_id: str, filename: str, text: str) -> str:
    data = str(text or "").encode("utf-8")
    return save_source_bytes(media_id, filename, data)


def save_segments(media_id: str, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [schema.normalize_segment(segment, media_id=media_id, fallback_index=index) for index, segment in enumerate(segments)]
    path = segments_path(media_id)
    write_json_atomic(path, {"segments": normalized})
    return [public_segment(item) for item in normalized]


def list_segments(media_id: str) -> list[dict[str, Any]]:
    path = segments_path(media_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("segments")
    segments = raw if isinstance(raw, list) else []
    result = []
    for index, item in enumerate(segments):
        if isinstance(item, dict):
            try:
                result.append(schema.normalize_segment(item, media_id=media_id, fallback_index=index))
            except AppError:
                continue
    return [public_segment(item) for item in result]


def public_media(media: dict[str, Any]) -> dict[str, Any]:
    return schema.normalize_media_record(media)


def public_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return dict(segment)


def redacted_media_payload(media: dict[str, Any]) -> dict[str, Any]:
    return redact_value(public_media(media))


def _load_store() -> list[dict[str, Any]]:
    path = store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw_items = data.get("media")
    rows = raw_items if isinstance(raw_items, list) else []
    result = []
    for item in rows:
        if isinstance(item, dict):
            try:
                result.append(schema.normalize_media_record(item))
            except AppError:
                continue
    return result


def _write_store(items: list[dict[str, Any]]) -> None:
    write_json_atomic(store_path(), {"schemaVersion": "media-library.v1", "media": items[-1000:]})
