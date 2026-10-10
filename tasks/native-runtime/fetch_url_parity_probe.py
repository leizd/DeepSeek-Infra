"""fetch_url parity probe, Python side.

Pins `resolve_public_url`, the DNS-time SSRF check, HTML extraction, the cache,
redirect revalidation and the success envelope of `fetch_url` against
`deepseek_policy::fetch_url`.

DNS and HTTP are stubbed the same way `tests/test_tools.py` stubs them: a public
host resolves to `93.184.216.34`, and `public_http_connection` returns a scripted
response. The probe never opens a real socket.

Usage::

    python tasks/native-runtime/fetch_url_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example fetch_url_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = tempfile.mkdtemp(prefix="fetch-url-parity-")
os.environ["DEEPSEEK_INFRA_ROOT"] = ROOT

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.rag.files import extract_html_text  # noqa: E402
from deepseek_infra.infra.tool_runtime import tools  # noqa: E402

PUBLIC_IP = "93.184.216.34"
NOW = 1_700_000_000.0
HTML = (
    b"<html><head><title>Doc</title><script>secret()</script></head>"
    b"<body><main><h1>Hello</h1><p>Readable page text.</p></main></body></html>"
)

RESOLVE_CASES = [
    ("https-path-query-fragment", "https://example.com/path?x=1#fragment"),
    ("explicit-default-port", "https://example.com:443/x"),
    ("http-default-port", "http://example.com/x"),
    ("explicit-http-port", "http://example.com:8080/x"),
    ("loopback", "http://127.0.0.1:8000/"),
    ("localhost", "http://localhost:8000"),
    ("local-suffix", "http://printer.local/"),
    ("ftp", "ftp://example.com"),
    ("credentials", "https://user:pass@example.com"),
    ("empty", ""),
    ("blank", "   "),
    ("no-host", "https://"),
    ("bad-port", "https://example.com:abc/"),
    ("metadata-ip", "http://169.254.169.254/"),
]


_real_resolve = tools.resolve_public_host


def resolve_host(host: str, port: int | None) -> list[str]:
    if host == "example.com":
        return [PUBLIC_IP]
    return _real_resolve(host, port)


def error_view(exc: AppError) -> dict[str, Any]:
    return {"error": str(exc), "code": exc.code.value, "status": exc.status}


def target_view(target: tools.PublicUrlTarget) -> dict[str, Any]:
    return {
        "url": target.url,
        "scheme": target.scheme,
        "host": target.host,
        "port": target.port,
        "hostHeader": target.host_header,
        "requestTarget": target.request_target,
        "address": target.address,
    }


class FakeResponse:
    def __init__(self, data: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}

    def getheader(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        if size >= 0:
            return self.data[:size]
        return self.data


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.target: tools.PublicUrlTarget | None = None

    def request(self, *args: object, **kwargs: object) -> None:
        return None

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        return None


def main() -> int:
    out: dict[str, Any] = {}

    with patch.object(tools, "resolve_public_host", side_effect=resolve_host):
        for label, url in RESOLVE_CASES:
            try:
                out[f"resolve::{label}"] = target_view(tools.resolve_public_url(url))
            except AppError as exc:
                out[f"resolve::{label}"] = error_view(exc)

        tools.ensure_public_address(PUBLIC_IP)
        out["address::public"] = "ok"
        for label, address in [
            ("loopback", "127.0.0.1"),
            ("metadata", "169.254.169.254"),
            ("cgnat", "100.64.0.1"),
            ("mapped-loopback", "::ffff:127.0.0.1"),
        ]:
            try:
                tools.ensure_public_address(address)
                out[f"address::{label}"] = "ok"
            except AppError as exc:
                out[f"address::{label}"] = error_view(exc)

        out["extract::html"] = extract_html_text(HTML)
        out["extract::plain"] = tools.extract_readable_text(b"just text\n\n\nmore", "text/plain")
        out["normalize"] = tools.normalize_text("  a   b\n\n\n\nc  ")

        connections: list[FakeConnection] = []

        def fake_ok(target: tools.PublicUrlTarget, timeout: float) -> FakeConnection:
            connection = FakeConnection(FakeResponse(HTML))
            connection.target = target
            connections.append(connection)
            return connection

        with patch.object(tools, "time") as time_module:
            time_module.time.return_value = NOW
            with patch.object(tools, "public_http_connection", side_effect=fake_ok):
                first = tools.fetch_url("https://example.com/path?x=1#fragment")
                second = tools.fetch_url("https://example.com/path?x=1#fragment")
        out["fetch::first"] = first
        out["fetch::second"] = second
        out["fetch::http-hits"] = len(connections)
        if connections:
            out["fetch::pinned-address"] = connections[0].target.address if connections[0].target else None
            out["fetch::host-header"] = connections[0].target.host_header if connections[0].target else None

        def fake_redirect(target: tools.PublicUrlTarget, timeout: float) -> FakeConnection:
            return FakeConnection(FakeResponse(b"", status=302, headers={"Location": "http://127.0.0.1/admin"}))

        with patch.object(tools, "public_http_connection", side_effect=fake_redirect):
            try:
                tools.fetch_url("https://example.com/path")
                out["fetch::redirect"] = "ok"
            except AppError as exc:
                out["fetch::redirect"] = error_view(exc)

        huge = b"x" * (tools.MAX_FETCH_BYTES + 10)

        def fake_huge(target: tools.PublicUrlTarget, timeout: float) -> FakeConnection:
            return FakeConnection(FakeResponse(huge, headers={"Content-Type": "text/html"}))

        with patch.object(tools, "public_http_connection", side_effect=fake_huge):
            try:
                tools.fetch_url("https://example.com/huge")
                out["fetch::too-large"] = "ok"
            except AppError as exc:
                out["fetch::too-large"] = error_view(exc)

        def fake_503(target: tools.PublicUrlTarget, timeout: float) -> FakeConnection:
            return FakeConnection(FakeResponse(b"", status=503))

        with patch.object(tools, "public_http_connection", side_effect=fake_503):
            try:
                tools.fetch_url("https://example.com/down")
                out["fetch::http-503"] = "ok"
            except AppError as exc:
                out["fetch::http-503"] = error_view(exc)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
