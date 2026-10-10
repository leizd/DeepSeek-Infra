"""Smoke tests for the programmatic server start/stop API used by the GUI."""

from __future__ import annotations

import socket
import time
import urllib.request

import pytest

from deepseek_infra.app import prepare_and_start, shutdown_handle


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.integration
def test_prepare_and_start_serves_then_shuts_down() -> None:
    port = _free_port()
    handle = prepare_and_start(host="127.0.0.1", port=port, serve=True)
    try:
        assert handle.port == port
        assert handle.computer_url.startswith("http://127.0.0.1:")
        assert "token=" in handle.computer_url

        # The server runs in a background thread; give it a moment to bind.
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(handle.computer_url, timeout=1.0) as resp:
                    assert resp.status in {200, 302}
                    break
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        else:
            pytest.fail(f"server never accepted requests: {last_error!r}")
    finally:
        shutdown_handle(handle)
    assert handle.stop_cache_cleanup.is_set()
