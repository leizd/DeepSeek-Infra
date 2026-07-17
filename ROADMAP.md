# DeepSeek Infra Roadmap

<!-- docs-language-switcher:start -->
[中文](ROADMAP.md) / [English](ROADMAP.en.md)
<!-- docs-language-switcher:end -->


已完成的能力以 [实现状态矩阵](docs/IMPLEMENTATION_STATUS.md) 为准（含各模块成熟度与明确缺口）；下面是接下来的计划，完成一项勾一项：

### v3.0.0: Personal AI Runtime GA
- [x] First-class Memory with scoped schema, search/edit/delete, skill read policy and sensitive-memory blocking.
- [x] Unified Workspace home across Projects, Memory, Skills, Media, Browser, Automations, Artifacts, Saved Items, Exports and Settings.
- [x] Workspace provenance graph linking project objects and export evidence.
- [x] GA smoke, demo screenshots, evidence file and `--ga` release preflight gate.

### v2.2.0: Visualization & Verification
- [x] Trace / Agent DAG / RAG Citation / MCP Tool Call 截图进 `docs/assets/`
- [x] Trace 瀑布图独立只读页面 + 导出（`GET /trace/{trace_id}` + `GET /api/traces/{trace_id}/export.json`）
- [x] RAG / 工具安全评测进 CI 门禁（`run_rag_eval` + `run_tool_eval` 作为 PR 必过项）
- [x] Docker 构建门禁（`docker build -t deepseek-infra:test .` + `docker compose config`）
- [x] Docker 基础瘦身（`python:3.12-slim`、`pip --no-cache-dir`、非 root、单数据卷、`/healthz` HEALTHCHECK、完整 `.dockerignore`）
- [x] 命名收口：`DeepSeekMobile.exe` → `DeepSeekInfra.exe`，旧名保留兼容；`deepseek-mobile-*.zip` → `deepseek-infra-*.zip`；Service Worker cache `deepseek-infra-*`

### v2.2.1: External MCP Tool Bridge
- [x] MCP 外部 server 工具目录合并进本地 Agent 工具面（`mcp__<server>__<tool>` 命名空间）
- [x] 外部 MCP 工具执行统一经过 Tool Policy、审批、结果清洗和审计
- [x] `GET /api/mcp/external/tools` 可查看外部 server / bridged tools / 风险等级
- [x] 修复 2.2.1 推送 CI 的 ruff / mypy 问题，清理误入库的 `tmp_tests/` 产物

### v2.2.2: MCP Policy Hardening
- [x] `/mcp tools/call` 外部 bridged tools 与 Agent 调用链共享同一个 executor 内部 ToolPolicy 闸门
- [x] 外部 MCP `isError=true` 结果映射为 `ok=false` / `upstream_tool_error`
- [x] `meta.network` / `meta.filesystem` 外部工具通用 SSRF 与路径参数扫描
- [x] 外部工具 schema 动态读取，Agent 工具面自动 refresh，sanitized 名称碰撞加 hash 后缀

### v2.2.3: MCP Interop & Trust Hardening
- [x] MCP Tool Hub 状态从 Experimental 推到 MVP（本地 server、mock external server、policy gate、失败场景、health API、trace diagnostics 均有测试）
- [x] 外部 MCP client 增加 per-server timeout、retry、短期 circuit breaker、`/api/mcp/external/tools` 健康态
- [x] Claude Desktop / Cursor 集成配置文档落地（GUI 实机仍待安装客户端后验证）
- [x] Prompt injection 对抗小语料库与 `block_rate` / `false_positive_rate` / `bypass_rate` report-only runner
- [x] Semantic cache benchmark 支持 `--provider hash|onnx`
- [x] CI coverage gate：60% → 70%

### v2.2.4: A2A Artifact Streaming & Agent Interop
- [x] A2A artifact chunks: `artifactId` / `chunkIndex` / `append` / `final`, `message/stream` streams `artifact-update` events before terminal status
- [x] `tasks/resubscribe`: clients can reconnect with an existing `taskId` and `afterChunkIndex`
- [x] Local external A2A peer loopback: `examples/a2a_peer_demo.py` + `A2AClient.message_stream()` / `resubscribe()`
- [x] A2A trace / metrics: `a2a_task`, `a2a_peer_call` spans and Prometheus `ai_a2a_*` metrics
- [x] Cancellation lifecycle hardening: `cancelRequestedAt`, `canceling -> canceled`, `discardedResult` diagnostics

### v2.2.5: Compatibility Smoke Pack
- [x] MCP compatibility smoke runner: `python scripts/smoke_mcp_compat.py --token <token>` checks `initialize` / `tools/list` / `tools/call` / policy gate / external health API
- [x] A2A contract smoke runner: `python scripts/smoke_a2a_compat.py --token <token>` checks Agent Card, `message/send`, `message/stream`, `tasks/resubscribe` and `tasks/cancel`
- [x] A2A contract regression: `tests/test_a2a_compat_contract.py` fixes artifact chunks, SSE final status, resubscribe cursor and cancel lifecycle without needing an API key
- [x] Edge Router runbook: `docs/EDGE_ROUTER_RUNBOOK.md` + `examples/edge_router_smoke.py` document Ollama / GGUF verification without promoting the router beyond Experimental

### v2.2.6: Eval Gate & Security Hardening
- [x] Prompt injection 对抗评测 soft gate：版本化阈值（`blockRate>=0.85` / `falsePositiveRate<=0.10` / `bypassRate<=0.15`），未达标 warning、`--strict` 硬失败
- [x] Tool Policy 可解释拒绝：`PolicyDecision` 携带 `reason` / `suggestion`，`denial_output` 输出结构化字段，审计日志落盘
- [x] Context Taint exfiltration 误伤修复：「提交」从动词表移除，`falsePositiveRate` 0.200 → 0.000
- [x] Coverage gate 70% → 75%
- [x] Security smoke checklist：`docs/SECURITY_SMOKE.md`

### v2.2.7: Eval Reports & Regression Evidence
- [x] Offline eval suite：`run_offline_eval_suite.py` 统一运行 RAG / Tool Policy / Prompt Injection adversarial eval
- [x] Eval report artifacts：`evals/reports/latest.json` / `latest.md` 记录版本、git SHA、数据集规模、阈值与指标
- [x] Regression baseline compare：`evals/baselines/v2.2.6.json` + `compare_eval_baseline.py` 标记 PASS / WARNING / FAIL
- [x] CI 上传 `offline-eval-report` artifact，PR 可下载 JSON / Markdown 证据
- [x] Eval reports 文档：`docs/EVAL_REPORTS.md`

### v2.2.8: Agent Eval Replay & Stability
- [x] Agent recording schema：`evals/schemas/agent_prediction.schema.json` 固定 prediction JSONL 字段
- [x] Agent recording normalizer：`agent_recording.py` 剔除 `runId` / `traceId` / timestamp / span 等非确定字段
- [x] Agent eval report：`run_agent_eval.py` 输出 `evals/reports/agent-latest.json` / `agent-latest.md`
- [x] Agent baseline：`evals/baselines/agent-v2.2.8.json` 做 current vs baseline report-only warning
- [x] Offline eval suite 可选包含 Agent Eval：`run_offline_eval_suite.py --include-agent`
- [x] Agent Eval 文档：`docs/AGENT_EVAL.md`

### v2.2.9: Release Readiness & Runtime Doctor
- [x] Runtime Doctor：`python scripts/doctor.py --offline` 检查 Python / 依赖 / .env / 数据目录权限 / static / 端口 / token，PASS / WARNING / FAIL 输出
- [x] Release Preflight：`python scripts/preflight_release.py --version 2.2.9` 校验 README 徽章 / CHANGELOG / Docker tag / 文档版本 / eval 报告版本同步
- [x] Release manifest & checksum：发布产物生成 `dist/deepseek-infra-2.2.9.zip.sha256` 与 `.manifest.json`
- [x] Release smoke suite：`python scripts/smoke_release.py --offline` 一键编排 doctor + offline eval + Agent Eval（`--with-server` 额外跑 MCP / A2A smoke）

### v2.3: Protocol Interop GA
- [x] MCP 客户端 SSE 响应解析 + 官方 MCP SDK Streamable HTTP partner 实测（`examples/external_mcp_server_partner.py`，`docs/integrations/external-mcp-server.md`）
- [x] A2A 独立进程 interop peer 实测（`examples/a2a_interop_peer.py`，`docs/integrations/a2a-interop.md`）
- [x] Prompt injection soft gate 升级为硬门禁（`run_injection_adversarial.py --strict` 进 CI 必过项 + suite 硬门禁）
- [x] Claude Desktop / Cursor GUI 验证 runbook 与 evidence template 落地（`docs/integrations/claude-desktop.md` / `cursor.md`）；GUI 实机仍待人工完成后更新 `docs/COMPATIBILITY.md`

### v2.3.1: GUI Interop Evidence Patch
- [x] `docs/COMPATIBILITY.md` 标题残留修正（`Compatibility Smoke Pack（v2.2.5）` → `Compatibility Smoke Pack`）
- [x] `preflight_release.py` 新增 `gui_interop_evidence` 检查：扫描 COMPATIBILITY.md 中 Claude Desktop / Cursor 行的状态，🟡 为 WARNING，✅ GUI tested 为 PASS
- [x] `docs/RELEASE_READINESS.md` 新增 GUI Interop Evidence Checklist 节
- [x] 新增 `docs/integrations/a2a-third-party-plan.md`：第三方生态 A2A 验证计划，兼容矩阵保持 🟡
- [ ] Claude Desktop / Cursor GUI 实机证据填入（需人工完成 GUI 测试后更新矩阵与 integration docs）

### v2.3.2: Headless MCP Client Compatibility Pack
- [x] Headless MCP bridge smoke：`scripts/smoke_mcp_headless_bridge.py` 启动本地服务，经 stdio bridge 跑 `initialize` / `tools/list` / `tools/call` / policy denial
- [x] MCP client config generator：`scripts/generate_mcp_client_config.py` 输出 Claude direct HTTP、Claude stdio bridge 与 Cursor 配置 JSON
- [x] Headless client 文档：`docs/integrations/headless-mcp-client.md` 说明 CI / server / no-GUI 验证路径
- [x] Preflight 硬证据：`headless_mcp_bridge_evidence` 缺失或不完整时 FAIL；Claude / Cursor GUI evidence 仍保持 WARNING
- [x] Compatibility matrix 新增 Headless MCP bridge ✅ Tested 行，Claude Desktop / Cursor 仍不标 GUI 通过

### v2.3.3: A2A External Peer Compatibility Pack
- [x] A2A external peer smoke：`scripts/smoke_a2a_external_peer.py` 验证 Agent Card / `message/send` / `message/stream` / `tasks/get` / `tasks/cancel` / `tasks/list` / artifact chunks / SSE final event
- [x] A2A evidence schema：`evals/schemas/a2a_external_peer_evidence.schema.json` 固定 `checks`、peer metadata 与 PASS/FAIL 状态
- [x] Adapter path：`examples/a2a_adapters/` 提供 LangGraph / CrewAI peer adapter skeleton，不把第三方生态强行标 ✅
- [x] Preflight 分层：`a2a_external_peer_evidence` 缺失或不完整时 FAIL；`a2a_third_party_peer_evidence` 缺失时 WARNING
- [x] Compatibility matrix 新增 A2A external peer smoke ✅ Tested 行，第三方 A2A ecosystem peer 当时保持 🟡，并在 v2.4.4 补齐结构化 evidence

### v2.3.4: Release Evidence Polish & Encoding Fix
- [x] 修复 CHANGELOG v2.3.3 顶部乱码（`???` / `??`），恢复为正常中文
- [x] 新增 `docs/EVIDENCE_INDEX.md`：MCP / A2A / GUI / eval / release evidence 统一索引入口
- [x] `scripts/preflight_release.py` 新增 `docs_encoding_sanity` 检查，发现文档乱码即 FAIL
- [x] Evidence JSON 元数据统一：`docs/evidence/*.json`、`evals/reports/*.json` 均包含 `version` / `commit` / `generatedAt` / `environment` / `status`
- [x] Release manifest 新增 `evidence` 字段，明确列出发布产物包含的证据文件
- [ ] 第三方 A2A 生态实机验证（LangGraph / CrewAI 等真实 peer）

### v2.4.0: Evaluation & Security Hardening
- [x] Coverage gate 75% → 80%（`pyproject.toml` + CI + README badge 同步）
- [x] Agent Eval CI 硬门禁：`run_agent_eval.py --strict`，阈值 Tool Call Accuracy 0.90 / Agent Success Rate 0.85 / Prompt Regression Pass 0.90
- [x] Baseline regression 硬门禁：`compare_eval_baseline.py --strict` 覆盖 RAG / Citation / Tool Policy / Injection / Agent Success Rate
- [x] 版本化安全语料库：`evals/golden/security/*.v2.4.jsonl` + `run_security_corpus.py --strict`
- [x] Quality Gate Evidence：`evals/reports/baseline-compare-latest.json`、`security-latest.json`、preflight `quality_gate_evidence`、release manifest `qualityGates`

### v2.4.1: Release Evidence Patch
- [x] 提交 `evals/reports/baseline-compare-latest.json` 作为 release evidence，记录 RAG / Citation / Tool Policy / Injection / Agent 相对 v2.2.6 基线的回归对比
- [x] 提交 `evals/reports/security-latest.json` / `security-latest.md`，安全语料指标全部 PASS（blockRate=1.0、bypassRate=0.0、falsePositiveRate=0.0）
- [x] 修复 `.gitignore` 对 `evals/reports/*` 的忽略过宽问题，whitelist baseline compare 与 security corpus 的 latest 报告
- [x] 统一 v2.4.0 CHANGELOG 中文分组与叙事风格，补齐 v2.4 系列 preflight 证据链

### v2.4.2: GUI Interop Evidence Patch
- [x] 版本号同步：README badge / `app_version` / Dockerfile tag / Android `versionName` / `versionCode` / 各文档「适用版本」/ CI preflight 全部更新到 2.4.2
- [x] Claude Desktop GUI 实机验证：tools/list、`data_transform`、`fetch_url` SSRF policy denial、系统提示无污染
- [x] Cursor GUI 实机验证：tools/list、`data_transform`、`fetch_url` SSRF policy denial、系统提示无污染
- [x] 更新 `docs/COMPATIBILITY.md` 与 `docs/EVIDENCE_INDEX.md`，Claude Desktop / Cursor 状态改为 ✅ GUI tested
- [x] 刷新 `docs/integrations/claude-desktop.md` 与 `docs/integrations/cursor.md` Evidence Template（版本 / commit / OS / 日期 / 通过项）
- [x] 重跑 `python scripts/preflight_release.py --version 2.4.2`，`gui_interop_evidence` 由 WARNING 变为 PASS
- [ ] 第三方 A2A 生态实机验证（LangGraph / CrewAI 等真实 peer）仍保持 🟡，留待后续小版本

### v2.4.3: Edge Router Evidence Patch
- [x] Edge Router smoke runner 支持 `--out docs/evidence/edge-router-smoke.json` 与 `--markdown docs/evidence/edge-router-smoke.md`，把 `/api/edge/status`、`/v1/models`、OpenAI-compatible local call 与 fallback readiness 输出为结构化 evidence
- [x] 新增 `edge_router_smoke_evidence` release preflight：缺失 evidence 时 WARNING，存在但 status / 必要 checks 非 PASS 时 FAIL
- [x] 兼容矩阵把 Edge Router 从 runbook-only 推进为 smoke evidence 路径，仍明确真实 GGUF / MLC 推理依赖本地模型与可选依赖，不强推 CI
- [x] Release manifest / Evidence Index / Release Readiness 收录 Edge Router smoke evidence

### v2.4.5: Continue.dev MCP Compatibility Patch
- [x] A2A third-party peer evidence：`docs/evidence/a2a-third-party-peer.json` / `.md` 记录第三方 A2A-compatible peer 的 Agent Card、send、stream、tasks、artifact chunks 与 SSE final event 验收结果
- [x] `scripts/smoke_a2a_external_peer.py` 支持 `--markdown`，`--peer-type third-party` 时输出 third-party schemaVersion 与 `peerType`
- [x] 新增 `a2a_third_party_peer_evidence` release preflight：缺失 WARNING，提交后 status / metadata / peerType / 必要 checks 不完整则 FAIL
- [x] 兼容矩阵把 Third-party A2A ecosystem peer 更新为 ✅ Third-party evidence tested，Release manifest / Evidence Index / Release Readiness 收录第三方 A2A evidence

### v2.5.5: Web Route Split Phase 2
- [x] 新增 `deepseek_infra/web/routes/files.py`，拆出 `/api/file-source`、`/api/file-page-image`、`/api/file-page-layout` 与 `/api/file-page-search`
- [x] 新增 `deepseek_infra/web/routes/downloads.py`，拆出 `/api/download`，保留生成文件媒体类型、下载文件名与 SVG inline 预览语义
- [x] `server.py` 继续保留 `create_app()` / `create_server()` / `FastAPIServer` 入口，并通过 `include_router(create_files_router(...))` 与 `include_router(create_downloads_router(...))` 装配 Phase 2 route module
- [x] 新增 `tests/test_web_file_routes.py` 与 `tests/test_web_download_routes.py`，验证 route registry、auth、`Content-Disposition`、`Cache-Control` 与 `X-Content-Type-Options` 不回归
- [x] Workspace Evidence：`python scripts/smoke_workspace.py --offline` 生成 `docs/evidence/workspace-v2.5.5.json`，preflight 与 release manifest 继续固化 `workspaceCore=PASS`

### v2.5.6: Web Route Split Phase 3
- [x] 新增 `deepseek_infra/web/routes/rag.py`，拆出 `POST /api/rag/reindex`、`/api/rag/verify-citation`、`/api/rag/eval`
- [x] 新增 `deepseek_infra/web/routes/memory.py`，拆出 `GET/POST /api/memory`、`DELETE /api/memory/{id}`、`POST /api/memory/conflicts`
- [x] `server.py` 移除 `memory_action`，通过 `include_router(create_rag_router(...))` 与 `include_router(create_memory_router(...))` 装配 Phase 3 route module
- [x] 新增 `tests/test_web_rag_routes.py`（11 tests）与 `tests/test_web_memory_routes.py`（15 tests），验证 route registry、auth、invalid payload AppError code 与 `server_module` patch 兼容
- [x] 编码回归测试 RAG 断言从 `server.py` 切换到 `routes/rag.py`

### v2.5.7: Web Route Split Phase 4
- [x] 新增 `deepseek_infra/web/routes/mcp.py`，拆出 `POST /mcp`（notification 202）与 `GET /api/mcp/external/tools`
- [x] 新增 `deepseek_infra/web/routes/a2a.py`，拆出 `GET /.well-known/agent-card.json`、`GET /a2a/agents`、`POST /a2a` 与 `POST /a2a/agents/{agent_id}`，保留 SSE streaming
- [x] 新增 `deepseek_infra/web/routes/edge.py`，拆出 `POST /api/edge/reload`
- [x] `mcp_enabled` / `a2a_enabled` 改用 Callable lambda 读取 settings，支持运行时配置变更
- [x] 新增 `tests/test_web_mcp_routes.py`（7 tests）、`tests/test_web_a2a_routes.py`（8 tests）、`tests/test_web_edge_routes.py`（6 tests）

### v2.5.8: Web Route Split Phase 5
- [x] 新增 `deepseek_infra/web/routes/workspace.py`，拆出 `POST /api/projects`、`POST /api/project-files` 与全部 `/api/workspace/*` REST 路由（22 条）
- [x] `server.py` 移除 `project_action`，精简 workspace 相关 imports
- [x] 新增 `tests/test_web_workspace_routes.py`（9 tests）

### v2.5.9: Web Route Split Final
- [x] 新增 `deepseek_infra/web/routes/chat.py`，拆出 `POST /api/chat`、`POST /api/title`、`POST /api/conversations/search`、`POST /v1/chat/completions`、`GET /v1/models`
- [x] `server.py` 不再内联 chat route handler，只做路由装配
- [x] 新增 `tests/test_web_chat_routes.py`（12 tests）
- [x] #14 全部路由拆分完成，关闭

### v2.6.0: Skill System
- [x] 新增 `deepseek_infra/infra/skills/` 技能注册表、schema、执行器、权限模型与模板系统
- [x] 内置 6 个 Skill：`code_review`、`document_reader`、`paper_writer`、`ppt_generator`、`research_brief`、`study_tutor`
- [x] 新增 `scripts/smoke_skills.py`、`evals/runners/run_skill_eval.py`、`docs/evidence/skills-v2.6.0.json`
- [x] 新增 `docs/SKILLS.md` 与 `tests/test_skills.py`
- [x] 全仓版本号同步到 2.6.0

### v2.6.1: Skill API Integration Patch
- [x] 新增 `deepseek_infra/web/routes/skills.py`，接入 `POST /api/skills` 与 `POST /api/skills/{skill_id}/run`
- [x] 新增 `tests/test_web_skills_routes.py`，覆盖 Skill HTTP 鉴权、CRUD、disable / enable 与 offline run
- [x] `scripts/smoke_skills.py --offline` 输出 `docs/evidence/skills-v2.6.1.json` 并验证 `skillApiRoutes`
- [x] `preflight_release.py` 与 release manifest 纳入 `skill_system_evidence` / `skillSystem` gate
- [x] `Settings.from_env(root=...)` 中 Skill 目录跟随 runtime root

### v2.6.2: Skill Workbench UI
- [x] 新增 Skill Workbench 前端入口：内置 / 自定义 Skill 浏览、启用 / 禁用、导入 / 导出、Recent Runs
- [x] 新增 Skill Run Panel，根据 `inputSchema` 自动生成运行表单并支持 projectId / offline / persist 参数
- [x] Project 面板展示 enabledSkills、defaultSkill、recentSkills，并通过 Workspace API 保存绑定
- [x] Skill 运行结果预览回链 Saved Items / Artifact Hub，产物 metadata 保留 `skillRunId`
- [x] 新增 `scripts/smoke_skills_ui.py --offline` 与 `docs/evidence/skills-ui-v2.6.2.json`，CI / preflight / release manifest 纳入 `skillWorkbench` gate

### v2.8.1: Browser Control Runtime
- [x] Add `deepseek_infra/infra/browser/` with session lifecycle, optional Playwright Chromium control, static HTML fallback, isolated profiles and isolated downloads.
- [x] Add safety-gated browser tools for open/read/screenshot/link extraction/DOM extraction/scroll/click/type/select/download/close.
- [x] Gate browser control behind `BROWSER_CONTROL_ENABLED`, block private hosts by default, require confirmation for high-risk actions and log decisions to `.browser-audit/audit.jsonl`.
- [x] Save webpage snapshots, screenshots and downloads into Media Library, emit `browser://` citations and index page text into Local RAG as `untrusted_browser`.
- [x] Add Browser skills (`web_researcher`, `webpage_reader`, `website_summarizer`, `form_assistant`, `download_and_summarize`, `browser_to_report`) with explicit `browserPolicy` permissions.
- [x] Add `docs/BROWSER_CONTROL.md`, `scripts/smoke_browser.py --offline`, `evals/runners/run_browser_eval.py`, browser fixtures and v2.8.1 smoke/eval evidence.

### v2.7.4: Context Taint Firewall Hardening
- [x] Add dedicated `untrusted_media` and `untrusted_rag` taint sources so Media Layer and Local RAG chunks are tracked separately from files and web search.
- [x] Add `riskLevel`, `escalatedTools`, and `recommendedAction` to `diagnostics.contextTaint` reports.
- [x] Add `scripts/smoke_context_taint.py --offline` and `docs/evidence/context-taint-v2.7.4.json` covering web/file/media injection, tool directives, and tainted-turn escalation.
- [x] Promote Context Taint Firewall to `MVP, release gated` in the implementation status matrix and add `contextTaint` to the release manifest quality gates.

### v2.7.3: Edge Router Stabilization
- [x] Add Edge Router doctor checks for provider support, optional dependencies, model path, GGUF suffix, quantization and actionable setup guidance.
- [x] Add `POST /api/edge/route-preview` to explain local/cloud routing decisions without loading a model.
- [x] Add a zero-dependency fake Edge provider plus offline routing/fallback smoke evidence at `docs/evidence/edge-router-v2.7.3.json`.
- [x] Promote Edge Router dry-run evidence into release preflight and the release manifest `edgeRouter` quality gate.

### v2.7.2: Release Hygiene & Encoding Gates
- [x] Fix Dockerfile comments and Docker image examples so release-facing text is clean ASCII/UTF-8 and uses the v2.7.2 tag.
- [x] Expand release preflight encoding checks across Dockerfile, GitHub workflows, scripts, README, CHANGELOG, and docs markdown.
- [x] Make CI trigger rules explicit for `main` push, `main` pull requests, and manual `workflow_dispatch` runs.
- [x] Add `docs/RELEASE_CHECKLIST.md` with version bump, smoke, preflight, encoding, CI, Docker, and release artifact steps.

### v2.7.1: Media Layer Hardening
- [x] Harden media source paths so records can only point at `.media/objects/{mediaId}/...`; absolute paths and traversal are rejected.
- [x] Add Media upload gates: 50 MB per source, 20 files per media request, and an allowlist for image/audio/video/PDF/HTML MIME types.
- [x] Add `PATCH /api/media/{mediaId}` plus `force=true` media reprocess for metadata polish and explicit RAG rebuilds.
- [x] Improve audio/video media granularity with transcript chunking, sorted frame captions, validated `framePath`, and more resilient Skill media context.

### v2.7.0: Multimodal Media Layer
- [x] 新增 `deepseek_infra/infra/media/`，把 image / PDF / webpage / audio / video / screenshot 注册为一级 workspace object。
- [x] 新增统一媒体 API：`POST /api/media`、`GET /api/media`、`GET /api/media/{mediaId}`、`POST /api/media/{mediaId}/process`、`GET /api/media/{mediaId}/segments` 与删除路径。
- [x] 图片支持 OCR/caption 片段，PDF 支持 page text/page citation，网页支持 HTML/text snapshot 导入；音频/视频提供 transcript 与 frame caption import MVP。
- [x] 媒体片段统一写入 Local RAG `media` collection，并生成 `media://...` citation，项目导出会包含 media metadata、segments 与 source。
- [x] 新增内置 Media Skills：`image_explainer`、`pdf_reader`、`webpage_summarizer`、`audio_transcript_summarizer`、`video_brief_generator`、`media_to_report`。
- [x] 新增 `docs/MEDIA.md`、`scripts/smoke_media.py --offline`、`evals/runners/run_media_eval.py`、`docs/evidence/media-v2.7.1.json` 与 `evals/reports/media-v2.7.1.json`。

### v2.6.9: Local Skill Catalog
- [x] 新增本地 Skill Catalog / Marketplace-lite：索引内置 Skill、自定义 Skill、内置 Pack、自定义 Pack 和已导入 Pack，不联网下载。
- [x] 新增 Catalog manifest，聚合 category、tags、trustLevel、riskScore、evalScore、installCount、requiredTools 和 signing-prep hash。
- [x] 新增 Catalog API：`catalog_list`、`catalog_get`、`catalog_search`、`catalog_install`、`catalog_uninstall`、`catalog_refresh` 和 `catalog_export`。
- [x] 新增 Skill Workbench Catalog 页签，支持搜索、信任筛选、安装前预检、权限摘要、安全审查跳转、安装/导出。
- [x] 安装前串联 Security / Eval / Tool Grant / Project Binding，blocked 或 high-risk 未批准条目无法直接安装。
- [x] 新增 `scripts/smoke_skill_catalog.py --offline` 和 `docs/evidence/skill-catalog-v2.6.9.json`，发布就绪 / preflight / CI 门禁纳入 `skillCatalog`。

### v2.6.8: Skill Security Review
- [x] 新增 Skill / Pack 安全审查，包含信任等级、风险评分、工具授权风险、可疑 prompt 指示器和审查时间戳。
- [x] 新增 prompt injection、密钥外泄、密钥文件访问、网络外泄、隐藏工具指令和编码文本静态扫描。
- [x] 新增本地信任商店控制项：`trust_skill`、`untrust_skill`、`block_skill`、篡改检测和签名预备哈希清单。
- [x] 新增 Skill Workbench 安全页签，包含摘要卡片、审查行、发现项、清单预览和信任/拦截操作。
- [x] 新增 Skill 分析中的运行安全元数据：`runSecurityLevel`、`securityReviewId`、`trustedAtRun`、`toolGrantHashAtRun`、`blockedReason` 和 `approvalRequired`。
- [x] 新增 `scripts/smoke_skill_security.py --offline` 和 `docs/evidence/skill-security-v2.6.8.json`，发布就绪 / preflight / CI 门禁纳入 `skillSecurity`。

### v2.6.7: Skill Run Analytics
- [x] 新增 Skill Run History，记录 skillRunId、skillId、skillVersion、packId、projectId、状态、延迟、模型、摘要、关联 artifact、saved item 和 trace 元数据。
- [x] 新增本地使用分析：总运行次数、成功/失败率、平均/P50/P90 延迟、热门 Skill/Pack、artifact 数量、项目绑定使用情况和 7 日趋势。
- [x] 新增失败诊断：schema 校验、工具策略、artifact 策略、项目绑定、LLM/API、超时、取消和未知错误。
- [x] 新增 `POST /api/skills` 分析操作：`list_runs`、`get_run`、`delete_run`、`analytics_summary`、`cleanup_runs`、`redact_run` 和 `export_runs`。
- [x] 新增 Skill Workbench Runs 页签，包含摘要卡片、运行详情、trace/artifact 链接、导出、清理和脱敏控制。
- [x] 新增 `scripts/smoke_skill_analytics.py --offline` 和 `docs/evidence/skill-analytics-v2.6.7.json`，发布就绪 / preflight / CI 门禁纳入 `skillAnalytics`。

### v2.6.6: Skill Versioning & Migration
- [x] 新增 Skill 版本历史快照，包含修订元数据、哈希和变更摘要。
- [x] 新增 Skill 版本 diff 和迁移计划 API，覆盖 schema、prompt、工具授权、记忆、artifact 和项目绑定。
- [x] 新增自定义 Skill 和自定义 Skill Pack 的回滚流程。
- [x] 新增版本化项目 Pack 绑定元数据，包含 `packId`、`version` 和 `installedAt`。
- [x] 新增评测感知的 Pack 升级门禁和回归风险摘要。
- [x] 新增 Skill Workbench 版本历史 UI、版本 diff 预览、迁移摘要和回滚控制。
- [x] 新增 `scripts/smoke_skill_versioning.py --offline` 和 `docs/evidence/skill-versioning-v2.6.6.json`。

### v2.6.5: Skill Eval Dashboard
- [x] 新增 Skill Workbench Eval 页签，展示 Skill / Pack 状态、评分、用例数、失败用例和最近运行元数据。
- [x] 新增评测用例构建器，支持期望关键词、JSON 路径、禁止正则、期望 artifact 和项目绑定要求。
- [x] 扩展 `evals/runners/run_skill_eval.py`，支持 Skill / Pack 评分、基线回归对比、JSON 导出和 Markdown 摘要。
- [x] 新增 `POST /api/skills` 评测操作：`eval_report`、`list_eval_cases`、`create_eval_case` 和 `delete_eval_case`。
- [x] 新增 `scripts/smoke_skill_eval_dashboard.py --offline`、`evals/reports/skills-v2.6.7.json` 和 `docs/evidence/skill-eval-dashboard-v2.6.7.json`。
- [x] 新增发布就绪 / preflight / CI 门禁：`skillEvalDashboard` 和 Skill 评测报告证据。

### v2.6.4: Skill Packs
- [x] 新增本地 Skill Pack（`.skillpack.json`）schema，支持 packId / name / description / version / author / skills，兼容引用和内嵌 Skill 配置。
- [x] 新增 Pack 导入 / 导出 / 校验，支持 skillId 冲突处理（error / overwrite / skip）和 allowedTools 权限 diff。
- [x] 新增内置模板库：Study / Research / Code / Office 四个 Skill Pack。
- [x] 新增 Skill Workbench Packs 页签，支持查看、安装、导出和删除 Pack，并展示导入摘要和高风险工具警告。
- [x] 新增项目 Pack 绑定（`enabledPacks`）和一键安装 Pack 为项目启用一组 Skill。
- [x] 新增 `scripts/smoke_skill_packs.py --offline` 和 `docs/evidence/skill-packs-v2.6.4.json`，CI / preflight / release manifest 纳入 `skillPacks` 门禁。

### v2.6.3: Custom Skill Builder
- [x] 新增自定义 Skill 构建器，引导填写基础元数据、systemPrompt、inputSchema、outputSchema、allowedTools、memoryPolicy、artifactPolicy 和 projectBinding。
- [x] 新增可视化 Schema 编辑器，支持 string / textarea / number / integer / enum / boolean 字段和生成的 `inputSchema`。
- [x] 新增工具权限选择器，按工具能力和风险标签选择，由服务端 schema 校验支撑。
- [x] 支持克隆为自定义 Skill，并用构建器面板替代旧的 `window.prompt` 编辑流程。
- [x] 新增预览 JSON / 校验 Schema / 离线 Dry Run / 保存 / 保存并运行流程。
- [x] 新增 `scripts/smoke_skill_builder.py --offline` 和 `docs/evidence/skill-builder-v2.6.3.json`，CI / preflight / release manifest 纳入 `skillBuilder` 门禁。
