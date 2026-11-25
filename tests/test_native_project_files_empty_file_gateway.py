"""Native gateway empty project file.

An empty ``notes.txt`` is refused before ``project.json`` changes. Python
project admission and ``write_project`` are not the owners under test.
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
from urllib.parse import urlencode

import pytest

from deepseek_infra.infra.data import projects as project_store
from deepseek_infra.infra.native_runtime import project_file_admission

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
PROJECT_ID = "proj-demo"
BOUNDARY = "DeepseekBoundary"
TOKEN = "project-files-empty-file-token"
SEED = (
    "{\n"
    f'  "id": "{PROJECT_ID}",\n'
    '  "name": "Demo",\n'
    '  "documents": [],\n'
    "  \"skills\": {\n"
    '    "enabledPacks": [],\n'
    '    "enabledPackVersions": [],\n'
    '    "enabledSkills": [],\n'
    '    "defaultSkill": "",\n'
    '    "recentSkills": []\n'
    "  },\n"
    '  "skillRuns": [],\n'
    '  "savedItems": [],\n'
    '  "artifacts": [],\n'
    '  "createdAt": 1,\n'
    '  "updatedAt": 1\n'
    "}\n"
).encode("utf-8")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _empty_file() -> tuple[str, bytes]:
    body = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="files"; filename="notes.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        f"\r\n--{BOUNDARY}--\r\n"
    ).encode("utf-8")
    return f"multipart/form-data; boundary={BOUNDARY}", body


def test_empty_project_file_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_admit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python project file admission entered")

    def _refuse_python_write(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("python project write entered")

    monkeypatch.setattr(project_file_admission, "admit_project_file_part", _refuse_python_admit)
    monkeypatch.setattr(project_store, "write_project", _refuse_python_write)
    project_dir = tmp_settings / ".projects" / PROJECT_ID
    project_dir.mkdir(parents=True)
    project_path = project_dir / "project.json"
    project_path.write_bytes(SEED)
    files_dir = project_dir / "files"
    content_type, body = _empty_file()
    port = _free_port()
    log_path = tmp_settings / "gateway-project-files-empty-file.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env.pop("AUTH_DISABLED", None)
    env["AUTH_TOKEN"] = TOKEN
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["DEEPSEEK_RUNTIME_MODE"] = "python_disabled"
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
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/project-files?{urlencode({'projectId': PROJECT_ID})}",
            data=body,
            headers={"Content-Type": content_type, "Authorization": f"Bearer {TOKEN}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status = response.status
                payload = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = exc.read()
        parsed = json.loads(payload.decode("utf-8"))
        assert isinstance(parsed, dict)
        assert status == 400
        assert parsed["error"] == "Uploaded file is empty: notes.txt"
        assert parsed["code"] == "invalid_payload"
        assert project_path.read_bytes() == SEED
        assert not files_dir.exists()
        assert not (tmp_settings / ".local-rag").exists()
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
