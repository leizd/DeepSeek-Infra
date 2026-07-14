from __future__ import annotations

import json
import threading
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
