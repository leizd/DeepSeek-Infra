"""Native gateway SVG download inline synonyms.

``store_generated_file`` writes the SVG. ``GET /api/download`` treats
``inline=yes`` and ``inline=on`` as inline, and ``inline=0`` as an
attachment. Python ``store_generated_file`` is not the owner of the HTTP
read.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from deepseek_infra.infra.tool_runtime import generated_files

ROOT = Path(__file__).resolve().parents[1]
# The native gateway is a prebuilt Rust binary. A developer checkout and the
# Linux CI runner do not share a path, so resolve it from an explicit override,
# then the conventional build outputs, and skip the module when none exists. A
# missing binary must never be reported as a product failure.
GATEWAY_BINARY_ENV = "DEEPSEEK_GATEWAY_BINARY"
GATEWAY_BINARY_CANDIDATES = (
    ROOT / "bin" / "deepseek-gateway",
    ROOT / "bin" / "deepseek-gateway.exe",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway.exe",
)


def _resolve_native_gateway() -> Path | None:
    override = os.environ.get(GATEWAY_BINARY_ENV, "").strip()
    candidates = (Path(override),) if override else GATEWAY_BINARY_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# Kept separate so the skip decision can test for absence while NATIVE_GATEWAY
# stays a plain Path for the call sites and the type checker.
_GATEWAY_BINARY = _resolve_native_gateway()
NATIVE_GATEWAY = _GATEWAY_BINARY or Path("deepseek-gateway-not-built")

pytestmark = pytest.mark.skipif(
    _GATEWAY_BINARY is None,
    reason=(
        "prebuilt native gateway is required; set "
        f"{GATEWAY_BINARY_ENV} or build rust/ -p deepseek-gateway --release"
    ),
)
SVG = b"<svg>DOWNLOAD-INLINE-YES</svg>"
INLINE = 'inline; filename="mindmap.svg"'
ATTACHMENT = 'attachment; filename="mindmap.svg"'


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _download(port: int, params: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/download?{urlencode(params)}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            headers = {key.lower(): value for key, value in response.headers.items()}
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = {key.lower(): value for key, value in exc.headers.items()}
        payload = exc.read()
    return status, headers, payload


def test_svg_inline_synonyms_use_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_store(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python generated file store entered")

    def _write_svg(path: Path) -> None:
        path.write_bytes(SVG)

    picture = generated_files.store_generated_file("Picture", "svg", _write_svg)
    picture_id = picture["fileId"]
    generated_dir = tmp_settings / ".generated"
    stored = generated_dir / f"{picture_id}.svg"
    assert stored.read_bytes() == SVG
    monkeypatch.setattr(generated_files, "store_generated_file", _refuse_python_store)
    before = sorted(path.name for path in generated_dir.iterdir())
    port = _free_port()
    log_path = tmp_settings / "gateway-download-inline.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env["AUTH_DISABLED"] = "1"
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env.pop("AUTH_TOKEN", None)
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        listening = False
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    listening = True
                    break
            except OSError:
                time.sleep(0.1)
        assert listening, log_path.read_text(encoding="utf-8", errors="replace")
        for inline in ("yes", "on"):
            status, headers, body = _download(port, {"id": picture_id, "inline": inline})
            assert status == 200
            assert body == SVG
            assert headers["content-type"] == "image/svg+xml"
            assert headers["content-disposition"] == INLINE
            assert headers["cache-control"] == "no-store"
        zero_status, zero_headers, zero_body = _download(
            port,
            {"id": picture_id, "inline": "0"},
        )
        assert zero_status == 200
        assert zero_body == SVG
        assert zero_headers["content-type"] == "image/svg+xml"
        assert zero_headers["content-disposition"] == ATTACHMENT
        assert zero_headers["cache-control"] == "no-store"
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    assert stored.read_bytes() == SVG
    after = sorted(path.name for path in generated_dir.iterdir())
    assert after == before
