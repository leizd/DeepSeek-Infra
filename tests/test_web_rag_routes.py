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


def test_rag_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/rag/reindex" in paths
    assert "/api/rag/verify-citation" in paths
    assert "/api/rag/eval" in paths


# --- auth enforcement ---


def test_rag_reindex_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/rag/reindex", body=b'{"action":"reindex"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_rag_verify_citation_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/rag/verify-citation", body=b'{"itemId":"x"}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_rag_eval_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "POST", "/api/rag/eval", body=b'{"cases":[]}')

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


# --- valid payloads ---


def test_rag_reindex_rebuild_invokes_backend() -> None:
    with _running_server() as server, patch.object(server_module, "rebuild_local_rag_index", return_value={"ok": True}) as rebuild:
        status, data, _ = _request(
            server, "POST", "/api/rag/reindex",
            body=b'{"action":"rebuild"}',
            headers=_auth_headers(),
        )

    assert status == 200
    assert json.loads(data.decode("utf-8")) == {"ok": True}
    rebuild.assert_called_once()


def test_rag_verify_citation_invokes_backend() -> None:
    citation = {"present": True, "sourceFile": "note.txt"}
    with _running_server() as server, patch.object(server_module, "verify_local_rag_citation", return_value=citation) as verify:
        status, data, _ = _request(
            server, "POST", "/api/rag/verify-citation",
            body=b'{"itemId":"item_a","snippet":"hello"}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["citation"] == citation
    verify.assert_called_once_with("item_a", "hello")


def test_rag_eval_invokes_backend() -> None:
    eval_result = {"ok": True, "recall": 0.85}
    with _running_server() as server, patch.object(server_module, "evaluate_local_rag_recall", return_value=eval_result) as evaluate:
        status, data, _ = _request(
            server, "POST", "/api/rag/eval",
            body=b'{"cases":[{"query":"test"}],"k":3}',
            headers=_auth_headers(),
        )

    assert status == 200
    result = json.loads(data.decode("utf-8"))
    assert result["ok"] is True
    assert result["eval"] == eval_result
    evaluate.assert_called_once()


# --- invalid payloads ---


def test_rag_reindex_rejects_unknown_action() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/rag/reindex",
            body=b'{"action":"nope"}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


def test_rag_verify_citation_rejects_missing_item_id() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/rag/verify-citation",
            body=b'{"itemId":"","snippet":"x"}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


def test_rag_eval_rejects_non_list_cases() -> None:
    with _running_server() as server:
        status, data, _ = _request(
            server, "POST", "/api/rag/eval",
            body=b'{"cases":"not-a-list"}',
            headers=_auth_headers(),
        )

    payload = json.loads(data.decode("utf-8"))
    assert status == 400
    assert payload["code"] == ErrorCode.INVALID_PAYLOAD.value


# --- server_module patch compatibility ---


def test_rag_routes_server_patch_compatibility() -> None:
    assert callable(server_module.create_app)
    assert callable(server_module.create_server)
    assert callable(server_module._rag_route_deps)
    assert hasattr(server_module, "rebuild_local_rag_index")
    assert hasattr(server_module, "verify_local_rag_citation")
    assert hasattr(server_module, "evaluate_local_rag_recall")
