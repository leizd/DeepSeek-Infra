from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepseek_infra.infra.rag import local_rag


@pytest.fixture
def memory_probe(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    probe_dir = Path(__file__).resolve().parents[1] / "tasks" / "native-runtime"
    monkeypatch.syspath_prepend(str(probe_dir))
    monkeypatch.setattr(local_rag, "LOCAL_RAG_ENABLED", True)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_BACKEND", "sqlite")
    return SimpleNamespace(**runpy.run_path(str(probe_dir / "memory_parity_probe.py")))


@pytest.mark.parametrize(
    ("query", "project_id", "expected_contents"),
    [
        (
            "请帮我记住: 我喜欢深色主题",
            None,
            ["重要：我用 React", "我喜欢深色主题", "我用 React 做前端"],
        ),
        ("Rust", "abc", ["重要：我用 React", "我用 React 做前端", "项目用 Rust 写"]),
    ],
)
def test_memory_probe_uses_no_index_even_when_rag_is_importable(
    memory_probe: SimpleNamespace, tmp_settings: Path, query: str, project_id: str | None,
    expected_contents: list[str],
) -> None:
    # A real, available index used to leak into this nominally no-index probe.
    local_rag.sync_memories(memory_probe.STORE_FIXTURE)
    namespace = memory_probe.build_namespace(tmp_settings / "probe")
    namespace["save_memories"](memory_probe.STORE_FIXTURE)
    message = {"role": "user", "content": query}
    if project_id:
        message["projectId"] = project_id
    state = namespace["prepare_memory_state"]({"messages": [message]})
    assert state["hitCount"] == len(expected_contents)
    context_rows = [row for row in state["context"].splitlines() if row.startswith("- ")]
    assert all(row.endswith(content) for row, content in zip(context_rows, expected_contents))
    assert len(context_rows) == len(expected_contents)


def test_memory_probe_does_not_replace_the_host_index(
    memory_probe: SimpleNamespace, tmp_settings: Path,
) -> None:
    local_rag.sync_memories([{"id": "host-memory", "content": "Host memory", "scope": "global"}])
    before = local_rag.LOCAL_RAG_DB.read_bytes()
    namespace = memory_probe.build_namespace(tmp_settings / "probe")
    namespace["save_memories"](memory_probe.STORE_FIXTURE)
    namespace["retrieve_memories"]("Rust", scopes=["global", "project:abc"])
    assert local_rag.LOCAL_RAG_DB.read_bytes() == before
    assert len(json.loads(namespace["MEMORY_FILE"].read_text(encoding="utf-8"))) == 4
    # The probe's isolation must not disable RAG in the rest of this process.
    assert local_rag.search_memories_index("Host memory", scopes=["global"])[0].source_id == "host-memory"
