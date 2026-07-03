"""Media processors for OCR/page text/webpage/transcript extraction."""

from __future__ import annotations

import json
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.media import library
from deepseek_infra.infra.rag import files as rag_files


def extract_segments(media: dict[str, Any], *, ocr_enabled: bool | None = None, ocr_api_key: str | None = None) -> list[dict[str, Any]]:
    media_type = str(media.get("type") or "")
    if media_type in {"image", "screenshot"}:
        return image_segments(media, ocr_enabled=ocr_enabled, ocr_api_key=ocr_api_key)
    if media_type == "pdf":
        return pdf_segments(media, ocr_enabled=ocr_enabled, ocr_api_key=ocr_api_key)
    if media_type == "webpage":
        return webpage_segments(media)
    if media_type == "audio":
        return audio_segments(media)
    if media_type == "video":
        return video_segments(media)
    raise AppError("Unsupported media type", code=ErrorCode.INVALID_PAYLOAD, status=400)


def image_segments(media: dict[str, Any], *, ocr_enabled: bool | None = None, ocr_api_key: str | None = None) -> list[dict[str, Any]]:
    metadata = _metadata(media)
    text = str(metadata.get("ocrText") or metadata.get("text") or "").strip()
    source = _source_bytes(media)
    if source and ocr_enabled:
        try:
            text = rag_files.extract_image_text(source, ocr_enabled=True, ocr_api_key=ocr_api_key) or text
        except AppError:
            if not text:
                raise
    caption = str(metadata.get("caption") or "").strip() or f"Image: {media.get('title') or media.get('mediaId')}"
    segments = []
    if text:
        segments.append({"type": "ocr_text", "text": text, "page": 1, "confidence": metadata.get("ocrConfidence", 1.0)})
    if caption and caption != text:
        segments.append({"type": "caption", "text": caption, "page": 1, "confidence": 1.0})
    return segments


def pdf_segments(media: dict[str, Any], *, ocr_enabled: bool | None = None, ocr_api_key: str | None = None) -> list[dict[str, Any]]:
    metadata = _metadata(media)
    page_texts = _page_texts_from_metadata(metadata)
    source = _source_bytes(media)
    if source and not page_texts:
        try:
            page_texts = rag_files.extract_pdf_page_texts_native(source)
        except AppError:
            if ocr_enabled:
                text = rag_files.extract_pdf_text(source, ocr_enabled=True, ocr_api_key=ocr_api_key)
                page_count = int(metadata.get("pageCount") or rag_files.count_pdf_pages(source) or 1)
                page_texts = rag_files.fallback_page_texts_from_text(text, page_count=page_count)
            else:
                raise
    text = str(metadata.get("text") or metadata.get("pdfText") or "").strip()
    if not page_texts and text:
        page_texts = rag_files.fallback_page_texts_from_text(text, page_count=int(metadata.get("pageCount") or 1))
    return [{"type": "page_text", "text": str(item.get("text") or ""), "page": int(item.get("page") or index + 1), "confidence": 1.0} for index, item in enumerate(page_texts) if str(item.get("text") or "").strip()]


def webpage_segments(media: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _metadata(media)
    text = str(metadata.get("webpageText") or metadata.get("text") or "").strip()
    if not text:
        source = _source_bytes(media)
        if source:
            text = rag_files.extract_html_text(source)
    if not text:
        return []
    return [{"type": "webpage_text", "text": text, "confidence": 1.0}]


def audio_segments(media: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _metadata(media)
    transcript = str(metadata.get("transcript") or metadata.get("transcriptText") or "").strip()
    return transcript_segments(transcript, media_type="audio")


def video_segments(media: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _metadata(media)
    segments = transcript_segments(str(metadata.get("transcript") or metadata.get("transcriptText") or "").strip(), media_type="video")
    raw_frames = metadata.get("frames") or metadata.get("frameCaptions")
    frames = raw_frames if isinstance(raw_frames, list) else []
    for index, item in enumerate(frames):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("caption") or "").strip()
            frame_path = str(item.get("framePath") or "").strip()
            time_range = item.get("timeRange") if isinstance(item.get("timeRange"), list) else []
        else:
            text = str(item or "").strip()
            frame_path = ""
            time_range = []
        if text:
            segment: dict[str, Any] = {"type": "frame", "text": text, "confidence": 1.0, "index": index}
            if time_range:
                segment["timeRange"] = time_range
            if frame_path:
                segment["framePath"] = frame_path
            segments.append(segment)
    return segments


def transcript_segments(transcript: str, *, media_type: str) -> list[dict[str, Any]]:
    if not transcript:
        return []
    segment_type = "transcript"
    return [{"type": segment_type, "text": transcript, "confidence": 1.0}]


def _metadata(media: dict[str, Any]) -> dict[str, Any]:
    value = media.get("metadata")
    return value if isinstance(value, dict) else {}


def _source_bytes(media: dict[str, Any]) -> bytes:
    path = library.media_file_path(media)
    if not path.is_file():
        return b""
    return path.read_bytes()


def _page_texts_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    raw = metadata.get("pageTexts") or metadata.get("pages")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        raw = parsed
    if not isinstance(raw, list):
        return []
    result = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            page = int(item.get("page") or index + 1)
        else:
            text = str(item or "").strip()
            page = index + 1
        if text:
            result.append({"page": page, "text": text})
    return result
