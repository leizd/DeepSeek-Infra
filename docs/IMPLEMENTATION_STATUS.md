# 实现状态

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：v4.2.0。

README 把 DeepSeek Infra 描述成一个 local-first agentic AI infrastructure platform。这一页回答一个更重要的问题：**每个模块到底落地到什么程度**——代码在哪、测试在哪、怎么亲手验证。所有链接都指向仓库内真实存在的文件；如果某格是 🟡 或 ❌，说明那部分还没做完，我们直接写出来，而不是让 README 替它画饼。

> 4.1.1 decomposes the frontend runtime by route. Workspace Providers now mount only for chat, while `/trace/:traceId` and the shared Diagnostics Trace detail load on demand with cancellable HTTP requests, route-level error containment, feature-owned CSS, and executable bundle evidence. The frozen 4.0 runtime contract is unchanged: Python remains default and authoritative; every Rust delegate remains opt-in; Python fallback is guaranteed throughout 4.x.

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
| 6 | Edge-Cloud Model Router | MVP, dry-run release gated | ✅ [infra/gateway/edge_inference.py](../deepseek_infra/infra/gateway/edge_inference.py) | ✅ | ✅ |
| 7 | MCP Tool Hub | MVP | ✅ [infra/mcp/](../deepseek_infra/infra/mcp/) | ✅ | ✅ |
| 8 | A2A Agent Mesh | MVP | ✅ [infra/agent_runtime/a2a.py](../deepseek_infra/infra/agent_runtime/a2a.py) | ✅ | ✅ |
| 9 | Context Taint Firewall | MVP, release gated | ✅ [infra/gateway/context_taint.py](../deepseek_infra/infra/gateway/context_taint.py) | ✅ | ✅ |
| 10 | Workspace Core | MVP | ✅ [infra/workspace/](../deepseek_infra/infra/workspace/) | ✅ | ✅ |
| 10.5 | Memory | MVP, release gated | [infra/memory/](../deepseek_infra/infra/memory/) | yes | yes |
| 11 | Multimodal Media Layer | MVP | ✅ [infra/media/](../deepseek_infra/infra/media/) | ✅ | ✅ |
| 12 | Browser Control Runtime | MVP, release gated | ✅ [infra/browser/](../deepseek_infra/infra/browser/) | ✅ | ✅ |
| 13 | Automation Runtime | MVP, release gated | ✅ [infra/automation/](../deepseek_infra/infra/automation/) | ✅ | ✅ |
| 14 | Rust Core Hybrid Runtime | MVP, default-disabled; 3.10.0 adds Python-owned dual-format cache storage and direct BLOB assembly without a delegate or ownership change | ✅ [semantic_cache.py](../deepseek_infra/infra/gateway/semantic_cache.py) · [infra/rust_core/](../deepseek_infra/infra/rust_core/) · [rust/](../rust/) | ✅ | ✅ [SEMANTIC_CACHE_BINARY_EMBEDDINGS.md](SEMANTIC_CACHE_BINARY_EMBEDDINGS.md) · [RAG_VECTOR_BINARY_TRANSPORT.md](RAG_VECTOR_BINARY_TRANSPORT.md) · [RUST_SIDECAR_PERFORMANCE.md](RUST_SIDECAR_PERFORMANCE.md) · [RUST_HYBRID_RUNTIME_RUNBOOK.md](RUST_HYBRID_RUNTIME_RUNBOOK.md) |

横切资产（不算独立模块，但支撑「可验证性」）：

| 资产 | 位置 | 状态 |
| --- | --- | --- |
| Evaluation Harness（RAG / Agent / Tool 三条评测线） | [evals/](../evals/) · 评分核心 [infra/evaluation/harness.py](../deepseek_infra/infra/evaluation/harness.py) | ✅ 全部离线可跑；CI 生成统一报告、Agent Eval strict、baseline compare artifact 与 security corpus report；v2.4 起全部纳入硬门禁 |
| Benchmarks（延迟 / 缓存 / 检索 / DAG） | [benchmarks/](../benchmarks/) | ✅ 离线两项可直接复跑；在线两项需本地服务 + Key |
| 一键 Demo | [examples/](../examples/) · [docs/DEMO.md](DEMO.md) | ✅ |
| 部署资产（Docker / Compose / .env） | [Dockerfile](../Dockerfile) · [docker-compose.yml](../docker-compose.yml) · [docs/DEPLOYMENT.md](DEPLOYMENT.md) | ✅ CI 覆盖 `docker build` + `docker compose config` |
| 安全工程（威胁模型 / CI 扫描） | [docs/THREAT_MODEL.md](THREAT_MODEL.md) · [ci.yml security job](../.github/workflows/ci.yml) | ✅ |
| Compatibility Smoke Pack | [scripts/smoke_mcp_compat.py](../scripts/smoke_mcp_compat.py) · [scripts/smoke_a2a_compat.py](../scripts/smoke_a2a_compat.py) · [scripts/smoke_a2a_external_peer.py](../scripts/smoke_a2a_external_peer.py) · [scripts/smoke_edge_router.py](../scripts/smoke_edge_router.py) · [examples/edge_router_smoke.py](../examples/edge_router_smoke.py) · [examples/external_mcp_server_partner.py](../examples/external_mcp_server_partner.py) · [examples/a2a_interop_peer.py](../examples/a2a_interop_peer.py) | ✅ 本地服务启动后可复跑；v2.3.0 新增官方 MCP SDK partner + A2A 独立进程 peer 实测；v2.3.3 新增 A2A external peer evidence；v2.4.3 新增 Edge Router structured smoke evidence；v2.8.0 新增 Edge Router dry-run release evidence；v2.4.5 新增 A2A third-party peer structured evidence |
| Release Readiness（发版体检 / 产物证明） | [scripts/doctor.py](../scripts/doctor.py) · [scripts/preflight_release.py](../scripts/preflight_release.py) · [scripts/smoke_release.py](../scripts/smoke_release.py) · [scripts/check_4_0_rc_readiness.py](../scripts/check_4_0_rc_readiness.py) · [docs/4_0_RC_READINESS.md](4_0_RC_READINESS.md) | ✅ Two complete Python runs each measured 95.2607% across 2589 tests + 58 subtests; the last Rust coverage record measured 80.22% across 172 tests. Current-version product, parity, protocol, upgrade and browser evidence is machine-checked before publication. |
| Rust Sidecar Performance Evidence | [scripts/run_rust_sidecar_benchmarks.py](../scripts/run_rust_sidecar_benchmarks.py) · [docs/evidence/rust-sidecar-performance-v4.2.0.json](evidence/rust-sidecar-performance-v4.2.0.json) · [RUST_SIDECAR_PERFORMANCE.md](RUST_SIDECAR_PERFORMANCE.md) | PASS contract: locked release profile; Python/core/warm HTTP/full integration and cold start separated; five delegate families covered; absolute latency informational; no default change. |
| RAG Vector Binary Parity | [scripts/check_rag_vector_binary_parity.py](../scripts/check_rag_vector_binary_parity.py) · [docs/evidence/rag-vector-binary-parity-v4.2.0.json](evidence/rag-vector-binary-parity-v4.2.0.json) · [RAG_VECTOR_BINARY_TRANSPORT.md](RAG_VECTOR_BINARY_TRANSPORT.md) | PASS: 110 valid + 16 malformed release-sidecar cases; JSON/binary/Python parity, fixed 24-byte response, stable errors, payload reduction, redaction, and no JSON retry. |
| Workspace Core Evidence | [scripts/smoke_workspace.py](../scripts/smoke_workspace.py) · [docs/evidence/workspace-v4.2.0.json](evidence/workspace-v4.2.0.json) · [docs/WORKSPACE.md](WORKSPACE.md) | ✅ 离线 smoke 覆盖项目创建/重命名、保存项、产物、对话导出、项目 ZIP、secret redaction 与删除边界 |
| Media Layer Evidence | [scripts/smoke_media.py](../scripts/smoke_media.py) · [docs/evidence/media-v4.2.0.json](evidence/media-v4.2.0.json) · [docs/MEDIA.md](MEDIA.md) · [evals/reports/media-v4.2.0.json](../evals/reports/media-v4.2.0.json) | PASS: 离线 smoke/eval 覆盖 image、PDF、webpage snapshot、segments、media-to-RAG、citations、project export 与 secret redaction |
| Browser Control Evidence | [scripts/smoke_browser.py](../scripts/smoke_browser.py) · [docs/evidence/browser-v4.2.0.json](evidence/browser-v4.2.0.json) · [docs/BROWSER_CONTROL.md](BROWSER_CONTROL.md) · [evals/reports/browser-v4.2.0.json](../evals/reports/browser-v4.2.0.json) | PASS: offline smoke/eval covers session create, page read, screenshot, links, private-host blocking, confirmation gates, Media snapshots, RAG chunks and audit logging |
| Frontend Browser Evidence | [scripts/smoke_frontend_browser.py](../scripts/smoke_frontend_browser.py) · [docs/evidence/frontend-browser-v4.2.0.json](evidence/frontend-browser-v4.2.0.json) · [docs/FRONTEND_MODULES.md](FRONTEND_MODULES.md) | PASS: real Chromium covers React root ownership, routed Trace direct load/refresh, Legacy 404, strict CSP, chat, persisted history, stop-generation, upload cancellation, not-found deep links, app-shell cache and offline refresh |
| Frontend Bundle Evidence | [scripts/check_frontend_bundle.py](../scripts/check_frontend_bundle.py) · [docs/evidence/frontend-bundle-v4.2.0.json](evidence/frontend-bundle-v4.2.0.json) | PASS: Vite manifest proves Trace page/detail are dynamic entries, Trace implementation markers stay out of the initial workspace bundle, and Trace CSS remains feature-owned |
| Automation Runtime Evidence | [scripts/smoke_automation.py](../scripts/smoke_automation.py) · [docs/evidence/automation-v4.2.0.json](evidence/automation-v4.2.0.json) · [docs/AUTOMATION.md](AUTOMATION.md) · [evals/reports/automation-v4.2.0.json](../evals/reports/automation-v4.2.0.json) | PASS: offline smoke/eval covers create, manual/schedule/event triggers, Skill and Browser read-only actions, project export, policy blocking, history, trace linkage, artifacts and templates |
| Skill System Evidence | [scripts/smoke_skills.py](../scripts/smoke_skills.py) · [docs/evidence/skills-v4.2.0.json](evidence/skills-v4.2.0.json) · [docs/SKILLS.md](SKILLS.md) | ✅ 离线 smoke 覆盖 Skill API route、内置 Skill 加载、自定义 Skill、schema validation、工具权限、artifact policy、project binding 与导出 |
| Skill Workbench UI Evidence | [scripts/smoke_skills_ui.py](../scripts/smoke_skills_ui.py) · [docs/evidence/skills-ui-v4.2.0.json](evidence/skills-ui-v4.2.0.json) · [SkillsDrawer.tsx](../frontend/src/features/skills/SkillsDrawer.tsx) | PASS: React source contract covers the workspace entry, create/edit/delete, API actions, project binding, lifecycle, styles, PWA ownership and TypeScript CI gate |
| Skill Builder Evidence | [scripts/smoke_skill_builder.py](../scripts/smoke_skill_builder.py) · [docs/evidence/skill-builder-v4.2.0.json](evidence/skill-builder-v4.2.0.json) · [skillsApi.ts](../frontend/src/api/skillsApi.ts) | PASS: React simple builder plus backend schema validation, offline dry-run and import/export API contracts |
| Skill Packs Evidence | [scripts/smoke_skill_packs.py](../scripts/smoke_skill_packs.py) · [docs/evidence/skill-packs-v4.2.0.json](evidence/skill-packs-v4.2.0.json) · [docs/SKILLS.md](SKILLS.md) | ✅ 离线 smoke 覆盖 Skill Pack schema、内置 Template Library、导入/导出、skillId 冲突处理、工具权限 diff、项目 Pack 绑定、安装 dry-run、Packs UI 页签与 JS syntax gate |
| Skill Eval Dashboard Evidence | [scripts/smoke_skill_eval_dashboard.py](../scripts/smoke_skill_eval_dashboard.py) · [docs/evidence/skill-eval-dashboard-v4.2.0.json](evidence/skill-eval-dashboard-v4.2.0.json) · [evals/reports/skills-v4.2.0.json](../evals/reports/skills-v4.2.0.json) | ✅ 离线 smoke 覆盖 Eval tab、Eval Case Builder、Skill / Pack scoring、regression compare、report export、截图、JS syntax 与 CI release gate |
| Skill Versioning Evidence | [scripts/smoke_skill_versioning.py](../scripts/smoke_skill_versioning.py) · [docs/evidence/skill-versioning-v4.2.0.json](evidence/skill-versioning-v4.2.0.json) · [deepseek_infra/infra/skills/versioning.py](../deepseek_infra/infra/skills/versioning.py) | ✅ 离线 smoke 覆盖 Skill revision snapshots、version diff、schema migration plan、Skill / Pack rollback、versioned project Pack binding、eval-aware upgrade gate、Version UI assets 与 CI release gate |
| Skill Analytics Evidence | [scripts/smoke_skill_analytics.py](../scripts/smoke_skill_analytics.py) · [docs/evidence/skill-analytics-v4.2.0.json](evidence/skill-analytics-v4.2.0.json) · [deepseek_infra/infra/skills/analytics.py](../deepseek_infra/infra/skills/analytics.py) | ✅ 离线 smoke 覆盖 Skill run history、metadata persistence、usage summary、failure diagnostics、project history、trace/artifact links、retention cleanup、privacy redaction、Runs UI 与 CI release gate |
| Skill Security Evidence | [scripts/smoke_skill_security.py](../scripts/smoke_skill_security.py) · [docs/evidence/skill-security-v4.2.0.json](evidence/skill-security-v4.2.0.json) · [deepseek_infra/infra/skills/security.py](../deepseek_infra/infra/skills/security.py) | PASS: 离线安全冒烟覆盖 Skill / Pack 审查、prompt 与 secret 扫描、工具授权风险 diff、trust/block 控制、篡改检测、manifest 导出、运行安全元数据、Security UI 与 CI release gate |
| Skill Catalog Evidence | [scripts/smoke_skill_catalog.py](../scripts/smoke_skill_catalog.py) · [docs/evidence/skill-catalog-v4.2.0.json](evidence/skill-catalog-v4.2.0.json) · [deepseek_infra/infra/skills/catalog.py](../deepseek_infra/infra/skills/catalog.py) | PASS: 离线本地目录冒烟覆盖 Catalog manifest、列表、搜索、安装预检、安装/卸载、安全门禁、eval 分数、工具权限摘要、Catalog UI 与 CI release gate |
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
- **亲手验证**：[examples/run_agent_dag_demo.py](../examples/run_agent_dag_demo.py)（实时打印 DAG 事件流）；[benchmarks/bench_agent_dag.py](../benchmarks/bench_agent_dag.py)；[evals/runners/run_agent_eval.py](../evals/runners/run_agent_eval.py)（录制 predictions 离线打分并用 `--strict` 作为 CI 硬门禁）；[docs/AGENT_EVAL.md](AGENT_EVAL.md)（录制格式与回放说明）。

### 3. Local RAG Data Layer — Working

- **代码**：[local_rag.py](../deepseek_infra/infra/rag/local_rag.py)（SQLite 索引 / BM25 + 向量 hybrid / 增量索引 / chunk lineage / 引用校验 / Recall@K 评估）、[files.py](../deepseek_infra/infra/rag/files.py)（解析 / 分块）、[context_compressor.py](../deepseek_infra/infra/rag/context_compressor.py)。
- **测试**：[test_local_rag.py](../tests/test_local_rag.py) · [test_files.py](../tests/test_files.py) · [test_context_compressor.py](../tests/test_context_compressor.py)。
- **亲手验证（全部离线、无需 Key）**：[examples/local_rag_demo.py](../examples/local_rag_demo.py)；[evals/runners/run_rag_eval.py](../evals/runners/run_rag_eval.py)（Recall@5 / Citation Accuracy）；[benchmarks/bench_rag_retrieval.py](../benchmarks/bench_rag_retrieval.py)。
- **边界（写清楚）**：默认零依赖跑哈希 embedding；`sqlite-vec` 向量表与 ONNX 本地 embedding 是可选增强（`requirements-rag.txt`），CI 只覆盖默认路径。
- **3.7.0 document preparation**: [document_preparation.py](../deepseek_infra/infra/rag/document_preparation.py) mirrors the established Python normalization/chunk contract and optionally compares it with Rust `/rag/documents/prepare`. Python parses files first and remains the sole owner of paths, OCR, embeddings, persistence, indexes, retrieval, and authorization. The 125-case gate covers Unicode character offsets, exact chunks, overlap, hashes, IDs, metadata isolation, and stable errors; malformed or divergent Rust output is discarded before persistence.
- **3.10.0 semantic-cache storage**: [semantic_cache.py](../deepseek_infra/infra/gateway/semantic_cache.py) dual-writes JSON plus `f64le-v1`, prefers valid BLOBs, handles mixed/corrupt rows per row, and preserves exact-match-before-vector behavior. [vector_binary.py](../deepseek_infra/infra/rust_core/vector_binary.py) copies validated candidate buffers into one unchanged `/rag/vectors/rank-binary` request without a candidate list-of-lists. [migrate_semantic_cache_embeddings.py](../scripts/migrate_semantic_cache_embeddings.py) provides explicit dry-run/batched/resumable backfill. JSON transport remains compatible/default, malformed or divergent results go directly to the full Python ranking, and no vector value enters diagnostics, logs, metrics, or evidence.

### 4. Tool Calling Runtime + Policy Engine — Working

- **代码**：[tools.py](../deepseek_infra/infra/tool_runtime/tools.py)（17 个本地工具）、[tool_policy.py](../deepseek_infra/infra/tool_runtime/tool_policy.py)（capability 切片 / schema 校验 / SSRF / 路径越界 / 密钥外泄 / 注入清洗 / 审计）、[search.py](../deepseek_infra/infra/tool_runtime/search.py)、[documents.py](../deepseek_infra/infra/tool_runtime/documents.py) / [presentations.py](../deepseek_infra/infra/tool_runtime/presentations.py) / [mindmaps.py](../deepseek_infra/infra/tool_runtime/mindmaps.py)（生成式产物）。
- **测试**：[test_tool_policy.py](../tests/test_tool_policy.py) · [test_tools.py](../tests/test_tools.py) · [test_search.py](../tests/test_search.py) · [test_documents.py](../tests/test_documents.py) · [test_presentations.py](../tests/test_presentations.py) · [test_mindmaps.py](../tests/test_mindmaps.py)。
- **亲手验证**：[evals/runners/run_tool_eval.py](../evals/runners/run_tool_eval.py)（离线重放策略闸门：Tool Policy Pass Rate + Injection Defense Pass Rate）；[examples/mcp_tool_demo.py](../examples/mcp_tool_demo.py)（经 MCP 真实调用工具）。

### 5. Observability & Trace — Working

- **代码**：[observability.py](../deepseek_infra/infra/observability/observability.py)（trace run / span 树、SQLite 持久化）、[trace_api.py](../deepseek_infra/infra/observability/trace_api.py)（`/api/traces` / `/trace/{id}` 路由）、[export.py](../deepseek_infra/infra/observability/export.py)（导出脱敏）、[metrics.py](../deepseek_infra/infra/observability/metrics.py)（Prometheus 文本）、[health.py](../deepseek_infra/infra/observability/health.py)（`/healthz` `/readyz`）、[features/trace/](../frontend/src/features/trace/)（React Trace page、共享摘要 / span tree / waterfall / 分类 / 错误组件）。
- **测试**：[test_observability_trace_tree.py](../tests/test_observability_trace_tree.py) · [test_observability_metrics.py](../tests/test_observability_metrics.py) · [test_server_integration.py](../tests/test_server_integration.py)。
- **亲手验证**：`curl http://127.0.0.1:8000/metrics`；前端每条助手消息的 Trace 按钮打开共享瀑布图；`GET /trace/{trace_id}` 打开可刷新、可分享的 React Trace page（本地 token 鉴权）；`GET /api/traces/{trace_id}/export.json` 导出脱敏 JSON。
- **展示资产**：[trace-waterfall.png](assets/trace-waterfall.png)、[agent-dag-run.png](assets/agent-dag-run.png)、[rag-citation.png](assets/rag-citation.png)、[mcp-tool-call.png](assets/mcp-tool-call.png) 已入库并由 README 首屏截图表引用。

### 6. Edge-Cloud Model Router — MVP

- **代码**：[edge_inference.py](../deepseek_infra/infra/gateway/edge_inference.py)（任务分类 → 端 / 云路由，云端失败回退本地；llama-cpp / MLC / fake provider；provider 支持、GGUF 后缀、量化识别与 suggestions）；多 provider 注册表 [providers/](../deepseek_infra/infra/gateway/providers/) 让 Ollama 模型经 `/v1` 暴露。
- **测试**：路由决策、配置面、云失败回退、fake provider、`POST /api/edge/route-preview`、doctor 建议与 release evidence 在 [test_edge_inference.py](../tests/test_edge_inference.py) / [test_deepseek_request.py](../tests/test_deepseek_request.py) / [test_web_edge_routes.py](../tests/test_web_edge_routes.py) / [test_runtime_doctor.py](../tests/test_runtime_doctor.py) / [test_preflight_release.py](../tests/test_preflight_release.py) 有覆盖。**真实端侧 GGUF / MLC 推理**仍需要可选依赖 + 本地模型文件，默认 CI 不跑真模型。
- **亲手验证**：[EDGE_ROUTER_RUNBOOK.md](EDGE_ROUTER_RUNBOOK.md)；`python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v4.2.0.json`；`EDGE_INFERENCE_ENABLED=1` + GGUF 后 `GET /api/edge/status` / `POST /api/edge/route-preview`；或 `OLLAMA_ENABLED=1` 后 `GET /v1/models` 看到 `ollama/<tag>`；`python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md` 可输出可选实机证据。

### 7. MCP Tool Hub — MVP

- **代码**：[server.py](../deepseek_infra/infra/mcp/server.py)（JSON-RPC 2.0：initialize / tools / resources / prompts）、[registry.py](../deepseek_infra/infra/mcp/registry.py)（17 工具 → MCP tools + 风险注解）、[permissions.py](../deepseek_infra/infra/mcp/permissions.py) + [adapters.py](../deepseek_infra/infra/mcp/adapters.py)（每个 tools/call 走 Tool Policy 闸门）、[client.py](../deepseek_infra/infra/mcp/client.py)（出方向 MCP client：timeout / retry / stats）、[bridge.py](../deepseek_infra/infra/mcp/bridge.py)（外部工具 profile / health / circuit breaker）、[executor.py](../deepseek_infra/infra/mcp/executor.py)（policy-gated external call + audit + trace）。
- **3.6.0 protocol preparation**：[protocol_preparation.py](../deepseek_infra/infra/mcp/protocol_preparation.py)先计算本地稳定结果，可选调用 Rust `/mcp/request/prepare`，只接受与 Python contract 完全一致且 `routing.owner=python` 的结果；任何后端异常、畸形响应、敏感字段注入、参数变化或语义分歧都会使用本地结果。Rust 不接收凭据、不记录完整 params/arguments，也不执行工具。
- **测试**：[test_mcp.py](../tests/test_mcp.py)（握手 / 目录 / 能力切片 / 真实执行 / 错误码族 / 回环 client / 外部工具 profile / policy gate / 外部 server 不可用 / 远端 `isError=true` / retry stats / circuit breaker / trace diagnostics）。
- **亲手验证**：[examples/mcp_tool_demo.py](../examples/mcp_tool_demo.py)；`python scripts/smoke_mcp_compat.py --token <token>` 验证握手、目录、工具调用、policy gate 和外部 health API；`GET /api/mcp/external/tools` 查看外部 server health；[COMPATIBILITY.md](COMPATIBILITY.md) 和 [integrations/](integrations/) 提供 Claude Desktop / Cursor 配置与官方 MCP SDK partner 实测记录。
- **MVP 边界**：本地 MCP server、mock external server、失败场景、危险参数拦截和观测链路已可验证；v2.3.0 新增官方 MCP Python SDK Streamable HTTP partner 实测（SSE 响应解析修复）。Claude Desktop / Cursor GUI 实机已在 v2.4.2 验证并更新兼容矩阵。

### 8. A2A Agent Mesh — MVP

- **代码**：[a2a.py](../deepseek_infra/infra/agent_runtime/a2a.py)（Agent Card 发现、JSON-RPC 任务生命周期 `message/send|stream`·`tasks/resubscribe`·`tasks/get|cancel|list`、artifact chunks、capability 隔离执行、`.a2a/` 持久化与重启对账、`A2AClient` 跨 Agent 委派）。
- **测试**：[test_a2a.py](../tests/test_a2a.py)（14 项，覆盖 artifact chunks、`tasks/resubscribe`、取消状态、A2AClient loopback、trace/metrics）；[test_a2a_compat_contract.py](../tests/test_a2a_compat_contract.py) 固定 Agent Card、`message/send`、`message/stream`、artifact chunks、`tasks/resubscribe` 与 `tasks/cancel` contract。
- **亲手验证**：`curl http://127.0.0.1:8000/.well-known/agent-card.json`；`python scripts/smoke_a2a_compat.py --token <token>` 跑 live smoke；`python examples/a2a_peer_demo.py --peer http://127.0.0.1:8001/a2a/agents/reasoner --token <token>` 跑本地 external peer loopback；`python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json` 跑独立进程 external peer evidence；`python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md` 生成第三方生态 evidence。
- **MVP 边界**：本地任务生命周期、artifact streaming chunks、断线重订阅、本地 external peer loopback、独立进程 interop peer、A2A external peer smoke evidence、third-party-style structured evidence 与观测链路已可验证；具体 LangGraph / CrewAI / Google A2A reference 等生态实现仍按候选清单继续扩展。

### 9. Context Taint Firewall — MVP, release gated

- **代码**：[context_taint.py](../deepseek_infra/infra/gateway/context_taint.py)（逐段信任打标 / 五类来源含 media & RAG / 三类指令扫描 / 隔离加固 / riskLevel 与 escalatedTools 诊断）+ [tool_policy.py](../deepseek_infra/infra/tool_runtime/tool_policy.py)（密钥外泄硬拦截、污染轮升级确认、v2.2.6 可解释 deny `reason`/`suggestion`）。
- **测试**：[test_context_taint.py](../tests/test_context_taint.py)（含 v2.2.6 per-category `scan_text` 矩阵、media/rag 来源与「提交」误伤回归）+ [test_tool_policy.py](../tests/test_tool_policy.py)（含 deny reason/suggestion 与审计落盘断言）。
- **亲手验证**：[evals/runners/run_tool_eval.py](../evals/runners/run_tool_eval.py) 输出 Prompt Injection Defense Pass Rate；[evals/runners/run_injection_adversarial.py](../evals/runners/run_injection_adversarial.py) 输出对抗小语料 block / false-positive / bypass rate；[evals/runners/run_security_corpus.py](../evals/runners/run_security_corpus.py) 输出 v2.4 版本化安全语料报告。运行中 `GET /api/taint` 看防火墙状态、`GET /api/tool-policy` 看最近 deny 审计（含 `reason`/`suggestion`）。最小复现命令集见 [SECURITY_SMOKE.md](SECURITY_SMOKE.md)。v2.8.0 新增 `python scripts/smoke_context_taint.py --offline --out docs/evidence/context-taint-v4.2.0.json` 作为 release hard gate。
- **MVP 的原因**：检测基于确定性 pattern 族（中英 + runner 侧 Base64 解码），对抗性变体已有门禁基准（阈值全绿）；v2.3.0 已把 `--strict` 接入 CI 必过项，v2.4.0 又补了版本化 security corpus，v2.8.0 新增 media / RAG 来源、risk 诊断与 release-gated smoke evidence，已满足日常使用的可解释防护。

### 10. Workspace Core — MVP

- **代码**：[workspace/projects.py](../deepseek_infra/infra/workspace/projects.py)（Project 2.0 facade）、[saved_items.py](../deepseek_infra/infra/workspace/saved_items.py)（保存项）、[artifacts.py](../deepseek_infra/infra/workspace/artifacts.py)（Artifact Hub）、[exports.py](../deepseek_infra/infra/workspace/exports.py)（Markdown / HTML / JSON / ZIP 导出）、[schema.py](../deepseek_infra/infra/workspace/schema.py)（ID / 类型 / sourceRef / redaction）。
- **测试**：[test_workspace.py](../tests/test_workspace.py) 覆盖项目、保存项、产物版本、预览脱敏与项目 ZIP；[test_smoke_workspace.py](../tests/test_smoke_workspace.py) 覆盖离线 evidence 生成；[test_preflight_release.py](../tests/test_preflight_release.py)、[test_smoke_release.py](../tests/test_smoke_release.py) 与 [test_release_manifest.py](../tests/test_release_manifest.py) 固定 release gate。
- **亲手验证**：`python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v4.2.0.json`；本地服务启动后可用 `/api/workspace/projects`、`/api/workspace/projects/{projectId}/saved-items`、`/api/workspace/projects/{projectId}/artifacts` 与 `/api/workspace/exports` 走完整工作台闭环。
- **MVP 边界**：v2.6.0 先稳定对象模型、API、导出包结构与证据链；复杂 Memory Graph、浏览器控制、自动化工作流与前端精装修留给后续版本。
