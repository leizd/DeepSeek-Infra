"""Unified media ingestion and processing pipeline."""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.media import citations, indexer, library, processors, schema
from deepseek_infra.infra.workspace.schema import normalize_source_ref


def ingest_upload(
    file_info: dict[str, Any],
    *,
    project_id: str = "",
    title: str = "",
    source: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    process: bool = False,
    ocr_enabled: bool | None = None,
    ocr_api_key: str | None = None,
) -> dict[str, Any]:
    filename = str(file_info.get("filename") or "media.bin")
    data = file_info.get("data")
    raw_data = data if isinstance(data, bytes) else b""
    schema.validate_media_upload_size(len(raw_data))
    mime_type = schema.validate_media_mime_type(file_info.get("content_type"), filename=filename)
    media_type = schema.normalize_media_type("", mime_type=mime_type, filename=filename)
    media_id = schema.new_media_id()
    path = library.save_source_bytes(media_id, filename, raw_data)
    media = library.register_media(
        media_id=media_id,
        project_id=project_id,
        media_type=media_type,
        title=title or filename,
        mime_type=mime_type,
        path=path,
        source=source or {"kind": "upload", "refId": filename},
        metadata=metadata or {},
    )
    if process:
        media = process_media(media["mediaId"], ocr_enabled=ocr_enabled, ocr_api_key=ocr_api_key)["media"]
    return media


def register_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppError("Media payload must be an object", code=ErrorCode.INVALID_PAYLOAD)
    project_id = str(payload.get("projectId") or "").strip()
    title = str(payload.get("title") or payload.get("name") or "Media").strip()
    mime_type = schema.normalize_mime_type(payload.get("mimeType") or payload.get("contentType"))
    filename = str(payload.get("filename") or title or "media").strip()
    if mime_type:
        mime_type = schema.validate_media_mime_type(mime_type, filename=filename)
    media_type = schema.normalize_media_type(payload.get("type"), mime_type=mime_type, filename=filename)
    media_id = schema.new_media_id()
    metadata = schema.normalize_metadata(payload.get("metadata"))
    source = normalize_source_ref(payload.get("source") if isinstance(payload.get("source"), dict) else {"kind": "upload", "refId": ""})
    path = str(payload.get("path") or "").strip()
    if path:
        path = library.validate_object_media_path(path, media_id=media_id)

    if media_type == "webpage":
        source = normalize_source_ref({**source, "kind": source.get("kind") or "browser", "url": payload.get("url") or payload.get("sourceUrl") or metadata.get("sourceUrl") or ""})
        html = str(payload.get("html") or payload.get("content") or "").strip()
        text = str(payload.get("text") or "").strip()
        if html:
            path = library.save_text_source(media_id, "snapshot.html", html)
            metadata.setdefault("sourceUrl", str(payload.get("url") or payload.get("sourceUrl") or ""))
        if text:
            metadata.setdefault("webpageText", text)
    elif payload.get("text"):
        key = "transcript" if media_type in {"audio", "video"} else "text"
        metadata.setdefault(key, str(payload.get("text") or ""))
        if not path:
            path = library.save_text_source(media_id, "transcript.txt" if key == "transcript" else "source.txt", str(payload.get("text") or ""))
    elif payload.get("transcript"):
        metadata.setdefault("transcript", str(payload.get("transcript") or ""))
        if not path:
            path = library.save_text_source(media_id, "transcript.txt", str(payload.get("transcript") or ""))

    page_texts = payload.get("pageTexts")
    if isinstance(page_texts, list):
        metadata["pageTexts"] = page_texts

    media = library.register_media(
        media_id=media_id,
        project_id=project_id,
        media_type=media_type,
        title=title,
        mime_type=mime_type or schema.guess_mime_type(filename),
        path=path,
        source=source,
        metadata=metadata,
    )
    if bool(payload.get("process")):
        media = process_media(media["mediaId"])["media"]
    return media


def process_media(media_id: str, *, ocr_enabled: bool | None = None, ocr_api_key: str | None = None, force: bool = False) -> dict[str, Any]:
    media = library.set_status(media_id, "processing")
    try:
        if force:
            indexer.delete_media_index(str(media["mediaId"]), project_id=str(media.get("projectId") or ""))
            library.save_segments(str(media["mediaId"]), [])
        raw_segments = processors.extract_segments(media, ocr_enabled=ocr_enabled, ocr_api_key=ocr_api_key)
        segments = []
        for index, segment in enumerate(raw_segments):
            normalized = schema.normalize_segment(segment, media_id=media["mediaId"], fallback_index=index)
            normalized["citation"] = citations.citation_for_segment(media, normalized, ordinal=index + 1)
            segments.append(normalized)
        saved_segments = library.save_segments(media["mediaId"], segments)
        indexed = indexer.index_media_segments(media, saved_segments)
        raw_metadata = media.get("metadata")
        metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        metadata.update(_segment_metadata(saved_segments, indexed=indexed))
        media = library.update_media(media["mediaId"], {"status": "ready", "metadata": metadata})
        return {"ok": True, "media": media, "segments": saved_segments, "indexed": indexed}
    except Exception as exc:
        media = library.set_status(media_id, "failed", metadata_patch={"error": str(exc)[:500]})
        if isinstance(exc, AppError):
            raise
        raise AppError(f"Media processing failed: {exc}", code=ErrorCode.INTERNAL, status=500) from exc


def _segment_metadata(segments: list[dict[str, Any]], *, indexed: int) -> dict[str, Any]:
    pages = [int(segment.get("page") or 0) for segment in segments if int(segment.get("page") or 0) > 0]
    durations = []
    for segment in segments:
        value = segment.get("timeRange")
        if isinstance(value, list) and len(value) >= 2:
            try:
                durations.append(float(value[1]))
            except (TypeError, ValueError):
                pass
    return {
        "segmentCount": len(segments),
        "indexedChunkCount": indexed,
        "pageCount": max(pages) if pages else 0,
        "durationSec": max(durations) if durations else 0,
    }
