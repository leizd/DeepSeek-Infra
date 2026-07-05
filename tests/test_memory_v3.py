from __future__ import annotations

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.memory import policy, search, store
from deepseek_infra.infra.workspace import projects


def test_memory_v3_add_search_edit_delete_and_project_isolation(tmp_settings: object) -> None:
    project = projects.create_project("Memory GA")
    other_project = projects.create_project("Other Memory GA")
    project_id = str(project["projectId"])

    memory = store.add_memory(
        "User prefers concise release status updates.",
        scope="project",
        project_id=project_id,
        memory_type="preference",
        source={"kind": "project", "refId": project_id},
        confidence=0.72,
    )

    assert memory["memoryId"]
    assert memory["scope"] == "project"
    assert memory["type"] == "preference"
    assert memory["source"]["kind"] == "project"
    assert memory["confidence"] == 0.72
    assert len(store.list_memories(scope="project", project_id=project_id)) == 1
    assert store.list_memories(scope="project", project_id=str(other_project["projectId"])) == []

    hits = search.search_memories("concise status", project_id=project_id)
    assert [item["memoryId"] for item in hits] == [memory["memoryId"]]

    edited = store.edit_memory(memory["memoryId"], {"content": "User prefers brief release summaries.", "type": "instruction"})
    assert edited["type"] == "instruction"
    assert "brief" in edited["content"]

    assert store.delete_memory(memory["memoryId"]) == 1
    assert store.list_memories(scope="project", project_id=project_id) == []


def test_memory_v3_blocks_sensitive_memory(tmp_settings: object) -> None:
    with pytest.raises(AppError) as exc:
        store.add_memory("api key should be sk-live-secret", memory_type="fact")

    assert exc.value.code == ErrorCode.SENSITIVE_CONTENT


def test_skill_memory_policy_respects_project_scope() -> None:
    skill = {"memoryPolicy": {"read": True, "scope": "project"}}

    assert policy.skill_can_read_memory(skill, project_id="proj_1234") is True
    assert policy.readable_scopes(project_id="proj_1234") == ["global", "project:proj_1234"]
