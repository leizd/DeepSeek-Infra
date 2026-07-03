#!/usr/bin/env python3
"""Offline Multimodal Media Layer smoke."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def app_version() -> str:
    from deepseek_infra.core.config import settings

    return settings.app_version


def git_short_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def configure_runtime_root(root: Path) -> None:
    from deepseek_infra.core import config
    from deepseek_infra.infra.data import projects as legacy_projects
    from deepseek_infra.infra.media import library as media_library
    from deepseek_infra.infra.rag import files, local_rag
    from deepseek_infra.infra.workspace import exports as workspace_exports

    projects_dir = root / ".projects"
    generated_dir = root / ".generated"
    local_rag_dir = root / ".local-rag"
    media_dir = root / ".media"
    config.ROOT = root
    config.PROJECTS_DIR = projects_dir
    config.GENERATED_DIR = generated_dir
    config.LOCAL_RAG_DIR = local_rag_dir
    config.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    config.MEDIA_DIR = media_dir
    legacy_projects.PROJECTS_DIR = projects_dir
    files.PROJECTS_DIR = projects_dir
    local_rag.PROJECTS_DIR = projects_dir
    local_rag.LOCAL_RAG_DIR = local_rag_dir
    local_rag.LOCAL_RAG_DB = local_rag_dir / "rag.sqlite3"
    media_library.MEDIA_DIR = media_dir
    workspace_exports.legacy_projects.PROJECTS_DIR = projects_dir


def run_media_smoke(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    from deepseek_infra.infra.media import ingestion, library, schema
    from deepseek_infra.infra.rag import local_rag
    from deepseek_infra.infra.workspace import exports, projects

    checks = {
        "imageImport": "FAIL",
        "pdfPageIndex": "FAIL",
        "webpageSnapshot": "FAIL",
        "mediaSegments": "FAIL",
        "mediaToRag": "FAIL",
        "mediaCitations": "FAIL",
        "mediaUploadLimits": "FAIL",
        "projectExportIncludesMedia": "FAIL",
        "secretRedaction": "FAIL",
    }
    details: dict[str, Any] = {"runtimeRoot": str(root)}
    project = projects.create_project("Media Smoke", description="Multimodal Media Layer")
    project_id = str(project["projectId"])

    image = ingestion.register_from_payload(
        {
            "projectId": project_id,
            "type": "image",
            "title": "Roadmap Screenshot",
            "mimeType": "image/png",
            "text": "2.7.3 Multimodal Media Layer with OCR and citations.",
            "metadata": {"caption": "Roadmap screenshot"},
            "process": True,
        }
    )
    checks["imageImport"] = "PASS" if image.get("status") == "ready" and image.get("type") == "image" else "FAIL"

    pdf = ingestion.register_from_payload(
        {
            "projectId": project_id,
            "type": "pdf",
            "title": "Media Spec",
            "mimeType": "application/pdf",
            "pageTexts": [
                {"page": 1, "text": "Media Library stores first-class media objects."},
                {"page": 2, "text": "Media chunks enter Local RAG and generate citations. api_key=sk-media-secret"},
            ],
            "process": True,
        }
    )
    pdf_segments = library.list_segments(str(pdf["mediaId"]))
    checks["pdfPageIndex"] = "PASS" if any(int(segment.get("page") or 0) == 2 for segment in pdf_segments) else "FAIL"

    webpage = ingestion.register_from_payload(
        {
            "projectId": project_id,
            "type": "webpage",
            "title": "Snapshot",
            "url": "https://example.test/media?token=secret-token",
            "html": "<article><h1>Media Snapshot</h1><p>Webpage text becomes citable media.</p></article>",
            "process": True,
        }
    )
    webpage_path = library.media_file_path(webpage)
    checks["webpageSnapshot"] = "PASS" if webpage_path.is_file() and webpage.get("type") == "webpage" else "FAIL"

    all_segments = [*library.list_segments(str(image["mediaId"])), *pdf_segments, *library.list_segments(str(webpage["mediaId"]))]
    checks["mediaSegments"] = "PASS" if len(all_segments) >= 4 else "FAIL"
    checks["mediaCitations"] = "PASS" if all(isinstance(segment.get("citation"), dict) and segment["citation"].get("uri") for segment in all_segments) else "FAIL"
    hits = local_rag.search_media_index("Local RAG citations", project_id=project_id, limit=5)
    checks["mediaToRag"] = "PASS" if hits and any(hit.source_id == pdf["mediaId"] for hit in hits) else "FAIL"
    try:
        ingestion.ingest_upload({"filename": "payload.exe", "content_type": "application/x-msdownload", "data": b"not media"})
    except Exception:
        upload_mime_rejected = True
    else:
        upload_mime_rejected = False
    original_media_upload_limit = schema.MAX_MEDIA_UPLOAD_BYTES
    try:
        schema.MAX_MEDIA_UPLOAD_BYTES = 8
        try:
            ingestion.ingest_upload({"filename": "large.png", "content_type": "image/png", "data": b"x" * 9})
        except Exception:
            upload_size_rejected = True
        else:
            upload_size_rejected = False
    finally:
        schema.MAX_MEDIA_UPLOAD_BYTES = original_media_upload_limit
    checks["mediaUploadLimits"] = "PASS" if upload_mime_rejected and upload_size_rejected else "FAIL"

    export = exports.export_project(project_id, export_format="zip")["export"]
    zip_path = Path(str(export["path"]))
    zip_names: set[str] = set()
    combined = ""
    if zip_path.is_file():
        with zipfile.ZipFile(zip_path) as archive:
            zip_names = set(archive.namelist())
            combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in zip_names)
    checks["projectExportIncludesMedia"] = "PASS" if "media/media.json" in zip_names and any(name.startswith("media/segments/") for name in zip_names) else "FAIL"
    checks["secretRedaction"] = "PASS" if "sk-media-secret" not in combined and "secret-token" not in combined else "FAIL"

    details["projectId"] = project_id
    details["mediaIds"] = [image["mediaId"], pdf["mediaId"], webpage["mediaId"]]
    details["projectExport"] = {"path": str(zip_path), "entries": sorted(zip_names)}
    details["localRag"] = local_rag.status()
    return checks, details


def build_evidence(checks: dict[str, str], details: dict[str, Any]) -> dict[str, Any]:
    status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
    return {
        "version": app_version(),
        "commit": git_short_sha(),
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": status,
        "checks": checks,
        "details": details,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Multimodal Media Layer smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / f"media-v{app_version()}.json")
    parser.add_argument("--json", action="store_true", help="Print evidence JSON to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-media-smoke-") as tmp:
        os.environ["DEEPSEEK_INFRA_ROOT"] = tmp
        runtime_root = Path(tmp)
        configure_runtime_root(runtime_root)
        checks, details = run_media_smoke(runtime_root)
        evidence = build_evidence(checks, details)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
