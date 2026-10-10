from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.media import ingestion, library, schema
from deepseek_infra.infra.rag import local_rag
from deepseek_infra.infra.tool_runtime import ocr
from deepseek_infra.infra.tool_runtime.ocr_trace import OcrTrace
from deepseek_infra.infra.workspace import exports, projects

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"traceable-image" * 4


def test_media_register_process_index_and_delete(tmp_settings: Path) -> None:
    project = projects.create_project("Media Project")
    project_id = str(project["projectId"])

    media = ingestion.register_from_payload(
        {
            "projectId": project_id,
            "type": "image",
            "title": "Whiteboard capture",
            "mimeType": "image/png",
            "text": "Roadmap says Multimodal Media Layer and citation support.",
            "metadata": {"caption": "A roadmap whiteboard"},
            "process": True,
        }
    )
    segments = library.list_segments(str(media["mediaId"]))
    hits = local_rag.search_media_index("citation support", project_id=project_id, media_id=str(media["mediaId"]), limit=3)

    assert media["status"] == "ready"
    assert segments
    assert segments[0]["type"] == "ocr_text"
    assert hits
    assert hits[0].source_id == media["mediaId"]
    assert hits[0].metadata["sourceType"] == "media"
    assert hits[0].metadata["citation"].startswith("media://")

    assert library.delete_media(str(media["mediaId"])) == 1
    assert library.list_media(project_id=project_id) == []
    assert local_rag.search_media_index("citation support", project_id=project_id, media_id=str(media["mediaId"]), limit=3) == []


def test_pdf_page_segments_and_project_export_include_media(tmp_settings: Path) -> None:
    project = projects.create_project("PDF Media Project")
    project_id = str(project["projectId"])

    media = ingestion.register_from_payload(
        {
            "projectId": project_id,
            "type": "pdf",
            "title": "Research Notes",
            "mimeType": "application/pdf",
            "pageTexts": [
                {"page": 1, "text": "Overview of local AI workspace."},
                {"page": 2, "text": "Media equals first-class workspace object. api_key=sk-pdf-secret"},
            ],
            "process": True,
        }
    )
    segments = library.list_segments(str(media["mediaId"]))
    export = exports.export_project(project_id, export_format="zip")["export"]
    zip_path = Path(str(export["path"]))

    assert len(segments) == 2
    assert segments[1]["citation"]["uri"].endswith("#page=2")
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in names)
    assert "media/media.json" in names
    assert any(name.startswith("media/segments/") for name in names)
    assert "Research Notes" in combined
    assert "sk-pdf-secret" not in combined


def test_webpage_snapshot_import_creates_citable_segment(tmp_settings: Path) -> None:
    project = projects.create_project("Webpage Media Project")
    media = ingestion.register_from_payload(
        {
            "projectId": project["projectId"],
            "type": "webpage",
            "title": "Snapshot",
            "url": "https://example.test/page?token=secret-token",
            "html": "<html><body><h1>Launch Notes</h1><p>Browser snapshots become media.</p></body></html>",
            "process": True,
        }
    )
    segments = library.list_segments(str(media["mediaId"]))

    assert media["type"] == "webpage"
    assert segments[0]["type"] == "webpage_text"
    assert "Launch Notes" in segments[0]["text"]
    assert segments[0]["citation"]["uri"].startswith(f"media://{media['mediaId']}")


def test_media_rejects_absolute_and_traversal_source_paths(tmp_settings: Path) -> None:
    absolute = str((tmp_settings / "outside.png").resolve())
    with pytest.raises(AppError):
        ingestion.register_from_payload({"type": "image", "title": "Outside", "mimeType": "image/png", "path": absolute})
    with pytest.raises(AppError):
        ingestion.register_from_payload({"type": "image", "title": "Traversal", "mimeType": "image/png", "path": "objects/../../outside.png"})
    with pytest.raises(AppError):
        library.register_media(media_type="image", title="Outside direct", mime_type="image/png", path=absolute)


def test_media_upload_limits_and_mime_whitelist(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError):
        ingestion.ingest_upload({"filename": "payload.exe", "content_type": "application/x-msdownload", "data": b"not media"})

    monkeypatch.setattr(schema, "MAX_MEDIA_UPLOAD_BYTES", 8)
    with pytest.raises(AppError):
        ingestion.ingest_upload({"filename": "large.png", "content_type": "image/png", "data": b"x" * 9})


def test_audio_transcript_is_chunked_for_rag_granularity(tmp_settings: Path) -> None:
    media = ingestion.register_from_payload(
        {
            "type": "audio",
            "title": "Long Transcript",
            "mimeType": "audio/wav",
            "metadata": {"durationSec": 90},
            "transcript": "\n".join(f"section {index} " + ("detail " * 80) for index in range(6)),
            "process": True,
        }
    )
    segments = library.list_segments(str(media["mediaId"]))

    assert len(segments) > 1
    assert all(segment["type"] == "transcript" for segment in segments)
    assert all(segment.get("timeRange") for segment in segments)
    assert segments[0]["citation"]["uri"].startswith(f"media://{media['mediaId']}#t=")


def test_video_frame_captions_are_sorted_and_frame_paths_validated(tmp_settings: Path) -> None:
    media = ingestion.register_from_payload(
        {
            "type": "video",
            "title": "Frame Captions",
            "mimeType": "video/mp4",
            "metadata": {
                "frameCaptions": [
                    {"caption": "Later frame", "timeRange": [12, 13], "framePath": "frames/later.jpg"},
                    {"caption": "Earlier frame", "timeRange": [2, 3], "framePath": "frames/earlier.jpg"},
                ]
            },
            "process": True,
        }
    )
    segments = library.list_segments(str(media["mediaId"]))

    assert [segment["text"] for segment in segments] == ["Earlier frame", "Later frame"]
    assert [segment["framePath"] for segment in segments] == ["frames/earlier.jpg", "frames/later.jpg"]

    with pytest.raises(AppError):
        ingestion.register_from_payload(
            {
                "type": "video",
                "title": "Bad Frame",
                "mimeType": "video/mp4",
                "metadata": {"frameCaptions": [{"caption": "bad", "framePath": "../outside.jpg"}]},
                "process": True,
            }
        )


def test_media_ocr_records_correlation_trace(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SimpleNamespace(name="tesseract", extract_image=lambda data: "traced text")
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([engine], []))
    trace = OcrTrace(correlation_id="media-req-1")

    media = ingestion.ingest_upload(
        {"filename": "scan.png", "content_type": "image/png", "data": TINY_PNG},
        title="Traced Scan",
        process=True,
        ocr_enabled=True,
        ocr_trace=trace,
    )

    assert media["status"] == "ready"
    payload = media["metadata"]["ocrTrace"]
    assert payload["correlationId"] == "media-req-1"
    assert payload["mode"] == "image"
    assert payload["engine"] == "tesseract"
    assert payload["inputBytes"] == len(TINY_PNG)
    assert payload["attempts"] == ["tesseract:candidate"]
    assert payload["timingsUs"]["transport"] >= 0
    assert "backendTimingsUs" not in payload


def test_media_ocr_failure_keeps_correlation_trace(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: Any, **kwargs: Any) -> str:
        raise AppError("engine down", code=ErrorCode.OCR_UNAVAILABLE, status=415)

    engine = SimpleNamespace(name="tesseract", extract_image=explode)
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([engine], []))
    project = projects.create_project("Media OCR Trace Failure")
    project_id = str(project["projectId"])

    with pytest.raises(AppError):
        ingestion.ingest_upload(
            {"filename": "scan.png", "content_type": "image/png", "data": TINY_PNG},
            project_id=project_id,
            process=True,
            ocr_enabled=True,
            ocr_trace=OcrTrace(correlation_id="media-fail-1"),
        )

    failed = library.list_media(project_id=project_id)[0]
    assert failed["status"] == "failed"
    payload = failed["metadata"]["ocrTrace"]
    assert payload["correlationId"] == "media-fail-1"
    assert payload["attempts"] == ["tesseract:unavailable"]
    assert failed["metadata"]["error"]


def test_media_without_ocr_keeps_metadata_shape(tmp_settings: Path) -> None:
    media = ingestion.register_from_payload(
        {
            "type": "webpage",
            "title": "No OCR Here",
            "html": "<main><p>Static snapshot.</p></main>",
            "process": True,
        }
    )

    assert media["status"] == "ready"
    assert "ocrTrace" not in media["metadata"]
