"""Native gateway vector ranking.

``POST /rag/vectors/rank`` returns the first highest positive dot product.
A tie keeps the earlier index. Orthogonal and empty candidates return no
match. Python ``rank_vectors`` is not the owner of those decisions.
"""

from __future__ import annotations

import json
import os
import socket
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/rag/vectors/rank",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    parsed = json.loads(raw.decode("utf-8"))
    assert isinstance(parsed, dict)
    return status, parsed


def test_rag_vector_rank_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_rank(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        raise AssertionError("python vector rank entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(rag_client, "rank_vectors", _refuse_python_rank)
    port = _free_port()
    log_path = tmp_settings / "gateway-rag-vector-rank.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env.pop("AUTH_DISABLED", None)
    env["AUTH_TOKEN"] = "rag-vector-rank-token"
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
        status, best = _post(
            port,
            {
                "query": [1.0, 0.0],
                "candidates": [[0.5, 0.0], [1.0, 0.0], [1.0, 0.0]],
            },
        )
        assert status == 200
        assert best == {"index": 1, "similarity": 1.0}
        tie_status, tie = _post(
            port,
            {
                "query": [1.0, 0.0],
                "candidates": [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
            },
        )
        assert tie_status == 200
        assert tie == {"index": 0, "similarity": 1.0}
        none_status, none = _post(
            port,
            {"query": [1.0, 0.0], "candidates": [[0.0, 1.0], []]},
        )
        assert none_status == 200
        assert none == {"index": None, "similarity": 0.0}
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
