"""Gap tests for chat routes."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
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


def test_v1_chat_completions_rust_gateway_ok_with_empty_auth_headers(chat_client: TestClient) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": False}) as convert, \
         patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_chat_to_rust", return_value=SimpleNamespace(ok=True, body={"result": "ok"})) as proxy, \
         patch("deepseek_infra.web.routes.chat.openai_chat_completion") as fallback:
        resp = chat_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"result": "ok"}
    convert.assert_called_once()
    proxy.assert_called_once()
    assert proxy.call_args.kwargs["headers"] == {}
    fallback.assert_not_called()


def test_v1_chat_completions_rust_gateway_unavailable_without_fallback_raises(chat_client: TestClient) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": False}) as convert, \
         patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_chat_to_rust", return_value=SimpleNamespace(ok=False, body="rust down")), \
         patch("deepseek_infra.web.routes.chat.fallback_to_python_enabled", return_value=False), \
         patch("deepseek_infra.web.routes.chat.openai_chat_completion") as fallback:
        resp = chat_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 502
    convert.assert_called_once()
    fallback.assert_not_called()


def test_v1_chat_compositions_rust_gateway_falls_back_to_python(chat_client: TestClient) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": False}) as convert, \
         patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_chat_to_rust", return_value=SimpleNamespace(ok=False, body="rust down")), \
         patch("deepseek_infra.web.routes.chat.fallback_to_python_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.openai_chat_completion", return_value={"content": "ok"}) as fallback:
        resp = chat_client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"content": "ok"}
    convert.assert_called_once()
    fallback.assert_called_once()


def test_v1_chat_completions_passes_authorization_header(chat_client: TestClient) -> None:
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    with patch("deepseek_infra.web.routes.chat.openai_to_internal_payload", return_value={"model": "deepseek-chat", "stream": False}), \
         patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_chat_to_rust", return_value=SimpleNamespace(ok=True, body={"result": "ok"})) as proxy:
        resp = chat_client.post("/v1/chat/completions", json=payload, headers={"Authorization": "Bearer token"})
    assert resp.status_code == 200
    proxy.assert_called_once()
    assert proxy.call_args.kwargs["headers"] == {"Authorization": "Bearer token"}


def test_v1_models_passes_authorization_header(chat_client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_models_to_rust", return_value=SimpleNamespace(ok=True, body={"object": "list", "data": []})) as proxy:
        resp = chat_client.get("/v1/models", headers={"Authorization": "Bearer token"})
    assert resp.status_code == 200
    proxy.assert_called_once()
    assert proxy.call_args.kwargs["headers"] == {"Authorization": "Bearer token"}


def test_v1_models_rust_gateway_ok_with_empty_auth_headers(chat_client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_models_to_rust", return_value=SimpleNamespace(ok=True, body={"object": "list", "data": []})) as proxy, \
         patch("deepseek_infra.web.routes.chat.openai_models_list") as fallback:
        resp = chat_client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json() == {"object": "list", "data": []}
    proxy.assert_called_once()
    assert proxy.call_args.kwargs["headers"] == {}
    fallback.assert_not_called()


def test_v1_models_rust_gateway_unavailable_without_fallback_raises(chat_client: TestClient) -> None:
    with patch("deepseek_infra.web.routes.chat.rust_gateway_enabled", return_value=True), \
         patch("deepseek_infra.web.routes.chat.proxy_models_to_rust", return_value=SimpleNamespace(ok=False, body="rust down")), \
         patch("deepseek_infra.web.routes.chat.fallback_to_python_enabled", return_value=False), \
         patch("deepseek_infra.web.routes.chat.openai_models_list") as fallback:
        resp = chat_client.get("/v1/models")
    assert resp.status_code == 502
    fallback.assert_not_called()
