from __future__ import annotations

import contextlib
import http.client
import json
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
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
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, http.client.HTTPResponse]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, data, response
    finally:
        connection.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {server_module.settings.auth.token}"}


def test_download_route_is_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/download" in paths


def test_download_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/download?id=" + "a" * 32)

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_download_serves_generated_file_with_safe_headers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        deck = Path(tmp) / "deck.pptx"
        deck.write_bytes(b"pptx-bytes")
        with _running_server() as server, patch.object(server_module, "resolve_generated_file", return_value=deck) as resolve_file:
            status, data, response = _request(server, "GET", "/api/download?id=" + "a" * 32, headers=_auth_headers())

    assert status == 200
    assert data == b"pptx-bytes"
    assert response.getheader("Content-Type") == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    disposition = response.getheader("Content-Disposition") or ""
    assert disposition.startswith("attachment;")
    assert "presentation.pptx" in disposition
    resolve_file.assert_called_once_with("a" * 32)


def test_download_serves_svg_inline_preview_with_safe_headers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mindmap = Path(tmp) / "map.svg"
        mindmap.write_text("<svg></svg>", encoding="utf-8")
        with _running_server() as server, patch.object(server_module, "resolve_generated_file", return_value=mindmap):
            status, data, response = _request(server, "GET", "/api/download?id=" + "b" * 32 + "&inline=1", headers=_auth_headers())

    assert status == 200
    assert data == b"<svg></svg>"
    assert response.getheader("Content-Type") == "image/svg+xml"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    disposition = response.getheader("Content-Disposition") or ""
    assert disposition.startswith("inline;")
    assert "mindmap.svg" in disposition
