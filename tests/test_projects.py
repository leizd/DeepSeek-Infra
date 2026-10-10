from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.rag.files import load_cached_file


def test_project_create_add_files_and_list(tmp_settings: Path) -> None:
    project = projects.create_project("Knowledge Base")

    added = projects.add_project_files(
        project["id"],
        [{"filename": "guide.txt", "content_type": "text/plain", "data": b"project library content"}],
    )
    listed = projects.list_projects()
    cached = load_cached_file(added[0]["fileId"], project_id=project["id"])

    assert listed[0]["id"] == project["id"]
    assert listed[0]["documents"][0]["name"] == "guide.txt"
    assert cached["name"] == "guide.txt"
    assert not (tmp_settings / ".file-cache" / f"{added[0]['fileId']}.json").exists()
    assert (tmp_settings / ".projects" / project["id"] / "files" / f"{added[0]['fileId']}.json").exists()


def test_project_document_limit_is_enforced(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Small")
    monkeypatch.setattr(projects, "MAX_PROJECT_DOCUMENTS", 1)
    projects.add_project_files(project["id"], [{"filename": "one.txt", "content_type": "text/plain", "data": b"one"}])

    with pytest.raises(AppError) as cm:
        projects.add_project_files(project["id"], [{"filename": "two.txt", "content_type": "text/plain", "data": b"two"}])

    assert cm.value.code == ErrorCode.UPLOAD_TOO_LARGE


def test_delete_project_removes_project_directory(tmp_settings: Path) -> None:
    project = projects.create_project("Disposable")
    projects.add_project_files(project["id"], [{"filename": "one.txt", "content_type": "text/plain", "data": b"one"}])

    assert projects.delete_project(project["id"]) == 1
    assert projects.list_projects() == []
    assert not (tmp_settings / ".projects" / project["id"]).exists()
