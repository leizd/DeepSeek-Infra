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


def test_a2a_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/.well-known/agent-card.json" in paths
    assert "/a2a/agents" in paths
    assert "/a2a" in paths
    assert "/a2a/agents/{agent_id}" in paths


# --- auth enforcement ---


def test_a2a_agents_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/a2a/agents")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_a2a_endpoint_auth_is_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/a2a", body=b'{"jsonrpc":"2.0","id":1,"method":"message/send"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- well-known agent card (unauthenticated) ---


def test_well_known_agent_card_is_unauthenticated() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/.well-known/agent-card.json")

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["protocolVersion"] is not None


# --- A2A non-streaming ---


def test_a2a_non_streaming_invokes_handler() -> None:
    response = {"jsonrpc": "2.0", "result": {"kind": "task", "id": "t1"}, "id": 1}
    with _running_server() as server, patch.object(server_module, "handle_a2a_message", return_value=response):
        status, data, _ = _request(
            server, "POST", "/a2a",
            body=b'{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hi"}],"messageId":"m1","kind":"message"}}}',
            headers=_auth_headers(),
        )

    assert status == 200
    assert json.loads(data.decode("utf-8")) == response


# --- A2A streaming ---


def test_a2a_streaming_returns_text_event_stream() -> None:
    def fake_stream(body, agent_id=None):
        yield b'data: {"result":{"kind":"task"}}\n\n'
        yield b'data: {"result":{"kind":"status-update","final":true}}\n\n'

    with _running_server() as server, patch.object(server_module, "is_stream_request", return_value=True), patch.object(server_module, "stream_message_events", side_effect=fake_stream):
        status, data, response = _request(
            server, "POST", "/a2a",
            body=b'{"jsonrpc":"2.0","id":1,"method":"message/stream","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hi"}],"messageId":"m1","kind":"message"}}}',
            headers=_auth_headers(),
        )

    assert status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert response.getheader("X-Accel-Buffering") == "no"
    assert response.getheader("Cache-Control") == "no-cache"
    assert b"data:" in data
    assert b"task" in data
    assert b"status-update" in data


# --- A2A agent endpoint ---


def test_a2a_agent_endpoint_routes_to_named_agent() -> None:
    response = {"jsonrpc": "2.0", "result": {"kind": "task", "id": "t2"}, "id": 1}
    with _running_server() as server, patch.object(server_module, "handle_a2a_message", return_value=response) as handler:
        status, data, _ = _request(
            server, "POST", "/a2a/agents/researcher",
            body=b'{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hi"}],"messageId":"m2","kind":"message"}}}',
            headers=_auth_headers(),
        )

    assert status == 200
    handler.assert_called_once()


# --- server_module patch compatibility ---


def test_a2a_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._a2a_route_deps)
    assert hasattr(server_module, "agent_card")
    assert hasattr(server_module, "agent_cards")
    assert hasattr(server_module, "handle_a2a_message")
