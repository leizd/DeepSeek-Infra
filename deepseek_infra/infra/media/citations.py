"""Citation helpers for media segments."""

from __future__ import annotations

from typing import Any


def citation_for_segment(media: dict[str, Any], segment: dict[str, Any], *, ordinal: int = 1) -> dict[str, Any]:
    media_id = str(media.get("mediaId") or segment.get("mediaId") or "")
    media_type = str(media.get("type") or "")
    page = int(segment.get("page") or 0)
    raw_time_range = segment.get("timeRange")
    time_range: list[Any] = raw_time_range if isinstance(raw_time_range, list) else []
    segment_id = str(segment.get("segmentId") or "")
    locator = ""
    if page:
        locator = f"page={page}"
    elif len(time_range) >= 2:
        locator = f"t={float(time_range[0]):.3f}"
    elif segment_id:
        locator = f"segment={segment_id}"
    uri = f"media://{media_id}" + (f"#{locator}" if locator else "")
    label = label_for_segment(media_type, ordinal=ordinal, page=page, time_range=time_range)
    return {
        "label": label,
        "uri": uri,
        "markdown": f"[^{label}]",
        "mediaId": media_id,
        "segmentId": segment_id,
        "page": page,
        "timeRange": time_range,
        "type": media_type,
    }


def label_for_segment(media_type: str, *, ordinal: int, page: int = 0, time_range: list[Any] | None = None) -> str:
    prefix = {"webpage": "W", "pdf": "M", "image": "M", "screenshot": "M", "audio": "M", "video": "M"}.get(media_type, "M")
    base = f"{prefix}{max(1, ordinal)}"
    if page:
        return f"{base}-p{page}"
    if time_range and len(time_range) >= 2:
        return f"{base}-{format_timestamp(float(time_range[0]))}"
    return base


def format_timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours = value // 3600
    minutes = (value % 3600) // 60
    secs = value % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
