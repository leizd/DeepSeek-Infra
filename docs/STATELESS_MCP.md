# 无状态 MCP Server

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.4.1。

`stateless-mcp/` 是可横向扩展的独立 MCP 服务。它使用官方 TypeScript SDK 的 Streamable HTTP 入口；每个 HTTP 请求都创建一个新的 `McpServer`，进程内不保存客户端会话。现有 Python `POST /mcp` 继续作为兼容端点，迁移期间不会被替换。

## 架构

```text
MCP Client
    │ :8010
    ▼
NGINX round robin
    ├── mcp-instance-1 ─┐
    └── mcp-instance-2 ─┼── Redis AOF
                        │   ├── idempotency index
                        │   ├── task records
                        │   └── lease queue
                        └── OpenTelemetry Collector
                            ├── traces (debug exporter)
                            └── metrics (:9464)
```

只有以下数据会持久化：

- `start_test_run` 的任务参数、状态、尝试次数和结果；
- SHA-256 后的幂等键索引；
- 当前执行实例和带过期时间的任务租约；
- 有界的 `stdout` / `stderr`。

MCP 握手、连接和客户端身份不写入进程内 Map，也不依赖粘性会话。任务处于 `running` 时会定期续租；实例突然退出后租约自然过期，另一个实例重新认领任务。完成操作会校验租约所有者，旧实例不能提交迟到结果。

## 工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `server_info` | 只读 | 返回实例 ID、`clientSessionState=none` 和任务存储类型 |
| `code_search` | 只读 | 在工作区内调用 `rg` 做定界的字面量搜索 |
| `start_test_run` | 写入、幂等 | 创建持久 pytest 任务；必须提供 `idempotencyKey` |
| `get_task` | 只读 | 查询任务、租约所有者、尝试次数和结果 |
| `query_logs` | 只读 | 按流、包含文本和最大行数查询持久任务日志 |

`start_test_run` 不接受任意 shell 字符串。目标路径必须位于 `MCP_WORKSPACE_ROOT`，pytest 参数作为独立 argv 传入并使用 `shell=false`。同一个幂等键和相同参数返回原任务；同一个键配不同参数会返回工具错误。

## 启动双实例

```powershell
$env:MCP_AUTH_TOKEN = '<replace-with-a-long-random-token>'
docker compose -f docker-compose.stateless-mcp.yml up -d --build
```

服务地址：

- 负载均衡 MCP：`http://127.0.0.1:8010/mcp`
- 实例探针：`http://127.0.0.1:8010/instance`
- Prometheus 指标：`http://127.0.0.1:9464/metrics`
- 直接实例端口（只用于诊断/演练）：`8011`、`8012`

默认 token `dev-change-me` 只适合本机演练。生产部署必须覆盖 `MCP_AUTH_TOKEN`，限制 Redis 和实例直连端口，并为负载均衡入口配置 TLS。
`MCP_ALLOWED_HOSTS` 可用逗号分隔的主机名覆盖 Host 校验白名单；默认仅允许本机地址和 `mcp-lb`，用于防止 DNS rebinding。

主要配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` | Compose 为 `dev-change-me` | MCP Bearer token；生产必须覆盖。 |
| `MCP_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],mcp-lb` | Host 头白名单。 |
| `REDIS_URL` / `REDIS_PREFIX` | `redis://127.0.0.1:6379` / `deepseek-infra:mcp:v1` | 持久状态地址和键空间。 |
| `MCP_WORKSPACE_ROOT` | 当前工作目录 | 搜索和 pytest 允许访问的安全根。 |
| `MCP_TASK_LEASE_MS` / `MCP_TASK_POLL_MS` | `15000` / `250` | owner 租约和待处理任务轮询间隔。 |
| `MCP_TASK_TIMEOUT_SECONDS` | `600` | 单个 pytest 任务的最长运行时间。 |
| `MCP_MAX_OUTPUT_BYTES` | `262144` | 每个任务可保留的输出上限。 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 未设置 | 设置后启用 OTLP/HTTP trace 和 metrics 导出。 |

## 故障恢复演练

下面的命令会自动：

1. 构建并启动 Redis、Collector、两个 MCP 实例和 NGINX；
2. 连续访问 `/instance`，证明轮询落到两个实例；
3. 创建一个会持续 12 秒的真实 pytest 任务；
4. 使用 `docker compose kill` 突然终止持有租约的实例；
5. 让重试客户端先访问已退出实例，再切换到存活实例；
6. 等待任务租约过期并由另一个实例恢复；
7. 用相同幂等键重放创建请求，确认任务 ID 不变；
8. 输出 JSON `PASS` 证据并恢复被终止的实例。

```powershell
npm ci --prefix stateless-mcp
npm run smoke:failover --prefix stateless-mcp
```

CI 中的 `stateless-mcp-failover` job 运行同一演练；`stateless-mcp` job 运行 TypeScript 严格类型检查和单元测试。

## OpenTelemetry

每个工具调用创建 `mcp.tool.<name>` span，并记录：

- `mcp.tool.calls` counter，含 `mcp.tool.status=ok|error`；
- `mcp.tool.failures` counter；
- `mcp.tool.duration` histogram，单位为毫秒；
- `mcp.tool.name` 与 `service.instance.id` 属性。

设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后启用 OTLP/HTTP 导出；未设置时使用 OpenTelemetry API 的无操作 provider，服务仍可离线运行。Compose 已将两个实例指向 Collector。

## 单实例开发

本地 Redis 可用时：

```powershell
npm ci --prefix stateless-mcp
npm run build --prefix stateless-mcp
$env:REDIS_URL = 'redis://127.0.0.1:6379'
$env:MCP_INSTANCE_ID = 'dev-1'
$env:MCP_AUTH_TOKEN = 'local-token'
node stateless-mcp/dist/src/server.js
```

生产服务不会回落到内存存储；Redis 不可用时启动失败或 `/readyz` 返回 `503`。`MemoryTaskStore` 仅用于验证幂等和租约合同的单元测试。

Redis AOF 不属于默认 DeepSeek Infra `/data` 卷。任务参数、日志和结果摘要可能包含敏感内容；备份、迁移、保留和销毁都应把该卷作为单独的私有数据源处理。安全边界、威胁映射、API 和兼容性状态分别见 [SECURITY.md](SECURITY.md#无状态-mcp-安全边界)、[THREAT_MODEL.md](THREAT_MODEL.md#t8--无状态-mcp-横向扩展与任务执行v441)、[API.md](API.md#独立无状态-mcp-服务v441) 与 [COMPATIBILITY.md](COMPATIBILITY.md#无状态-mcp-横向扩展兼容性)。
