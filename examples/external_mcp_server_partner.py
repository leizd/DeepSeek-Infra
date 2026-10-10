#!/usr/bin/env python3
"""Standalone MCP server built on the *official* ``mcp`` Python SDK.

This is an **interop partner** for DeepSeek Infra's MCP external-server bridge.
It is deliberately a separate process with its own server, tools and port so
that DeepSeek Infra's ``MCPClient`` exercises a real Streamable HTTP transport
end-to-end — not an in-process mock.

Prerequisites (NOT in the repo's core requirements)::

    pip install "mcp>=1.0" uvicorn

Run it as a standalone process on port 9001::

    python examples/external_mcp_server_partner.py
    # or: python examples/external_mcp_server_partner.py --port 9001

Then point DeepSeek Infra at it::

    $env:MCP_CLIENT_ENABLED="1"
    $env:MCP_CLIENT_SERVERS='[{"name":"interop-partner","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":10}]'
    python app.py

Or smoke-test the external endpoint directly::

    python scripts/smoke_mcp_compat.py --external-server-url http://127.0.0.1:9001/mcp

The official MCP SDK responds with ``text/event-stream`` (SSE) for every POST,
which is why DeepSeek Infra's ``MCPClient`` gained SSE response parsing in
v2.3.0.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Official MCP SDK Streamable HTTP interop partner server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    args = parser.parse_args(argv)

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("ERROR: the 'mcp' package is not installed.", file=sys.stderr)
        print("Install it with:  pip install \"mcp>=1.0\" uvicorn", file=sys.stderr)
        return 2

    mcp = FastMCP("interop-partner")

    @mcp.tool()
    def echo(text: str) -> str:
        """Return the provided text unchanged."""
        return text

    @mcp.tool()
    def word_count(text: str) -> int:
        """Count the number of whitespace-separated words in *text*."""
        return len(text.split())

    print(f"interop-partner MCP server starting on http://{args.host}:{args.port}/mcp", flush=True)
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
