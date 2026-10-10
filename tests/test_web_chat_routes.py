from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import ErrorCode


def _collect_route_paths(routes: list[Any]) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is not None:
            paths |= _collect_route_paths(getattr(original, "routes", []))
    return paths


@contextlib.contextmanager
def _running_server() -> Iterator[Any]:
    server, _ = server_module.create_server(0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: Any,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, data, response
    finally:
        connection.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {server_module.settings.auth.token}"}


# --- route registration ---


def test_chat_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    for expected in {
        "/api/chat",
        "/api/title",
        "/api/conversations/search",
        "/v1/chat/completions",
        "/v1/models",
    }:
        assert expected in paths


# --- auth enforcement ---


def test_chat_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/chat", body=b'{"messages":[]}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_title_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/title", body=b'{"messages":[]}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_conversation_search_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/conversations/search", body=b'{"query":"x"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_v1_completions_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/v1/chat/completions", body=b'{"model":"deepseek-chat"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_v1_models_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/v1/models")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- invalid payload ---


def test_chat_invalid_json_returns_error() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/chat",
            body=b"not json",
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


# --- non-streaming chat ---


def test_chat_non_streaming_invokes_cascade() -> None:
    with _running_server() as server, patch("deepseek_infra.web.routes.chat.call_deepseek_cascade", return_value={"content": "ok"}) as cascade:
        status, data, _ = _request(
            server, "POST", "/api/chat",
            body=b'{"messages":[{"role":"user","content":"hi"}]}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["content"] == "ok"
    cascade.assert_called_once()


# --- server_module patch compatibility ---


def test_title_invokes_backend() -> None:
    with _running_server() as server, patch("deepseek_infra.web.routes.chat.generate_title_payload", return_value={"title": "test"}) as gen:
        status, data, _ = _request(
            server, "POST", "/api/title",
            body=b'{"messages":[{"role":"user","content":"hi"}]}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["title"] == "test"
    gen.assert_called_once()


# --- conversation search ---


def test_conversation_search_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "conversation_search", return_value={"ok": True, "matches": []}) as search:
        status, data, _ = _request(
            server, "POST", "/api/conversations/search",
            body=b'{"conversations":[],"query":"x"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["matches"] == []
    search.assert_called_once()


# --- v1/models ---


def test_v1_models_invokes_backend() -> None:
    with _running_server() as server, patch("deepseek_infra.web.routes.chat.openai_models_list", return_value={"object": "list", "data": []}):
        status, data, _ = _request(
            server, "GET", "/v1/models",
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["object"] == "list"


# --- server_module patch compatibility ---


def test_chat_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._chat_route_deps)
    assert hasattr(server_module, "chat_event_stream")
    assert hasattr(server_module, "conversation_search")
