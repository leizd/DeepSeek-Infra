"""Exercise project reads against a real native gateway process and restart.

The Python process is an offline HTTP/evidence harness. The server under test is
the supplied Rust executable in python_disabled mode with no Go API configured.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "rust/crates/deepseek-policy/tests/fixtures/workspace_projects_oracle.json"


def snapshot(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")}


def run(binary: Path, output: Path) -> dict[str, Any]:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    assert case["name"] == "full_children"
    evidence: dict[str, Any] = {
        "schema": "native-project-read-process-proof-v1", "status": "FAIL",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "dirty_worktree": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "runtime_mode": "python_disabled", "release_readiness": "NOT_READY", "checks": [], "processes": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="native-project-read-") as directory:
            parent = Path(directory)
            root = parent / "workspace"
            root.mkdir()
            for relative, value in case["files"].items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            ui = parent / "static/ui"
            ui.mkdir(parents=True)
            (ui / "index.html").write_text("<!doctype html><main>native fixture</main>", encoding="utf-8")
            token = secrets.token_hex(24)
            env = {**os.environ, "AUTH_TOKEN": token, "DEEPSEEK_RUNTIME_MODE": "python_disabled",
                   "DEEPSEEK_INFRA_ROOT": str(root), "DEEPSEEK_INFRA_STATIC_DIR": str(ui.parent),
                   "GO_CONTROL_ADDR": "", "DEEPSEEK_GO_CONTROL_URL": ""}
            before = snapshot(root)
            # Ignore ambient proxy settings: this proof must talk to this process.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def check(name: str, condition: bool) -> None:
                if not condition:
                    raise AssertionError(name)
                evidence["checks"].append({"name": name, "status": "PASS"})

            for cycle in range(2):
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
                origin = f"http://127.0.0.1:{port}"

                def request(path: str, body: dict[str, Any] | None = None, *, authenticated: bool = True) -> tuple[int, Any]:
                    headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
                    data = None
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                        data = json.dumps(body).encode()
                    req = urllib.request.Request(origin + path, data=data, headers=headers)
                    try:
                        with opener.open(req, timeout=5) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)

                log_path = output.with_name(f"{output.stem}.gateway-{cycle}.log")
                with log_path.open("wb") as log:
                    process = subprocess.Popen([str(binary)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
                    evidence["processes"].append({"cycle": cycle, "pid": process.pid, "executable": str(binary)})
                    try:
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                raise RuntimeError(f"gateway exited during startup; see {log_path}")
                            try:
                                if request("/healthz")[0] == 200:
                                    break
                            except (OSError, urllib.error.URLError):
                                pass
                            time.sleep(0.05)
                        else:
                            raise TimeoutError(f"gateway startup timed out; see {log_path}")
                        expected = case["expected"]
                        checks = [
                            ("/api/workspace/projects", {"ok": True, "projects": expected["list"]["ok"]}),
                            ("/api/workspace/projects/proj-read", {"ok": True, "project": expected["get"]["ok"]}),
                            ("/api/workspace/projects/proj-read/conversations", {"ok": True, "conversations": expected["conversations"]["ok"]}),
                            ("/api/workspace/projects/proj-read/saved-items?type=chat_snippet&tags=A",
                             {"ok": True, "savedItems": expected["saved_filtered"]["ok"]}),
                            ("/api/workspace/projects/proj-read/artifacts", {"ok": True, "artifacts": expected["artifacts"]["ok"]}),
                        ]
                        for path, payload in checks:
                            status, actual = request(path)
                            check(f"cycle_{cycle} {path}", status == 200 and actual == payload)
                        status, actual = request("/api/projects", {"action": "list"})
                        check(f"cycle_{cycle} legacy_list", status == 200 and actual == {"projects": expected["legacy_list"]["ok"]})
                        status, actual = request("/api/projects", {"action": "get", "projectId": "proj-read"})
                        check(f"cycle_{cycle} legacy_get", status == 200 and actual == {"ok": True, "project": expected["get"]["ok"]})
                        status, actual = request("/api/workspace/projects", authenticated=False)
                        check(f"cycle_{cycle} auth_required", status == 401)
                        status, actual = request("/api/projects", {"action": "delete", "id": "proj-read"})
                        check(f"cycle_{cycle} mutation_closed", status == 501 and actual.get("code") == "NATIVE_PROJECTS_MUTATIONS_NOT_READY")
                        check(f"cycle_{cycle} unchanged_workspace", snapshot(root) == before)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=10)
                check(f"cycle_{cycle} unchanged_after_process_exit", snapshot(root) == before)
            check("unchanged_binary", hashlib.sha256(binary.read_bytes()).hexdigest() == evidence["binary_sha256"])
            evidence["status"] = "PASS"
    except Exception as error:
        evidence["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/workspace-projects-read-e2e.json")
    args = parser.parse_args()
    evidence = run(args.binary.resolve(strict=True), args.output.resolve())
    print(f"Native project read process proof: {evidence['status']}; {len(evidence['checks'])} checks; {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
