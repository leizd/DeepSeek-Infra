"""Native gateway binary vector ranking.

``POST /rag/vectors/rank-binary`` returns the versioned 24-byte response.
A bad magic is refused, and a JSON content type is refused. Python
``rank_vectors`` is not the owner of those bytes.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.rust_core import rag_client

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
REQUEST_MAGIC = b"DSVRNK01"
RESPONSE_MAGIC = b"DSVRSP01"
CONTENT_TYPE = "application/vnd.deepseek.vector-rank.v1+octet-stream"
NO_MATCH = 0xFFFFFFFF


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _encode(query: list[float], candidates: list[list[float]]) -> bytes:
    scalars = list(query)
    for candidate in candidates:
        scalars.extend(candidate)
    return REQUEST_MAGIC + struct.pack("<II", len(query), len(candidates)) + struct.pack(
        "<" + ("d" * len(scalars)),
        *scalars,
    )


def _post(
    port: int,
    payload: bytes,
    content_type: str,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/rag/vectors/rank-binary",
        data=payload,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            headers = {key.lower(): value for key, value in response.headers.items()}
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = {key.lower(): value for key, value in exc.headers.items()}
        raw = exc.read()
    return status, headers, raw


def _response(raw: bytes) -> tuple[bytes, int, int, float]:
    assert len(raw) == 24
    magic, index, pad, similarity = struct.unpack("<8sIId", raw)
    assert isinstance(magic, bytes)
    return magic, index, pad, similarity


def test_rag_vector_binary_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_rank(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        raise AssertionError("python vector rank entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(rag_client, "rank_vectors", _refuse_python_rank)
    port = _free_port()
    log_path = tmp_settings / "gateway-rag-vector-binary.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env.pop("AUTH_DISABLED", None)
    env["AUTH_TOKEN"] = "rag-vector-binary-token"
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
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
        status, headers, raw = _post(
            port,
            _encode([1.0, 0.0], [[0.5, 0.0], [1.0, 0.0], [1.0, 0.0]]),
            CONTENT_TYPE,
        )
        assert status == 200
        assert headers["content-type"] == CONTENT_TYPE
        magic, index, pad, similarity = _response(raw)
        assert magic == RESPONSE_MAGIC
        assert index == 1
        assert pad == 0
        assert similarity == 1.0
        none_status, none_headers, none_raw = _post(
            port,
            _encode([1.0, 0.0], [[0.0, 1.0]]),
            CONTENT_TYPE,
        )
        assert none_status == 200
        assert none_headers["content-type"] == CONTENT_TYPE
        none_magic, none_index, none_pad, none_similarity = _response(none_raw)
        assert none_magic == RESPONSE_MAGIC
        assert none_index == NO_MATCH
        assert none_pad == 0
        assert none_similarity == 0.0
        magic_status, _magic_headers, magic_raw = _post(port, b"X" * 16, CONTENT_TYPE)
        magic_body = json.loads(magic_raw.decode("utf-8"))
        assert magic_status == 400
        assert magic_body == {
            "ok": False,
            "code": "invalid_binary_magic",
            "message": "binary vector request magic is invalid",
        }
        type_status, _type_headers, type_raw = _post(
            port,
            _encode([1.0, 0.0], [[1.0, 0.0]]),
            "application/json",
        )
        type_body = json.loads(type_raw.decode("utf-8"))
        assert type_status == 415
        assert type_body == {
            "ok": False,
            "code": "invalid_content_type",
            "message": "binary vector ranking requires the versioned binary content type",
        }
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
