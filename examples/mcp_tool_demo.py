#!/usr/bin/env python3
"""MCP Tool Hub demo：用仓库内置的 MCPClient 对本机 ``POST /mcp`` 做标准协议回环。

流程：``initialize``（协议握手）→ ``tools/list``（17 个本地工具 + 风险注解）→
``tools/call``（真实执行 ``python_eval``，全程经过 Tool Policy 安全闸门）。

先启动本地服务（``AUTH_DISABLED=1 python app.py`` 或正常带 token 启动），然后::

    python examples/mcp_tool_demo.py
    python examples/mcp_tool_demo.py --expression "sum(range(1, 101))"

Claude Desktop / Cursor 等任意 MCP 客户端把 Streamable HTTP server 地址指向
``http://127.0.0.1:8000/mcp``（Authorization: Bearer <本地 token>）即可获得同一工具面。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.mcp.client import MCPClient  # noqa: E402


def resolve_token(explicit: str) -> str:
    if explicit:
        return explicit
    for env_name in ("DEEPSEEK_INFRA_TOKEN", "AUTH_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    token_file = Path(".auth-token")
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return "local"


class AuthedMCPClient(MCPClient):
    """MCPClient + 本地 Bearer token（外部 MCP 客户端接入本 Hub 用同样的头）。"""

    def __init__(self, base_url: str, token: str, **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)
        self._token = token

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self._token}"
        return headers


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP Tool Hub loopback demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--token", default="")
    parser.add_argument("--expression", default="(23 * 89 + 7) ** 0.5")
    args = parser.parse_args()

    client = AuthedMCPClient(args.base_url, resolve_token(args.token), name="local-hub")
    try:
        info = client.initialize()
    except Exception as exc:
        print(f"initialize 失败：{exc}", file=sys.stderr)
        print("请确认本地服务已启动（python app.py），MCP_ENABLED 未关闭，token 正确。", file=sys.stderr)
        return 1

    server_info = info.get("serverInfo") or {}
    print(f"[initialize] protocol={info.get('protocolVersion')} server={server_info.get('name')} v{server_info.get('version')}")

    tools = client.list_tools()
    print(f"\n[tools/list] {len(tools)} tools:")
    for tool in tools:
        annotations = tool.get("annotations") or {}
        marks: list[str] = []
        if annotations.get("readOnlyHint"):
            marks.append("read-only")
        if annotations.get("destructiveHint"):
            marks.append("destructive")
        if annotations.get("openWorldHint"):
            marks.append("open-world")
        print(f"   - {tool.get('name')}" + (f"  [{', '.join(marks)}]" if marks else ""))

    print(f"\n[tools/call] python_eval expression={args.expression!r}")
    result = client.call_tool("python_eval", {"expression": args.expression})
    structured = result.get("structuredContent")
    if structured is not None:
        print("structuredContent: " + json.dumps(structured, ensure_ascii=False))
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            print(f"content[text]: {block.get('text')}")
    if result.get("isError"):
        print("（工具级错误：被 Tool Policy 拒绝或执行失败）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
