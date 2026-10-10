"""Gap tests for memory routes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.memory import MemoryRouteDeps, create_memory_router


@pytest.fixture
def memory_client() -> Iterator[tuple[TestClient, MagicMock]]:
    delete_memory_by_id = MagicMock(return_value=1)
    deps = MemoryRouteDeps(
        load_memories=lambda: [],
        clear_memories=lambda: 0,
        normalize_memory_category=lambda category, content: str(category or "general"),
        normalize_memory_scope=lambda scope: str(scope or "global"),
        detect_memory_conflicts=lambda content, category, scope: [],
        upsert_memory=lambda *args, **kwargs: {"id": "m1"},
        delete_memories_by_query=lambda query, scopes: 0,
        delete_memory_by_id=delete_memory_by_id,
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_memory_router(deps))
    with patch("deepseek_infra.web.routes.memory.require_api_auth", lambda request: None):
        yield TestClient(app), delete_memory_by_id


def test_memory_deletebyid_action(memory_client: tuple[TestClient, MagicMock]) -> None:
    client, delete_memory_by_id = memory_client
    resp = client.post("/api/memory", json={"action": "deletebyid", "id": "m1"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    delete_memory_by_id.assert_called_once_with("m1")


def test_memory_edit_by_id(memory_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = memory_client
    with patch("deepseek_infra.web.routes.memory.memory_store.edit_memory", return_value={"id": "m1", "content": "updated"}) as edit:
        resp = client.patch("/api/memory/m1", json={"content": "updated"})
    assert resp.status_code == 200
    assert resp.json()["memory"]["content"] == "updated"
    edit.assert_called_once_with("m1", {"content": "updated"})


def test_memory_search_with_query_and_limit(memory_client: tuple[TestClient, MagicMock]) -> None:
    client, _ = memory_client
    with patch("deepseek_infra.web.routes.memory.memory_search.search_memories", return_value=[{"id": "m1"}]) as search:
        resp = client.get("/api/memory/search?q=test&limit=5&projectId=p1&skillId=s1&automationId=a1")
    assert resp.status_code == 200
    assert resp.json()["memories"] == [{"id": "m1"}]
    search.assert_called_once_with("test", project_id="p1", skill_id="s1", automation_id="a1", limit=5)
