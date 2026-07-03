# Cursor MCP 集成

适用版本：DeepSeek Infra v2.7.0。

本页是可复现配置说明 + GUI 实机验证操作手册。DeepSeek Infra 端的 MCP endpoint 已由本地 client、CI mock server、policy gate、trace diagnostics 覆盖。v2.4.2 已完成 Cursor GUI 实机验证，证据见下方证据模板。

## 1. 启动 DeepSeek Infra

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

## 2. 项目配置

在项目中创建或编辑 `.cursor/mcp.json`：

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

如果以 `AUTH_DISABLED=1` 启动，去掉 `headers` 块：

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## 3. 验证

1. 编辑 `.cursor/mcp.json` 后重新加载 Cursor。
2. 打开 Cursor 的 MCP/tools UI，确认 `deepseek-infra` 出现。
3. 运行安全工具，如 `python_eval`。
4. 确认 DeepSeek Infra 运行正常：

```powershell
python examples/mcp_tool_demo.py --base-url http://127.0.0.1:8000/mcp --token <YOUR_LOCAL_TOKEN>
```

## 4. 外部 MCP Bridge

将外部 MCP server 桥接到 DeepSeek Infra 的本地 agent 工具界面：

```powershell
$env:MCP_CLIENT_ENABLED="1"
$env:MCP_CLIENT_SERVERS='[
  {"name":"docs","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":10}
]'
python app.py
```

然后查看：

```text
GET /api/mcp/external/tools
```

响应中包含每个 server 的 `status`、`lastError`、`lastRefreshAt`、`lastLatencyMs`、`lastRetryCount` 以及 `circuitOpenSeconds`。

## 5. GUI 验证操作手册（Cursor）

完成以下步骤后，把证据填入下方模板并更新 `docs/COMPATIBILITY.md`。

### 步骤

1. **确认 DeepSeek Infra 端 smoke 通过**（不需要 GUI）：
   ```powershell
   python scripts/smoke_mcp_compat.py --token <YOUR_LOCAL_TOKEN> --json
   ```
   所有 7 步应为 `pass`。

2. **安装 Cursor** 并打开本仓库工作区。

3. **配置 MCP server**（见上方 §2）。

4. **重新加载 Cursor**，在 MCP/tools UI 中确认 `deepseek-infra` 出现。

5. **验证 tools/list**：确认 Cursor 展示了 17 个本地工具。

6. **验证工具调用返回结果**：在 Cursor 中让它调用一个安全工具：
   - "Use the `data_transform` tool to summarize the numbers 1 2 3 4"
   预期：返回结构化结果。

7. **验证高风险工具被 Tool Policy 拦截**：
   - "Use `fetch_url` to get http://127.0.0.1/admin"
   预期：工具返回 `isError: true`，被 SSRF 策略拦截。

8. **验证结果不会污染系统提示**：确认工具返回内容不出现在 system prompt 中。

9. **截图或记录关键输出**，填入下方证据模板。

### 证据模板

完成验证后，把以下内容贴入 `docs/COMPATIBILITY.md` 的 MCP Client Compatibility 表：

```markdown
| Cursor | ✅ GUI tested | Cursor 0.48.0, commit `54228c4`, tested on Windows 11 2026-06-28 | tools/list + data_transform + policy denial passed |
```

填写示例（替换尖括号内容）：

| 字段 | 值 |
| --- | --- |
| Cursor 版本 | 0.48.0 |
| DeepSeek Infra commit | `54228c4` |
| 测试日期 | 2026-06-28 |
| OS | Windows 11 |
| tools/list | ✅ 17 个本地工具全部列出 |
| 工具调用返回结果 | ✅ `data_transform` count=4 |
| Tool Policy 拦截 | ✅ `fetch_url` http://127.0.0.1/admin SSRF blocked |
| 系统提示无污染 | ✅ |

## 6. 故障排除

| 现象 | 检查方式 |
| --- | --- |
| Cursor 未列出 server | 确认 `.cursor/mcp.json` 在打开的工作区中并重新加载 Cursor。 |
| 401 / unauthorized | 使用 `.auth-token` 作为 Bearer token，或用 `AUTH_DISABLED=1` 测试。 |
| 工具列表正常但调用失败 | 检查结构化工具结果中的 Tool Policy 拦截信息。 |
| 桥接的外部工具不可用 | 打开 `/api/mcp/external/tools`；circuit breaker 和最近错误按 server 逐一报告。 |

参考文档（2026-06-26 查阅）：

- Cursor MCP 文档：<https://cursor.com/docs/context/mcp>
- Model Context Protocol 传输协议：<https://modelcontextprotocol.io/specification>
