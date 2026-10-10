#!/usr/bin/env python3
"""Headless MCP stdio-bridge compatibility smoke for DeepSeek Infra.

The default path starts an embedded local DeepSeek Infra server with
``AUTH_DISABLED=1``, launches a small stdio -> Streamable HTTP bridge, then runs
MCP ``initialize``, ``tools/list``, ``tools/call`` and a policy-denial probe
through that bridge. It does not require Claude Desktop, Cursor, or npm.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import SmokeFailure, join_url, jsonrpc, request_json, resolve_token, rpc_result, service_base_from_endpoint  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402

SCHEMA_VERSION = "headless-mcp-bridge-evidence.v1"
DEFAULT_EVIDENCE_PATH = REPO_ROOT / "docs" / "evidence" / "headless-mcp-bridge.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_value(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def app_version() -> str:
    from deepseek_infra.core.config import APP_VERSION

    return APP_VERSION


def build_environment() -> dict[str, Any]:
    return {
        "os": platform.system(),
        "python": platform.python_version(),
        "ci": bool(os.environ.get("CI")),
    }


def record(steps: list[dict[str, Any]], name: str, status: str, detail: str, data: dict[str, Any] | None = None) -> None:
    steps.append({"name": name, "status": status, "detail": detail, "data": data or {}})


def wait_for_healthz(base_url: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            return request_json("GET", join_url(base_url, "/healthz"), timeout_seconds=3)
        except SmokeFailure as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise SmokeFailure(f"server did not become healthy: {last_error}")


def start_embedded_server(port: int, timeout_seconds: int) -> tuple[str, Any]:
    os.environ.setdefault("AUTH_DISABLED", "1")
    os.environ.setdefault("DEFAULT_HOST", "127.0.0.1")
    from deepseek_infra.app import prepare_and_start

    handle = prepare_and_start(host="127.0.0.1", port=port, serve=True)
    base_url = f"http://127.0.0.1:{handle.port}"
    wait_for_healthz(base_url, timeout_seconds)
    return join_url(base_url, "/mcp"), handle


def shutdown_embedded_server(handle: Any | None) -> None:
    if handle is None:
        return
    from deepseek_infra.app import shutdown_handle

    shutdown_handle(handle)


def bridge_child_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal stdio -> MCP HTTP bridge child.")
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        message_id: Any = None
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("message must be a JSON object")
            message_id = message.get("id")
            response = request_json("POST", args.mcp_url, token=args.token, payload=message, timeout_seconds=args.timeout, extra_headers={"Accept": "application/json, text/event-stream"})
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32000, "message": str(exc)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def start_builtin_bridge(mcp_url: str, token: str, timeout_seconds: int) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--stdio-bridge-child",
        "--mcp-url",
        mcp_url,
        "--timeout",
        str(timeout_seconds),
    ]
    if token:
        command.extend(["--token", token])
    return subprocess.Popen(command, cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def read_bridge_line(process: subprocess.Popen[str], timeout_seconds: int) -> str:
    stdout = process.stdout
    if stdout is None:
        raise SmokeFailure("bridge stdout is not available")
    output: queue.Queue[str] = queue.Queue(maxsize=1)

    def reader() -> None:
        output.put(stdout.readline())

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        line = output.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        process.kill()
        raise SmokeFailure("stdio bridge timed out waiting for response") from exc
    if not line:
        stderr = ""
        if process.stderr is not None:
            try:
                stderr = process.stderr.read()
            except OSError:
                stderr = ""
        raise SmokeFailure(f"stdio bridge exited without a response: {stderr.strip()}")
    return line


def bridge_rpc(process: subprocess.Popen[str], method: str, params: dict[str, Any] | None, message_id: int, timeout_seconds: int) -> dict[str, Any]:
    if process.stdin is None:
        raise SmokeFailure("bridge stdin is not available")
    process.stdin.write(json.dumps(jsonrpc(method, params, message_id), ensure_ascii=False) + "\n")
    process.stdin.flush()
    line = read_bridge_line(process, timeout_seconds)
    try:
        response = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"stdio bridge returned non-JSON: {line[:300]}") from exc
    if not isinstance(response, dict):
        raise SmokeFailure("stdio bridge returned non-object JSON")
    return response


def run_smoke(mcp_url: str, *, token: str, timeout_seconds: int) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    bridge: subprocess.Popen[str] | None = None
    try:
        base_url = service_base_from_endpoint(mcp_url, "mcp")
        health = request_json("GET", join_url(base_url, "/healthz"), timeout_seconds=timeout_seconds)
        record(steps, "server.healthz", "pass", f"status={health.get('status')}", {"url": join_url(base_url, "/healthz")})

        bridge = start_builtin_bridge(mcp_url, token, timeout_seconds)
        record(steps, "bridge.start", "pass", "builtin stdio -> Streamable HTTP bridge started", {"bridge": "builtin-python"})

        init = rpc_result(bridge_rpc(bridge, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, 1, timeout_seconds), "initialize")
        server_info_value = init.get("serverInfo")
        server_info: dict[str, Any] = server_info_value if isinstance(server_info_value, dict) else {}
        record(steps, "mcp.initialize", "pass", f"protocol={init.get('protocolVersion')} server={server_info.get('name')}", {"protocolVersion": init.get("protocolVersion"), "serverInfo": server_info})

        tools_result = rpc_result(bridge_rpc(bridge, "tools/list", None, 2, timeout_seconds), "tools/list")
        tools_value = tools_result.get("tools")
        tools = [tool for tool in tools_value if isinstance(tool, dict)] if isinstance(tools_value, list) else []
        names = {str(tool.get("name") or "") for tool in tools}
        missing = {"data_transform", "fetch_url"} - names
        if missing:
            raise SmokeFailure(f"tools/list missing expected tools: {', '.join(sorted(missing))}")
        record(steps, "mcp.tools_list", "pass", f"{len(tools)} tools exposed", {"toolCount": len(tools), "requiredTools": ["data_transform", "fetch_url"]})

        call = rpc_result(
            bridge_rpc(
                bridge,
                "tools/call",
                {"name": "data_transform", "arguments": {"operation": "number_summary", "input": "1 2 3 4"}},
                3,
                timeout_seconds,
            ),
            "tools/call data_transform",
        )
        structured_value = call.get("structuredContent")
        structured: dict[str, Any] = structured_value if isinstance(structured_value, dict) else {}
        result_value = structured.get("result")
        result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
        if call.get("isError") is True or structured.get("ok") is not True:
            raise SmokeFailure("data_transform returned a tool-level error")
        record(steps, "mcp.tools_call", "pass", f"data_transform count={result.get('count')}", {"tool": "data_transform", "count": result.get("count")})

        blocked = rpc_result(
            bridge_rpc(
                bridge,
                "tools/call",
                {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/admin"}},
                4,
                timeout_seconds,
            ),
            "tools/call fetch_url policy gate",
        )
        body = json.dumps(blocked.get("structuredContent") or blocked, ensure_ascii=False).lower()
        if blocked.get("isError") is not True or not any(marker in body for marker in ("ssrf", "blocked", "forbidden", "localhost")):
            raise SmokeFailure("fetch_url localhost SSRF probe was not blocked")
        record(steps, "mcp.policy_denial", "pass", "fetch_url localhost probe blocked by Tool Policy", {"tool": "fetch_url", "probe": "http://127.0.0.1/admin"})
    except SmokeFailure as exc:
        record(steps, "headless_mcp_bridge", "fail", str(exc))
    finally:
        if bridge is not None:
            try:
                if bridge.stdin is not None:
                    bridge.stdin.close()
                bridge.terminate()
                bridge.wait(timeout=5)
            except Exception:
                bridge.kill()
    return steps


def build_evidence(steps: list[dict[str, Any]], *, mcp_url: str, auth_mode: str) -> dict[str, Any]:
    failed = [step for step in steps if step.get("status") == "fail"]
    revision = evidence_revision(REPO_ROOT)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": app_version(),
        "commit": revision["testedRevision"],
        **revision,
        "generatedAt": utc_now(),
        "environment": build_environment(),
        "gitSha": revision["testedRevision"],
        "gitDirty": revision["sourceTreeDirty"],
        "status": "FAIL" if failed else "PASS",
        "transport": {
            "server": "embedded-or-local-http",
            "bridge": "builtin-stdio-http",
            "mcpUrl": mcp_url,
            "auth": auth_mode,
        },
        "covers": ["initialize", "tools/list", "tools/call:data_transform", "policy_denial:fetch_url_ssrf", "stdio_to_http_bridge"],
        "steps": steps,
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run headless MCP stdio bridge compatibility smoke.")
    parser.add_argument("--mcp-url", default="", help="Use an already-running MCP endpoint instead of starting an embedded server.")
    parser.add_argument("--port", type=int, default=0, help="Embedded server port; 0 chooses an ephemeral port.")
    parser.add_argument("--token", default="", help="Bearer token for an already-running authenticated server.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--out", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable evidence.")
    parser.add_argument("--stdio-bridge-child", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stdio_bridge_child:
        child_args = [item for item in (argv or sys.argv[1:]) if item != "--stdio-bridge-child"]
        return bridge_child_main(child_args)

    handle: Any | None = None
    token = ""
    auth_mode = "disabled"
    try:
        if args.mcp_url:
            token = resolve_token(args.token)
            auth_mode = "bearer" if token else "none"
            mcp_url = args.mcp_url.rstrip("/") if args.mcp_url.rstrip("/").endswith("/mcp") else join_url(args.mcp_url, "/mcp")
            wait_for_healthz(service_base_from_endpoint(mcp_url, "mcp"), args.timeout)
        else:
            mcp_url, handle = start_embedded_server(args.port, args.timeout)
        steps = run_smoke(mcp_url, token=token, timeout_seconds=args.timeout)
        evidence = build_evidence(steps, mcp_url=mcp_url, auth_mode=auth_mode)
    finally:
        shutdown_embedded_server(handle)

    if not args.no_write:
        write_evidence(args.out, evidence)
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        print(f"Headless MCP bridge evidence: {evidence['status']} ({len(evidence['steps'])} steps)")
        if not args.no_write:
            print(f"Wrote {args.out}")
    return 1 if evidence["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
