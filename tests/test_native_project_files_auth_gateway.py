"""Native gateway project-file authentication.

With auth enabled and Python de-authorised, a request without a token is
refused and leaves ``project.json`` unchanged. The same upload with the
configured bearer token is stored. Python project admission and
``write_project`` are not the owners under test.
"""

from __future__ import annotations

import json
import os
import re
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
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
PROJECT_ID = "proj-demo"
BOUNDARY = "DeepseekBoundary"
TOKEN = "project-files-auth-token"
FILE_ID = re.compile(r"[0-9a-f]{32}")
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


def _multipart(filename: str, content_type: str, data: bytes) -> tuple[str, bytes]:
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={BOUNDARY}", head + data + tail


def _post(
    port: int,
    query: str,
    content_type: str,
    body: bytes,
    token: str = "",
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/project-files?{query}",
        data=body,
        headers=headers,
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
    return status, parsed


def test_project_file_auth_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_admit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python project file admission entered")

    def _refuse_python_write(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("python project write entered")

    monkeypatch.setattr(project_file_admission, "admit_project_file_part", _refuse_python_admit)
    monkeypatch.setattr(project_store, "write_project", _refuse_python_write)
    assert NATIVE_GATEWAY.is_file()
    project_dir = tmp_settings / ".projects" / PROJECT_ID
    project_dir.mkdir(parents=True)
    project_path = project_dir / "project.json"
    project_path.write_bytes(SEED)
    files_dir = project_dir / "files"
    payload = b"hello project"
    content_type, body = _multipart("notes.txt", "text/plain", payload)
    query = urlencode({"projectId": PROJECT_ID})
    port = _free_port()
    log_path = tmp_settings / "gateway-project-files-auth.log"
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
        denied_status, denied = _post(port, query, content_type, body)
        assert denied_status == 401
        assert denied["error"]["code"] == "UNAUTHORIZED"
        assert denied["error"]["message"] == "Auth required"
        assert project_path.read_bytes() == SEED
        assert not files_dir.exists()
        allowed_status, allowed = _post(port, query, content_type, body, TOKEN)
        assert allowed_status == 200
        assert allowed["ok"] is True
        document = allowed["documents"][0]
        assert document["name"] == "notes.txt"
        assert document["preview"] == "hello project"
        assert document["kind"] == "txt"
        file_id = document["fileId"]
        assert isinstance(file_id, str)
        assert FILE_ID.fullmatch(file_id)
        assert (files_dir / f"{file_id}.source").read_bytes() == payload
        stored = json.loads(project_path.read_text(encoding="utf-8"))
        assert stored["documents"][0]["fileId"] == file_id
        assert stored["createdAt"] == 1
        assert int(stored["updatedAt"]) > 1
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
