# Claude Desktop MCP Integration

适用版本：DeepSeek Infra v2.2.8。

本机未安装 Claude Desktop，因此本页是可复现配置说明，不标记为 GUI 实机通过。DeepSeek Infra 端已验证的 MCP endpoint 是 `POST /mcp`（Streamable HTTP / JSON-RPC 2.0），本地鉴权默认需要 Bearer token。

## 1. Start DeepSeek Infra

开发机快速验证可以临时关闭本地鉴权：

```powershell
$env:AUTH_DISABLED="1"
python app.py
```

更接近真实使用的方式是保留鉴权，然后从启动日志或 `.auth-token` 取 token：

```powershell
python app.py
Get-Content .auth-token
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## 2. Direct Remote MCP Config

如果你的 Claude Desktop 版本支持 remote / Streamable HTTP MCP server，可在 Claude Desktop 的 MCP 配置中加入：

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_LOCAL_TOKEN>"
      }
    }
  }
}
```

如果你用 `AUTH_DISABLED=1` 启动，可以去掉 `headers`。

## 3. Stdio Bridge Fallback

如果 Claude Desktop 只接受 stdio MCP server，可以用 `mcp-remote` 在本机做一层 stdio → HTTP bridge：

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8000/mcp",
        "--header",
        "Authorization: Bearer <YOUR_LOCAL_TOKEN>"
      ]
    }
  }
}
```

`AUTH_DISABLED=1` 时可删除 `--header` 和下一项。

## 4. Verify

1. Restart Claude Desktop after editing its MCP config.
2. Ask Claude to list available MCP tools or call a safe tool such as `python_eval`.
3. On the DeepSeek Infra side, verify:

```powershell
python examples/mcp_tool_demo.py --base-url http://127.0.0.1:8000/mcp --token <YOUR_LOCAL_TOKEN>
```

Expected local evidence:

- `initialize` reports server `deepseek-infra`
- `tools/list` returns the local tool catalog
- `tools/call python_eval` returns structured content

## 5. Troubleshooting

| Symptom | Check |
| --- | --- |
| Claude shows no tools | Confirm DeepSeek Infra is running and `MCP_ENABLED=1`. |
| 401 / unauthorized | Use the token from `.auth-token`, or start with `AUTH_DISABLED=1` for local-only testing. |
| Tool call denied | The Tool Policy gate is working. For high-risk tools, pass explicit approval metadata or use safe tools first. |
| External bridged tool missing | Check `MCP_CLIENT_ENABLED=1`, `MCP_CLIENT_SERVERS`, then open `GET /api/mcp/external/tools`. |

Reference docs checked on 2026-06-26:

- Anthropic MCP docs: <https://docs.anthropic.com/en/docs/agents-and-tools/mcp>
- Model Context Protocol transports: <https://modelcontextprotocol.io/specification>
