# DeepSeek Infra 项目介绍 PPT · 内容简报（Content Brief）

> 调研员：内容调研员_ContentBrief ｜ 所有事实与数字均来自仓库文件原文，逐条标注来源。
> 当前版本：**v4.0.3**（来源：README.md 徽章行、docs/IMPLEMENTATION_STATUS.md 第 8 行、docs/ARCHITECTURE.md 第 8 行）

---

## 1. 一句话定位 + 三个核心卖点

**一句话定位**（README.md 第 44–46 行）：
DeepSeek Infra 是一个**本地优先的 Agentic AI 基础设施平台**——一套本机 FastAPI 后端把 LLM 网关、多 Agent DAG 运行时、本地向量 RAG、工具调用运行时、链路可观测性和端云模型路由组装成一个可私有化、多端运行、可观测、可扩展的 Agentic AI 系统，并以 MCP / A2A / OpenAI `/v1` 标准协议对外互操作。

**三个核心卖点**（README.md「亮点」节，第 124–130 行）：
1. **一套后端，多种形态**：同一份 Python 后端可跑桌面内嵌 WebView 本地应用窗口、打包 Android APK、或作为本机 / 局域网服务启动。
2. **本地优先、数据不出端**：对话历史、草稿、文件缓存、向量索引、长期记忆、追踪与缓存全部保存在本机；DeepSeek / Tavily API Key 可以只用环境变量、不落库。
3. **标准协议互操作 + 可观测可运维**：OpenAI 兼容 `/v1` 网关、MCP（`POST /mcp`）工具面、A2A Agent 委派三协议齐备；每轮请求生成 trace，`/metrics` Prometheus 指标 + `/healthz`·`/readyz` 探针。

---

## 2. 关键数字清单（全部来自原文）

| 数字 | 含义 | 来源 |
| --- | --- | --- |
| 4.0.3 | 当前版本号 | README.md 徽章 / IMPLEMENTATION_STATUS.md |
| 13 个 | 核心基础设施模块（另加 #10.5 Memory 与 #14 Rust Core Hybrid Runtime） | README.md 模块表 / IMPLEMENTATION_STATUS.md |
| 17 个 | 本地工具经 MCP 暴露（搜索、抓取、文件检索、Python 计算、图表、思维导图、PPT/Word/PDF 生成、记忆、提醒等） | README.md 第 171 行 / docs/DEMO.md |
| 2 个 | 模型：`deepseek-v4-pro`（默认）与 `deepseek-v4-flash` | README.md 第 135 行 |
| 95% | Python 覆盖率硬门禁（CI 阻断） | README.md 徽章 / CHANGELOG 4.0.0 |
| 80% | Rust 工作区行覆盖率硬门禁 | CHANGELOG 4.0.0-rc.2 |
| 95.2521% | 实测 Python 语句+分支覆盖率（两次完整运行，2599 个测试 + 58 个 subtests） | docs/IMPLEMENTATION_STATUS.md 第 47 行 |
| 80.22% | 实测 Rust 覆盖率（172 个测试） | docs/IMPLEMENTATION_STATUS.md 第 47 行 |
| **RAG 检索基准（离线实测）** | 95 chunks 索引 130 ms；检索 avg 20.2 ms · P95 21.7 ms；**Recall@5 = 1.000 · MRR = 0.917** | README.md「Benchmarks」节 |
| **语义缓存基准（离线实测）** | hash provider：store avg 17.9 ms · lookup avg 8.4 ms；**精确命中 1.00 · 无关误命中 0.00** | README.md「Benchmarks」节 |
| 基准环境 | Windows 11 · Python 3.13 · i7-13700H · 16 GB RAM · SSD · 95 chunks · runs 10 · warmup 2 | README.md「Benchmarks」节 |
| Eval 基线 | RAG Recall@5 1.000 / Citation Accuracy 0.8333；26 个固定攻防用例 **Tool Policy Pass Rate 1.000 / Prompt Injection Defense Pass 1.000** | README.md「Benchmarks」节 |
| 注入对抗硬门禁 | blockRate ≥ 0.85 · falsePositiveRate ≤ 0.10 · bypassRate ≤ 0.15 | AGENTS.md / ROADMAP v2.2.6 |
| Agent Eval 硬门禁 | Tool Call Accuracy ≥ 0.90 · Agent Success Rate ≥ 0.85 · Prompt Regression Pass Rate ≥ 0.90 | AGENTS.md / ROADMAP v2.4.0 |
| 安全语料实测 | blockRate = 1.0 · bypassRate = 0.0 · falsePositiveRate = 0.0（全部 PASS） | ROADMAP.md v2.4.1 |
| Python/Rust 一致性语料 | Gateway 请求准备 68 例、MCP 协议准备 105 例、RAG 文档准备 125 例、向量 binary 110 有效 + 16 畸形例 | README.md Rust Sidecar 节 / CHANGELOG |
| 200 MB | 默认单文件上传上限（multipart 请求体上限 220 MB） | README.md 第 151 行 / 环境变量节 |
| 2,000,000 | 多 Agent 单次运行默认 token 预算（`MULTI_AGENT_TOKEN_BUDGET`，设 0 不限） | README.md 环境变量节 |
| 40 个 | 自定义 Seek 助手上限；每个 Seek 最多 6 个参考文件 | README.md 第 193 行 / ARCHITECTURE.md |
| 5 轮 | 普通 / 流式工具调用循环上限 | ARCHITECTURE.md 第 301 行 |
| MCP 协议版本 | `2025-06-18` | docs/DEMO.md / ARCHITECTURE.md |
| 支持文件格式 | 文本 / Markdown / CSV / JSON / 代码 / RTF / HTML / DOCX / XLSX / PPTX / EPUB / PDF + PNG/JPG/WebP/BMP/TIFF/GIF 图片 | README.md 第 151 行 |

---

## 3. 架构分层描述（适合画分层架构图）

来源：docs/ARCHITECTURE.md 第 111–152 行「分层架构」ASCII 图与 Hybrid Runtime mermaid 图。

**第 1 层 · Client Layer（客户端层）**
Web UI / PWA · Desktop WebView 桌面窗口 · Android APK · OpenAI SDK（`/v1`）· MCP 客户端（`/mcp`）· A2A Peer（`/a2a`）；协议为 HTTP · NDJSON · SSE · JSON-RPC 2.0。

**第 2 层 · Python Default Runtime（Python 默认权威运行时）**
FastAPI / ASGI：本地 token 鉴权 · HTTP/SSE 流式 · `/v1/chat` · `/healthz` · `/metrics`；拥有流式、上游 HTTP、凭据、MCP 传输会话、真实工具执行、文件解析 OCR、embeddings、持久化与业务状态。

**第 3 层 · 核心运行时组件（三大块）**
- **LLM Gateway**：Model Router（能力/成本/延迟路由 + 级联 + 质量门控）、Context Manager（prompt-cache 友好前缀）、Context Engine（token 预算裁剪）、Scheduler（优先级队列 / 并发上限 / 令牌桶限流 / backpressure 503 卸载 / DLQ）、Budget Manager（USD 成本估算 + 每日账本）、语义缓存、韧性请求队列、OpenAI 兼容门面、多 Provider（DeepSeek / Ollama）。
- **Agent DAG Runtime + A2A Mesh**：Planner 动态生成执行图 → 拓扑分层同层并行 → Critic 修订 → Synthesizer 综合；事件源持久化、断线重放、断点续跑。
- **可选 Rust Sidecar（默认禁用）**：5 个确定性委托——Gateway 请求准备、MCP 协议准备、工具策略评估、RAG 向量排序（JSON/compact binary）、RAG 文档准备；任何超时/畸形/分歧都回退 Python。

**第 4 层 · Local Data & Observability（本地数据与可观测，Python 拥有）**
SQLite 向量 RAG · 长期记忆 · 语义缓存 · Trace Runs / Span Tree · 请求队列 · DLQ · 预算账本。

**第 5 层 · 显式外部调用**
DeepSeek API · Tavily Search · Ollama / 端侧推理（llama-cpp / MLC）。

**职责边界金句**（ARCHITECTURE.md 第 74–77 行）：Python 是默认且权威运行时；Rust 委托可选、确定性、带 fallback；Rust 不读文件、不写索引、不持凭据、不执行工具。

---

## 4. 13 个核心模块简明清单

来源：README.md「核心基础设施模块」表 + docs/IMPLEMENTATION_STATUS.md 状态列。

| # | 名称 | 一句话职责 | 状态 |
| --- | --- | --- | --- |
| 1 | LLM Gateway | OpenAI 兼容 `/v1` 门面、模型路由、流式转发、Prompt Cache 友好上下文管理、请求队列重试与 fallback | Working |
| 2 | Agent DAG Runtime | Planner 动态生成执行图、依赖调度、同层并行、Critic 修订环、token 预算护栏、事件持久化与断线重放 | Working |
| 3 | Local RAG Data Layer | 文档解析 / 分块 / 本地 embedding / SQLite·sqlite-vec 向量索引 / BM25+向量混合检索 / 引用回链 | Working |
| 4 | Tool Calling Runtime + Policy Engine | 17 个受控本地工具执行，前置 Capability 策略引擎：schema 校验、SSRF / 路径越界 / 敏感写入防护、人工确认、注入清洗与审计 | Working |
| 5 | Observability & Trace | 每轮请求 trace run/span、瀑布图、Prometheus `/metrics`、`/healthz`·`/readyz` 探针 | Working |
| 6 | Edge-Cloud Model Router | 简单任务路由本地端侧模型、复杂任务走云端 DeepSeek，云端失败可回退本地 | MVP（dry-run release gated） |
| 7 | MCP Tool Hub | 本地工具面封装成 MCP server（JSON-RPC 2.0：tools / resources / prompts），可桥接外部 MCP server | MVP |
| 8 | A2A Agent Mesh | 每个本地 Agent 暴露 Agent Card 与任务生命周期（send/stream/resubscribe/get/cancel/list），支持 artifact chunks 与跨 Agent 委派 | MVP |
| 9 | Context Taint Firewall | 上下文按来源打信任标签，扫描注入 / 密钥外泄 / 工具指令，隔离包装不可信块，污染轮高危工具升级人工确认 | MVP, release gated |
| 10 | Workspace Core | Project 2.0、Saved Items、Artifact Hub 与 Markdown / HTML / JSON / ZIP 导出，统一本地工作台对象模型 | MVP |
| 10.5 | Memory | 作用域化长期记忆（global / project / seek）、检索编辑删除、敏感记忆拦截 | MVP, release gated |
| 11 | Multimodal Media Layer | 图片 / PDF / 音频 / 视频 / 网页 snapshot 的导入、分段、citation、导出与 media-to-RAG 生命周期 | MVP |
| 12 | Browser Control Runtime | 受控浏览器会话、安全门控动作、私有地址拦截、Browser-to-Media snapshot、网页 RAG 索引与审计 | MVP, release gated |
| 13 | Automation Runtime | 本地自动化定义（trigger / condition / action / policy）、run history、trace 关联 | MVP, release gated |
| 14 | Rust Core Hybrid Runtime | 可选 Rust sidecar 委托 + Python 拥有的双格式语义缓存存储；全部默认关闭，Python fallback 全程可用 | MVP, default-disabled |

---

## 5. 一次典型请求的处理链路（适合画流程图）

来源：docs/ARCHITECTURE.md「聊天流程」mermaid 图（第 243–267 行）。

```
浏览器 POST /api/chat
  → web.server.handle_chat（本地 token 鉴权）
  → deepseek_client.prepare_deepseek_call
      ├─ observability.ensure_trace() 生成 traceId
      ├─ edge_inference.select_edge_route 判定端/云
      │    ├─ 端侧 → llama.cpp / MLC 本地模型 → 直接响应
      │    └─ 云端 ↓
  → memory.prepare_memory_state（作用域长期记忆检索）
  → search.search_if_needed（联网搜索编排，可选）
  → build_deepseek_request（组装 prompt + 工具定义）
  → Context Taint Tracking（按来源打信任标签、扫描注入）
  → Gateway Context Manager（稳定前缀 / prompt-cache 友好裁剪）
  → semantic_cache.lookup()（无工具/无搜索/无附件时先查语义缓存）
  → Scheduler 准入（优先级队列 / 限流 / backpressure）
  → SQLite Request Queue / Resiliency（断网退避重试）
  → DeepSeek API
  → 返回 tool_calls？
      ├─ 是 → tools.execute_tool_calls（先过 Tool Policy 闸门：schema/SSRF/路径/密钥外泄/人工确认）
      │        → 结果清洗后追加 → 回到 Context Manager 再请求（最多 5 轮）
      └─ 否 → JSON 响应或 NDJSON 流式输出 → 全程写 trace span / metrics
```

多 Agent 模式下走 `stream_multi_agent()`：Planner 拆解 → worker 按 depends_on 拓扑分层并行 → Critic 修订 → Synthesizer 综合，共享同一 traceId（ARCHITECTURE.md 第 265 行）。

---

## 6. 安全机制要点

来源：README.md 第 174–176 行、ARCHITECTURE.md 第 186/199/303 行、docs/IMPLEMENTATION_STATUS.md #4 #9。

1. **Capability-based Tool Policy Engine**（`tool_policy.py`）：工具风险卡元数据、按角色切片的能力画像、轻量 schema 校验、静态 SSRF 防护（`evaluate_url_safety`）、路径越界检测（`evaluate_path_safety`）、敏感写入拦截、高风险工具人工确认、工具结果 prompt injection 清洗（`sanitize_tool_result`）、每条决策写 `.tool-audit/audit.jsonl` 审计日志。
2. **Context Taint Tracking + 注入防火墙**（`context_taint.py`）：prompt 按来源分段打信任标签——系统提示 / 用户输入 / 记忆可信，网页搜索 / 上传文件 / 工具结果不可信；对不可信段扫描三类指令：prompt 注入、密钥外泄、工具调用指令；搜索与文件上下文前置确定性「防注入隔离」声明。
3. **密钥外泄硬拒绝**：工具调用参数中出现运行时自身凭证（DeepSeek / Tavily Key、本地 token）一律硬拒绝（`secret_exfiltration_blocked`）。
4. **污染轮升级**：本轮上下文检出注入指令后，高风险 / 敏感写入工具（`fetch_url`、`forget_memory`、`suggest_memory`、`create_reminder`）自动转为待人工确认，直到用户显式批准。
5. **执行沙箱**：`python_eval` 走隔离 Python 子进程，仅允许小型数学 AST、受控函数和 2 秒超时；`data_transform` 不执行用户代码；`fetch_url` 先校验 URL 和解析后 IP、拒绝内网/保留地址、最多读 2 MB。
6. **威胁建模与 CI 扫描**：`docs/THREAT_MODEL.md` 七类威胁→缓解→测试逐条映射；CI security job 跑 pip-audit / bandit（HIGH）/ detect-secrets（AGENTS.md）。
7. **导出脱敏**：trace 导出与项目 ZIP 递归脱敏 API Key / Authorization / token / cookie，并截断大段私有文本（ARCHITECTURE.md export.py 条目）。

---

## 7. 评测与质量保障要点

来源：README.md「Benchmarks」节、AGENTS.md、ROADMAP.md v2.2.7–v2.4.1、docs/IMPLEMENTATION_STATUS.md。

- **离线 Eval Gates（无需 API Key，全部 CI 硬门禁）**：
  - `run_rag_eval.py`（Recall@K / Citation Accuracy，须 `PYTHONHASHSEED=0`）
  - `run_tool_eval.py`（工具策略误判即 exit 1）
  - `run_injection_adversarial.py --strict`（blockRate≥0.85 / FPR≤0.10 / bypass≤0.15）
  - `run_security_corpus.py --strict`（版本化安全语料：注入、tool policy attack、benign 误报、SSRF、路径越界、密钥外泄）
  - `run_agent_eval.py --strict`（Tool Call Accuracy ≥ 0.90 / Agent Success Rate ≥ 0.85 / Prompt Regression ≥ 0.90）
  - `compare_eval_baseline.py --strict`（对照 v2.2.6 / agent-v2.2.8 baseline 阻断回归）
- **评分核心是纯函数库** `deepseek_infra/infra/evaluation/harness.py`，runner 只负责编排。
- **覆盖率门禁**：Python 95%（语句+分支，v3.3.2 从 90% 提升）、Rust 80%；实测 95.2521%（2599 tests + 58 subtests）/ Rust 80.22%（172 tests）。
- **CI 检查链**：ruff（E4/E7/E9/F）→ mypy → pytest --cov → 前端 `npm run check` → 旧 JS `node --check` → 安全扫描 → Docker build gate。
- **发版体检**：`scripts/doctor.py`（运行时体检）+ `preflight_release.py`（版本/证据同步校验）+ `smoke_release.py`（一键 doctor + eval + smoke），发布产物带 sha256 与 manifest（ROADMAP v2.2.9）。
- **证据文化**：docs/evidence/*.json 与 evals/reports/*.json 统一携带 `version` / `commit` / `generatedAt` / `environment` / `status` 元数据（ROADMAP v2.3.4）。

---

## 8. 部署与多端形态

来源：README.md「快速开始」节（第 313–402 行）。

| 形态 | 方式 | 要点 |
| --- | --- | --- |
| 桌面本地应用窗口（推荐） | 双击 `launch.bat` / `launch.sh`（pywebview 内嵌 WebView） | 自动用 `desktop=1` token 入口完成认证，不跳外部浏览器 |
| 手机本机运行 | Termux / Pydroid 跑 `python launch_mobile.py`（或 `launch.py --mobile`） | 默认 `127.0.0.1:8000`，`--lan` 开放局域网 |
| 命令行服务 | `python app.py` | 终端打印 `Computer` / `Phone` 两个带 token 地址 |
| 单文件 exe 分发 | `python scripts/build_exe.py` → `dist/DeepSeekInfra.exe` | PyInstaller；面向无 Python 环境电脑 |
| Android APK | `android/` Android Studio 工程 `gradle :app:assembleDebug` | Chaquopy 把 Python 后端打进 APK，内置 WebView 打开本机地址 |
| Docker / Compose | `docker compose up -d` | python:3.12-slim、非 root、内置 `/healthz` HEALTHCHECK、单 `/data` 卷持久化；可选 Rust sidecar 用 `docker-compose.rust.yml` 叠加 |
| OpenAI 兼容客户端 | 任意 OpenAI SDK `base_url` 指向 `http://127.0.0.1:8000/v1` | `api_key` 传本地访问 token；支持流式与非流式；启用 Ollama 后多出 `ollama/<模型>` |

---

## 9. 版本演进里程碑

来源：CHANGELOG.md。

1. **v3.3.0 — 4.0 运行时架构决策（ADR-0040）**：批准 `python_first_hybrid` 架构合同——Python 为默认权威运行时、Rust 委托全部显式可选加入、默认部署纯 Python、4.x 全周期支持 Python fallback。
2. **v3.3.2 — 95% 覆盖率与 RC 就绪**：覆盖率门禁 90% → 95%，两次完整实测 95.3428% / 95.3396%，4.0 RC 就绪合同标记 READY。
3. **v3.5 – v3.10 — Rust Sidecar 委托逐版本落地**：Gateway 请求准备（3.5）→ MCP 协议准备（3.6）→ RAG 文档准备（3.7）→ 性能与可观测（3.8）→ compact binary 传输（3.9）→ 语义缓存 binary embedding 存储（3.10），每个委托都带共享 parity 语料与 Python fallback。
4. **v4.0.0 — Python-first Hybrid Runtime 稳定版**：从验证过的 v4.0.0-rc.2 无改动提升，保留 95% Python / 80% Rust 阻断门禁、冻结的十端点协议合同与完整升级/回滚证据。
5. **v4.0.1 – 4.0.3 — 前端安全加固与 React 迁移**：4.0.1 严格自托管 CSP、API Key 不再落 localStorage、Chromium 冒烟门禁；4.0.2 引入隔离的 React 19 + TypeScript + Vite `/ui/` 应用；4.0.3 完成 React 普通聊天纵切片（NDJSON 流式、Markdown、停止生成、本地会话历史、密钥仅驻内存）。

---

## 10. 路线图要点

来源：ROADMAP.md 未完成项、CHANGELOG 4.0.3「Compatibility」、docs/IMPLEMENTATION_STATUS.md MVP 边界、README Rust 节。

1. **前端 React 迁移后续切片**：附件、Agent/activity 面板、Projects、Skills、Memory、高级设置、语音、诊断与 PWA 仍由旧入口拥有，将逐步迁移到 `/ui/`（CHANGELOG 4.0.3；README 双轨说明）。
2. **第三方 A2A 生态实机验证**：LangGraph / CrewAI 等真实 peer 验证保持 🟡，留待后续小版本（ROADMAP v2.3.4 / v2.4.2 未完成勾选项；v2.4.5 已先补 third-party-style 结构化 evidence）。
3. **Rust 委托能力渐进扩展**：README「3.0.3 设计原则」明确后续小版本逐步接入 upstream proxy、MCP 处理、Tool Policy 等能力（详见 `docs/RUST_MIGRATION_ROADMAP.md`）；所有委托保持默认关闭、Python fallback 不移除。
4. **Edge Router 真实端侧推理**：真实 GGUF / MLC 推理仍需可选依赖 + 本地模型文件，默认 CI 不跑真模型，模块保持 MVP dry-run gated（IMPLEMENTATION_STATUS #6）。
5. **Workspace Core 后续深化**：复杂 Memory Graph、前端精装修等留待后续版本（IMPLEMENTATION_STATUS #10 MVP 边界原文）。

---

## 11. 可在 PPT 中引用的截图资源（docs/assets/ 实际存在的文件）

来源：`ls docs/assets/`（本次调研实际列出）。

**架构图（SVG / Mermaid）**
- `architecture.svg`（英文架构总览）
- `architecture.zh-CN.svg`（中文架构总览，README 首屏引用）
- `architecture.mmd`（可编辑 Mermaid 源文件）

**核心功能截图（README 截图表引用）**
- `trace-waterfall.png`（Trace 瀑布图）
- `agent-dag-run.png`（多 Agent DAG 运行）
- `rag-citation.png`（RAG 引用回链）
- `mcp-tool-call.png`（MCP 工具调用）

**Demo 流程截图（docs/DEMO.md 系列）**
- `01-chat.png`
- `02-rag-citation.png`
- `03-agent-dag.png`
- `04-trace.png`

**3.0 Workspace 截图**
- `3.0-workspace-overview.png`
- `3.0-automation-run.png`
- `3.0-project-export.png`
- `3.0-skill-run.png`

**Skill 系统截图**
- `skill-builder.png`、`skill-builder-dry-run.png`
- `skill-catalog.png`、`skill-catalog-install-preview.png`
- `skill-eval-dashboard.png`、`skill-eval-case-builder.png`
- `skill-security-review.png`、`skill-trust-store.png`
- `skill-version-history.png`、`skill-version-diff.png`
- `skill-analytics.png`、`skill-packs.png`、`skill-pack-import.png`
- `skill-run-result.png`、`skill-runs.png`、`skill-workbench.png`

（注意：`rag-citation.png` 与 `02-rag-citation.png`、`agent-dag-run.png` 与 `03-agent-dag.png` 为不同文件，引用时区分。）

---

## 附：2 分钟 Demo 流程概要（docs/DEMO.md）

四个脚本按「零门槛 → 需要服务 → 需要 Key」排序：
1. **本地 RAG**（完全离线）：`python examples/local_rag_demo.py` — 索引 5 docs / 95 chunks（126 ms），检索展示 chunk lineage 回溯与 `verify_citation` 拒绝编造引用。
2. **MCP Tool Hub 协议回环**（需服务，免 Key）：`python examples/mcp_tool_demo.py` — `initialize → tools/list（17 tools）→ tools/call`，每次调用过 Tool Policy 闸门。
3. **OpenAI 兼容网关**（需服务 + Key）：`python examples/openai_compatible_client.py` — 任意 OpenAI SDK 直连 `/v1`。
4. **多 Agent DAG 流式**（需服务 + Key）：`python examples/run_agent_dag_demo.py` — 实时打印 Planner / Researcher / Reasoner / Synthesizer 事件流，跑完拿 traceId 打开 `/trace/{traceId}` 瀑布图。

---

*简报完。所有数字均可回溯至上述来源文件；未做任何推测性补充。*
