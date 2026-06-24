# Implementation Status（实现状态矩阵）

适用版本：v2.2.1。

README 把 DeepSeek Infra 描述成一个 local-first agentic AI infrastructure platform。这一页回答一个更重要的问题：**每个模块到底落地到什么程度**——代码在哪、测试在哪、怎么亲手验证。所有链接都指向仓库内真实存在的文件；如果某格是 🟡 或 ❌，说明那部分还没做完，我们直接写出来，而不是让 README 替它画饼。

图例：

- **Status**：`Working` = 核心路径稳定多版本、测试覆盖深、可日常使用；`MVP` = 功能完整可用，但落地时间短 / 兼容性矩阵未铺开 / 接口可能演进；`Experimental` = 核心路径可用，协议/兼容性仍在活跃迭代，接口尚未承诺稳定。
- **Code / Tests / Demo**：✅ 完整；🟡 部分（缺口见备注）；❌ 未开始。

| # | Module | Status | Code | Tests | Demo |
| --- | --- | --- | --- | --- | --- |
| 1 | LLM Gateway | Working | ✅ [infra/gateway/](../deepseek_infra/infra/gateway/) | ✅ | ✅ |
| 2 | Agent DAG Runtime | Working | ✅ [infra/agent_runtime/](../deepseek_infra/infra/agent_runtime/) | ✅ | ✅ |
| 3 | Local RAG Data Layer | Working | ✅ [infra/rag/](../deepseek_infra/infra/rag/) | ✅ | ✅ |
| 4 | Tool Calling Runtime + Policy Engine | Working | ✅ [infra/tool_runtime/](../deepseek_infra/infra/tool_runtime/) | ✅ | ✅ |
| 5 | Observability & Trace | Working | ✅ [infra/observability/](../deepseek_infra/infra/observability/) | ✅ | ✅ |
| 6 | Edge-Cloud Model Router | Experimental | ✅ [infra/gateway/edge_inference.py](../deepseek_infra/infra/gateway/edge_inference.py) | 🟡 | 🟡 |
| 7 | MCP Tool Hub | Experimental | ✅ [infra/mcp/](../deepseek_infra/infra/mcp/) | ✅ | ✅ |
| 8 | A2A Agent Mesh | Experimental | ✅ [infra/agent_runtime/a2a.py](../deepseek_infra/infra/agent_runtime/a2a.py) | ✅ | 🟡 |
| 9 | Context Taint Firewall | Experimental | ✅ [infra/gateway/context_taint.py](../deepseek_infra/infra/gateway/context_taint.py) | ✅ | ✅ |

横切资产（不算独立模块，但支撑「可验证性」）：

| 资产 | 位置 | 状态 |
| --- | --- | --- |
| Evaluation Harness（RAG / Agent / Tool 三条评测线） | [evals/](../evals/) · 评分核心 [infra/evaluation/harness.py](../deepseek_infra/infra/evaluation/harness.py) | ✅ 全部离线可跑；CI 必跑 RAG + Tool Policy / Injection，Agent 录制评测暂为可选 |
| Benchmarks（延迟 / 缓存 / 检索 / DAG） | [benchmarks/](../benchmarks/) | ✅ 离线两项可直接复跑；在线两项需本地服务 + Key |
| 一键 Demo | [examples/](../examples/) · [docs/DEMO.md](DEMO.md) | ✅ |
| 部署资产（Docker / Compose / .env） | [Dockerfile](../Dockerfile) · [docker-compose.yml](../docker-compose.yml) · [docs/DEPLOYMENT.md](DEPLOYMENT.md) | ✅ CI 覆盖 `docker build` + `docker compose config` |
| 安全工程（威胁模型 / CI 扫描） | [docs/THREAT_MODEL.md](THREAT_MODEL.md) · [ci.yml security job](../.github/workflows/ci.yml) | ✅ |
| UI 截图 / Trace 瀑布图 | docs/assets/ | ✅ `trace-waterfall.png` / `agent-dag-run.png` / `rag-citation.png` / `mcp-tool-call.png` 入库；独立 `/trace/{id}` 只读页面已上线 |

---

## 各模块明细

### 1. LLM Gateway — Working

- **代码**：[openai_api.py](../deepseek_infra/infra/gateway/openai_api.py)（OpenAI 兼容 `/v1`）、[deepseek_client.py](../deepseek_infra/infra/gateway/deepseek_client.py)（上游调用 / 流式 / 工具循环）、[model_router.py](../deepseek_infra/infra/gateway/model_router.py)（策略路由 + 级联）、[scheduler.py](../deepseek_infra/infra/gateway/scheduler.py)（优先级队列 / 限流 / backpressure / DLQ）、[context_engine.py](../deepseek_infra/infra/gateway/context_engine.py)（token 预算 / prompt-cache 感知裁剪）、[semantic_cache.py](../deepseek_infra/infra/gateway/semantic_cache.py)、[budget_manager.py](../deepseek_infra/infra/gateway/budget_manager.py)、[resiliency.py](../deepseek_infra/infra/gateway/resiliency.py)（重试队列）、[providers/](../deepseek_infra/infra/gateway/providers/)（DeepSeek / Ollama 多 provider）。
- **测试**：[test_gateway_openai.py](../tests/test_gateway_openai.py) · [test_model_router.py](../tests/test_model_router.py) · [test_scheduler.py](../tests/test_scheduler.py) · [test_context_engine.py](../tests/test_context_engine.py) · [test_observability_semantic_cache.py](../tests/test_observability_semantic_cache.py) · [test_budget_manager.py](../tests/test_budget_manager.py) · [test_gateway_resiliency.py](../tests/test_gateway_resiliency.py) · [test_providers.py](../tests/test_providers.py)。
- **亲手验证**：[examples/openai_compatible_client.py](../examples/openai_compatible_client.py)（任意 OpenAI SDK 直连 `/v1`）；[benchmarks/bench_chat_latency.py](../benchmarks/bench_chat_latency.py)（TTFT / 总延迟）；[benchmarks/bench_semantic_cache.py](../benchmarks/bench_semantic_cache.py)（离线）。

### 2. Agent DAG Runtime — Working

- **代码**：[multi_agent.py](../deepseek_infra/infra/agent_runtime/multi_agent.py)（planner → DAG 拓扑分层 → 同层并行 → critic 修订 → synthesizer）、[agent_runs.py](../deepseek_infra/infra/agent_runtime/agent_runs.py)（事件源持久化 / 断线重放 / 断点续跑）、[agent_state.py](../deepseek_infra/infra/agent_runtime/agent_state.py)（节点级状态机）。
- **测试**：[test_multi_agent.py](../tests/test_multi_agent.py) · [test_agent_runs.py](../tests/test_agent_runs.py) · [test_agent_state.py](../tests/test_agent_state.py)。
- **亲手验证**：[examples/run_agent_dag_demo.py](../examples/run_agent_dag_demo.py)（实时打印 DAG 事件流）；[benchmarks/bench_agent_dag.py](../benchmarks/bench_agent_dag.py)；[evals/runners/run_agent_eval.py](../evals/runners/run_agent_eval.py)（录制 predictions 离线打分）。

### 3. Local RAG Data Layer — Working

- **代码**：[local_rag.py](../deepseek_infra/infra/rag/local_rag.py)（SQLite 索引 / BM25 + 向量 hybrid / 增量索引 / chunk lineage / 引用校验 / Recall@K 评估）、[files.py](../deepseek_infra/infra/rag/files.py)（解析 / 分块）、[context_compressor.py](../deepseek_infra/infra/rag/context_compressor.py)。
- **测试**：[test_local_rag.py](../tests/test_local_rag.py) · [test_files.py](../tests/test_files.py) · [test_context_compressor.py](../tests/test_context_compressor.py)。
- **亲手验证（全部离线、无需 Key）**：[examples/local_rag_demo.py](../examples/local_rag_demo.py)；[evals/runners/run_rag_eval.py](../evals/runners/run_rag_eval.py)（Recall@5 / Citation Accuracy）；[benchmarks/bench_rag_retrieval.py](../benchmarks/bench_rag_retrieval.py)。
- **边界（写清楚）**：默认零依赖跑哈希 embedding；`sqlite-vec` 向量表与 ONNX 本地 embedding 是可选增强（`requirements-rag.txt`），CI 只覆盖默认路径。

### 4. Tool Calling Runtime + Policy Engine — Working

- **代码**：[tools.py](../deepseek_infra/infra/tool_runtime/tools.py)（17 个本地工具）、[tool_policy.py](../deepseek_infra/infra/tool_runtime/tool_policy.py)（capability 切片 / schema 校验 / SSRF / 路径越界 / 密钥外泄 / 注入清洗 / 审计）、[search.py](../deepseek_infra/infra/tool_runtime/search.py)、[documents.py](../deepseek_infra/infra/tool_runtime/documents.py) / [presentations.py](../deepseek_infra/infra/tool_runtime/presentations.py) / [mindmaps.py](../deepseek_infra/infra/tool_runtime/mindmaps.py)（生成式产物）。
- **测试**：[test_tool_policy.py](../tests/test_tool_policy.py) · [test_tools.py](../tests/test_tools.py) · [test_search.py](../tests/test_search.py) · [test_documents.py](../tests/test_documents.py) · [test_presentations.py](../tests/test_presentations.py) · [test_mindmaps.py](../tests/test_mindmaps.py)。
- **亲手验证**：[evals/runners/run_tool_eval.py](../evals/runners/run_tool_eval.py)（离线重放策略闸门：Tool Policy Pass Rate + Injection Defense Pass Rate）；[examples/mcp_tool_demo.py](../examples/mcp_tool_demo.py)（经 MCP 真实调用工具）。

### 5. Observability & Trace — Working

- **代码**：[observability.py](../deepseek_infra/infra/observability/observability.py)（trace run / span 树、SQLite 持久化）、[trace_api.py](../deepseek_infra/infra/observability/trace_api.py)（`/api/traces` / `/trace/{id}` 路由）、[export.py](../deepseek_infra/infra/observability/export.py)（导出脱敏）、[metrics.py](../deepseek_infra/infra/observability/metrics.py)（Prometheus 文本）、[health.py](../deepseek_infra/infra/observability/health.py)（`/healthz` `/readyz`）、[trace_viewer.html](../static/trace_viewer.html) / [trace_viewer.js](../static/modules/trace_viewer.js) / [trace_waterfall.js](../static/modules/trace_waterfall.js)（独立只读页面）。
- **测试**：[test_observability_trace_tree.py](../tests/test_observability_trace_tree.py) · [test_observability_metrics.py](../tests/test_observability_metrics.py) · [test_server_integration.py](../tests/test_server_integration.py)。
- **亲手验证**：`curl http://127.0.0.1:8000/metrics`；前端每条助手消息的 Trace 按钮打开瀑布图（span 树按 parent 缩进）；`GET /trace/{trace_id}` 独立只读页面（本地 token 鉴权）；`GET /api/traces/{trace_id}/export.json` 导出脱敏 JSON。
- **展示资产**：[trace-waterfall.png](assets/trace-waterfall.png)、[agent-dag-run.png](assets/agent-dag-run.png)、[rag-citation.png](assets/rag-citation.png)、[mcp-tool-call.png](assets/mcp-tool-call.png) 已入库并由 README 首屏截图表引用。

### 6. Edge-Cloud Model Router — Experimental

- **代码**：[edge_inference.py](../deepseek_infra/infra/gateway/edge_inference.py)（任务分类 → 端 / 云路由，云端失败回退本地；llama-cpp / MLC 双后端）；多 provider 注册表 [providers/](../deepseek_infra/infra/gateway/providers/) 让 Ollama 模型经 `/v1` 暴露。
- **测试（🟡 的原因）**：路由决策、配置面与云失败回退在 [test_deepseek_request.py](../tests/test_deepseek_request.py) / [test_config.py](../tests/test_config.py) / [test_server_integration.py](../tests/test_server_integration.py) 有覆盖，但**真实端侧推理**需要可选依赖 + GGUF 模型文件，CI 不跑真模型。
- **亲手验证**：`EDGE_INFERENCE_ENABLED=1` + GGUF 后 `GET /api/edge/status`；或 `OLLAMA_ENABLED=1` 后 `GET /v1/models` 看到 `ollama/<tag>`。

### 7. MCP Tool Hub — Experimental

- **代码**：[server.py](../deepseek_infra/infra/mcp/server.py)（JSON-RPC 2.0：initialize / tools / resources / prompts）、[registry.py](../deepseek_infra/infra/mcp/registry.py)（17 工具 → MCP tools + 风险注解）、[permissions.py](../deepseek_infra/infra/mcp/permissions.py) + [adapters.py](../deepseek_infra/infra/mcp/adapters.py)（每个 tools/call 走 Tool Policy 闸门）、[client.py](../deepseek_infra/infra/mcp/client.py)（出方向 MCP client）。
- **测试**：[test_mcp.py](../tests/test_mcp.py)（11 项：握手 / 目录 / 能力切片 / 真实执行 / 错误码族 / 回环 client）。
- **亲手验证**：[examples/mcp_tool_demo.py](../examples/mcp_tool_demo.py)。
- **Experimental 的原因**：协议层与安全闸门完整，但尚未对 Claude Desktop / Cursor 等外部客户端逐一跑兼容性矩阵；外部 server 桥接（client 方向的目录合并进本地 Agent 工具面）在 Roadmap v2.3。

### 8. A2A Agent Mesh — Experimental

- **代码**：[a2a.py](../deepseek_infra/infra/agent_runtime/a2a.py)（Agent Card 发现、JSON-RPC 任务生命周期 `message/send|stream`·`tasks/get|cancel|list`、capability 隔离执行、`.a2a/` 持久化与重启对账、`A2AClient` 跨 Agent 委派）。
- **测试**：[test_a2a.py](../tests/test_a2a.py)（11 项）。
- **亲手验证**：`curl http://127.0.0.1:8000/.well-known/agent-card.json`（见 [docs/DEMO.md](DEMO.md) A2A 一节）。
- **Experimental 的原因**：任务执行是「提交即后台跑」+ SSE 快照推送；产物级流式增量（artifact streaming chunks）在 Roadmap v2.3，也尚未与第三方 A2A 实现互测。

### 9. Context Taint Firewall — Experimental

- **代码**：[context_taint.py](../deepseek_infra/infra/gateway/context_taint.py)（逐段信任打标 / 三类指令扫描 / 隔离加固）+ [tool_policy.py](../deepseek_infra/infra/tool_runtime/tool_policy.py)（密钥外泄硬拦截、污染轮升级确认）。
- **测试**：[test_context_taint.py](../tests/test_context_taint.py)（13 项）。
- **亲手验证**：[evals/runners/run_tool_eval.py](../evals/runners/run_tool_eval.py) 输出 Prompt Injection Defense Pass Rate；运行中 `GET /api/taint` 看防火墙状态。
- **Experimental 的原因**：检测基于确定性 pattern 族（中英），对抗性变体的系统化基准（注入语料库 + 绕过率量化）在 Roadmap v2.4。
