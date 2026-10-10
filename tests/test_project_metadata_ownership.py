"""Project metadata ownership denies Python before durable or derived effects."""
from pathlib import Path

import pytest

from deepseek_infra.infra.data import projects
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import projects as workspace_projects


def test_python_project_metadata_writes_denied_after_cutover(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Existing")
    project_id = project["id"]
    path = tmp_settings / ".projects" / project_id / "project.json"
    before = path.read_bytes()
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    for write in [lambda: projects.write_project(project), lambda: projects.create_project("Denied"),
                  lambda: workspace_projects.rename_project(project_id, "Denied"),
                  lambda: workspace_projects.upsert_project_conversation(project_id, {"id": "conv-denied"}),
                  lambda: projects.delete_project(project_id),
                  lambda: projects.add_project_files(project_id, [{"filename": "denied.txt", "content_type": "text/plain", "data": b"denied"}])]:
        with pytest.raises(PythonWriterMechanicallyDeniedError):
            write()
        assert path.read_bytes() == before
    assert len(projects.list_projects()) == 1
    assert not (path.parent / "files").exists()
