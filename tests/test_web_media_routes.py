from __future__ import annotations

import contextlib
import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import ErrorCode
from deepseek_infra.infra.workspace import projects


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
    return {"Authorization": f"Bearer {server_module.settings.auth.token}", "Content-Type": "application/json"}


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


def test_media_routes_are_registered() -> None:
    paths = _collect_route_paths(server_module.create_app().routes)
    assert "/api/media" in paths
    assert "/api/media/{media_id}" in paths
    assert "/api/media/{media_id}/process" in paths
    assert "/api/media/{media_id}/segments" in paths


def test_media_auth_enforced() -> None:
    with _running_server() as server:
        status, data, _ = _request(server, "GET", "/api/media")

    payload = json.loads(data.decode("utf-8"))
    assert status == 401
    assert payload["code"] == ErrorCode.UNAUTHORIZED.value


def test_media_json_register_list_segments_and_delete(tmp_settings: Path) -> None:
    project = projects.create_project("Media Route Project")
    body = json.dumps(
        {
            "projectId": project["projectId"],
            "type": "webpage",
            "title": "Route Snapshot",
            "html": "<main><h1>Media API</h1><p>Segments are citable.</p></main>",
            "process": True,
        }
    ).encode("utf-8")

    with _running_server() as server:
        status, created_raw, _ = _request(server, "POST", "/api/media", body=body, headers=_auth_headers())
        assert status == 200
        created = json.loads(created_raw.decode("utf-8"))
        media_id = created["media"]["mediaId"]
        assert created["media"]["status"] == "ready"

        status, list_raw, _ = _request(server, "GET", f"/api/media?projectId={project['projectId']}", headers=_auth_headers())
        assert status == 200
        listed = json.loads(list_raw.decode("utf-8"))
        assert listed["media"][0]["mediaId"] == media_id

        status, segments_raw, _ = _request(server, "GET", f"/api/media/{media_id}/segments", headers=_auth_headers())
        assert status == 200
        segments = json.loads(segments_raw.decode("utf-8"))
        assert segments["segments"][0]["citation"]["uri"].startswith(f"media://{media_id}")

        status, deleted_raw, _ = _request(server, "DELETE", f"/api/media/{media_id}", headers=_auth_headers())
        assert status == 200
        assert json.loads(deleted_raw.decode("utf-8"))["deleted"] == 1
