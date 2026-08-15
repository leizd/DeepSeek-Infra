"""Targeted test coverage boosters for remaining infra branches."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from deepseek_infra.core.config import APP_VERSION
from deepseek_infra.infra.automation import evidence as auto_evidence
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.mcp import adapters as mcp_adapters
from deepseek_infra.infra.media import evidence as media_evidence
from deepseek_infra.infra.memory import search as mem_search
from deepseek_infra.infra.memory import store as mem_store
from deepseek_infra.infra.rag import context_compressor
from deepseek_infra.infra.workspace import (
    backup_targets,
    backup_writer_lease,
    home as ws_home,
    mutation_gate as ws_mutation,
    saved_items as ws_saved,
)


def test_automation_and_media_evidence_payloads() -> None:
    auto_res = auto_evidence.automation_evidence_payload(
        APP_VERSION,
        checks={"sample": "PASS"},
        details={"extra": 123},
    )
    assert auto_res["checks"]["sample"] == "PASS"

    media_res = media_evidence.evidence_metadata(
        APP_VERSION,
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

    # Search memories
    mem_list = mem_search.search_memories("query", project_id="proj_1", limit=5)
    assert isinstance(mem_list, list)


def test_memory_store_operations(tmp_settings: Path) -> None:
    added = mem_store.add_memory(
        "Remember this important testing fact",
        scope="global",
        memory_type="fact",
        confidence=0.95,
        pinned=True,
    )
    assert added["content"] == "Remember this important testing fact"
    mem_id = str(added["id"])

    listed = mem_store.list_memories(scope="global")
    assert any(m["id"] == mem_id for m in listed)


def test_context_compressor_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    summary_ctx = context_compressor.format_context_summary_context("Summary text")
    assert "Summary text" in summary_ctx

    empty_res = context_compressor.compress_context_payload({"messages": [], "apiKey": "test-key"})
    assert empty_res["compressedMessageCount"] == 0

    # Serialization
    serialized = context_compressor.serialize_messages_for_context_summary(
        [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there"},
        ]
    )
    assert "Hello world" in serialized
    assert "Hi there" in serialized


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


def test_backup_targets_reinitialize(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_temp = tmp_settings / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))
    t_root = tmp_settings / "new_target_dir"
    t_root.mkdir(parents=True, exist_ok=True)
    target_info = backup_targets.reinitialize_target(t_root, label="External USB")
    assert target_info["label"] == "External USB"
    assert "targetId" in target_info
