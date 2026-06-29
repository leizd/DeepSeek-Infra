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


def test_file_routes_are_registered() -> None:
    app = server_module.create_app()
    paths = _collect_route_paths(app.routes)

    assert "/api/file-source" in paths
    assert "/api/file-page-image" in paths
    assert "/api/file-page-layout" in paths
    assert "/api/file-page-search" in paths


def test_file_source_auth_is_still_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/file-source?fileId=" + "a" * 32)

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_file_source_serves_original_file_with_safe_headers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "guide.txt"
        source.write_bytes(b"alpha beta")
        cached = {"name": "guide.txt", "kind": "txt", "type": "text/plain"}
        with _running_server() as server, patch.object(server_module, "cached_file_source", return_value=(cached, source)) as mocked:
            status, data, response = _request(server, "GET", "/api/file-source?fileId=" + "a" * 32, headers=_auth_headers())

    assert status == 200
    assert data == b"alpha beta"
    assert "text/plain" in (response.getheader("Content-Type") or "")
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    disposition = response.getheader("Content-Disposition") or ""
    assert disposition.startswith("inline;")
    assert "guide.txt" in disposition
    mocked.assert_called_once_with("a" * 32, project_id=None)


def test_file_page_routes_preserve_headers_and_server_patch_compatibility() -> None:
    png = b"\x89PNG\r\n\x1a\nrendered"
    layout = {"ok": True, "page": {"words": [{"text": "hello"}]}}
    search = {"ok": True, "matches": [{"page": 2, "snippet": "beta"}]}
    with (
        _running_server() as server,
        patch.object(server_module, "file_page_image", return_value=({"name": "scan.pdf"}, png, 2, 4)) as image,
        patch.object(server_module, "file_page_layout", return_value=layout) as layout_mock,
        patch.object(server_module, "file_page_search", return_value=search) as search_mock,
    ):
        image_status, image_data, image_response = _request(
            server,
            "GET",
            "/api/file-page-image?fileId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&projectId=proj_123&page=2&scale=1.4",
            headers=_auth_headers(),
        )
        layout_status, layout_data, _ = _request(
            server,
            "GET",
            "/api/file-page-layout?fileId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&projectId=proj_123&page=3",
            headers=_auth_headers(),
        )
        search_status, search_data, _ = _request(
            server,
            "GET",
            "/api/file-page-search?fileId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&projectId=proj_123&query=beta",
            headers=_auth_headers(),
        )

    assert image_status == 200
    assert image_data == png
    assert image_response.getheader("Content-Type") == "image/png"
    assert image_response.getheader("Cache-Control") == "no-store"
    assert image_response.getheader("X-Content-Type-Options") == "nosniff"
    assert image_response.getheader("X-File-Page") == "2"
    assert image_response.getheader("X-File-Page-Count") == "4"
    assert (image_response.getheader("Content-Disposition") or "").startswith("inline;")
    assert layout_status == 200
    assert json.loads(layout_data.decode("utf-8"))["page"]["words"][0]["text"] == "hello"
    assert search_status == 200
    assert json.loads(search_data.decode("utf-8"))["matches"][0]["page"] == 2
    image.assert_called_once_with("a" * 32, project_id="proj_123", page="2", scale="1.4")
    layout_mock.assert_called_once_with("a" * 32, project_id="proj_123", page="3")
    search_mock.assert_called_once_with("a" * 32, project_id="proj_123", query="beta")
