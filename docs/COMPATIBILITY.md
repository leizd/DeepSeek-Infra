# Compatibility Matrix（兼容性矩阵）

适用版本：v2.2.8。

这页只记录已经可复现的互操作结果，不把“协议上应该兼容”写成“实机已验证”。v2.2.5 的重点是把 v2.2.3/v2.2.4 已完成的 MCP / A2A 能力整理成一键 smoke 入口：本地服务可跑、失败能排查、日志可贴回兼容矩阵。Claude Desktop / Cursor 的配置片段已补齐，但本机未安装这两个 GUI 客户端；真实第三方 Streamable HTTP MCP server 与第三方 A2A 实现也仍标为待实机。

## Compatibility Smoke Pack（v2.2.5）

先启动本地服务。开发机上最少可用：

```powershell
$env:AUTH_DISABLED="1"
python app.py
```

如果启用了本地鉴权，请把 `.auth-token` 里的值传给 `--token`，或设置 `DEEPSEEK_INFRA_TOKEN` / `AUTH_TOKEN`。

```powershell
python scripts/smoke_mcp_compat.py --token <local-token>
python scripts/smoke_a2a_compat.py --token <local-token>
python examples/edge_router_smoke.py --token <local-token>
```

真实第三方 MCP server 冒烟入口：

```powershell
python scripts/smoke_mcp_compat.py --token <local-token> --external-server-url http://127.0.0.1:9001/mcp
```

建议把命令、commit、时间、客户端/第三方 server 版本和关键输出一起贴入本页矩阵。默认 A2A smoke 不强制要求 artifact chunk，因为没有 `DEEPSEEK_API_KEY` 时任务可能以 `failed` 终态收束；离线 contract 测试会固定 artifact chunk 行为。需要在有上游 Key 的环境强制验 artifact，可加 `--strict-artifacts`。

### v2.2.5 Smoke Evidence

| Path | Status | Command | Covers |
| --- | --- | --- | --- |
| MCP local smoke | ✅ Runner added | `python scripts/smoke_mcp_compat.py` | `/healthz`、`initialize`、`tools/list`、`tools/call`、policy gate、`/api/mcp/external/tools` |
| MCP real external server smoke | 🟡 Entry ready | `python scripts/smoke_mcp_compat.py --external-server-url <url>` | 第三方 server 的 `initialize` / `tools/list`；本仓库未记录实机通过 |
| A2A live smoke | ✅ Runner added | `python scripts/smoke_a2a_compat.py` | Agent Card、agents list、`message/send`、`message/stream`、`tasks/resubscribe`、`tasks/cancel` |
| A2A contract regression | ✅ Tested | `pytest tests/test_a2a_compat_contract.py` | artifact chunks、SSE final status、resubscribe cursor、cancel lifecycle |
| Edge Router smoke | 🟡 Runbook added | `python examples/edge_router_smoke.py` | `/api/edge/status`、`/v1/models`、Ollama model exposure check |

### Failure Triage

| Symptom | First check | Likely fix |
| --- | --- | --- |
| `401 / unauthorized` | `cat .auth-token` or env token | Pass `--token`, set `DEEPSEEK_INFRA_TOKEN`, or run local-only with `AUTH_DISABLED=1` |
| `connection refused` | `curl http://127.0.0.1:8000/healthz` | Start `python app.py`; verify `DEFAULT_PORT` |
| MCP tool call fails but initialize works | Smoke output for `mcp.policy_gate` / `structuredContent` | Check `MCP_CAPABILITY`, Tool Policy denial, or tool arguments |
| A2A stream has no artifact chunks | Final status in smoke output | Configure `DEEPSEEK_API_KEY`, or treat as endpoint smoke only and rely on contract tests |
| Real external MCP server has empty tools | Third-party server log | Confirm server uses Streamable HTTP JSON-RPC and supports `tools/list` |

## MCP Client Compatibility

| Client / Path | Status | Evidence | Notes |
| --- | --- | --- | --- |
| `examples/mcp_tool_demo.py` | ✅ Tested | `python examples/mcp_tool_demo.py` | 本地 Python MCP client，覆盖 `initialize` / `tools/list` / `tools/call`。 |
| MCP local smoke runner | ✅ Runner added | `python scripts/smoke_mcp_compat.py` | 覆盖本地 health、握手、目录、工具执行、policy gate 和外部 health API。 |
| MCP test suite (`tests/test_mcp.py`) | ✅ Tested | CI + local pytest | 覆盖握手、目录、能力切片、工具执行、错误码、loopback client、外部 server profile、policy gate、结果清洗、trace diagnostics。 |
| `curl` JSON-RPC | ✅ Tested | `POST /mcp` | 适合排查 token、协议响应和工具目录。 |
| Claude Desktop | 🟡 Config documented + smoke entry ready | [integrations/claude-desktop.md](integrations/claude-desktop.md) + `scripts/smoke_mcp_compat.py` | 本机未安装 Claude Desktop，未标为 GUI 实机通过。先用 smoke 验 DeepSeek Infra 端，再接 GUI。 |
| Cursor | 🟡 Config documented + smoke entry ready | [integrations/cursor.md](integrations/cursor.md) + `scripts/smoke_mcp_compat.py` | 本机未安装 Cursor，未标为 GUI 实机通过。Cursor MCP 配置片段与排障步骤已补。 |
| Continue.dev | 🔲 Not tested | - | 待补配置和实机验证。 |

## MCP External Server Bridge

v2.2.1 起，外部 MCP server 的工具会以 `mcp__<server>__<tool>` 桥接进本地 Agent 工具面；v2.2.2 起，Agent 调用和 `/mcp tools/call` 都共享 executor 内部 ToolPolicy 闸门，远端 `isError=true` 会映射为本地 `upstream_tool_error`。

| Scenario | Status | Evidence | Notes |
| --- | --- | --- | --- |
| Local mock external MCP server | ✅ Tested | `tests/test_mcp.py` | `MCPClient` 消费外部 `tools/list`，生成 `mcp__<server>__<tool>` profiles。 |
| External tool policy gate | ✅ Tested | `tests/test_mcp.py` + `scripts/smoke_mcp_compat.py` | 高风险/敏感参数进入 Tool Policy，拒绝时不会触达外部 server。 |
| External server unavailable | ✅ Tested | `tests/test_mcp.py` | 外部 server 失败不影响本地 MCP tools。 |
| Timeout / retry stats | ✅ Tested | `test_client_retries_retryable_transport_failures` | `MCPClient.last_stats` 记录 attempts、retry count、latency、timeout/error type。 |
| Circuit breaker | ✅ Tested | `test_external_mcp_registry_reports_health_and_opens_circuit` | 连续失败后进入短期 `circuit_open`，`/api/mcp/external/tools` 返回健康态。 |
| Trace diagnostics | ✅ Tested | `test_external_mcp_call_records_trace_diagnostics` | `mcp_external` span 记录 latency、attempts、retryCount、timeout、errorType。 |
| Real third-party Streamable HTTP MCP server | 🟡 Smoke entry ready, not tested in this workspace | `scripts/smoke_mcp_compat.py --external-server-url <url>` | 需要选择一个稳定公开/本地第三方 server 后补实测记录。 |

## Current MCP MVP Acceptance

| Acceptance item | v2.2.5 result |
| --- | --- |
| 本地 MCP server | ✅ `POST /mcp` + examples + CI + smoke runner |
| 本地 mock external MCP server | ✅ CI |
| Claude Desktop | 🟡 配置文档已补，smoke 入口已补，GUI 未实机 |
| Cursor | 🟡 配置文档已补，smoke 入口已补，GUI 未实机 |
| 一个真实外部 MCP server | 🟡 smoke 入口已补，待实机 |
| 外部 server 挂掉 | ✅ health + local tools unaffected |
| schema/响应异常 | ✅ invalid JSON / malformed tool catalog mapped to upstream failure |
| 工具超时/重试 | ✅ client stats + trace diagnostics |
| 危险参数拦截 | ✅ Tool Policy gate |

## Health API

`GET /api/mcp/external/tools` 返回：

- `servers[]`: `available`、`status`、`timeoutSeconds`、`consecutiveFailures`、`lastError`、`lastErrorType`、`lastRefreshAt`、`lastLatencyMs`、`lastRetryCount`、`circuitOpenSeconds`
- `tools[]`: `server`、`tool`、`bridgedName`、`risk`、`network`、`filesystem`、`requiresApproval`

## OpenAI API Compatibility

| Client | Status | Evidence |
| --- | --- | --- |
| OpenAI Python SDK (`openai>=1.0`) | ✅ Tested | `examples/openai_compatible_client.py` |
| `curl` | ✅ Tested | README examples |
| Ollama as provider | ✅ Tested | `OLLAMA_ENABLED=1` exposes `ollama/<tag>` through `/v1/models` |
| Edge Router runbook | 🟡 Repro path documented | [EDGE_ROUTER_RUNBOOK.md](EDGE_ROUTER_RUNBOOK.md) + `examples/edge_router_smoke.py` |
| Other OpenAI-compatible SDKs | 🔲 Not tested | Should work with standard `/v1/chat/completions` and `/v1/models`, but not claimed here. |

## A2A Interop Compatibility

| Peer | Status | Evidence |
| --- | --- | --- |
| Local A2A test suite (`tests/test_a2a.py`) | ✅ Tested | 14 cases: artifact chunks, `tasks/resubscribe`, canceling, loopback client, metrics |
| A2A compatibility contract (`tests/test_a2a_compat_contract.py`) | ✅ Tested | Agent Card, `message/send`, `message/stream`, artifact chunks, `tasks/resubscribe`, `tasks/cancel` |
| A2A live smoke runner | ✅ Runner added | `python scripts/smoke_a2a_compat.py` | Endpoint-level smoke against a running local server; artifact chunks can be strict with `--strict-artifacts` |
| Local Agent Card discovery | ✅ Tested | `GET /.well-known/agent-card.json` |
| Local external A2A peer loopback | ✅ Tested | `examples/a2a_peer_demo.py` against `http://127.0.0.1:8001/a2a/agents/reasoner` |
| Third-party A2A implementation | 🟡 Contract smoke ready, not tested | `scripts/smoke_a2a_compat.py` / `examples/a2a_compat_smoke.py`; ecosystem interop still pending. |

## A2A MVP Acceptance

| Acceptance item | v2.2.5 result |
| --- | --- |
| Artifact streaming chunks | ✅ `artifactId` / `chunkIndex` / `append` / `final` in `artifact-update` SSE events |
| `tasks/resubscribe` | ✅ Reconnect by `taskId` and `afterChunkIndex` |
| Local external peer loopback | ✅ `A2AClient.message_stream()` + `examples/a2a_peer_demo.py` |
| A2A trace / metrics | ✅ `a2a_task` / `a2a_peer_call` spans + `ai_a2a_*` Prometheus metrics |
| Cancellation lifecycle | ✅ `cancelRequestedAt`, `canceling -> canceled`, `discardedResult` trace diagnostics |
| Compatibility smoke entry | ✅ `scripts/smoke_a2a_compat.py` + `tests/test_a2a_compat_contract.py` |
