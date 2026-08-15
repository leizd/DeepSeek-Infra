"""Targeted test coverage boosters for remaining infra branches."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.infra.automation import evidence as auto_evidence
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.mcp import adapters as mcp_adapters
from deepseek_infra.infra.media import evidence as media_evidence
from deepseek_infra.infra.memory import search as mem_search
from deepseek_infra.infra.workspace import (
    backup_writer_lease,
    home as ws_home,
    mutation_gate as ws_mutation,
    saved_items as ws_saved,
)


def test_automation_and_media_evidence_payloads() -> None:
    auto_res = auto_evidence.automation_evidence_payload(
        "4.5.1",
        checks={"sample": "PASS"},
        details={"extra": 123},
    )
    assert auto_res["checks"]["sample"] == "PASS"

    media_res = media_evidence.evidence_metadata(
        "4.5.1",
        status="ok",
        checks={"ocr": "PASS"},
        details={"extra": 456},
    )
    assert media_res["checks"]["ocr"] == "PASS"


def test_memory_search_and_context(tmp_settings: Path) -> None:
    # Memory context for skill with no read policy
    assert mem_search.memory_context_for_skill({}, "query") == ""

    # Memory context for skill with read policy
    skill_with_policy = {
        "memoryPolicy": {
            "read": True,
            "scope": "project",
        }
    }
    ctx = mem_search.memory_context_for_skill(skill_with_policy, "query", project_id="proj_1")
    assert isinstance(ctx, str)


def test_mcp_hub_web_search_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_adapters, "TAVILY_API_KEY", "tvly-test-key")
    cb = mcp_adapters._hub_web_search_callback()
    assert cb is not None

    monkeypatch.setattr(
        mcp_adapters,
        "search_single_round",
        lambda query, **k: {"results": [{"title": "test", "content": "res"}]},
    )
    res = cb("test query", "search")
    assert "results" in res


def test_workspace_home_and_saved_items_coverage(tmp_settings: Path) -> None:
    # Home
    home_data = ws_home.workspace_home()
    assert isinstance(home_data, dict)

    # Saved items
    proj = legacy_projects.create_project("Test Project")
    proj_id = str(proj["id"])

    item = ws_saved.create_saved_item(
        proj_id,
        item_type="artifact",
        title="Test Item",
        content="hello world content",
    )
    assert item["title"] == "Test Item"
    saved_id = str(item["savedId"])
    assert len(ws_saved.list_saved_items(proj_id)) == 1

    updated = ws_saved.update_saved_item(proj_id, saved_id, {"title": "Updated Item"})
    assert updated["title"] == "Updated Item"
    assert ws_saved.delete_saved_item(proj_id, saved_id) == 1


def test_mutation_gate_and_writer_lease(tmp_settings: Path) -> None:
    # Mutation gate acquire/release
    with ws_mutation.mutation_scope("test_op"):
        pass

    # Target writer lease
    root = tmp_settings / "target_root"
    root.mkdir(parents=True, exist_ok=True)
    lease = backup_writer_lease.TargetWriterLease(
        root=root,
        target_id="target_test",
        owner_run_id="run_test",
        owner_instance_id="proc_test",
        fencing_token=1,
    )
    with lease:
        lease.assert_owned()
        lease.renew()
