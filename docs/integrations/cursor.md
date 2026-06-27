# Cursor MCP Integration

适用版本：DeepSeek Infra v2.2.8。

本机未安装 Cursor，因此本页是可复现配置说明，不标记为 GUI 实机通过。DeepSeek Infra 端的 MCP endpoint 已由本地 client、CI mock server、policy gate、trace diagnostics 覆盖。

## 1. Start DeepSeek Infra

```powershell
python app.py
Get-Content .auth-token
```

开发机也可以临时关闭鉴权：

```powershell
$env:AUTH_DISABLED="1"
python app.py
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## 2. Project Config

Create or edit `.cursor/mcp.json` in the project:

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

If you started with `AUTH_DISABLED=1`, remove the `headers` block:

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## 3. Verify

1. Reload Cursor after editing `.cursor/mcp.json`.
2. Open Cursor's MCP/tools UI and confirm `deepseek-infra` appears.
3. Run a safe tool such as `python_eval`.
4. Confirm DeepSeek Infra is healthy:

```powershell
python examples/mcp_tool_demo.py --base-url http://127.0.0.1:8000/mcp --token <YOUR_LOCAL_TOKEN>
```

## 4. External MCP Bridge

To bridge external MCP servers into DeepSeek Infra's local agent tool surface:

```powershell
$env:MCP_CLIENT_ENABLED="1"
$env:MCP_CLIENT_SERVERS='[
  {"name":"docs","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":10}
]'
python app.py
```

Then inspect:

```text
GET /api/mcp/external/tools
```

The response includes each server's `status`, `lastError`, `lastRefreshAt`, `lastLatencyMs`, `lastRetryCount`, and `circuitOpenSeconds`.

## 5. Troubleshooting

| Symptom | Check |
| --- | --- |
| Cursor does not list the server | Verify `.cursor/mcp.json` is in the opened workspace and reload Cursor. |
| 401 / unauthorized | Use `.auth-token` as Bearer token, or test with `AUTH_DISABLED=1`. |
| Tools list but calls fail | Check Tool Policy denial in the structured tool result. |
| Bridged external tool is unavailable | Open `/api/mcp/external/tools`; circuit breaker and last error are reported per server. |

Reference docs checked on 2026-06-26:

- Cursor MCP docs: <https://cursor.com/docs/context/mcp>
- Model Context Protocol transports: <https://modelcontextprotocol.io/specification>
