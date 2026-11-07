"""Offline cross-check of the native public-auth algorithm against the oracle.

This is a migration-time verification model, not production code. It mirrors the
Rust `require_production_auth` middleware in
`rust/crates/deepseek-gateway/src/auth.rs` (Host allow-list first, then the token,
flat refusal bodies) and compares it with a fresh Python application under the
identical environment. The Rust integration test `tests/auth_semantics.rs` runs
the same comparison in the cloud; this script makes the design checkable without
a native compiler.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TOKEN = "auth-semantics-route-token"
CASES: list[dict[str, Any]] = [
    {"name": "status-no-token", "method": "GET", "path": "/api/rust/status", "host": "127.0.0.1", "token": False},
    {"name": "media-no-token", "method": "GET", "path": "/api/media", "host": "127.0.0.1", "token": False},
    {"name": "skills-no-token", "method": "GET", "path": "/api/skills", "host": "127.0.0.1", "token": False},
    {"name": "config-no-token", "method": "GET", "path": "/api/config", "host": "127.0.0.1", "token": False},
    {"name": "backup-policies-no-token", "method": "GET", "path": "/api/workspace/backup-policies", "host": "127.0.0.1", "token": False},
    {"name": "traces-no-token", "method": "GET", "path": "/api/traces", "host": "127.0.0.1", "token": False},
    {"name": "semantic-cache-no-token", "method": "GET", "path": "/api/semantic-cache/status", "host": "127.0.0.1", "token": False},
    {"name": "wrong-method-no-token", "method": "POST", "path": "/api/rust/status", "host": "127.0.0.1", "token": False},
    {"name": "mcp-no-token", "method": "POST", "path": "/mcp", "host": "127.0.0.1", "token": False},
    {"name": "a2a-no-token", "method": "POST", "path": "/a2a", "host": "127.0.0.1", "token": False},
    {"name": "v1-no-token", "method": "POST", "path": "/v1/chat/completions", "host": "127.0.0.1", "token": False},
    {"name": "localhost-host", "method": "GET", "path": "/api/rust/status", "host": "localhost", "token": False},
    {"name": "host-with-port", "method": "GET", "path": "/api/rust/status", "host": "127.0.0.1:9999", "token": False},
    {"name": "foreign-host-no-token", "method": "GET", "path": "/api/rust/status", "host": "evil.invalid", "token": False},
    {"name": "foreign-host-valid-token", "method": "GET", "path": "/api/rust/status", "host": "evil.invalid", "token": True},
    {"name": "foreign-host-media", "method": "GET", "path": "/api/media", "host": "evil.invalid", "token": True},
    {"name": "foreign-host-mcp", "method": "POST", "path": "/mcp", "host": "evil.invalid", "token": True},
    {"name": "local-ip-host", "method": "GET", "path": "/api/rust/status", "host": "__LOCAL_IP__", "token": False},
    {"name": "valid-token-status", "method": "GET", "path": "/api/rust/status", "host": "127.0.0.1", "token": True},
    {"name": "valid-token-config", "method": "GET", "path": "/api/config", "host": "127.0.0.1", "token": True},
]
REFUSALS = {
    "unauthorized": {"error": "Auth required", "code": "unauthorized"},
    "forbidden": {"error": "Host not allowed", "code": "forbidden"},
}
ORACLE = r"""
import json, sys
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app
from deepseek_infra.core.utils import local_ip
data = json.load(sys.stdin)
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
local = local_ip()
results = []
for case in data["cases"]:
    host = local if case["host"] == "__LOCAL_IP__" else case["host"]
    headers = {"Host": host}
    if case["token"]:
        headers["Authorization"] = "Bearer " + data["token"]
    try:
        response = client.request(case["method"], case["path"], headers=headers)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__})
client.close()
print(json.dumps({"localIp": local, "results": results}))
"""


def python_strip(value: str) -> str:
    return value.strip()


def host_without_port(value: str) -> str:
    host = python_strip(value).split(",", 1)[0].strip()
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[").lower()
    return host.split(":", 1)[0].lower()


def env_truthy(name: str) -> bool:
    return python_strip(os.environ.get(name, "")).lower() in {"1", "true", "yes", "on"}


def requires_auth(path: str) -> bool:
    return (
        path.startswith("/api/")
        or path.startswith("/v1/")
        or path == "/mcp"
        or path.startswith("/a2a/")
        or path == "/a2a"
    )


def local_ip() -> str:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidate = sock.getsockname()[0]
            if is_lan_ip(candidate):
                return candidate
    except OSError:
        pass
    return "127.0.0.1"


def is_lan_ip(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4 or not all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        return False
    first, second, last = int(parts[0]), int(parts[1]), int(parts[3])
    if first == 127 or first == 0:
        return False
    if first == 169 and second == 254:
        return False
    return last not in {0, 255}


def allowed_auth_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}
    configured = python_strip(os.environ.get("HOST", "")).lower()
    if configured and configured != "0.0.0.0":
        hosts.add(configured)
    hosts.add(local_ip())
    for host in os.environ.get("AUTH_ALLOWED_HOSTS", "").split(","):
        host = python_strip(host).lower()
        if host:
            hosts.add(host)
    return hosts


def native_model(case: dict[str, Any], local: str) -> dict[str, Any]:
    if env_truthy("AUTH_DISABLED"):
        return {"status": 200, "body": "AUTH_DISABLED"}
    if not requires_auth(case["path"]):
        return {"status": 200, "body": "NO_AUTH_REQUIRED"}
    host = local if case["host"] == "__LOCAL_IP__" else case["host"]
    if host_without_port(host) not in allowed_auth_hosts():
        return {"status": 403, "body": REFUSALS["forbidden"]}
    if not case["token"]:
        return {"status": 401, "body": REFUSALS["unauthorized"]}
    return {"status": 200, "body": "AUTHORIZED"}


def run_oracle(cases: list[dict[str, Any]]) -> dict[str, Any]:
    root = tempfile.mkdtemp()
    env = dict(os.environ)
    env.update(
        {
            "DEEPSEEK_INFRA_ROOT": root,
            "AUTH_TOKEN": TOKEN,
            "AUTH_DISABLED": "",
            "AUTH_ALLOWED_HOSTS": "",
            "HOST": "",
            "DEEPSEEK_RUNTIME_MODE": "",
            "GO_CONTROL_ADDR": "",
            "DEEPSEEK_GO_CONTROL_URL": "",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", ORACLE],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        input=json.dumps({"token": TOKEN, "cases": cases}),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr[-2000:])
    return json.loads(result.stdout)


def main() -> int:
    expected = run_oracle(CASES)
    local = expected["localIp"]
    report: dict[str, Any] = {"status": "PASS", "localIp": local, "cases": {}}
    for case, want in zip(CASES, expected["results"], strict=True):
        model = native_model(case, local)
        wanted_body = json.loads(want["body"]) if want["body"].strip().startswith("{") else want["body"]
        if want["status"] == 200:
            matched = model["status"] == 200
        else:
            matched = model["status"] == want["status"] and model["body"] == wanted_body
        report["cases"][case["name"]] = {
            "matched": matched,
            "oracle": {"status": want["status"], "body": wanted_body},
            "model": model,
        }
        if not matched:
            report["status"] = "FAIL"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
