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


def test_memory_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/memory" in paths
    assert "/api/memory/{memory_id}" in paths
    assert "/api/memory/conflicts" in paths


# --- auth enforcement ---


def test_memory_list_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/memory")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_memory_upsert_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/memory", body=b'{"action":"add","content":"test"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_memory_delete_by_id_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "DELETE", "/api/memory/mem_x")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_memory_conflicts_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/memory/conflicts", body=b'{"content":"test"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- valid payloads ---


def test_memory_list_returns_memories() -> None:
    with _running_server() as server, patch.object(server_module, "load_memories", return_value=[{"id": "m1", "content": "hello"}]) as load:
        status, data, _ = _request(server, "GET", "/api/memory", headers=_auth_headers())

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result == {"memories": [{"id": "m1", "content": "hello"}]}
    load.assert_called_once()


def test_memory_add_invokes_backend() -> None:
    with (
        _running_server() as server,
        patch.object(server_module, "detect_memory_conflicts", return_value=[]) as conflicts,
        patch.object(server_module, "upsert_memory", return_value={"id": "mem_new", "content": "test"}) as upsert,
    ):
        status, data, _ = _request(
            server, "POST", "/api/memory",
            body=b'{"action":"add","content":"test"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["memory"] == {"id": "mem_new", "content": "test"}
    conflicts.assert_called_once()
    upsert.assert_called_once()


def test_memory_add_reports_conflicts() -> None:
    conflict_item = {"id": "mem_old", "content": "test"}
    with (
        _running_server() as server,
        patch.object(server_module, "detect_memory_conflicts", return_value=[conflict_item]),
    ):
        status, data, _ = _request(
            server, "POST", "/api/memory",
            body=b'{"action":"add","content":"test"}',
            headers=_auth_headers(),
        )

    assert status == 409
    result = json.loads(data.decode("utf-8"))
    assert result["code"] == ErrorCode.MEMORY_CONFLICT.value
    assert result["conflicts"] == [conflict_item]


def test_memory_delete_by_id_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "delete_memory_by_id", return_value=1) as delete:
        status, data, _ = _request(
            server, "DELETE", "/api/memory/mem_x",
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["deleted"] == 1
    delete.assert_called_once_with("mem_x")


def test_memory_conflicts_invokes_backend() -> None:
    conflicts_list = [{"id": "mem_dup", "content": "dup"}]
    with _running_server() as server, patch.object(server_module, "detect_memory_conflicts", return_value=conflicts_list) as detect:
        status, data, _ = _request(
            server, "POST", "/api/memory/conflicts",
            body=b'{"content":"dup"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["conflicts"] == conflicts_list
    detect.assert_called_once()


def test_memory_action_delete_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "delete_memories_by_query", return_value=2) as delete:
        status, data, _ = _request(
            server, "POST", "/api/memory",
            body=b'{"action":"delete","query":"test"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["deleted"] == 2
    delete.assert_called_once()


def test_memory_action_clear_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "clear_memories", return_value=3) as clear:
        status, data, _ = _request(
            server, "POST", "/api/memory",
            body=b'{"action":"clear"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["deleted"] == 3
    clear.assert_called_once()


def test_memory_action_list_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "load_memories", return_value=[{"id": "m2"}]) as load:
        status, data, _ = _request(
            server, "POST", "/api/memory",
            body=b'{"action":"list"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result == {"memories": [{"id": "m2"}]}
    load.assert_called_once()


# --- invalid payload ---


def test_memory_action_rejects_unknown_action() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/memory",
            body=b'{"action":"nope"}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


# --- server_module patch compatibility ---


def test_memory_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._memory_route_deps)
    assert hasattr(server_module, "load_memories")
    assert hasattr(server_module, "upsert_memory")
    assert hasattr(server_module, "delete_memory_by_id")
    assert hasattr(server_module, "detect_memory_conflicts")
