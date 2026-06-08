from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import deepseek_infra.core.config as config
import deepseek_infra.infra.rag.files as files
import deepseek_infra.infra.agent_runtime.agent_runs as agent_runs
import deepseek_infra.infra.data.memory as memory
import deepseek_infra.infra.rag.local_rag as local_rag
import deepseek_infra.infra.observability.observability as observability
import deepseek_infra.infra.data.projects as projects
import deepseek_infra.infra.data.reminders as reminders
import deepseek_infra.infra.tool_runtime.search as search
import deepseek_infra.infra.gateway.budget_manager as budget_manager
import deepseek_infra.infra.gateway.resiliency as resiliency
import deepseek_infra.infra.gateway.semantic_cache as semantic_cache
import deepseek_infra.infra.tool_runtime.tools as tools


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point local state directories at a fresh temporary workspace."""
    file_cache_dir = tmp_path / ".file-cache"
    agent_runs_dir = tmp_path / ".agent-runs"
    memory_dir = tmp_path / ".memory"
    search_cache_dir = tmp_path / ".search-cache"
    reminders_dir = tmp_path / ".reminders"
    projects_dir = tmp_path / ".projects"
    local_rag_dir = tmp_path / ".local-rag"
    traces_dir = tmp_path / ".traces"
    semantic_cache_dir = tmp_path / ".semantic-cache"
    request_queue_dir = tmp_path / ".request-queue"
    budget_dir = tmp_path / ".budget"

    monkeypatch.setattr(config, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(config, "AGENT_RUNS_DIR", agent_runs_dir)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(config, "MEMORY_FILE", memory_dir / "memories.json")
    monkeypatch.setattr(config, "SEARCH_CACHE_DIR", search_cache_dir)
    monkeypatch.setattr(config, "REMINDERS_DIR", reminders_dir)
    monkeypatch.setattr(config, "REMINDERS_FILE", reminders_dir / "reminders.json")
    monkeypatch.setattr(config, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(config, "LOCAL_RAG_DIR", local_rag_dir)
    monkeypatch.setattr(config, "LOCAL_RAG_DB", local_rag_dir / "rag.sqlite3")
    monkeypatch.setattr(config, "TRACE_DIR", traces_dir)
    monkeypatch.setattr(config, "TRACE_DB", traces_dir / "traces.sqlite3")
    monkeypatch.setattr(config, "SEMANTIC_CACHE_DIR", semantic_cache_dir)
    monkeypatch.setattr(config, "SEMANTIC_CACHE_DB", semantic_cache_dir / "cache.sqlite3")
    monkeypatch.setattr(config, "GATEWAY_REQUEST_QUEUE_DIR", request_queue_dir)
    monkeypatch.setattr(config, "GATEWAY_REQUEST_QUEUE_DB", request_queue_dir / "queue.sqlite3")
    monkeypatch.setattr(config, "BUDGET_DIR", budget_dir)
    monkeypatch.setattr(config, "BUDGET_DB", budget_dir / "budget.sqlite3")

    monkeypatch.setattr(files, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(agent_runs, "AGENT_RUNS_DIR", agent_runs_dir)
    monkeypatch.setattr(memory, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(memory, "MEMORY_FILE", memory_dir / "memories.json")
    monkeypatch.setattr(search, "SEARCH_CACHE_DIR", search_cache_dir)
    monkeypatch.setattr(reminders, "REMINDERS_DIR", reminders_dir)
    monkeypatch.setattr(reminders, "REMINDERS_FILE", reminders_dir / "reminders.json")
    monkeypatch.setattr(projects, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(files, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(local_rag, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(local_rag, "MEMORY_FILE", memory_dir / "memories.json")
    monkeypatch.setattr(local_rag, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_DIR", local_rag_dir)
    monkeypatch.setattr(local_rag, "LOCAL_RAG_DB", local_rag_dir / "rag.sqlite3")
    monkeypatch.setattr(observability, "TRACE_DIR", traces_dir)
    monkeypatch.setattr(observability, "TRACE_DB", traces_dir / "traces.sqlite3")
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_DIR", semantic_cache_dir)
    monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_DB", semantic_cache_dir / "cache.sqlite3")
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_DIR", request_queue_dir)
    monkeypatch.setattr(resiliency, "GATEWAY_REQUEST_QUEUE_DB", request_queue_dir / "queue.sqlite3")
    monkeypatch.setattr(budget_manager, "BUDGET_DIR", budget_dir)
    monkeypatch.setattr(budget_manager, "BUDGET_DB", budget_dir / "budget.sqlite3")
    monkeypatch.setattr(tools, "FILE_CACHE_DIR", file_cache_dir)
    monkeypatch.setattr(tools, "SEARCH_CACHE_DIR", search_cache_dir)
    monkeypatch.setattr(tools, "PROJECTS_DIR", projects_dir)

    files._load_cached_file_cached.cache_clear()
    yield tmp_path
    files._load_cached_file_cached.cache_clear()


@pytest.fixture
def fake_deepseek() -> Callable[[str, str, dict[str, int] | None], dict[str, object]]:
    def _make(content: str = "hello", reasoning: str = "", usage: dict[str, int] | None = None) -> dict[str, object]:
        return {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": content, "reasoning_content": reasoning}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    return _make


@pytest.fixture
def mock_urlopen() -> object:
    with patch("urllib.request.urlopen") as mocked:
        yield mocked


def deepseek_response_bytes(content: str = "hello", usage: dict[str, int] | None = None) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": content}}],
            "usage": usage or {},
        }
    ).encode("utf-8")


