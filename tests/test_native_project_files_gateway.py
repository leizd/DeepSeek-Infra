"""Native gateway project-file attach.

A non-owner mode refuses before ``project.json`` changes. ``python_disabled``
stores the upload and the workspace read returns it. Python project admission
and ``write_project`` are not the owners under test.
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
FILE_ID = re.compile(r"[0-9a-f]{32}")
NOT_OWNED = (
    "The project metadata store is still written by the Python runtime, "
    "so this gateway refuses to mutate it."
)
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


def _post(port: int, query: str, content_type: str, body: bytes) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/project-files?{query}",
        data=body,
        headers={"Content-Type": content_type},
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


def _get_json(port: int, path: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
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


def _post_json(port: int, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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


def _source(port: int, file_id: str) -> tuple[int, bytes]:
    query = urlencode({"fileId": file_id, "projectId": PROJECT_ID})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-source?{query}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _start(root: Path, mode: str, port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_handle = (root / f"gateway-project-files-{mode or 'empty'}.log").open("wb")
    env = os.environ.copy()
    env["AUTH_DISABLED"] = "1"
    env["DEEPSEEK_INFRA_ROOT"] = str(root)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["DEEPSEEK_RUNTIME_MODE"] = mode
    env.pop("AUTH_TOKEN", None)
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process, log_handle
        except OSError:
            time.sleep(0.1)
    process.kill()
    process.wait(timeout=10)
    log_handle.close()
    raise AssertionError((root / f"gateway-project-files-{mode or 'empty'}.log").read_text(encoding="utf-8", errors="replace"))


def _stop(process: subprocess.Popen[bytes], log_handle: Any) -> None:
    process.kill()
    process.wait(timeout=10)
    log_handle.close()


def test_project_file_attach_uses_the_native_gateway(
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
    refused_type, refused_body = _multipart("notes.txt", "text/plain", b"secret")
    query = urlencode({"projectId": PROJECT_ID})
    refused_port = _free_port()
    refused_process, refused_log = _start(tmp_settings, "python_authoritative", refused_port)
    try:
        status, refused = _post(refused_port, query, refused_type, refused_body)
        assert status == 409
        assert refused["error"] == NOT_OWNED
        assert refused["code"] == "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED"
        assert project_path.read_bytes() == SEED
        assert not files_dir.exists()
        assert b"secret" not in project_path.read_bytes()
        assert not (tmp_settings / ".local-rag").exists()
    finally:
        _stop(refused_process, refused_log)
    payload = b"hello project"
    content_type, body = _multipart("notes.txt", "text/plain", payload)
    port = _free_port()
    process, log_handle = _start(tmp_settings, "python_disabled", port)
    try:
        status, attached = _post(port, query, content_type, body)
        assert status == 200
        assert attached["ok"] is True
        document = attached["documents"][0]
        file_id = document["fileId"]
        assert isinstance(file_id, str)
        assert FILE_ID.fullmatch(file_id)
        assert document["name"] == "notes.txt"
        assert document["projectId"] == PROJECT_ID
        assert document["kind"] == "txt"
        assert document["preview"] == "hello project"
        assert len(document["id"]) == 16
        stored = json.loads(project_path.read_text(encoding="utf-8"))
        assert stored["documents"][0]["fileId"] == file_id
        assert stored["documents"][0]["preview"] == "hello project"
        assert stored["createdAt"] == 1
        assert int(stored["updatedAt"]) > 1
        source_path = files_dir / f"{file_id}.source"
        assert source_path.read_bytes() == payload
        assert not (tmp_settings / ".local-rag").exists()
        read_status, project = _get_json(port, f"/api/workspace/projects/{PROJECT_ID}")
        assert read_status == 200
        assert project["project"]["documents"][0]["fileId"] == file_id
        assert project["project"]["files"][0]["preview"] == "hello project"
        assert project["project"]["stats"]["files"] == 1
        reader_status, reader = _post_json(
            port,
            "/api/file-reader",
            {"fileId": file_id, "projectId": PROJECT_ID, "chunkStart": 1, "chunkCount": 1},
        )
        assert reader_status == 200
        assert reader["chunks"][0]["text"] == "hello project"
        source_status, source_body = _source(port, file_id)
        assert source_status == 200
        assert source_body == payload
        assert project_path.read_bytes() != SEED
    finally:
        _stop(process, log_handle)
