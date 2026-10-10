"""Gap tests for chat routes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes.chat import ChatRouteDeps, create_chat_router


@pytest.fixture
def chat_client() -> Iterator[TestClient]:
    deps = ChatRouteDeps(
        chat_event_stream=lambda payload: iter([b'{"chunk": 1}']),
        conversation_search=lambda payload: {"ok": True, "matches": []},
    )
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "code": exc.code.value}, status_code=exc.status or 400)

    app.include_router(create_chat_router(deps))
    with patch("deepseek_infra.web.routes.chat.require_api_auth", lambda request: None):
        yield TestClient(app)


def test_api_chat_stream_preflight(chat_client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.chat.preflight_chat_payload") as preflight:
        resp = chat_client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    preflight.assert_called_once()


def test_v1_chat_completions_stream(chat_client: TestClient) -> None:
    def fake_stream(payload, model):
        yield b'data: {"id":"1"}\n\n'

    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": True}) as convert, \
         patch("deepseek_infra.web.routes.chat.openai_chat_stream", side_effect=fake_stream) as stream:
        resp = chat_client.post("/v1/chat/completions", json={"model": "deepseek-chat", "messages": [], "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    convert.assert_called_once()
    stream.assert_called_once()


def test_v1_chat_completions_uses_python_execution_after_translation(chat_client: TestClient) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": False}) as convert, \
         patch("deepseek_infra.web.routes.chat.openai_chat_completion", return_value={"result": "ok"}) as execute:
        resp = chat_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"result": "ok"}
    convert.assert_called_once()
    execute.assert_called_once_with({"model": "deepseek-chat", "stream": False}, "deepseek-chat")


def test_v1_chat_completions_does_not_forward_authorization_to_execution(chat_client: TestClient) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": False}) as convert, \
         patch("deepseek_infra.web.routes.chat.openai_chat_completion", return_value={"result": "ok"}) as execute:
        resp = chat_client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer local-token"})
    assert resp.status_code == 200
    convert.assert_called_once()
    execute.assert_called_once_with({"model": "deepseek-chat", "stream": False}, "deepseek-chat")


def test_v1_models_remains_python_owned(chat_client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.chat.openai_models_list", return_value={"object": "list", "data": []}) as models:
        resp = chat_client.get("/v1/models", headers={"Authorization": "Bearer local-token"})
    assert resp.status_code == 200
    assert resp.json() == {"object": "list", "data": []}
    models.assert_called_once_with()
