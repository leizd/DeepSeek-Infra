from __future__ import annotations

import zipfile
from pathlib import Path

from deepseek_infra.infra.media import ingestion, library
from deepseek_infra.infra.rag import local_rag
from deepseek_infra.infra.workspace import exports, projects


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
