from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import memory


def test_prepare_disabled_and_explicit_command_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = memory.prepare_memory_state({"memoryEnabled": False})
    assert disabled["enabled"] is False

    monkeypatch.setattr(memory, "apply_explicit_memory_command", lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("denied")))
    monkeypatch.setattr(memory, "retrieve_memories", lambda *_args, **_kwargs: [])
    state = memory.prepare_memory_state({"messages": [{"role": "user", "content": "remember this"}]})
    assert "denied" in state["notice"]


@pytest.mark.parametrize("payload", ["{", "{}", "42"])
def test_corrupt_memory_file_returns_empty(tmp_settings: Path, payload: str) -> None:
    memory.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory.MEMORY_FILE.write_text(payload, encoding="utf-8")
    assert memory.load_memories() == []


def test_save_cleans_invalid_records_and_tolerates_index_failure(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.rag import local_rag

    monkeypatch.setattr(local_rag, "sync_memories", lambda _: (_ for _ in ()).throw(RuntimeError("index unavailable")))
    memory._save_memories_unlocked(
        [
            None,  # type: ignore[list-item]
            {"content": ""},
            {"content": "valid", "confidence": "bad", "expiresAt": "tomorrow", "source": {"kind": "test"}},
        ]
    )
    saved = json.loads(memory.MEMORY_FILE.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["confidence"] == 0.9
    assert saved[0]["expiresAt"] == "tomorrow"


def test_posix_memory_lock_uses_flock_without_mutating_global_os(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    fake_fcntl = ModuleType("fcntl")
    fake_fcntl.LOCK_EX = 1  # type: ignore[attr-defined]
    fake_fcntl.LOCK_UN = 2  # type: ignore[attr-defined]
    fake_fcntl.flock = lambda _fd, mode: calls.append(mode)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    monkeypatch.setattr(memory, "os", SimpleNamespace(name="posix"))
    with memory.memory_file_lock():
        calls.append(3)
    assert calls == [1, 3, 2]


def test_scope_labels_categories_and_conflict_domains() -> None:
    assert memory.memory_scope_from_payload({"messages": [{"role": "user", "content": "x", "seekId": "seek-1"}]}) == "seek:seek-1"
    assert memory.memory_scope_from_payload({"messages": [{"role": "assistant"}, "bad"]}) == "global"
    assert memory.memory_scope_label("project:alpha") == "project:alpha"
    assert memory.memory_scope_label("invalid scope") == "global"
    assert memory.infer_memory_category("I prefer concise replies") == "preference"
    assert memory.memory_conflict_key("English please", "preference") == "preference:language"
    assert memory.memory_conflict_key("call me Ada", "preference") == "preference:addressing"
    assert memory.memory_conflict_key("dark theme", "preference") == "preference:theme"
    assert memory.memory_conflict_key("project alpha uses sqlite", "project") == "project:alpha"


def test_conflict_filters_and_suggestion_rejections(tmp_settings: Path) -> None:
    assert memory.detect_memory_conflicts("") == []
    assert memory.detect_memory_conflicts("ordinary fact", category="fact") == []
    old = memory.upsert_memory("English replies", category="preference", scope="project:alpha")
    memory.upsert_memory("English replies", category="preference", scope="project:beta")
    conflicts = memory.detect_memory_conflicts("Chinese replies", category="preference", scope="project:alpha")
    assert [item["id"] for item in conflicts] == [old["id"]]
    with pytest.raises(AppError, match="empty"):
        memory.build_memory_suggestion("")
    with pytest.raises(AppError, match="sensitive"):
        memory.build_memory_suggestion("password: secret")
    with pytest.raises(AppError, match="empty"):
        memory.upsert_memory("")


def test_delete_clear_retrieve_and_context_budget_edges(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert memory.delete_memories_by_query("") == 0
    memory.upsert_memory("Pinned project memory", category="project", pinned=True)
    memory.upsert_memory("", category="fact") if False else None

    from deepseek_infra.infra.rag import local_rag

    monkeypatch.setattr(local_rag, "search_memories_index", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("corrupt index")))
    hits = memory.retrieve_memories("memory", scopes=["global"])
    assert hits and hits[0]["pinned"] is True

    monkeypatch.setattr(memory, "MEMORY_CONTEXT_CHAR_BUDGET", 5)
    context = memory.format_memory_context([{"content": "", "category": "fact"}, {"content": "long memory", "category": "fact"}])
    assert "[" in context
    assert memory.clear_memories() == 1
    assert memory.load_memories() == []


def test_explicit_english_remember_forget_and_opt_out(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert memory.apply_explicit_memory_command("do not remember this") == ""
    remember_matches = iter([None, None, SimpleNamespace(group=lambda _: "concise replies")])
    def remember_search(pattern: str, string: str, flags: int = 0) -> object:
        try:
            return next(remember_matches)
        except StopIteration:
            return re.search(pattern, string, flags)
    monkeypatch.setattr(
        memory,
        "re",
        SimpleNamespace(search=remember_search, sub=re.sub, fullmatch=re.fullmatch, IGNORECASE=re.IGNORECASE, DOTALL=re.DOTALL),
    )
    notice = memory.apply_explicit_memory_command("remember concise replies")
    assert "concise replies" in notice
    forget_matches = iter([None, SimpleNamespace(group=lambda _: "concise replies")])
    def forget_search(pattern: str, string: str, flags: int = 0) -> object:
        try:
            return next(forget_matches)
        except StopIteration:
            return re.search(pattern, string, flags)
    monkeypatch.setattr(
        memory,
        "re",
        SimpleNamespace(search=forget_search, sub=re.sub, fullmatch=re.fullmatch, IGNORECASE=re.IGNORECASE, DOTALL=re.DOTALL),
    )
    deleted = memory.apply_explicit_memory_command("forget concise replies")
    assert "1" in deleted
    assert memory.format_memory_notice("saved")
