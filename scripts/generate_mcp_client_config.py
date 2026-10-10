#!/usr/bin/env python3
"""Generate copyable MCP client config snippets for DeepSeek Infra."""

from __future__ import annotations

import argparse
import json
from typing import Any

DEFAULT_MCP_URL = "http://127.0.0.1:8000/mcp"
SERVER_NAME = "deepseek-infra"
TOKEN_PLACEHOLDER = "<YOUR_LOCAL_TOKEN>"


def authorization_headers(token: str, *, auth_disabled: bool) -> dict[str, str]:
    if auth_disabled:
        return {}
    value = token.strip() or TOKEN_PLACEHOLDER
    return {"Authorization": f"Bearer {value}"}


def remote_http_server(mcp_url: str, token: str, *, auth_disabled: bool) -> dict[str, Any]:
    server: dict[str, Any] = {"url": mcp_url}
    headers = authorization_headers(token, auth_disabled=auth_disabled)
    if headers:
        server["headers"] = headers
    return server


def stdio_bridge_server(mcp_url: str, token: str, *, auth_disabled: bool) -> dict[str, Any]:
    args = ["-y", "mcp-remote", mcp_url]
    headers = authorization_headers(token, auth_disabled=auth_disabled)
    if headers:
        args.extend(["--header", headers["Authorization"].replace("Bearer ", "Authorization: Bearer ")])
    return {"command": "npx", "args": args}


def generate_config(client: str, *, mcp_url: str = DEFAULT_MCP_URL, token: str = "", auth_disabled: bool = False, stdio_bridge: bool = False) -> dict[str, Any]:
    client = client.lower().strip()
    if client not in {"claude", "cursor"}:
        raise ValueError("client must be 'claude' or 'cursor'")
    if client == "cursor" and stdio_bridge:
        raise ValueError("Cursor config uses direct Streamable HTTP in this generator")

    server = stdio_bridge_server(mcp_url, token, auth_disabled=auth_disabled) if stdio_bridge else remote_http_server(mcp_url, token, auth_disabled=auth_disabled)
    return {"mcpServers": {SERVER_NAME: server}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Claude Desktop or Cursor MCP config JSON.")
    parser.add_argument("--client", choices=("claude", "cursor"), required=True)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--token", default="", help="Local DeepSeek Infra token. Omit to use a placeholder.")
    parser.add_argument("--auth-disabled", action="store_true", help="Do not emit Authorization headers.")
    parser.add_argument("--stdio-bridge", action="store_true", help="Generate a Claude stdio bridge config using npx mcp-remote.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = generate_config(
            args.client,
            mcp_url=args.mcp_url,
            token=args.token,
            auth_disabled=bool(args.auth_disabled),
            stdio_bridge=bool(args.stdio_bridge),
        )
    except ValueError as exc:
        print(f"config generation failed: {exc}")
        return 1
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
