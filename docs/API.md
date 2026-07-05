# HTTP API

适用版本：v2.9.0。

默认情况下，所有 `/api/*` 路由都需要本地 token 鉴权。客户端可以发送 `Authorization: Bearer <token>`，也可以使用打开 `/?token=<token>` 后写入的 `auth_token` Cookie。未设置 `AUTH_TOKEN` 时，服务端会把自动生成的 token 保存到本地 `.auth-token`，重启后继续复用。

桌面内嵌 WebView 启动时会使用 `/?token=<token>&desktop=1`。该入口仍会校验 token；校验通过后直接返回首页并写入 `auth_token` Cookie，而不是先 302 跳转，避免 WebView 丢 Cookie 后显示 `Auth required`。

错误响应保留旧版 `error` 字段，并增加稳定 `code` 字段：

```json
{"error": "Auth required", "code": "unauthorized"}
```

## GET `/api/config`

返回前端配置和能力标记。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version` | string | 应用版本。 |
| `hasServerKey` | boolean | 服务端是否配置了 `DEEPSEEK_API_KEY`。 |
| `hasSearch` | boolean | 服务端是否配置 Tavily 搜索；前端仍可通过 `tavilyApiKey` 临时启用。 |
| `defaultModel` | string | 默认模型名。 |
| `models` | array | 支持的模型列表。 |
| `modelRoutes` | object | 快速/专家模式到模型名的映射。 |
| `searchModes` | array | 搜索模式列表：`off`、`auto`、`on`。 |
| `uploadLimits` | object | 上传限制：`fileMaxBytes` 单文件上限、`requestMaxBytes` multipart 请求体上限、`maxFiles` 单次文件数上限。 |
| `ocr` | object | OCR 能力摘要：`enabled`、`mode`、`localOnly`。`localOnly=false` 表示 OCR 会优先调用 DeepSeek API，失败时才回退本地引擎；这里只返回配置，不探测真实 OCR 引擎，避免启动变慢。 |
| `edgeInference` | object | 端侧推理能力摘要：`enabled`、`provider`、`available`、`modelName`、`quantization`、`nCtx`、`nGpuLayers` 等。默认不加载模型，只报告配置和依赖是否可用。 |
| `localRag` | object | 本地 RAG 数据层摘要：`enabled`、`backend`、`databasePath`、`sqliteVecAvailable`、`vectorTableAvailable`、`embeddingProvider`、`indexedItems`、`indexedFiles`、`indexedMemories` 和 `lastError`。 |
| `tracing` | object | 本地 trace 状态：`enabled`、`.traces/traces.sqlite3` 路径、trace/span 数量和最近错误。 |
| `semanticCache` | object | 本地语义缓存状态：`enabled`、`.semantic-cache/cache.sqlite3` 路径、相似度阈值、TTL、条目数、命中数、embedding provider，以及 v2.0.7 新增的 `cacheVersion`（命名空间戳）、`minQualityScore`（低质量门控阈值）、`cacheAttachments`（是否缓存文件上下文）。 |
| `gateway` | object | API 网关韧性状态：`contextManager` 描述稳定 JSON、工具顺序和滑动窗口配置；`requestQueue` 描述 `.request-queue/queue.sqlite3`、最大重试次数、退避配置和队列统计。 |
| `computerUrl` / `phoneUrl` | string | 带认证 token 的启动地址；鉴权关闭时不带 token。 |

## GET `/api/rag/status`

返回本地 RAG 数据层状态，不会主动重建索引。

字段包括：

- `enabled`：是否启用本地 RAG。
- `backend`：当前后端，默认 `sqlite_vec`；没有 sqlite-vec 依赖时会回退到 SQLite 表 + Python 本地相似度。
- `databasePath`：本地 `.local-rag/rag.sqlite3` 路径。
- `sqliteVecAvailable` / `vectorTableAvailable`：Python 包和 `vec0` 虚表是否可用。
- `embeddingProvider` / `embeddingProviderRequested`：实际使用和请求的 embedding provider。
- `indexedItems` / `indexedFiles` / `indexedMemories`：当前索引统计。

## POST `/api/rag/reindex`

重建 `.file-cache`、`.projects` 和 `.memory` 的本地 RAG 索引。请求体可传：

```json
{"action": "reindex"}
```

v1.7.6 的 Local Data Infra 全程在本地完成：文件分块、embedding、SQLite / sqlite-vec 写入和检索都发生在本机。默认无额外依赖，使用哈希 embedding；安装 `requirements-rag.txt` 并配置 `LOCAL_RAG_EMBEDDING_PROVIDER=onnx`、`LOCAL_RAG_ONNX_MODEL_PATH`、`LOCAL_RAG_TOKENIZER_PATH` 后，可用 ONNX Runtime 本地 embedding 模型。

### Local RAG Data Plane（v2.0.8）

本地 RAG 升级为完整数据层：

- **Hybrid 检索**：稠密向量相似度与 BM25 词法分数融合排序（`status.hybridSearch="bm25+vector"`，`bm25K1`/`bm25B` 可调）。
- **增量索引 + 文档版本**：每个 chunk 带内容 `hash`，文档有内容寻址的 `docVersion`。重新索引时内容哈希未变的文档直接跳过、未变的 chunk 复用已存向量（`LOCAL_RAG_INCREMENTAL`）。
- **Chunk lineage（引用追溯）**：每条检索结果可经 `chunk_lineage` 追溯到 `chunkId` / `docId` / `projectId` / `page` / `startChar` / `endChar` / `hash` / `docVersion`；`search_files` 工具结果带 `lineage` 字段。
- **删除级联**：删除项目会级联清理其文件的全部 chunk（向量表同步删除）。
- **POST `/api/rag/verify-citation`**：请求体 `{itemId, snippet}`，校验引用片段是否真实存在于该 chunk（精确匹配或 token 覆盖率），返回 `{grounded, coverage, lineage}`。
- **POST `/api/rag/eval`**：请求体 `{cases:[{query, relevant:[docId|chunkId]}], k}`，返回 `{recallAtK, mrr, details}` 的 RAG Recall@K 评估。

## GET `/api/traces`

返回最近的本地 trace 列表。可用 `?limit=50` 控制数量。每条 trace 包含：

- `traceId`：本轮请求的稳定 ID，会出现在 `/api/chat` 响应或 NDJSON `done.diagnostics.traceId`。
- `kind`：`chat`、`agent` 或 `edge`。
- `status`、`startedAt`、`completedAt`、`durationMs`。
- `spanCount`、`metadata` 和错误摘要。

## GET `/api/traces/{traceId}`

返回单条 trace 明细，包含 `spans` 和 `summary`。span 会记录：

- `name` / `kind` / `status`。
- `parentSpanId`，配合 `offsetMs` / `durationMs` 供前端渲染 OpenTelemetry 风格的层级瀑布图（前端用 `buildTraceSpanTree` 按 `parentSpanId` 深度优先展开成树、按深度缩进）。
- 输入和输出摘要、`usage`、`diagnostics`、`cacheHitRate`、`totalTokens` 和错误文本。

v2.0.6 起 span 形成端到端调用树（run 为根）。典型一次多 Agent 请求：

```
(run)
├── agent.planner → llm(deepseek)
├── agent.researcher → context.build → {memory.retrieve, rag.retrieve}, tool.web_search, llm(deepseek)
├── agent.coder → llm(deepseek)
├── agent.critic → llm(deepseek)
└── agent.synthesizer → llm(deepseek)
```

普通单聊路径不带 `agent.*` 包裹，`context.build` / `memory.retrieve` / `tool.web_search` / `deepseek` span 直接挂在 run 根下（`parentSpanId` 为空），与旧行为一致。

Trace 数据只写在本机 `.traces/traces.sqlite3`，不会上传到第三方观测平台。

## GET `/api/traces/{traceId}/export.json`

下载单条 trace 的机器可读 JSON，响应带 `Content-Disposition: attachment`。导出会在返回前再次脱敏：

- API Key、Authorization、auth token、cookie、password、secret 等字段替换为 `[redacted]`。
- URL query 中的 `token` / `api_key` / `access_token` 等敏感参数替换为 `[redacted]`。
- 大段 `content` / `text` / `prompt` / `fileText` / `rawContent` 等私有内容会截断，避免导出完整隐私文件内容。
- token 用量、cache hit、span 层级和错误摘要会保留，方便离线排障。

## GET `/trace/{traceId}`

打开独立只读 Trace Viewer 页面（本地 token 鉴权）。页面从 `/api/traces/{traceId}` 加载数据，展示 trace 基本信息、span 树、瀑布图、Agent / Tool / RAG / LLM 耗时分组、token 用量、cache hit、错误信息，并提供一键导出 JSON。

## GET `/api/semantic-cache/status`

返回本地语义缓存状态，不会触发重建或清理。字段与 `/api/config.semanticCache` 一致。

### 语义缓存高级机制（v2.0.7）

每条 `/api/chat` 响应的 `diagnostics.semanticCache` 会带本轮决策细节：

- `cacheVersion`：命名空间戳 `<SEMANTIC_CACHE_VERSION>:<embedding provider>:<dimensions>`。切换 embedding 模型/维度或调高 `SEMANTIC_CACHE_VERSION` 会换命名空间，旧条目不再被命中（按 TTL/容量自然淘汰），避免用不兼容的向量空间误命中。
- `scope`：隐私/项目隔离命名空间（来自 `memoryScope` 或 `projectId`，默认 `global`）。答案不会跨 scope 复用。
- `qualityScore`：答案质量启发分（0–1）。低于 `SEMANTIC_CACHE_MIN_QUALITY`（默认 0.3，可经环境变量调整）的回答——拒答、空综合回退、过短——不写入缓存（`storeSkippedReason="low_quality"`）。
- `exactMatchOnly`：带文件/附件上下文的请求只走**精确 prompt 命中**（不做模糊相似度），因为展开后的文件文本会主导 embedding、模糊匹配会把「同一文件的不同问题」错误命中；并按项目 scope 隔离，不跨项目复用。`SEMANTIC_CACHE_ATTACHMENTS=0` 可改回完全跳过附件请求。

## GET `/api/tool-policy`

返回 Capability-based Tool Policy Engine 的状态与最近审计，不会改动任何状态。响应结构：

```json
{
  "ok": true,
  "toolPolicy": {
    "enabled": true,
    "enforceSchema": false,
    "requireConfirm": false,
    "sanitizeResults": true,
    "auditEnabled": true,
    "auditLogPath": ".tool-audit/audit.jsonl",
    "capabilities": {"full": ["..."], "researcher": ["web_search", "compare_search_results", "fetch_url"], "coder": ["search_files", "read_file_chunk", "python_eval"], "reasoner": [], "critic": []},
    "tools": [{"name": "fetch_url", "risk": "high", "network": true, "filesystem": false, "requiresConfirm": false, "capability": "research"}]
  },
  "audit": [{"ts": "...Z", "scope": "global", "tool": "fetch_url", "action": "deny", "risk": "critical", "reasons": ["ssrf_blocked:..."], "capability": "full"}]
}
```

`limit` 查询参数控制返回的审计条数（默认 50，最大 500）。`/api/config.toolPolicy` 给同一份状态的全局视图。

### 工具策略诊断（v2.1.0）

模型不直接调用工具，每个 LLM 工具调用先经过策略闸门：**schema 校验 → 能力/权限检查 → 风险分级 → 人工确认（如需要）→ 执行器**，再加结果注入清洗与审计。每个 Agent 角色拿到不同工具权限（capability 画像），主聊天用 `full`。本轮发生工具调用时，`/api/chat` 响应的 `diagnostics.toolPolicy` 会带：

- `capability`：本轮能力画像（主聊天 `full`，worker 为其角色 id）。
- `evaluated` / `allowed` / `denied` / `confirmations`：策略评估、放行、拦截、待确认的工具调用数。
- `sanitizedInjections`：从工具结果外部文本里红action 掉的疑似注入指令数。
- `blockedTools`：被拦或待确认的工具名列表。

被策略拦截的工具调用返回 `{"ok": false, "code": "forbidden"|"requires_confirmation", "policy": {...}}`，不会真正执行。可经 `TOOL_POLICY_*` 环境变量配置（启用、强制 schema、强制确认、结果清洗、审计）。

## GET `/api/scheduler`

返回本地请求调度层的实时快照与 Dead Letter Queue，不会改动任何状态。响应结构：

```json
{
  "ok": true,
  "scheduler": {
    "enabled": true,
    "inFlight": 0,
    "waiting": 0,
    "maxConcurrency": 16,
    "maxQueueDepth": 256,
    "ratePerSecond": 0.0,
    "availableTokens": 16,
    "admitted": 42,
    "shed": 0,
    "timedOut": 0,
    "cancelled": 0,
    "rateLimitedWaits": 0,
    "peakInFlight": 3,
    "byPriority": {"0": 40, "10": 2},
    "deadLetterQueue": {"enabled": true, "count": 0, "byReason": {}, "recent": []}
  },
  "deadLetters": [{"id": "...", "kind": "deepseek_json", "reason": "retry_exhausted", "attempts": 6, "priority": 0, "createdAt": 0.0}]
}
```

`limit` 控制返回的死信条数（默认 50）。`/api/config.gateway.scheduler` 与每轮 `/api/chat` 响应的 `diagnostics.gatewayResiliency.scheduler` 给出同一份快照。

### 请求调度层（v2.1.2）

模型不再无序冲击上游：`call_deepseek` / `stream_deepseek` 的每次上游调用先经过进程内准入控制——**优先级队列**（交互 > Agent worker > 后台）、**并发上限**、**令牌桶限流**、**backpressure**（waiting+in-flight 越过 `max_queue_depth` 即以 503 `{"code":"rate_limited"}` 快速卸载，而不是无界堆积）。耗尽重试的基础设施失败与被卸载的请求落入持久化 Dead Letter Queue（`.scheduler/scheduler.sqlite3`），服务启动时 `recover_orphans` 会把上次崩溃残留的在途请求标记失败并 dead-letter。默认配置不限流、并发 16、队列 256，对正常负载透明；可经 `SCHEDULER_*` 环境变量收紧。

## POST `/mcp`（MCP Tool Hub，v2.1.3；External Bridge v2.2.1；Policy Hardening v2.2.2）

MCP（Model Context Protocol）JSON-RPC 2.0 端点：一次 POST 一条 JSON-RPC 消息，返回一条 JSON 响应（通知返回 `202` 空体）。把任意 MCP 客户端（Claude Desktop、Cursor 等）的 Streamable HTTP server 地址指向 `http://127.0.0.1:8000/mcp` 并携带本地 token 即可使用本地工具面。支持的方法：

- `initialize` / `notifications/initialized` / `ping`：握手（`protocolVersion: 2025-06-18`、`serverInfo.name: deepseek-infra`、capabilities 含 `tools` / `resources` / `prompts`）。
- `tools/list`：本地 17 个工具按 `MCP_CAPABILITY` 能力画像切片后的 MCP 目录，`inputSchema` 直通工具的 JSON schema，`annotations` 带 read-only / destructive / open-world 提示。
- `tools/call`：`{"name": ..., "arguments": {...}}`。执行结果以 `content`（text part，稳定 JSON）+ `structuredContent` 返回；策略拒绝与工具失败是 `isError: true` 的工具级错误，不是协议错误。每次调用都过 Tool Policy 闸门（capability / schema / SSRF / 路径 / 敏感写入 / 结果清洗），需要确认的工具可经 `params._meta.approvedTools` 预批。
- `resources/list` / `resources/read`：生成产物（`generated://<fileId>`，pptx/docx/pdf 为 base64 blob、svg 为文本）与 `runtime://capabilities`（工具策略 JSON 文档）。
- `prompts/list` / `prompts/get`：`slides-outline`、`research-brief` 两个参数化 prompt 模板。

错误码遵循 JSON-RPC：`-32700` parse / `-32600` invalid request / `-32601` method not found / `-32602` invalid params / `-32603` internal。`GET /api/mcp` 返回 Hub 状态（协议版本、能力画像、工具数、外接 client 配置）。

v2.2.1 起，外接方向不只做 client 回环：`MCP_CLIENT_ENABLED=1` + `MCP_CLIENT_SERVERS` 配置后，外部 MCP server 的 `tools/list` 会被桥接进本地 Agent 工具面，工具名统一为 `mcp__<server>__<tool>`。这些 bridged tools 继续走 Tool Policy、审批、结果清洗和审计；高风险或 destructive / sensitive schema 的外部工具会触发确认。`GET /api/mcp/external/tools` 返回外部 server 可用性、bridged name、risk、network/filesystem 标记和 requiresApproval，便于上线前核对工具面。v2.2.2 起，`/mcp tools/call` 调外部 bridged tool 与 Agent 调用链共享 executor 内部的防御式 `ToolPolicy.evaluate()`，远端 `isError: true` 会映射为本地 `ok: false` / `upstream_tool_error`，`network` / `filesystem` 外部工具也会扫描通用 URL/path 参数。

v2.2.5 起，兼容性冒烟入口为 `python scripts/smoke_mcp_compat.py --token <local-token>`；它会验证 `initialize`、`tools/list`、`tools/call`、policy gate 和 `/api/mcp/external/tools`。真实第三方 Streamable HTTP MCP server 可用 `--external-server-url <url>` 做单独握手和 `tools/list` smoke。

### GET `/api/mcp/external/tools`

触发外部 MCP catalog refresh（受 TTL 限制）并返回桥接工具和 server 健康态：

```json
{
  "ok": true,
  "servers": [
    {
      "name": "docs",
      "url": "http://127.0.0.1:9001/mcp",
      "available": true,
      "status": "ok",
      "timeoutSeconds": 10,
      "consecutiveFailures": 0,
      "failureCount": 0,
      "timeoutCount": 0,
      "callCount": 3,
      "lastError": "",
      "lastErrorType": "",
      "lastRefreshAt": "2026-06-26T08:00:00Z",
      "lastSuccessAt": "2026-06-26T08:00:00Z",
      "lastCallAt": "2026-06-26T08:02:10Z",
      "lastLatencyMs": 12,
      "lastRetryCount": 0,
      "circuitOpenSeconds": 0
    }
  ],
  "tools": [
    {
      "server": "docs",
      "tool": "search",
      "bridgedName": "mcp__docs__search",
      "risk": "medium",
      "network": true,
      "filesystem": false,
      "requiresApproval": false
    }
  ]
}
```

`status` 可为 `unknown`、`ok`、`unavailable`、`circuit_open` 或 `disabled`。外部工具调用会写入 `mcp_external` trace span，diagnostics 包含 `latencyMs`、`transportLatencyMs`、`attempts`、`retryCount`、`timeout`、`errorType`。

## A2A Agent Mesh（v2.2.5）

- `GET /.well-known/agent-card.json`：A2A 发现端点（不鉴权，仅元数据），返回 orchestrator 的 Agent Card。
- `GET /a2a/agents`：全部本地 Agent 的 Card（orchestrator / researcher / coder / reasoner / critic）。
- `POST /a2a` 与 `POST /a2a/agents/{agentId}`：A2A JSON-RPC 端点。方法：
  - `message/send`：提交任务并立即返回 Task（`id`、`contextId`、`status`、`history`、`artifacts`、`artifactChunks`、`kind`），后台经该角色 capability 切片执行。
  - `message/stream`：以 SSE 推送 JSON-RPC 响应流，先发 Task 快照，再发 `artifact-update` / `status-update`。artifact chunk 包含 `artifactId`、`chunkIndex`、`append`、`final` 与 `artifact.parts[]`。
  - `tasks/resubscribe`：用已有 `id` 重新接入 SSE；可带 `afterChunkIndex`，只补发该游标之后的 artifact chunks，并继续推送状态直到终态。
  - `tasks/get`（可带 `historyLength`）/ `tasks/cancel` / `tasks/list`：任务查询、取消（云端请求可能无法硬中断，但结果会被丢弃并记录 `discardedResult`）与最近任务列表。
- 任务状态机：`submitted -> working -> completed | failed | canceling -> canceled`；快照持久化在 `.a2a/`，重启后磁盘上残留的非终态任务读取时标记 `failed`。错误码：`-32001` 任务不存在、`-32002` 任务不可取消，其余同 JSON-RPC 标准。
- 兼容性冒烟：`python scripts/smoke_a2a_compat.py --token <local-token>` 会按 Agent Card、`message/send`、`message/stream`、`tasks/resubscribe`、`tasks/cancel` 走一遍 live contract；离线回归见 `tests/test_a2a_compat_contract.py`。

## GET `/api/taint`（Context Taint 防火墙，v2.1.5）

返回 Context Taint Tracking 与 Prompt Injection Firewall 的配置状态：信任层级与来源分类（`trusted_system` / `trusted_user` / `trusted_memory` / `untrusted_web` / `untrusted_file` / `untrusted_tool_result`）、隔离加固开关、升级确认开关与敏感工具清单。每轮 `/api/chat` 的 `diagnostics.contextTaint` 给出该请求的分段报告：各段来源 / 字符数 / 注入 / 密钥外泄 / 工具指令命中数与整轮 `tainted` 判定；命中后该轮 `diagnostics.toolPolicy` 会出现 `tainted: true` 与 `taint_escalated_confirmation` / `secret_exfiltration_blocked` 拦截。

## POST `/api/semantic-cache`

语义缓存管理接口。请求体：

```json
{"action": "clear"}
```

`action=status` 会返回当前状态；`action=clear` 会清空 `.semantic-cache/cache.sqlite3` 中的缓存条目。

## GET `/api/gateway/status`

返回 API 网关层状态，不会触发请求重试或清理。响应结构：

```json
{
  "ok": true,
  "gateway": {
    "contextManager": {
      "enabled": true,
      "stableJson": true,
      "toolOrder": "function.name",
      "slidingWindowMessages": 36
    },
    "requestQueue": {
      "enabled": true,
      "available": true,
      "dbPath": ".request-queue/queue.sqlite3",
      "maxAttempts": 6,
      "counts": {"succeeded": 3}
    }
  }
}
```

可用环境变量：`GATEWAY_CONTEXT_MANAGER_ENABLED`、`GATEWAY_CONTEXT_WINDOW_MESSAGES`、`GATEWAY_REQUEST_QUEUE_ENABLED`、`GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS`、`GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS`、`GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS`。

云端 DeepSeek 请求遇到断网、超时、HTTP 408/425/429/502/503/504 时会写入本地 SQLite 队列并退避重试；HTTP 500 带明确上游错误体时会直接返回错误，避免流式客户端长时间等待。

## POST `/api/title`

v0.9.4 新增的 best-effort 标题生成接口。前端在首轮 assistant 回复完成后异步调用，用轻量模型把首轮用户问题和助手摘要整理为短标题；失败、离线、限流或用户已手动改名时保留原本标题。

请求体为 JSON：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `apiKey` | string | 否 | 未提供时使用服务端 `DEEPSEEK_API_KEY`。 |
| `titleModel` | string | 否 | 标题生成模型；非法值回退到轻量默认模型。 |
| `userMessage` | string | 是 | 首轮用户问题，后端最多取前 1200 字符。 |
| `assistantMessage` | string | 否 | 首轮助手回复摘要，后端最多取前 600 字符。 |

响应：

```json
{"title": "搜索引用修复"}
```

同一 API key 哈希在 60 秒内最多 12 次；超限返回 HTTP 429，前端静默回退到本地标题。

## POST `/api/chat`

请求体为 JSON。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `apiKey` | string | 否 | 未提供时使用服务端 `DEEPSEEK_API_KEY`。 |
| `model` | string | 否 | 默认 `deepseek-v4-pro`；支持 `fast`、`expert` 等别名。 |
| `messages` | array | 是 | user/assistant 消息对象列表。 |
| `stream` | boolean | 否 | `true` 时返回 NDJSON 流事件。 |
| `agentMode` | boolean | 否 | `true` 且 `stream=true` 时启用 Leader + 多 Agent 编排；非流式请求忽略该字段并保持普通回答路径。 |
| `systemPrompt` | string | 否 | 稳定系统提示词。 |
| `contextSummary` | string | 否 | 旧历史压缩摘要。 |
| `searchEnabled` | boolean | 否 | 是否允许搜索。 |
| `searchMode` | string | 否 | 搜索模式：`off`、`auto`、`on`。 |
| `tavilyApiKey` | string | 否 | 本次请求使用的 Tavily API Key；未提供时使用服务端 `TAVILY_API_KEY`。 |
| `memoryEnabled` | boolean | 否 | 是否启用长期记忆，默认启用。 |
| `memoryScope` | string | 否 | 当前长期记忆作用域：`global`、`project:<id>` 或 `seek:<id>`；未提供时后端会从最新 user 消息的 `projectId` / `seekId` 推断。 |
| `continuationContext` | string | 否 | 继续生成的本轮上下文。 |
| `thinkingEnabled` | boolean | 否 | 控制 DeepSeek 思考相关字段。 |
| `reasoningEffort` | string | 否 | 思考强度：`low`、`high`、`max`；后端还兼容 `minimal` / `medium`，非法值回退到 `high`。 |
| `toolsEnabled` | boolean | 否 | 是否允许模型调用本地工具，默认允许；设为 `false` 时不发送 `tools` 字段。 |
| `semanticCacheEnabled` | boolean | 否 | 是否允许本轮查本地语义缓存；默认按服务端 `SEMANTIC_CACHE_ENABLED` 决定，传 `false` 可禁用本轮缓存。 |
| `autoRoute` | boolean | 否 | 开启 Model Router 自动选模（按能力/成本/延迟在 flash/pro 间路由）；也可用 `model:"auto"`。显式 `model` 时不路由。 |
| `cascade` | boolean | 否 | 级联推理：先用便宜模型出草稿，过质量门控则返回，否则升级到贵模型精算（流式请求由服务端把级联结果回放成流事件）。 |
| `judge` | boolean | 否 | 级联质量门控额外用 Judge 模型对草稿打分；也可由服务端 `MODEL_ROUTER_JUDGE_ENABLED` 默认开启。 |
| `budget` | object | 否 | 本轮预算覆盖：`{max_total_tokens, max_agent_tokens, max_search_calls, max_tool_calls, max_estimated_cost_usd}`（缺省回退服务端默认；0 表示不限）。 |
| `budgetPolicy` | string | 否 | `downgrade_to_flash_when_exceeded` 时，所属 scope（项目/记忆 scope）当日超预算会自动降级到便宜模型；默认 `none`。 |
| `edgeMode` | string | 否 | 端云路由模式：`auto` 默认自动路由；`local` 强制端侧模型；`cloud` / `off` 强制云端。 |
| `edgeModelPath` | string | 否 | 可选 GGUF 路径覆盖；只有服务端设置 `EDGE_ALLOW_MODEL_PATH_OVERRIDE=1` 时生效。 |

v1.7.5 新增 Edge Inference Infra。服务端可通过 `EDGE_INFERENCE_ENABLED=1`、`EDGE_INFERENCE_PROVIDER=llama_cpp`、`EDGE_MODEL_PATH=<*.gguf>` 启用本地 GGUF 模型；推荐使用 DeepSeek-R1-Distill 1.5B/7B 的 4-bit 量化文件。`auto` 模式下，简单闲聊、概括、改写、翻译等任务会优先走端侧模型；代码、数学、联网、PPT/文档/思维导图、多 Agent 和带图片任务仍走云端 DeepSeek-V3/R1 路径。云端连接失败时，简单任务会尝试本地回退。`diagnostics.edgeInference` 会记录本轮是否走端侧、provider、路由原因、量化标记和回退错误。

v1.7.7 新增本地 trace 与语义缓存诊断。普通 JSON 响应和流式 `done` 事件都会在 `diagnostics.traceId` 中返回本轮 trace ID；前端 `Trace` 按钮会用该 ID 读取 `/api/traces/{traceId}`。当请求满足“无工具、无搜索、无附件”条件时，后端会先查本地语义缓存，`diagnostics.semanticCache` 会记录 `checked`、`hit`、`similarity`、`threshold`、`cacheId`、`skippedReason` 和 `stored` 等字段。命中缓存时不会请求 DeepSeek API，响应内容来自 `.semantic-cache/cache.sqlite3`。

v1.8.0 新增 Gateway & Resiliency 诊断。DeepSeek 云端请求在发送前会经过 Context Manager：稳定 system prompt 前缀、按 `function.name` 固定工具定义顺序，并使用稳定 JSON 序列化请求体；当已有 `contextSummary` 时可启用滑动窗口。普通 JSON 响应和流式 `done` 事件会在 `diagnostics.contextManager` 返回是否启用、工具顺序、是否应用滑动窗口、丢弃消息数等字段。上游请求通过 SQLite 队列 `.request-queue/queue.sqlite3` 记录，断网、超时、HTTP 408/425/429/502/503/504 会退避重试；`diagnostics.gatewayResiliency` 返回本轮上游请求数、尝试次数、重试次数、最后队列 ID、最后状态和最后错误。

## GET `/api/edge/status`

返回端侧推理能力摘要，不会主动加载模型。

响应字段与 `/api/config.edgeInference` 一致，包含 `enabled`、`provider`、`providerSupported`、`available`、`dependencyAvailable`、`modelPathConfigured`、`modelPathExists`、`modelPathSuffixSupported`、`modelName`、`loaded`、`quantization`、`nCtx`、`nThreads`、`nGpuLayers`、`maxTokens`、`allowModelPathOverride` 和 `suggestions`。该接口只做状态检查，不会加载 GGUF / MLC 模型。

## POST `/api/edge/route-preview`

v2.7.3 新增的 Edge dry-run 路由解释接口。请求体使用与 `/api/chat` 相同的核心字段，例如 `messages`、`edgeMode`、`searchMode`、`agentMode` 和附件元数据。接口会返回本轮是否会走端侧、为什么走端侧或云端，以及当前 Edge 状态；不会调用云端 API，也不会加载本地模型。

请求示例：

```json
{
  "edgeMode": "auto",
  "messages": [{"role": "user", "content": "Summarize this note."}]
}
```

响应示例：

```json
{
  "useEdge": true,
  "reason": "simple_task_local",
  "mode": "auto",
  "provider": "llama_cpp",
  "status": {
    "enabled": true,
    "available": true,
    "providerSupported": true,
    "quantization": "Q4_K_M",
    "suggestions": []
  }
}
```

常见 `reason`：`simple_task_local`、`complex_task_cloud`、`unsupported_payload`、`edge_unavailable`、`cloud_forced`、`local_forced`、`cloud_unavailable_simple_local`。当 `edgeMode=local` 但端侧不可用时返回 HTTP 409，并在错误信息中给出缺依赖、模型路径、后缀或 provider 配置建议。

## POST `/api/edge/reload`

释放当前已加载的端侧模型实例，并返回最新 `edgeInference` 状态。请求体可传：

```json
{"action": "unload"}
```

`action=reload` 也会先卸载当前模型；下一次端侧请求会按当前环境变量或允许的请求覆盖路径重新懒加载。

Seek 助手和公式渲染都是前端本地能力，不新增独立后端路由。前端会把当前消息对应 Seek 的名称、简介、专属指令和参考文件名称合并进 `systemPrompt`；继续生成、重新生成、编辑后重发和上下文压缩都会使用消息快照里的 Seek 指令和参考文件，避免串用当前全局选择。用户可以在输入区或 Seek 卡片中停用当前 Seek；停用后请求会回到普通角色提示词。前端还会追加公式输出约束，引导模型在数学、物理、统计和工程问题中使用 `\( ... \)`、`\[ ... \]` 或 `$$...$$` 形式的 LaTeX，并由本地 KaTeX 渲染为页面 HTML。

自定义 Seek 的导入/导出也在前端完成，不新增 API。导出文件是 JSON，包含 `type=deepseek-mobile.seeks`、`version`、`exportedAt` 和 `seeks` 数组；v2 导出会在每个 Seek 中保留 `referenceAttachments`。导入时前端会重新规范化字段、处理重名和 ID 冲突，再写回浏览器 `localStorage`。

Seek 参考文件使用现有附件协议。编辑 Seek 时先通过 `/api/file-text` 上传并获得 `fileId`；发送聊天请求时，前端只把参考文件合并到对应 user 消息的 `attachments` 中，让后端按用户问题检索相关片段。assistant 消息不会展开参考文件，避免把同一份资料重复注入历史。
项目空间同样使用附件协议。当前项目的文档会随 user 消息快照写入 `projectAttachments`，发送给后端时合并到 `attachments`，并带上 `projectId`，后端会从 `.projects/{projectId}/files/` 读取持久索引。

后端会自动把当前本地时间和 UTC 时间作为 `[Current time]` dynamic context 追加到本轮请求尾部。前端不需要传新字段；这个动态块用于回答“今天”“明天”“现在几点”等相对时间问题，并避免把每分钟变化的时间写进稳定 system prompt。

v0.7.4 的命令面板、快捷键、主题、字号、代码块折叠、公式复制、Mermaid 轻量 flowchart 渲染和表格 SVG 图表均为前端能力，不新增后端 API。PWA 离线模式只在 `/api/config` 无法读取时让页面降级为历史查看壳；离线状态下前端不会发起 `/api/chat` 发送。
v0.8.2 的语音输入、回复朗读和“引用所选”都属于前端能力：语音输入使用浏览器 `SpeechRecognition` / `webkitSpeechRecognition` 写入输入框，回复朗读使用 `speechSynthesis` 分句播放助手消息，选取聊天消息片段提问复用本地引用草稿，不新增模型请求字段。语音语言和引用草稿保存在浏览器本地状态中。v0.8.3 的 PWA 图标和 favicon、v0.8.4 的动效反馈与流式渲染节流、v0.8.5 的思考状态文案和选区引用稳定性修复、v0.8.6 的思考计时和流式期间草稿交互优化、v0.9.0 的侧边栏与历史列表重构也都是前端能力。v0.9.1 新增 `reasoningEffort` 请求字段，并强化本地工具 schema 与工具回合转发；v0.9.2 只扩展 `/api/config` 的 `uploadLimits` 字段并统一上传限制；v0.9.3 将联网搜索改为模型驱动的 `web_search` 工具循环，并把整段提问入口替换为选区浮动引用提问；v0.9.4 新增 `/api/title`，同时让网页引用使用 `[^Wn]` chip，并在本地 timeline 中交错展示 reasoning 与搜索步骤；v0.9.6 修复搜索 timeline 收尾和引用去重，并扩展本地工具集；v1.0.0 的视觉风格、明暗模式和主题 token 都是纯前端状态，不新增后端接口。v1.0.1 调整搜索编排和前端搜索状态恢复。v1.1.1 继续只调整前端主题 CSS。v1.1.5 新增流式 `agentMode` 请求字段和 `agent` 事件，同时对搜索工具循环增加硬预算。v1.6.6 进一步放宽选区引用条件，只要选区实际命中单条用户或助手消息气泡即可引用，并修复触屏点击被 `touchstart` 吞掉的问题。PWA Share Target 会使用下面的 `/share-target` 和 `/api/share-target` 两个入口把系统分享内容导入为草稿。

非流式响应包含：

- `content`：最终回答。
- `reasoning`：模型推理内容。
- `usage`：模型 token 使用量。
- `diagnostics`：请求诊断信息，包括消息数、摘要长度、记忆/搜索命中、附件数量、本地工具调用次数、缓存命中 token、命中率、`contextManager`、`contextEngine`、`gatewayResiliency`；多 Agent 模式还会包含 `agentDurations` worker 耗时表。
  - **Model Router（v2.0.9）**：`autoRoute`/`model:"auto"` 时带 `modelRouter`（`{model, tier, capability, fallbackModel, reasons:[{router,decision}]}`，路由维度含 capability/cost/latency）。级联请求带 `modelCascade`（`{escalated, draftModel, refineModel, gate:{passed,score,reasons}, judge, judgeScore?}`）。`/api/config.modelRouter` 暴露 `enabled`/`cascadeEnabled`/`judgeEnabled`/`draftModel`/`refineModel`。
  - **Cost & Budget（v2.0.10）**：每轮诊断带 `costUsd`（按模型定价从 token usage 估算的美元成本）；多 Agent 带 `agentCostUsd` 与 `agentTokenByAgent`（每 Agent token）。启用降级策略时带 `budgetPolicy` 与 `budgetDowngraded`。每次上游模型调用按 scope（项目/记忆 scope）累计到本地**每日**账本（`.budget/budget.sqlite3`），`GET /api/budget?scope=<scope>` 返回 `{enabled, pricing, policy, today:{totalTokens,costUsd,modelCalls,searchCalls,toolCalls}, overBudget}`，`/api/config.budget` 给全局视图。可经 `BUDGET_*` 环境变量配置定价、预算上限与策略。
- `search`：如本轮触发搜索，则返回面向前端展示的搜索信息。
- `memorySuggestions`：如模型调用 `suggest_memory`，返回待用户确认的记忆建议列表。

### `diagnostics.contextEngine`（v2.0.4 起）

Prompt-cache-aware Context Engine 的只读观测块（后端组装，前端忽略未知字段即可）：

- `tokenBudget`：本轮 prompt 的 token 预算预估。`contextWindow` 为按模型查表得到的上下文窗口（`deepseek-v4-*` 默认 131072，端侧 / Ollama / 未知模型回落到默认窗口），`reservedOutputTokens` 为给补全预留的余量，`availableInputTokens = contextWindow - reservedOutputTokens - 安全余量`；`estimatedPromptTokens` 与 `breakdown`（`system` / `tools` / `history` / `dynamic`）为无 tokenizer 的确定性估算（CJK 与拉丁字符分别加权），`utilizationPct`、`headroomTokens`、`withinBudget` 与 `recommendation`（`ok` / `compress` / `trim`）给出预算结论。
- `contextDiff`：相对稳定前缀的本轮上下文构成。`baseContextId` 是「角色提示 + 模型名 + 工具名序列」这段缓存锚点的哈希，跨轮稳定——它一旦变化即提示前缀发生了会导致缓存失效的漂移；`delta` 描述本轮叠加的内容（history 条数、trailing dynamic 字符数、工具数、以及发生 token 感知裁剪时的 `droppedMessages`）。

token 预算只做观测与裁剪决策，**不**改写缓存锚定的 prompt 前缀字节。当且仅当已存在压缩摘要、触发滑动窗口、且估算仍溢出预算时，引擎才在消息条数窗口之上**额外**丢弃最旧历史（`contextManager.tokenAwareTrimApplied=true`），始终保留首条 system 前缀与尾部 dynamic context。可经 `CONTEXT_ENGINE_*` 环境变量调参或关闭。

当普通对话触发本地工具调用时，后端可能会向 DeepSeek 发起多次上游请求。v1.6.0 起，`usage` 和 `diagnostics.cacheHitTokens` / `cacheMissTokens` / `cacheHitRate` 会聚合本轮所有上游请求，而不是只取最后一次最终回答请求，避免工具调用后缓存命中率被误显示为 0%。

流式响应使用 `application/x-ndjson`，每行是一个 JSON 事件：

| `type` | 字段 |
| --- | --- |
| `reasoning` | `text` |
| `system_note` | `text` |
| `content` | `text` |
| `search` | `search` |
| `agent` | `phase`、`status`、`name`、`text` |
| `agent_reasoning` | `phase`、`name`、`text` |
| `agent_delta` | `phase`、`name`、`text` |
| `agent_note` | `phase`、`name`、`text` |
| `agent_search` | `phase`、`name`、`search` |
| `memory_suggestion` | `content`、`category`、`scope`、`conflicts` |
| `done` | `id`、`model`、`content`、`reasoning`、`usage`、`search`、`diagnostics`、`memorySuggestions` |
| `error` | `error`、`code` |

如果上游 SSE 返回 `event: error`，后端会转换为 `type=error` 的前端事件。

流式请求会在发送 NDJSON 头之前完成快速 payload 校验。缺少 API Key、空消息、没有 user 消息或超过硬限制且没有压缩摘要时，接口返回普通 JSON 错误和对应 4xx 状态，而不是 200 流式错误事件。`context_compression_required` 使用 HTTP 409，表示前端需要先调用 `/api/compress-context`。

v0.7.2 起 `/api/chat` 默认会把本地工具定义随请求发送给 DeepSeek；v0.7.3 增加长期记忆建议工具。v0.9.6 后当前内置工具包括：

| 工具 | 说明 |
| --- | --- |
| `python_eval` | 执行小型、无副作用的 Python 数学表达式，例如阶乘、组合数、平方根。 |
| `search_files` | 跨 `.file-cache` 和 `.projects` 检索已缓存附件/项目文档片段。 |
| `fetch_url` | 读取一个公共 http(s) URL 的正文，用于搜索结果二次精读。 |
| `web_search` | 执行单轮 Tavily 联网搜索，返回可引用的 `[^Wn]` 来源。 |
| `suggest_memory` | 生成一条待用户确认的长期记忆建议，不会直接写入 `.memory`。 |
| `create_reminder` / `list_reminders` | 创建本地提醒或列出 active/notified/all 提醒；`dueAt` 必须是 ISO datetime。 |
| `recall_memory` / `forget_memory` | 检索或按非空 substring 删除允许作用域内的本地长期记忆；删除默认限制在全局和当前项目/Seek 作用域。 |
| `list_project_files` / `read_file_chunk` | 列出项目文档库文件，或按 `fileId`、`projectId`、`chunkIndex` 读取一个缓存 chunk。 |
| `data_transform` | 执行 `extract_regex`、`json_path`、`csv_summary`、`number_summary` 四种白名单数据处理，不执行代码。 |
| `generate_chart` | 校验图表数据并返回 `{type,title,data,markdownTable}`；模型应把 `markdownTable` 放入最终回答以复用前端表格图表按钮。 |
| `create_mindmap` | 根据 `title`、可选 `subtitle` 和树状 `nodes` 生成可下载 SVG 思维导图；用于“思维导图 / 脑图 / mind map”请求，最终回复会用 Markdown 图片语法在正文中显示。 |
| `create_pptx` | 根据标题和分页大纲生成真实 `.pptx` 演示文稿，并返回 `/api/download` 链接。 |
| `create_document` | 根据结构化章节生成 `.docx` 或 `.pdf` 文档，并返回 `/api/download` 链接。 |
| `compare_search_results` | 最多执行 2 个相关联网搜索 query，复用同一 turn 的搜索 timeline、引用编号和去重逻辑。 |

工具调用最多连续执行 3 轮；普通对话本轮搜索最多 5 次。安全的相邻工具会并行执行，但结果按原 `tool_calls` 顺序返回；`create_reminder`、`forget_memory`、`suggest_memory` 等副作用工具保持串行。流式模式下，执行工具前后会额外发送 `system_note` 事件；这些事件是后端流程提示，不属于模型 `reasoning`。多 Agent 模式下，Researcher 可联网搜索，Coder 只能使用本地代码/文件工具，Reasoner 和 Critic 默认无工具；worker content、reasoning、system note、search 会分别转成 `agent_delta`、`agent_reasoning`、`agent_note`、`agent_search`，并按 `phase` 显示在 Activity Agent 卡片内。v1.2.9 不改变流式协议，只修正前端对持久化 `durationMs: null` 的恢复语义，避免刷新后误显示 `0ms`。v1.3.0 在多 Agent `done.diagnostics` 中新增 `agentDurations`，按 worker id 记录本轮执行耗时（毫秒），方便性能分析和导出报告对照。

v1.3.5 不改变 `/api/chat` 的事件协议；前端会把 `agentMode` 固化到当前 assistant message，用于稳定 75 分钟请求超时和 Activity 展示判断。后端多 Agent 层级超时仍可通过 `MULTI_AGENT_TIMEOUT_SECONDS` 配置，默认 `3900` 秒。多 Agent worker 请求的动态 prior context 和当前子任务现在追加在历史消息之后，不再进入 `systemPrompt`，以便 DeepSeek prefix cache 更容易复用稳定系统提示和长历史前缀。

v1.3.6 继续不改变 `/api/chat` 事件协议；多 Agent worker 的角色职责和工具/搜索约束也从 `systemPrompt` 后移到历史消息之后。所有 worker 在同一轮请求中共享统一 system prompt，只有历史对话之后的最后一条动态 user message 会因 Researcher / Coder / Reasoner / Critic 角色不同而分叉。

v1.3.7 在多 Agent 最终 `done.diagnostics` 中新增 `agentCache`，聚合 worker 和 Synthesizer 的 DeepSeek cache usage：`hitTokens`、`missTokens`、`hitRate` 和 `byAgent` 明细。顶层流式事件协议不变；普通单请求的 `usage` 和 cache diagnostics 仍按原字段返回。

v1.3.8 为 `agentCache` 与每个 `byAgent` 明细补充 `totalTokens` 和 `hasData`。当没有 cache usage 数据时，`hitRate` 为 `null`、`hasData=false`；当确实全部 miss 时，`missTokens > 0` 且 `hitRate=0.0`。这只改变 diagnostics 语义，不改变顶层流式事件类型。

v1.3.9 不改变 API 字段，只调整前端诊断面板显示：Agent cache 标签中文化，`byAgent` 明细改为多行展示。

v1.4.0 将多 Agent Researcher 搜索预算提高到单 Agent 5 次、单次 Agent Run 总预算 12 次，worker 工具循环提高到 4 轮；普通 `/api/chat` 的搜索上限保持 5 次。

Unreleased 的 `/api/config` 新增 `ocr` 摘要对象，只下发 OCR 配置状态，不探测本机 OCR 引擎。v1.6.6 的 `/api/chat` 请求组装会在尾部 dynamic context 注入当前本地时间和 UTC 时间；桌面 WebView 首屏认证可使用 `desktop=1` token 入口直接写入 Cookie 并返回首页。v1.6.3 的 Windows 桌面端默认改为本地应用窗口，内嵌 WebView 仍访问同一组 `127.0.0.1` 本机 HTTP 路由和 token Cookie。v1.6.2 的 Android APK 内 OCR 会通过原生 ML Kit 桥接实现，仍复用 `/api/file-text` 和项目文件上传里的 `ocrEnabled=1` 字段。v1.6.1 中，模型主动调用 `web_search` 时，后端会保留工具交换中的上游原始 `tool_call_id` 和参数 JSON，让下一轮请求能匹配上一轮模型输出末尾的 DeepSeek prompt cache；工具结果仍用稳定 JSON，并让单轮联网搜索工具复用 `.search-cache`，减少工具结果后的提前分叉。v1.6.0 手机本机运行只新增启动入口和依赖清单，服务启动后仍使用同一组 HTTP 路由、本地 token Cookie 和 `/api/config` 能力下发。v1.5.1 开启搜索时，`WEB_SEARCH_SYSTEM_HINT` 会随搜索结果一起放入本轮尾部 dynamic context，不再改写首个 system message；这能让 DeepSeek prompt cache 更稳定地复用系统提示和长历史前缀。Activity 复制、Escape 关闭面板和焦点陷阱栈均为前端交互修复，不改变 `/api/chat` 或 Agent Run 事件协议。

## Agent Run API

v1.4.0 新增可恢复 Agent Run；v1.5.1 保持这些接口兼容。普通 `/api/chat` 保持兼容；新的恢复、断线续接、计划确认和重跑能力只用于 Agent Run。

### POST `/api/agent-runs`

创建一个持久化多 Agent run。请求体：

```json
{
  "payload": {},
  "confirmPlan": false,
  "agentPreset": "full"
}
```

`payload` 使用 `/api/chat` 的流式请求字段，服务端会强制 `agentMode=true`。`apiKey` / `tavilyApiKey` 只用于本次启动，不会写入 `.agent-runs/`。

响应：

```json
{"ok": true, "runId": "run_xxx", "run": {"runId": "run_xxx", "status": "created"}}
```

状态流为 `created -> planning -> awaiting_plan/running -> done/failed/cancelled/orphaned`。手动 `full` 默认直接执行；`confirmPlan=true`、`agentPreset=auto` 或高复杂任务会在 Leader 产出计划后进入 `awaiting_plan`。

### GET `/api/agent-runs/{runId}`

返回 run 快照和完整事件日志。`events` 是恢复 UI 的唯一事实源；`finalAnswer`、`agentOutputs`、`diagnostics`、`nodes` 只是为了快速读取的派生快照。

v2.0.5 起快照新增 `nodes`：由 plan + 事件日志纯重放得到的节点级状态机，每个 worker 节点形如 `{"state": "succeeded", "attempts": 1, "latencyMs": 1200, "promptTokens": 800, "completionTokens": 200, "failed": false}`。节点状态机为 `created → queued → running → succeeded`，失败分支 `running → failed → retrying → running`，取消分支 `→ cancelled`；`created` = 依赖未满足，`queued` = 依赖已满足待执行。`nodes` 始终等于对 `events` 的重放结果，可安全丢弃重算。

### GET `/api/agent-runs/{runId}/events?after=N`

返回 `index > N` 的事件数组，用于断线后的轮询恢复。

### GET `/api/agent-runs/{runId}/stream?after=N`

返回 `application/x-ndjson` 事件流。连接建立后先 replay `index > N` 的历史事件，再等待后续事件；浏览器断开只关闭当前 stream，不取消后台 run。多个客户端可以同时 attach 同一个 run。

### POST `/api/agent-runs/{runId}/plan`

确认并可覆盖计划：

```json
{
  "payload": {},
  "plan": [{"id": "coder", "task": "检查实现路径"}]
}
```

计划项 `id` 支持 `researcher`、`coder`、`reasoner`、`critic`。确认后会发 `final_reset`，状态进入 `running` 并执行计划。

### POST `/api/agent-runs/{runId}/rerun`

重跑单个 worker 或只重新综合：

```json
{
  "payload": {},
  "agentId": "coder",
  "resynthesize": true
}
```

`agentId` 可为 worker id，也可用 `synthesizer` 只重新综合最终回答。重跑 worker 会先发：

```json
{"type": "agent_reset", "phase": "coder", "reason": "rerun_agent"}
```

随后替换该 Agent 输出；若 `resynthesize=true`，再发：

```json
{"type": "final_reset", "scope": "final_answer", "reason": "rerun_agent"}
```

1.4.0 不做依赖级联重跑：例如重跑 Researcher 不会自动重跑 Coder / Reasoner / Critic，最终回答会基于最新 Researcher 和现有其它 Agent 输出重新综合。

### POST `/api/agent-runs/{runId}/resume`

断点续跑（v2.0.5）。对被中断的 run（`orphaned` / `failed` / `cancelled` / `done`）从最近检查点恢复：

```json
{"payload": {}}
```

服务端调用 `/api/agent-runs/{run_id}/resume`，从事件日志重放节点状态机，**跳过已成功的节点**（把它们的持久化输出作为下游 `prior_outputs` 复用，幂等不重跑），只对未完成 / 失败的节点重跑，未完成节点会先发 `agent_reset(reason="resume")`，最后只综合一次。若所有节点都已成功：有正文则直接置 `done`，无正文则只重新综合。`running` / `planning` / `created` 状态拒绝（409）；`awaiting_plan` 需先确认计划。持久化 run 不存 `apiKey`，因此续跑请求需在 `payload` 带 key 或服务端配置 `DEEPSEEK_API_KEY`。

服务重启默认仍把中断的 run 标记为 `orphaned`、由用户手动续跑；设 `AGENT_RUNTIME_AUTO_RESUME=1` 可在启动时自动续跑所有 `orphaned` run（需要服务端 `DEEPSEEK_API_KEY`）。

## POST `/share-target`

PWA Share Target 接收入口。手机系统分享菜单会按 `static/manifest.webmanifest` 的 `share_target` 配置把标题、正文、URL 和文件以 `multipart/form-data` POST 到该路径。

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | string | 分享来源标题，可选。 |
| `text` | string | 分享正文，可选。 |
| `url` | string | 分享链接，可选。 |
| `files` | file[] | 分享文件，可选；复用 `/api/file-text` 的文件解析和 OCR 路径。 |

manifest 当前允许图片、文本、PDF、RTF、JSON、Markdown、CSV、DOCX、XLSX、PPTX 和 EPUB 类型进入分享菜单；服务端仍以附件解析白名单为准。

该入口不依赖 `Authorization` 头或 `auth_token` Cookie，因为 Android Chrome 的系统分享 POST 不会携带 `SameSite=Strict` Cookie。服务端仍会校验请求 `Host` 是否在本地白名单内。成功后后端会把分享内容写入短生命周期内存缓存，返回 `303 Location: /?share=<id>`；浏览器随后打开首页，由已鉴权的前端读取 `/api/share-target?id=<id>`，用户确认后才把内容填入草稿。缓存默认约 30 分钟过期，且被读取后立即删除。

## GET `/api/share-target`

读取并消费一次 PWA 分享缓存。

请求：

```text
GET /api/share-target?id=<share-id>
```

响应：

```json
{"ok": true, "share": {"prompt": "...", "attachments": [], "errors": []}}
```

`prompt` 会写入输入框草稿；`attachments` 会进入当前附件列表；`errors` 用于提示某些分享文件无法识别。找不到或过期时返回 404。
该读取端点仍属于 `/api/*`，需要本地 token 鉴权。

## POST `/api/auth/logout`

清除 `auth_token` Cookie 并返回：

```json
{"ok": true}
```

该端点仍需要当前请求通过本地鉴权。前端“清空本地数据”会先调用它，再删除浏览器保存的 DeepSeek Infra localStorage / sessionStorage 数据。

## POST `/api/conversations/search`

对浏览器传入的本地历史会话做全文搜索。服务端不持久化会话，只返回匹配结果，方便前端在大量历史中筛选。

请求体：

```json
{"query": "关键词", "conversations": []}
```

响应：

```json
{"results": [{"id": "conversation-id", "title": "标题", "tags": ["标签"], "matches": []}]}
```

前端仍会先做本地过滤；该接口用于统一搜索语义，并为后续服务端索引留出兼容入口。

## POST `/api/fetch-url`

读取一个公共网页并抽取可读正文，供前端或工具调用做搜索结果二次精读。请求体：

```json
{"url": "https://example.com/article"}
```

响应：

```json
{"ok": true, "page": {"url": "https://example.com/article", "contentType": "text/html", "text": "...", "charCount": 1234}}
```

该端点会拒绝非 http(s) URL、localhost、`.local` 域名、私有/回环/链路本地/保留地址和超过 2 MB 的页面。抓取结果写入 `.search-cache`，按搜索缓存过期时间复用。

## POST `/api/projects`

管理本地持久项目空间。项目数据保存在 `.projects/{projectId}/project.json`，项目文档索引保存在 `.projects/{projectId}/files/`，不会被临时 `.file-cache` 清理任务删除。该接口是兼容旧前端的 action API；v2.6.0 起新开发优先使用下面的 `/api/workspace/*` REST 风格接口。

请求体使用 `action` 字段：

| Action | 说明 |
| --- | --- |
| `list` | 返回项目列表。 |
| `create` | 创建项目，需要 `name`。 |
| `get` | 读取项目详情，需要 `id` 或 `projectId`。 |
| `rename` | 重命名项目或更新 `description`，需要 `id` 或 `projectId`。 |
| `delete` | 删除项目及其文档库，需要 `id`。 |

响应示例：

```json
{"projects": [{"id": "proj-abc123", "name": "考研资料", "documents": []}]}
```

## Workspace Core API

v2.5.0 新增 Workspace Core，将项目、保存项、产物和导出统一成工作台对象。完整设计见 [WORKSPACE.md](WORKSPACE.md)。

所有 `/api/workspace/*` 请求都走普通本地 API 鉴权。

### Project 2.0

`GET /api/workspace/projects` 返回项目列表，含 `projectId`、`description`、ISO 时间和 `stats`：

```json
{
  "ok": true,
  "projects": [
    {
      "projectId": "proj-abc123",
      "name": "考研408复习",
      "description": "复习计划",
      "stats": {"files": 12, "savedItems": 35, "artifacts": 8, "conversations": 4, "memories": 2}
    }
  ]
}
```

`POST /api/workspace/projects` 创建项目：

```json
{"name": "考研408复习", "description": "复习计划"}
```

`GET /api/workspace/projects/{projectId}` 返回项目详情，包含 `files` / `documents`、`conversations`、`memories`、`savedItems` 与 `artifacts`。`PATCH /api/workspace/projects/{projectId}` 可更新 `name` 与 `description`；`DELETE /api/workspace/projects/{projectId}` 删除该项目目录，不删除全局 `.generated/`、`.memory/` 或其它项目。

项目对话快照：

- `GET /api/workspace/projects/{projectId}/conversations`
- `POST /api/workspace/projects/{projectId}/conversations`

对话写入示例：

```json
{
  "conversationId": "conv-1",
  "title": "408 review",
  "messages": [
    {"id": "m1", "role": "user", "content": "解释调度算法"},
    {"id": "m2", "role": "assistant", "content": "RR 适合分时系统..."}
  ]
}
```

### Saved Items

保存项接口：

- `GET /api/workspace/projects/{projectId}/saved-items`
- `GET /api/workspace/projects/{projectId}/saved-items?type=chat_snippet&tags=408,OS`
- `POST /api/workspace/projects/{projectId}/saved-items`
- `PATCH /api/workspace/projects/{projectId}/saved-items/{savedId}`
- `DELETE /api/workspace/projects/{projectId}/saved-items/{savedId}`

创建示例：

```json
{
  "type": "chat_snippet",
  "title": "OS 调度总结",
  "content": "RR 适合分时系统...",
  "sourceRef": {"conversationId": "conv-1", "messageId": "m2"},
  "tags": ["408", "OS"],
  "purpose": "export_fragment"
}
```

`type` 支持 `chat_snippet`、`assistant_answer`、`file_quote`、`rag_citation`、`artifact`、`webpage`、`media`、`trace`、`eval_result`。`purpose` 支持 `reference`、`memory_candidate`、`export_fragment`。

### Artifact Hub

产物接口：

- `GET /api/workspace/projects/{projectId}/artifacts`
- `POST /api/workspace/projects/{projectId}/artifacts`
- `PATCH /api/workspace/projects/{projectId}/artifacts/{artifactId}`
- `DELETE /api/workspace/projects/{projectId}/artifacts/{artifactId}`
- `GET /api/workspace/artifacts/{artifactId}/preview?projectId=<projectId>`
- `GET /api/workspace/artifacts/{artifactId}/download?projectId=<projectId>`

注册示例：

```json
{
  "type": "markdown",
  "title": "复习提纲",
  "path": ".generated/summary.md",
  "source": {"conversationId": "conv-1", "messageId": "m3"}
}
```

`PATCH` 传 `title` 可重命名；传 `path` 会新增一个版本并更新当前下载路径。文本类产物预览会脱敏 API key、Bearer token 和 query token。

### Export

创建导出：

```http
POST /api/workspace/exports
```

```json
{"kind": "project", "projectId": "proj-abc123", "format": "zip"}
```

`kind` 支持 `conversation`、`project`、`saved_items`、`artifacts`、`evidence`；`format` 支持 `markdown`、`html`、`json`、`zip`。

响应：

```json
{
  "ok": true,
  "export": {
    "exportId": "export_abc123",
    "projectId": "proj-abc123",
    "kind": "project",
    "format": "zip",
    "filename": "project-project-export.zip",
    "downloadUrl": "/api/workspace/exports/export_abc123/download?projectId=proj-abc123"
  }
}
```

项目 ZIP 包固定包含 `metadata.json`、`project.md`、`conversations/`、`saved-items/saved-items.json`、`artifacts/`、`files/source-files/` 与 `traces/`。导出会脱敏 API key / auth token / password / secret 类字段。

## Skill System

Skill HTTP 入口走本地 token 鉴权：

```http
POST /api/skills
POST /api/skills/{skill_id}/run
```

`POST /api/skills` 是 action API，支持 `list`、`builtin`、`get`、`create`、`update`、`disable`、`enable`、`delete`、`import`、`export`、`run`。`run` 会校验 Skill input schema，加载可用项目上下文，按 `allowedTools` 进入统一 Tool Policy，再按 artifact policy 写入 Workspace。

运行示例：

```json
{
  "action": "run",
  "skillId": "skill_research_brief",
  "input": {"topic": "Skill System", "depth": "quick"},
  "projectId": "proj-abc123",
  "offline": true
}
```

`POST /api/skills/{skill_id}/run` 等价于 action `run`，适合外部客户端直接绑定某个 Skill。

## POST `/api/project-files?projectId=<id>`

接收 `multipart/form-data`，把文件解析并加入指定项目。支持与 `/api/file-text` 相同的文件类型、`ocrEnabled=1` 字段和可选 `apiKey` 字段；成功后返回新增文档列表：

```json
{"ok": true, "documents": [{"name": "notes.pdf", "fileId": "0123...", "projectId": "proj-abc123"}]}
```

## POST `/api/file-chunk`

按 `fileId`、可选 `projectId` 和 1-based `chunkIndex` 读取附件片段，用于前端引用回链预览。普通临时附件不传 `projectId`；项目文档传项目 id。

```json
{"fileId": "0123456789abcdef0123456789abcdef", "projectId": "proj-abc123", "chunkIndex": 2}
```

响应包含文件元数据和对应 chunk：

```json
{"file": {"name": "notes.pdf", "projectId": "proj-abc123"}, "chunk": {"index": 1, "text": "..."}}
```

## 文档原样阅读（豆包式阅读工作台）

上传 PDF / 图片 / 纯文本类附件后，前端在宽屏（≥960px）打开「原样预览」即进入文档阅读工作台：左侧是围绕该文档的对话与摘要，右侧是逐页渲染的原文阅读栏，支持翻页、缩放、文档目录缩略图、搜索、可选中文字层、截图框选、翻译全文与解释/翻译/复制/提问。这些能力由下面一组只读接口支撑，全部走普通 API 鉴权，并按 `fileId`（普通临时附件）或 `fileId + projectId`（项目文档）定位缓存。

### POST `/api/file-reader`

按窗口分段读取提取后的文本（文本阅读模式与不支持原样预览的格式回退用）。请求 `{"fileId": "...", "projectId": "", "chunkStart": 1, "chunkCount": 6}`，响应 `{"file": {...}, "window": {"chunkStart", "chunkEnd", "totalChunks", "hasPrevious", "hasNext"}, "chunks": [{"index", "text", "lineStart", "lineEnd"}]}`。

### GET `/api/file-source?fileId=<id>`

把原始上传文件按真实 MIME 原样返回，供右侧阅读栏直接加载（PDF / 图片 / 文本）。可选 `projectId`；带 `download=1` 时使用附件下载，否则 `inline` 内嵌预览。

### GET `/api/file-page-image?fileId=<id>&page=<n>&scale=<s>`

把 PDF 指定页渲染成 PNG（优先 PyMuPDF，回退 pdf2image）。`scale` 控制清晰度（约 0.35 缩略图、1.6 正常阅读）。响应是 `image/png`，并带 `X-File-Page`、`X-File-Page-Count` 头，供前端校正页码与总页数。缩略图侧栏与逐页主图复用同一接口。

### GET `/api/file-page-layout?fileId=<id>&page=<n>`

返回该页可选文字的归一化坐标，用于在页面图片上叠加透明可选文字层（实现选中→解释/翻译/复制/提问）。响应 `{"page": {"index", "pageCount", "width", "height", "text", "hasText", "words": [{"text", "left", "top", "width", "height"}]}}`，`left/top/width/height` 均为相对页面尺寸的百分比。

### GET `/api/file-page-search?fileId=<id>&query=<q>`

在各页提取文本里做关键字检索，用于阅读栏内搜索与命中高亮跳转。响应 `{"matches": [{"index", "page", "start", "end", "text", "snippet"}], "pageCount", "truncated"}`。

### POST `/api/file-page-text`

读取单页提取文本（文字层面板与按页解释/翻译）。请求 `{"fileId": "...", "projectId": "", "page": 3}`，响应 `{"page": {"text", "hasText", ...}}`；没有可复制文字的扫描页 `hasText` 为 `false`，前端引导改用截图框选提问。

## GET `/api/download?id=<fileId>`

下载本地生成文件。用于 `create_pptx`（PowerPoint）、`create_document`（Word `.docx` / PDF）和 `create_mindmap`（SVG 思维导图）工具生成的文件；请求仍走普通 API 鉴权。`id` 只接受 32 位十六进制字符串，服务端只解析 `.generated/<id>.{pptx,docx,pdf,svg}` 并按真实后缀返回对应 MIME，过期或不存在返回 `404`。SVG 可附带 `inline=1` 作为正文预览图加载，此时响应使用 `Content-Disposition: inline`；普通下载链接仍使用附件下载。

PPT 生成由 `/api/chat` 的 function calling 链路触发：用户要求“做 PPT / 幻灯片 / 演示文稿”时，后端会强制 `tool_choice=create_pptx`；如果上游模型只输出大纲没有工具调用，后端会基于最终文本兜底生成 `.pptx`，并在回复中追加 Markdown 下载链接。`create_pptx` 支持每页 `layout` 提示，生成器会自动加入目录页并在卡片、流程、对比、观点和总结版式间切换。

Word / PDF 生成由 `create_document` 工具完成：用户要求做 Word / PDF / 报告 / 方案 / 说明书等成文文件时，模型用 `format` 选择 `docx` 或 `pdf`，并把内容组织成带 `heading`、正文段落 `body`、要点 `bullets` 和可选 `table` 的章节；后端用 `python-docx` / `reportlab`（内置中文 CID 字体）渲染成带标题块、分章节、页码与配色主题的精排文件，并返回 Markdown 下载链接。与 PPT 不同，文档没有“漏调工具时从文本兜底”的链路，完全依赖模型主动调用工具。

思维导图生成由 `create_mindmap` 工具完成：用户要求画思维导图、脑图或 mind map 时，后端会强制 `tool_choice=create_mindmap`；模型只需要输出 `title`、可选 `subtitle` 和树状 `nodes`，后端会渲染为 `.svg` 并返回 Markdown 图片块。前端只对本地 `/api/download?id=<32 hex>` 图片块做内嵌预览，并在图下保留下载链接。

`create_pptx`、`create_document` 和 `create_mindmap` 都属于终态产物工具：工具执行成功后，后端会直接用本地结果生成最终下载回复，不再发起第二次 DeepSeek 上游请求；传给模型的工具结果也会压缩为下载元数据和简短结构摘要，避免完整大纲/正文重复进入下一轮 prompt 造成 cache miss。

## POST `/api/reminders`

本地提醒队列。使用 `action` 字段：

| Action | 说明 |
| --- | --- |
| `list` | 返回本地提醒列表。 |
| `create` | 创建提醒，需要 `title`、`content`、`dueAt`。`dueAt` 为 ISO datetime。 |
| `delete` | 根据 `id` 删除提醒。 |

提醒保存在 `.reminders/reminders.json`，不会发送给 DeepSeek。

## POST `/api/reminders/due`

返回已经到期且尚未通知的提醒，并把它们标记为已通知。前端定时轮询该接口，再通过 Service Worker 调用 Web Notification。

## POST `/api/file-text`

接收 `multipart/form-data`，支持一个或多个文件 part。可选字段 `ocrEnabled=1` 会为本次上传开启 OCR 重试；可选字段 `apiKey` 会作为本次 OCR 的 DeepSeek API Key，优先于服务端环境变量。图片上传走 OCR 路线，只提取图中的文字并作为 `kind=image` 附件缓存。HTML 会清洗脚本和样式后抽取可见文本；EPUB 会读取 HTML/XHTML 章节；PPTX 会读取幻灯片文本节点。

服务端依赖 `multipart>=1.3,<2` 的流式 parser。启动环境如果被不兼容的同名命名空间覆盖，接口会返回稳定 JSON 错误，而不是抛出未处理的 `AttributeError`。

响应示例：

```json
{"files": [], "errors": [], "file": null}
```

成功文件会包含：

- `fileId`
- `name`
- `kind`
- `preview`
- `charCount`
- `chunkCount`
- `size`
- `pageCount`（PDF 总页数，0 表示未知；文档阅读栏据此渲染全部页）
- `sourceAvailable`（是否保留了可原样预览的原始文件）

常见 `kind` 包括 `text`、`html`、`docx`、`xlsx`、`pptx`、`epub`、`pdf`、`image`。图片支持 PNG、JPG、WebP、BMP、TIFF、GIF 等常见格式；如果 OCR 未开启，图片会返回 `ocr_required`，前端可用同一个文件重试并附带 `ocrEnabled=1`。

如果所有文件都失败，首个文件错误会作为 HTTP 错误响应返回。常见错误码：

- `upload_too_large`
- `unsupported_file`
- `file_index_expired`
- `ocr_required`
- `ocr_unavailable`
- `ocr_empty`

## POST `/api/compress-context`

将较早的对话历史压缩成摘要。前端会传入已有摘要和新增待压缩消息，并把返回的摘要保存起来，供后续 `/api/chat` 使用。

当压缩请求来自带 Seek 助手的对话时，前端会把该消息快照中的 Seek 指令和参考文件名称放进 `systemPrompt`，保证压缩摘要与原对话助手语义一致。参考文件本体仍按附件检索逻辑处理，不会作为独立后端路由传入。

常见返回字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `summary` | string | 新摘要。 |
| `compressedMessageCount` | number | 本次参与压缩的消息数。 |
| `usage` | object | 压缩调用的模型 usage。 |

## GET/POST `/api/memory`

`GET /api/memory` 返回长期记忆列表。

`POST /api/memory` 使用 `action` 字段选择操作：

| Action | 说明 |
| --- | --- |
| `list` | 返回所有本地记忆。 |
| `add` | 根据 `content` 添加或更新记忆，可带 `category`、`scope`、`pinned` 和 `replaceIds`。 |
| `delete` | 删除与 `query` 匹配的记忆，可带 `scope` 限定在全局和当前作用域内删除。 |
| `deletebyid` | 根据 `id` 删除单条记忆。 |
| `clear` | 清空全部记忆。 |

`scope` 支持 `global`、`project:<id>` 和 `seek:<id>`。新增记忆如果与同作用域内已有记忆存在轻量冲突，接口会返回 HTTP 409：

```json
{"error": "Memory conflicts with an existing item", "code": "memory_conflict", "conflicts": []}
```

用户确认替换后，前端可把冲突项 id 放入 `replaceIds` 重新提交。
