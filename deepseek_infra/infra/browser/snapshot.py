"""Persist Browser outputs into Media Library and Workspace artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media import indexer, library, schema
from deepseek_infra.infra.workspace.schema import redact_sensitive_text, utc_now


def save_page_snapshot(
    page: dict[str, Any],
    *,
    session_id: str,
    project_id: str = "",
    selector: str = "",
) -> dict[str, Any]:
    media_id = schema.new_media_id()
    url = str(page.get("url") or "")
    title = str(page.get("title") or "") or "Browser page snapshot"
    html = str(page.get("html") or "")
    text = str(page.get("text") or "").strip()
    path = library.save_text_source(media_id, "snapshot.html", html or text)
    source = {"kind": "browser", "url": url, "browserSessionId": session_id}
    media = library.register_media(
        media_id=media_id,
        project_id=project_id,
        media_type="webpage",
        title=title,
        mime_type="text/html",
        path=path,
        source=source,
        metadata={
            "sourceUrl": url,
            "browserSessionId": session_id,
            "capturedAt": utc_now(),
            "taint": "untrusted_browser",
        },
        status="ready",
    )
    segments = []
    if text:
        segment_selector = selector or str(page.get("selector") or "body")
        segments.append(
            {
                "type": "webpage_text",
                "text": redact_sensitive_text(text),
                "selector": segment_selector,
                "citation": {
                    "label": "B1",
                    "uri": f"browser://{session_id}#selector={segment_selector}",
                    "markdown": "[^B1]",
                    "browserSessionId": session_id,
                    "url": url,
                    "selector": segment_selector,
                    "taint": "untrusted_browser",
                },
            }
        )
    saved_segments = library.save_segments(media_id, segments)
    indexed = indexer.index_media_segments(media, saved_segments)
    media = library.update_media(media_id, {"metadata": {"segmentCount": len(saved_segments), "indexedChunkCount": indexed}})
    return {"media": media, "segments": saved_segments, "indexed": indexed}


def save_screenshot(
    image: dict[str, Any],
    *,
    session_id: str,
    project_id: str = "",
    title: str = "Browser screenshot",
) -> dict[str, Any]:
    data = image.get("bytes")
    raw = data if isinstance(data, bytes) else b""
    media_id = schema.new_media_id()
    path = library.save_source_bytes(media_id, "screenshot.png", raw)
    media = library.register_media(
        media_id=media_id,
        project_id=project_id,
        media_type="screenshot",
        title=title,
        mime_type=str(image.get("mimeType") or "image/png"),
        path=path,
        source={"kind": "browser", "url": str(image.get("url") or ""), "browserSessionId": session_id},
        metadata={
            "browserSessionId": session_id,
            "selector": str(image.get("selector") or ""),
            "capturedAt": utc_now(),
            "taint": "untrusted_browser",
        },
        status="ready",
    )
    return {"media": media, "segments": [], "indexed": 0}


def register_download(download: dict[str, Any], *, session_id: str, project_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"download": download, "media": None, "artifact": None}
    path = Path(str(download.get("path") or ""))
    mime_type = str(download.get("mimeType") or schema.guess_mime_type(path.name))
    if path.is_file():
        try:
            safe_mime = schema.validate_media_mime_type(mime_type, filename=path.name)
            media_id = schema.new_media_id()
            media_path = library.save_source_bytes(media_id, path.name, path.read_bytes())
            media_type = schema.normalize_media_type("", mime_type=safe_mime, filename=path.name)
            result["media"] = library.register_media(
                media_id=media_id,
                project_id=project_id,
                media_type=media_type,
                title=path.name,
                mime_type=safe_mime,
                path=media_path,
                source={"kind": "browser", "url": str(download.get("sourceUrl") or ""), "browserSessionId": session_id},
                metadata={"browserSessionId": session_id, "taint": "untrusted_browser"},
                status="ready",
            )
        except AppError:
            result["media"] = None
    if project_id and path.is_file():
        try:
            from deepseek_infra.infra.workspace import artifacts

            result["artifact"] = artifacts.register_artifact(
                project_id,
                artifact_type=path.suffix.lstrip(".") or "txt",
                title=path.name,
                path=str(path),
                source={"kind": "browser", "url": str(download.get("sourceUrl") or ""), "browserSessionId": session_id},
            )
        except Exception:
            result["artifact"] = None
    return result
