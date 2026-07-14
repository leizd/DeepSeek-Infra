from __future__ import annotations

import json
import http.client
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from deepseek_infra.infra.rust_core import gateway_client, transport


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        server = self.server
        with server.connection_lock:  # type: ignore[attr-defined]
            server.connection_count += 1  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.last_path = self.path  # type: ignore[attr-defined]
        if self.path == "/http-error":
            body = b'{"error":"unavailable"}'
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/oversize":
            body = b"x" * 2048
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"ok": True, "request": {"model": "deepseek-v4-pro", "messages": []}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-DeepSeek-Rust-Processing-Us", "17")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


@contextmanager
def _server() -> Iterator[tuple[ThreadingHTTPServer, str]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveHandler)
    server.connection_lock = threading.Lock()  # type: ignore[attr-defined]
    server.connection_count = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        yield server, f"http://{host}:{port}"
    finally:
        transport.reset_persistent_clients()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _reset_transport() -> Iterator[None]:
    transport.reset_persistent_clients()
    yield
    transport.reset_persistent_clients()


def _enable(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_URL", base_url)


def test_persistent_client_reuses_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    with _server() as (server, base_url):
        _enable(monkeypatch, base_url)
        first = gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"})
        second = gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"})

        assert first.ok and second.ok
        assert first.connection_reused is False
        assert second.connection_reused is True
        assert second.rust_processing_us == 17
        assert server.connection_count == 1  # type: ignore[attr-defined]
        assert transport.transport_stats().connections_created == 1


def test_client_can_be_closed_and_recreated(monkeypatch: pytest.MonkeyPatch) -> None:
    with _server() as (server, base_url):
        _enable(monkeypatch, base_url)
        assert gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"}).ok
        transport.reset_persistent_clients()
        assert transport.transport_stats().connections_created == 0
        assert gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"}).ok
        assert server.connection_count == 2  # type: ignore[attr-defined]


def test_pid_change_recreates_client(monkeypatch: pytest.MonkeyPatch) -> None:
    with _server() as (server, base_url):
        _enable(monkeypatch, base_url)
        assert gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"}).ok
        original_pid = transport.os.getpid()
        monkeypatch.setattr(transport.os, "getpid", lambda: original_pid + 1)
        assert gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"}).ok
        assert server.connection_count == 2  # type: ignore[attr-defined]


def test_transport_rejects_credentials_in_sidecar_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, "http://user:secret@127.0.0.1:8787")
    result = gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"})
    assert result.ok is False
    assert result.error_kind == "rust_backend_unavailable"
    assert "secret" not in str(result.body)


def test_transport_rejects_invalid_scheme_and_bounds_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    request = urllib.request.Request("ftp://127.0.0.1/file", data=b"{}", method="POST")
    with pytest.raises(urllib.error.URLError, match="http or https"):
        transport.urlopen(request, timeout=0.1)

    monkeypatch.setenv("DEEPSEEK_RUST_SIDECAR_MAX_CONNECTIONS", "invalid")
    assert transport._PoolManager._max_connections() == transport.DEFAULT_MAX_CONNECTIONS
    monkeypatch.setenv("DEEPSEEK_RUST_SIDECAR_MAX_CONNECTIONS", "999")
    assert transport._PoolManager._max_connections() == 128
    monkeypatch.setenv("DEEPSEEK_RUST_SIDECAR_MAX_RESPONSE_BYTES", "invalid")
    assert transport._response_limit() == transport.DEFAULT_MAX_RESPONSE_BYTES


def test_pool_timeout_close_and_https_construction() -> None:
    pool = transport._ConnectionPool("http", "127.0.0.1", 1, 1)
    connection, _reused, _count = pool.acquire(0.1)
    with pytest.raises(TimeoutError, match="waiting"):
        pool.acquire(0.001)
    pool.release(connection, reusable=False)
    pool.close()
    with pytest.raises(OSError, match="closed"):
        pool.acquire(0.1)

    https = transport._ConnectionPool("https", "localhost", 443, 1)._new_connection(0.1)
    assert isinstance(https, http.client.HTTPSConnection)
    https.close()


def test_http_error_query_response_limit_headers_and_fork_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenHeaders:
        @staticmethod
        def get(_name: str) -> str:
            raise TypeError("bad headers")

    assert transport.response_header(object(), "missing") is None
    assert transport.response_header(type("Response", (), {"headers": _BrokenHeaders()})(), "x") is None

    with _server() as (server, base_url):
        query_request = urllib.request.Request(f"{base_url}/query?mode=safe", data=b"{}", method="POST")
        with transport.urlopen(query_request, timeout=1) as response:
            assert response.status == 200
        assert server.last_path == "/query?mode=safe"  # type: ignore[attr-defined]

        error_request = urllib.request.Request(f"{base_url}/http-error", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            transport.urlopen(error_request, timeout=1)
        assert exc_info.value.code == 503
        assert b"unavailable" in exc_info.value.read()

        monkeypatch.setenv("DEEPSEEK_RUST_SIDECAR_MAX_RESPONSE_BYTES", "1024")
        oversize_request = urllib.request.Request(f"{base_url}/oversize", data=b"{}", method="POST")
        with pytest.raises(transport.ResponseTooLargeError):
            transport.urlopen(oversize_request, timeout=1)

    old_manager = transport._manager
    transport._after_fork_child()
    assert transport._manager is not old_manager
    assert transport.transport_stats().connections_created == 0
