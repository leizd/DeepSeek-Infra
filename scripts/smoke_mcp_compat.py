#!/usr/bin/env python3
"""Compatibility smoke runner for DeepSeek Infra's MCP Tool Hub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import (  # noqa: E402
    SmokeFailure,
    StepResult,
    finish,
    join_url,
    jsonrpc,
    print_step,
    request_json,
    resolve_token,
    rpc_result,
    service_base_from_endpoint,
)


def _record(steps: list[StepResult], name: str, status: str, detail: str, data: dict[str, Any] | None = None, *, as_json: bool) -> None:
    step = StepResult(name=name, status=status, detail=detail, data=data or {})
    steps.append(step)
    print_step(step, as_json=as_json)


def _post_rpc(url: str, method: str, params: dict[str, Any] | None, *, token: str, timeout: int, message_id: int) -> dict[str, Any]:
    return request_json("POST", url, token=token, payload=jsonrpc(method, params, message_id), timeout_seconds=timeout)


def _check_local_mcp(args: argparse.Namespace, steps: list[StepResult], token: str) -> None:
    mcp_url = args.mcp_url.rstrip("/")
    service_base = service_base_from_endpoint(mcp_url, "mcp")

    try:
        health = request_json("GET", join_url(service_base, "/healthz"), timeout_seconds=args.timeout)
        _record(steps, "healthz", "pass", f"status={health.get('status')}", {"url": join_url(service_base, "/healthz")}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "healthz", "fail", str(exc), as_json=args.json)
        return

    try:
        init = rpc_result(
            _post_rpc(mcp_url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, token=token, timeout=args.timeout, message_id=1),
            "initialize",
        )
        server_info_value = init.get("serverInfo")
        server_info: dict[str, Any] = server_info_value if isinstance(server_info_value, dict) else {}
        _record(
            steps,
            "mcp.initialize",
            "pass",
            f"protocol={init.get('protocolVersion')} server={server_info.get('name')}",
            {"serverInfo": server_info, "protocolVersion": init.get("protocolVersion")},
            as_json=args.json,
        )
    except SmokeFailure as exc:
        _record(steps, "mcp.initialize", "fail", str(exc), as_json=args.json)
        return

    try:
        tools_result = rpc_result(_post_rpc(mcp_url, "tools/list", None, token=token, timeout=args.timeout, message_id=2), "tools/list")
        tools_value = tools_result.get("tools")
        tools = [tool for tool in tools_value if isinstance(tool, dict)] if isinstance(tools_value, list) else []
        names = {str(tool.get("name") or "") for tool in tools}
        missing = {"data_transform", "fetch_url"} - names
        if missing:
            raise SmokeFailure(f"tools/list missing expected tools: {', '.join(sorted(missing))}")
        _record(steps, "mcp.tools_list", "pass", f"{len(tools)} tools exposed", {"toolCount": len(tools)}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "mcp.tools_list", "fail", str(exc), as_json=args.json)
        return

    try:
        call = rpc_result(
            _post_rpc(
                mcp_url,
                "tools/call",
                {"name": "data_transform", "arguments": {"operation": "number_summary", "input": "1 2 3 4"}},
                token=token,
                timeout=args.timeout,
                message_id=3,
            ),
            "tools/call data_transform",
        )
        structured_value = call.get("structuredContent")
        structured: dict[str, Any] = structured_value if isinstance(structured_value, dict) else {}
        if call.get("isError") is True or structured.get("ok") is not True:
            raise SmokeFailure("data_transform returned a tool-level error")
        result_value = structured.get("result")
        result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
        _record(steps, "mcp.tools_call", "pass", f"data_transform count={result.get('count')}", {"structuredContent": structured}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "mcp.tools_call", "fail", str(exc), as_json=args.json)

    try:
        blocked = rpc_result(
            _post_rpc(
                mcp_url,
                "tools/call",
                {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/admin"}},
                token=token,
                timeout=args.timeout,
                message_id=4,
            ),
            "tools/call fetch_url policy gate",
        )
        if blocked.get("isError") is not True:
            raise SmokeFailure("fetch_url localhost SSRF probe was not blocked")
        body = json.dumps(blocked.get("structuredContent") or blocked, ensure_ascii=False).lower()
        status = "pass" if "ssrf" in body or "blocked" in body or "forbidden" in body else "warn"
        _record(steps, "mcp.policy_gate", status, "fetch_url localhost probe returned tool error", as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "mcp.policy_gate", "fail", str(exc), as_json=args.json)

    try:
        health_api = request_json("GET", join_url(service_base, "/api/mcp/external/tools"), token=token, timeout_seconds=args.timeout)
        servers_value = health_api.get("servers")
        health_tools_value = health_api.get("tools")
        servers: list[Any] = servers_value if isinstance(servers_value, list) else []
        health_tools: list[Any] = health_tools_value if isinstance(health_tools_value, list) else []
        _record(
            steps,
            "mcp.external_health_api",
            "pass",
            f"servers={len(servers)} bridgedTools={len(health_tools)}",
            {"servers": len(servers), "tools": len(health_tools)},
            as_json=args.json,
        )
    except SmokeFailure as exc:
        _record(steps, "mcp.external_health_api", "fail", str(exc), as_json=args.json)


def _check_external_mcp(args: argparse.Namespace, steps: list[StepResult]) -> None:
    if not args.external_server_url:
        _record(
            steps,
            "mcp.real_external_server",
            "warn",
            "skipped; pass --external-server-url to smoke a real third-party Streamable HTTP MCP server",
            as_json=args.json,
        )
        return
    headers = {}
    if args.external_bearer_token:
        headers["Authorization"] = f"Bearer {args.external_bearer_token}"
    try:
        init = rpc_result(
            request_json(
                "POST",
                args.external_server_url,
                payload=jsonrpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, 100),
                timeout_seconds=args.timeout,
                extra_headers=headers,
            ),
            "external initialize",
        )
        tools_result = rpc_result(
            request_json(
                "POST",
                args.external_server_url,
                payload=jsonrpc("tools/list", None, 101),
                timeout_seconds=args.timeout,
                extra_headers=headers,
            ),
            "external tools/list",
        )
        tools_value = tools_result.get("tools")
        tools = [tool for tool in tools_value if isinstance(tool, dict)] if isinstance(tools_value, list) else []
        server_info_value = init.get("serverInfo")
        server_info = server_info_value if isinstance(server_info_value, dict) else {}
        _record(
            steps,
            "mcp.real_external_server",
            "pass",
            f"server={server_info.get('name') or '<unknown>'} tools={len(tools)}",
            {"serverInfo": server_info, "toolCount": len(tools)},
            as_json=args.json,
        )
    except SmokeFailure as exc:
        _record(steps, "mcp.real_external_server", "fail", str(exc), as_json=args.json)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MCP compatibility smoke checks against a local DeepSeek Infra server.")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8000/mcp", help="Local MCP endpoint or service root")
    parser.add_argument("--token", default="", help="Local auth token; defaults to env or .auth-token")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--external-server-url", default="", help="Optional real third-party MCP Streamable HTTP endpoint")
    parser.add_argument("--external-bearer-token", default="", help="Optional Bearer token for --external-server-url")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = parser.parse_args(argv)

    token = resolve_token(args.token)
    if args.mcp_url.rstrip("/").endswith("/mcp"):
        args.mcp_url = args.mcp_url.rstrip("/")
    else:
        args.mcp_url = join_url(args.mcp_url, "/mcp")

    steps: list[StepResult] = []
    _check_local_mcp(args, steps, token)
    _check_external_mcp(args, steps)
    return finish(steps, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
