"""Targeted test coverage boosters for generated_files and artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.tool_runtime import generated_files
from deepseek_infra.infra.workspace import artifacts


def test_generated_files_lifecycle(tmp_settings: Path) -> None:
    def write_doc(p: Path) -> None:
        p.write_text("# Heading\n\nContent", encoding="utf-8")

    # 1. Store generated markdown file
    res = generated_files.store_generated_file(
        "My Generated Document",
        "md",
        write_doc,
    )
    file_id = res["fileId"]
    assert res["filename"] == "My Generated Document.md"
    assert "downloadUrl" in res

    # 2. Resolve generated file
    resolved = generated_files.resolve_generated_file(file_id)
    assert resolved is not None
    assert resolved.is_file()

    # 3. Download descriptor
    media_type, name = generated_files.download_descriptor(resolved)
    assert "markdown" in media_type
    assert name == "notes.md"

    # 4. Save to custom downloads dir
    d_dir = tmp_settings / "test_downloads"
    saved = generated_files.save_generated_file_to_downloads(file_id, "Report.md", downloads_dir=d_dir)
    assert saved["ok"] is True
    assert (d_dir / "Report.md").is_file()

    # 5. Invalid file id
    assert generated_files.resolve_generated_file("invalid_id") is None
    with pytest.raises(AppError):
        generated_files.save_generated_file_to_downloads("0" * 32, downloads_dir=d_dir)


def test_artifacts_crud_lifecycle(tmp_settings: Path) -> None:
    proj = legacy_projects.create_project("Artifact Project")
    proj_id = str(proj["id"])

    p_dir = config.PROJECTS_DIR / proj_id
    p_dir.mkdir(parents=True, exist_ok=True)
    doc_file = p_dir / "report.md"
    doc_file.write_text("# Artifact Report", encoding="utf-8")

    # 1. Register artifact
    art = artifacts.register_artifact(
        proj_id,
        artifact_type="md",
        title="Initial Report",
        path=str(doc_file),
    )
    art_id = str(art["artifactId"])
    assert art["title"] == "Initial Report"
    assert art["version"] == 1

    # 2. List artifacts
    listed = artifacts.list_artifacts(proj_id)
    assert len(listed) == 1
    assert listed[0]["artifactId"] == art_id

    # 3. Rename artifact
    renamed = artifacts.rename_artifact(proj_id, art_id, "Renamed Report")
    assert renamed["title"] == "Renamed Report"

    # 4. Add version
    v2_file = p_dir / "report_v2.md"
    v2_file.write_text("# Artifact Report V2", encoding="utf-8")
    updated = artifacts.add_artifact_version(proj_id, art_id, path=str(v2_file))
    assert updated["version"] == 2
    assert len(updated["versions"]) == 2

    # 5. Delete artifact
    assert artifacts.delete_artifact(proj_id, art_id) == 1
    assert len(artifacts.list_artifacts(proj_id)) == 0
