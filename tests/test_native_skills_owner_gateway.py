"""Native gateway skills-store ownership.

``POST /api/skills`` is served by the prebuilt gateway. A missing token
is the flat skills 401. While ``DEEPSEEK_RUNTIME_MODE`` is unset, a
bearer create is refused and ``.skills`` stays absent. After the mode
is ``python_disabled``, the same create persists the skill and a get
returns it. A later non-owner delete is refused and the stored bytes
stay unchanged. Python's skills router and registry writers are not
the owners of those HTTP responses.
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

from deepseek_infra.infra.skills import registry as skill_registry
from deepseek_infra.web.routes import skills as skills_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "skills-owner-token"
UNAUTHORIZED = {"error": "Auth required", "code": "unauthorized"}
NOT_OWNED = {
    "error": "The skills store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_SKILLS_WRITE_NOT_OWNED",
}
SKILL = {
    "skillId": "custom",
    "name": "学习",
    "description": "Test skill",
    "version": "1.0",
    "systemPrompt": "Explain the topic",
    "inputSchema": {"type": "object"},
    "outputSchema": {"type": "object"},
    "allowedTools": [],
    "memoryPolicy": {"scope": "none"},
    "artifactPolicy": {"types": []},
    "projectBinding": {"enabled": False},
}
CREATE = {"action": "create", "skill": SKILL}
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
    "SKILLS_DIR",
    "DEEPSEEK_SKILLS_DIR",
    "BUILTIN_SKILLS_DIR",
    "DEEPSEEK_BUILTIN_SKILLS_DIR",
    "BUILTIN_PACKS_DIR",
    "DEEPSEEK_BUILTIN_PACKS_DIR",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/skills",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
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


def _child_env(tmp_settings: Path, port: int, mode: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in CLEARED:
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if mode:
        env["DEEPSEEK_RUNTIME_MODE"] = mode
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int, label: str) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / f"gateway-skills-owner-{label}.log"
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
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
    if not listening:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
        raise AssertionError(log_path.read_text(encoding="utf-8", errors="replace"))
    return process, log_handle


def _stop(served: tuple[subprocess.Popen[bytes], Any]) -> None:
    served[0].kill()
    served[0].wait(timeout=10)
    served[1].close()


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    skills = root / ".skills"
    if skills.is_dir():
        for path in skills.rglob("*"):
            if path.is_file():
                found[path.relative_to(root).as_posix()] = path.read_bytes()
    generation = root / ".workspace-generation"
    if generation.is_file():
        found[".workspace-generation"] = generation.read_bytes()
    return found


def test_skill_create_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python skills router entered")

    def _refuse_python_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_custom_skill entered")

    def _refuse_python_delete(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python delete_skill entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(skills_routes, "create_skills_router", _refuse_python_router)
    monkeypatch.setattr(skill_registry, "create_custom_skill", _refuse_python_create)
    monkeypatch.setattr(skill_registry, "delete_skill", _refuse_python_delete)
    skill_file = tmp_settings / ".skills" / "custom" / "custom.json"

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _post(denied_port, CREATE)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert not (tmp_settings / ".skills").exists()

        status, payload = _post(denied_port, CREATE, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert not (tmp_settings / ".skills").exists()
        assert _snapshot(tmp_settings) == {}
    finally:
        _stop(denied)

    owner_port = _free_port()
    owner = _serve(
        tmp_settings,
        _child_env(tmp_settings, owner_port, "python_disabled"),
        owner_port,
        "owner",
    )
    try:
        status, payload = _post(owner_port, CREATE, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        created = payload["skill"]
        assert created["skillId"] == "custom"
        assert created["name"] == "学习"
        assert created["version"] == "1.0"
        assert created["systemPrompt"] == "Explain the topic"
        assert created["securityReview"]["reviewStatus"] == "local-custom"
        assert skill_file.is_file()
        stored = skill_file.read_bytes()
        assert b"Explain the topic" in stored
        assert b"custom" in stored

        listed_status, listed = _post(owner_port, {"action": "get", "id": "custom"}, TOKEN)
        assert listed_status == 200
        assert listed["ok"] is True
        fetched = listed["skill"]
        assert fetched["skillId"] == created["skillId"]
        assert fetched["name"] == created["name"]
        assert fetched["version"] == created["version"]
        assert fetched["systemPrompt"] == created["systemPrompt"]
    finally:
        _stop(owner)

    stored_after_create = _snapshot(tmp_settings)
    assert ".skills/custom/custom.json" in stored_after_create
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _post(refused_port, {"action": "delete", "id": "custom"}, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert _snapshot(tmp_settings) == stored_after_create
    finally:
        _stop(refused)
