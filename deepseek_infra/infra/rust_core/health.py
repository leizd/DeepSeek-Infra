"""Health checks for Rust-backed components."""

from __future__ import annotations

import http.client
from urllib.parse import urlsplit


def check_rust_gateway_health(url: str, timeout: float = 2.0) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        conn: http.client.HTTPConnection
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            return response.status == 200
        finally:
            conn.close()
    except Exception:
        return False
