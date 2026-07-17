# 外部 MCP Server 互操作

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：DeepSeek Infra v2.3.0。

本页记录 DeepSeek Infra 的 MCP 外部 server 桥接与一个**真实第三方 MCP SDK 实现**的互操作验证结果。验证使用官方 `mcp` Python SDK（PyPI `mcp>=1.0`）构建的 Streamable HTTP server 作为 interop partner，不是同进程 mock。

## 互操作 Partner

| 字段 | 值 |
| --- | --- |
| Partner 实现 | 官方 `mcp` Python SDK v1.28.1（`FastMCP` + `streamable-http` transport） |
| Partner 脚本 | `examples/external_mcp_server_partner.py` |
| 传输协议 | Streamable HTTP / JSON-RPC 2.0 over `POST /mcp` |
| 响应格式 | `text/event-stream`（SSE）—— 官方 SDK 对每个 POST 都返回 SSE |
| Partner 工具 | `echo(text)` → str、`word_count(text)` → int |
| 验证日期 | 2026-06-27 |
| 验证 commit | `6edcda5` |
| OS | Windows 11 |

> **关键发现**：官方 MCP SDK 的 Streamable HTTP transport 对每个 POST 请求都返回 `text/event-stream`（SSE）响应体，而不是 `application/json`。v2.3.0 之前 DeepSeek Infra 的 `MCPClient` 只解析 JSON 响应，无法与官方 SDK 互通。v2.3.0 为 `MCPClient` 和 smoke runner 增加了 SSE 响应解析，使真实互操作成为可能。

## v2.3.0 修复：SSE 响应解析

| 组件 | 变更 |
| --- | --- |
| `deepseek_infra/infra/mcp/client.py` | `_post()` 检查响应 `Content-Type`；`text/event-stream` 时用 `_parse_sse_jsonrpc()` 从 `data:` 行提取 JSON-RPC 对象 |
| `deepseek_infra/infra/mcp/client.py` | `MCPClient.__init__` 新增 `extra_headers` 参数，支持外部 server 鉴权 |
| `scripts/_smoke_common.py` | `request_json()` 同样处理 SSE 响应 |
| `scripts/smoke_mcp_compat.py` | 外部 server 检查改用 `MCPClient`（自动处理 session ID、SSE、Accept header） |

## 复现步骤

### 1. 安装 partner 依赖

Partner 使用官方 MCP SDK，不在仓库核心 `requirements.txt` 中：

```bash
pip install "mcp>=1.0" uvicorn
```

### 2. 启动 partner server（独立进程，端口 9001）

```bash
python examples/external_mcp_server_partner.py --port 9001
```

### 3. 启动 DeepSeek Infra（桥接外部 server）

```powershell
$env:AUTH_DISABLED="1"
$env:MCP_CLIENT_ENABLED="1"
$env:MCP_CLIENT_SERVERS='[{"name":"interop-partner","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":10}]'
python app.py
```

### 4. 跑 smoke

```bash
python scripts/smoke_mcp_compat.py --external-server-url http://127.0.0.1:9001/mcp --json
```

## 验证结果（2026-06-27 · commit `6edcda5`）

### Smoke 步骤（全部 PASS）

| 步骤 | 状态 | 详细 |
| --- | --- | --- |
| `healthz` | ✅ PASS | `status=ok` |
| `mcp.initialize` | ✅ PASS | `protocol=2025-06-18 server=deepseek-infra` |
| `mcp.tools_list` | ✅ PASS | 19 tools exposed（17 local + 2 bridged） |
| `mcp.tools_call` | ✅ PASS | `data_transform count=4`（本地工具不受外部影响） |
| `mcp.policy_gate` | ✅ PASS | `fetch_url` localhost SSRF probe 被 Tool Policy 拦截 |
| `mcp.external_health_api` | ✅ PASS | `servers=1 bridgedTools=2` |
| `mcp.real_external_server` | ✅ PASS | `server=interop-partner protocol=2025-06-18 tools=2 [echo, word_count]` |

### 桥接工具

| 桥接名称 | 服务器 | 工具 | 风险 | 网络 | 文件系统 | 需要审批 |
| --- | --- | --- | --- | --- | --- | --- |
| `mcp__interop-partner__echo` | interop-partner | echo | medium | false | false | false |
| `mcp__interop-partner__word_count` | interop-partner | word_count | medium | false | false | false |

### 外部 server 健康态（`GET /api/mcp/external/tools`）

```json
{
  "name": "interop-partner",
  "url": "http://127.0.0.1:9001/mcp",
  "available": true,
  "status": "ok",
  "consecutiveFailures": 0,
  "lastLatencyMs": 15,
  "circuitOpenSeconds": 0.0
}
```

### 桥接工具调用

- `tools/call mcp__interop-partner__echo {"text":"bridge works!"}` → `isError: false`, `structuredContent.result: "bridge works!"`
- `tools/call mcp__interop-partner__word_count {"text":"one two three four"}` → `isError: false`, `structuredContent.result: 4`

### 外部 server 挂掉时

- 本地工具（`data_transform` 等）不受影响，仍正常返回。
- 桥接工具调用返回 `isError: true` / `ok: false`（优雅降级，不崩溃）。
- `/api/mcp/external/tools` 在连续失败后进入 `circuit_open`（由 circuit breaker 控制）。

## 诚实标注

- 本次验证的 partner 使用**官方 MCP Python SDK** 构建，是真实的 MCP 协议实现（非手写 mock），但它不是第三方**产品**（如 GitHub MCP server、Slack MCP server 等）。它的价值在于验证 DeepSeek Infra 与官方 SDK 的 Streamable HTTP transport 真正互通。
- 验证在 Windows 11 本地完成；未在 CI 环境中自动执行（CI 不安装 `mcp` 包）。
- Session 管理（`Mcp-Session-Id`）、`MCP-Protocol-Version` header 和 `notifications/initialized` 通知均由 `MCPClient` 自动处理。
