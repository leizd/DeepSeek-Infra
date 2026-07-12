from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects


def test_project_listing_limits_and_corrupt_records(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    projects.PROJECTS_DIR.mkdir()
    (projects.PROJECTS_DIR / "plain.txt").write_text("skip", encoding="utf-8")
    corrupt = projects.PROJECTS_DIR / "proj_bad"
    corrupt.mkdir()
    (corrupt / "project.json").write_text("not-json", encoding="utf-8")
    array = projects.PROJECTS_DIR / "proj_array"
    array.mkdir()
    (array / "project.json").write_text("[]", encoding="utf-8")
    assert projects.list_projects() == []

    monkeypatch.setattr(projects, "MAX_PROJECTS", 0)
    with pytest.raises(AppError):
        projects.create_project("blocked")
    assert projects.delete_project("proj_none") == 0


def test_project_read_normalizes_old_and_partial_state(tmp_settings: Path) -> None:
    directory = projects.PROJECTS_DIR / "proj_legacy"
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(
        json.dumps(
            {
                "name": " Legacy\nProject ",
                "documents": [None, {"fileId": "bad"}, {"fileId": "a" * 32, "projectId": "proj_legacy"}],
                "skills": {"enabledSkills": ["skill:one"], "defaultSkill": "missing"},
                "skillRuns": [None, {"runId": "run-old", "latencyMs": 0}],
                "savedItems": [None, {}],
                "artifacts": [None, {}],
            }
        ),
        encoding="utf-8",
    )
    project = projects.read_project("proj_legacy")
    assert project is not None
    assert project["name"] == "Legacy Project"
    assert len(project["documents"]) == 1
    assert project["skills"]["defaultSkill"] == ""
    assert project["skillRuns"][0]["skillRunId"] == "run-old"
    assert projects.read_project("proj_none") is None
    with pytest.raises(AppError):
        projects.require_project("proj_none")


def test_project_normalizers_reject_malformed_nested_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projects.secrets, "token_hex", lambda _size: "b" * 16)
    assert projects.normalize_documents("bad") == []
    assert projects.normalize_documents([None, {"fileId": "short", "projectId": "p"}]) == []
    assert projects.normalize_skill_runs("bad") == []
    assert projects.normalize_skill_runs([None]) == []
    run = projects.normalize_skill_run(
        {
            "artifactIds": ["a", "a", ""],
            "savedItemIds": ["s"],
            "artifactCount": "bad",
            "savedItemCount": None,
            "input": "bad",
            "latencyMs": 0,
        }
    )
    assert run["artifactIds"] == ["a"]
    assert run["artifactCount"] == 1
    assert run["savedItemCount"] == 1
    assert run["input"] == {}
    assert projects._safe_int(-4) == 0
    assert projects._safe_int(object(), default=-2) == 0
    assert projects.normalize_saved_items("bad") == []
    saved = projects.normalize_saved_items([None, {"source": "bad"}])[0]
    assert saved["id"] == "saved-" + "b" * 16
    assert saved["source"] == {}
    assert projects.normalize_project_artifacts("bad") == []
    artifact = projects.normalize_project_artifacts([None, {"source": "bad"}])[0]
    assert artifact["source"] == {}


def test_project_skill_pack_normalization_and_touch_edges() -> None:
    skills = projects.normalize_project_skills(
        {
            "enabledPacks": ["pack:one", {"packId": "pack:two"}, "bad id"],
            "enabledPackVersions": [
                {"packId": "pack:one", "version": "1", "updatedAt": "yesterday"},
                {"packId": "pack:one", "version": "2"},
                "pack:three",
                "bad id",
            ],
            "enabledSkills": ["skill:one", "skill:one", "bad id"],
            "recentSkills": ["skill:two"],
            "defaultSkill": "skill:one",
        }
    )
    assert skills["enabledPacks"] == ["pack:one", "pack:two", "pack:three"]
    assert skills["enabledPackVersions"] == [
        {"packId": "pack:one", "version": "1", "installedAt": "yesterday"},
        {"packId": "pack:three", "version": "", "installedAt": ""},
    ]
    assert skills["enabledSkills"] == ["skill:one"]
    project: dict[str, object] = {"skills": {}}
    projects.touch_project_skill(project, "bad id")
    assert project == {"skills": {}}
    projects.touch_project_skill(project, "skill:new")
    assert project["skills"]["defaultSkill"] == "skill:new"  # type: ignore[index]
    assert projects.normalize_pack_id_for_project({"packId": "pack:new"}) == "pack:new"
    assert projects.normalize_pack_id_for_project({}) == ""
    assert projects.normalize_skill_id_for_project("no spaces") == ""
    assert projects.unique_strings((item for item in ["a", "a", "", "b"])) == ["a", "b"]


def test_project_file_ingestion_handles_non_bytes_and_replacement(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Files")
    calls: list[bytes] = []

    def extract(_name: str, _content_type: str, data: bytes, **_kwargs: object) -> dict[str, object]:
        calls.append(data)
        return {"fileId": "a" * 32, "projectId": project["id"], "name": "same.txt"}

    monkeypatch.setattr(projects, "extract_uploaded_file", extract)
    added = projects.add_project_files(project["id"], [{"filename": "one", "data": "not-bytes"}, {"filename": "two", "data": b"ok"}])
    assert calls == [b"", b"ok"]
    assert len(added) == 2
    assert len(projects.require_project(project["id"])["documents"]) == 1
    with pytest.raises(AppError):
        projects.validate_project_id("../escape")
