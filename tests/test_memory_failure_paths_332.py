from __future__ import annotations

import json
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


def test_conflict_detection_skips_unrelated_and_identical_memories(tmp_settings: Path) -> None:
    different_category = memory.upsert_memory("English is a language", category="fact")
    different_domain = memory.upsert_memory("I prefer a dark theme", category="preference")
    identical = memory.upsert_memory("Chinese replies", category="preference")
    conflict = memory.upsert_memory("English replies", category="preference")

    conflicts = memory.detect_memory_conflicts("Chinese replies", category="preference")

    assert [item["id"] for item in conflicts] == [conflict["id"]]
    returned_ids = {item["id"] for item in conflicts}
    assert different_category["id"] not in returned_ids
    assert different_domain["id"] not in returned_ids
    assert identical["id"] not in returned_ids


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


def test_every_memory_write_path_is_denied_once_python_is_de_authorized(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`memory_store` is a declared domain (python -> rust), and the handover has to be mechanical.

    Every write path funnels through `_save_memories_unlocked`, so one gate there covers them all —
    `upsert_memory`, `save_memories`, `delete_memories_by_query`, `delete_memory_by_id`,
    `clear_memories`, and the turn-level command, which reaches the store through the first and
    third. The paths that only call the choke point when they actually delete are deliberately not
    denied when they do nothing: a no-op is not a write.

    The gate is mode-driven, because ADR-0049 says "a cutover gate that has not passed leaves the
    prior owner authoritative; it does not permit dual writers" — so the assertions on the other
    side of this test matter just as much: the default mode leaves every path working.
    """
    from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError

    seeded = memory.upsert_memory("Python still owns the store here", category="fact")
    memory.save_memories([seeded])
    for write in (
        lambda: memory.upsert_memory("written while Python owns it", category="fact"),
        lambda: memory.save_memories([seeded]),
        lambda: memory.delete_memories_by_query("still owns"),
        lambda: memory.delete_memory_by_id(str(seeded["id"])),
        memory.clear_memories,
    ):
        write()
    assert memory.load_memories() == [], "the default mode must keep every write path working"

    # The denial batch has to name what is actually stored: the two delete paths reach the choke
    # point only when they would really delete. Seeded in the default mode, then captured.
    present = memory.upsert_memory("still Python's until the cutover", category="fact")
    survivor = str(present["id"])
    before = memory.MEMORY_FILE.read_text(encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    for denied_write in (
        lambda: memory.upsert_memory("after the cutover", category="fact"),
        lambda: memory.save_memories([present]),
        lambda: memory.delete_memories_by_query("until the cutover"),
        lambda: memory.delete_memory_by_id(survivor),
        memory.clear_memories,
        lambda: memory.apply_explicit_memory_command("请帮我记住: 之后"),
    ):
        with pytest.raises(PythonWriterMechanicallyDeniedError):
            denied_write()
    assert memory.MEMORY_FILE.read_text(encoding="utf-8") == before, "a denied write left a change behind"

    # A call that would delete nothing never reaches the choke point, so it stays allowed: a no-op
    # is not a write, and denying it would be a wider behaviour change than the ownership question
    # asks for. These two lines are why the delete paths above had to name what is stored.
    assert memory.delete_memories_by_query("nothing matches this") == 0
    assert memory.delete_memory_by_id("no-such-id") == 0
    assert memory.MEMORY_FILE.read_text(encoding="utf-8") == before


def test_the_domain_is_the_one_the_ownership_contract_declares() -> None:
    """The gate's domain string and the contract's id are the same string, or the gate never fires.

    `release/native_runtime_ownership_v1.json` is the authority for the id; this pins the two
    together so a rename on either side fails here instead of silently disarming the gate.
    """
    import json

    from deepseek_infra.infra.native_runtime.authority import RUST_DATA_DOMAINS

    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "release" / "native_runtime_ownership_v1.json").read_text(encoding="utf-8")
    )
    declared = {
        item["id"]
        for item in contract["domains"]
        if item.get("current_owner") == "python" and item.get("target_owner") == "rust" and item.get("durable_store") == "rust_data"
    }
    assert declared, "the contract declares no python -> rust data domain"
    assert RUST_DATA_DOMAINS <= declared, f"gate domains not declared by the contract: {sorted(RUST_DATA_DOMAINS - declared)}"


def test_explicit_memory_commands_are_parsed_by_their_real_patterns(tmp_settings: Path) -> None:
    """The command grammar is module-level regexes, so this runs them.

    The test this replaces monkeypatched `memory.re` and handed the function fabricated match
    objects, so it passed whatever the patterns said. Both patterns had in fact kept a leading
    `:` in their first alternative -- `(:忘记|…` where `(?:忘记|…` was meant -- which made every
    natural phrasing ("记住: X", "forget: X") fail to match at all.
    """

    # Nothing to parse is not a command, and "don't remember" is an instruction not to save --
    # the latter is checked before every branch below.
    assert memory.apply_explicit_memory_command("") == ""
    assert memory.apply_explicit_memory_command("不要记住: 这是临时的") == ""
    assert memory.apply_explicit_memory_command("do not remember this") == ""
    assert memory.load_memories() == []

    # The remember branch requires the whole prefix it spells out -- `请`, then `帮我`, then the
    # verb -- and saves the text after the colon rather than the command word. Whether those
    # prefixes *should* be optional (so that a bare "记住: X" also matches) is a product decision
    # separate from repairing the typo, so it is deliberately not asserted here either way.
    for query, content in [
        ("请帮我记住: 我的生日是3月5日", "我的生日是3月5日"),
        ("请帮我记住：牙医预约在周四", "牙医预约在周四"),
        ("请帮我以后记得: 部署要走 CI", "部署要走 CI"),
    ]:
        notice = memory.apply_explicit_memory_command(query)
        assert content in notice, query
        assert content in {item["content"] for item in memory.load_memories()}, query

    # "don't forget" asks for the content to be *kept*. It must not reach the forget branch, which
    # matches the bare `忘记:` substring and would delete what the user asked to keep.
    notice = memory.apply_explicit_memory_command("别忘记: 明天下午三点开会")
    assert "明天下午三点开会" in notice
    contents = {item["content"] for item in memory.load_memories()}
    assert "明天下午三点开会" in contents
    assert "我的生日是3月5日" in contents

    # "forget" deletes against what follows the colon, not against the command word: while the
    # alternation captured, the target was the literal "忘记", so the count was always 0.
    notice = memory.apply_explicit_memory_command("忘记: 牙医预约")
    assert "删除 1 条" in notice
    remaining = {item["content"] for item in memory.load_memories()}
    assert "牙医预约在周四" not in remaining
    assert "我的生日是3月5日" in remaining
