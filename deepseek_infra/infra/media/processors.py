"""Media processors for OCR/page text/webpage/transcript extraction."""

from __future__ import annotations

import json
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.media import library, schema
from deepseek_infra.infra.rag import files as rag_files

TRANSCRIPT_CHUNK_CHARS = 1_200


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
    imported = imported_transcript_segments(metadata.get("transcriptSegments") or metadata.get("segments"))
    if imported:
        return imported
    transcript = str(metadata.get("transcript") or metadata.get("transcriptText") or "").strip()
    return transcript_segments(transcript, media_type="audio", duration_sec=metadata_float(metadata, "durationSec"))


def video_segments(media: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _metadata(media)
    segments = imported_transcript_segments(metadata.get("transcriptSegments") or metadata.get("segments"))
    if not segments:
        segments = transcript_segments(str(metadata.get("transcript") or metadata.get("transcriptText") or "").strip(), media_type="video", duration_sec=metadata_float(metadata, "durationSec"))
    raw_frames = metadata.get("frames") or metadata.get("frameCaptions")
    frames = raw_frames if isinstance(raw_frames, list) else []
    frame_segments: list[dict[str, Any]] = []
    for index, item in enumerate(frames):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("caption") or "").strip()
            frame_path = schema.normalize_media_path(item.get("framePath"))
            time_range = schema.normalize_time_range(item.get("timeRange") if isinstance(item.get("timeRange"), list) else [item.get("startSec"), item.get("endSec")])
        else:
            text = str(item or "").strip()
            frame_path = ""
            time_range = []
        if text:
            segment: dict[str, Any] = {"type": "frame", "text": text, "confidence": 1.0, "_sourceIndex": index}
            if time_range:
                segment["timeRange"] = time_range
            if frame_path:
                segment["framePath"] = frame_path
            frame_segments.append(segment)
    frame_segments.sort(key=_segment_sort_key)
    for index, segment in enumerate(frame_segments):
        segment.pop("_sourceIndex", None)
        segment["index"] = len(segments) + index
        segments.append(segment)
    return segments


def transcript_segments(transcript: str, *, media_type: str, duration_sec: float = 0.0) -> list[dict[str, Any]]:
    if not transcript:
        return []
    chunks = chunk_transcript_text(transcript)
    if not chunks:
        return []
    result = []
    seconds_per_chunk = duration_sec / len(chunks) if duration_sec > 0 else 0
    for index, chunk in enumerate(chunks):
        segment: dict[str, Any] = {"type": "transcript", "text": chunk, "confidence": 1.0, "index": index}
        if seconds_per_chunk > 0:
            start = round(index * seconds_per_chunk, 3)
            segment["timeRange"] = [start, round(start + seconds_per_chunk, 3)]
        result.append(segment)
    return result


def imported_transcript_segments(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("transcript") or "").strip()
            time_range = schema.normalize_time_range(item.get("timeRange") if isinstance(item.get("timeRange"), list) else [item.get("startSec"), item.get("endSec")])
            confidence = item.get("confidence", 1.0)
        else:
            text = str(item or "").strip()
            time_range = []
            confidence = 1.0
        if not text:
            continue
        segment: dict[str, Any] = {"type": "transcript", "text": text, "confidence": confidence, "_sourceIndex": index}
        if time_range:
            segment["timeRange"] = time_range
        result.append(segment)
    result.sort(key=_segment_sort_key)
    for index, segment in enumerate(result):
        segment.pop("_sourceIndex", None)
        segment["index"] = index
    return result


def chunk_transcript_text(text: str, *, max_chars: int = TRANSCRIPT_CHUNK_CHARS) -> list[str]:
    normalized = "\n".join(line.strip() for line in str(text or "").replace("\r\n", "\n").splitlines()).strip()
    if not normalized:
        return []
    chunks: list[str] = []
    current = ""
    for piece in _transcript_pieces(normalized):
        candidate = f"{current}\n{piece}".strip() if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks


def _transcript_pieces(text: str) -> list[str]:
    pieces: list[str] = []
    for paragraph in [part.strip() for part in text.split("\n") if part.strip()]:
        if len(paragraph) <= TRANSCRIPT_CHUNK_CHARS:
            pieces.append(paragraph)
            continue
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip() if current else word
            if len(candidate) <= TRANSCRIPT_CHUNK_CHARS:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                current = word
        if current:
            pieces.append(current)
    return pieces


def _segment_sort_key(segment: dict[str, Any]) -> tuple[float, int]:
    raw_range = segment.get("timeRange")
    time_range = raw_range if isinstance(raw_range, list) else []
    if time_range:
        try:
            return (float(time_range[0]), int(segment.get("_sourceIndex") or 0))
        except (TypeError, ValueError):
            pass
    return (float("inf"), int(segment.get("_sourceIndex") or 0))


def metadata_float(metadata: dict[str, Any], key: str) -> float:
    try:
        return max(0.0, float(metadata.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


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
