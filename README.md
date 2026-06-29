# DeepSeek Infra

![版本](https://img.shields.io/badge/version-2.5.8-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![Coverage Gate](https://img.shields.io/badge/coverage%20gate-80%25-brightgreen)
![许可证](https://img.shields.io/badge/license-MIT-black)

## 30 秒概览

DeepSeek Infra 是一个**本地优先的 Agentic AI Infra 平台**，提供：

- **OpenAI 兼容网关** — 把任意 OpenAI SDK 的 `base_url` 指向 `localhost:8000/v1` 即可接入
- **持久化 Agent DAG 运行时** — Planner → Worker → Critic → Synthesizer，支持断线恢复
- **MCP 原生工具中心** — 17 个本地工具以标准 MCP Server 暴露，外部 MCP server 桥接带 health / retry / 熔断
- **A2A 风格 Agent 网格** — Agent Card 发现 + 任务生命周期，跨 Agent 委派
- **本地 RAG 数据层** — 混合检索 + 引用回链，零外部依赖
- **工具策略引擎** — 按能力的权限控制，SSRF / 路径 / 注入防护逐次生效
- **Workspace Core 工作台** — 项目、保存项、产物中心和导出包统一为本地对象模型
- **Trace 可观测性** — 瀑布图、Prometheus 指标、健康探针
- **Docker 一键部署** — 单卷持久化、非 root 运行、内置 HEALTHCHECK

```bash
cp .env.example .env   # 填写 DEEPSEEK_API_KEY
docker compose up -d
curl http://127.0.0.1:8000/healthz
```

**眼见为实 →** [截图](#screenshots-截图) · [2 分钟 Demo](docs/DEMO.md) · [实现状态矩阵](docs/IMPLEMENTATION_STATUS.md)

---

**DeepSeek Infra is a local-first agentic AI infrastructure platform: an agent runtime with an MCP-native tool hub, an A2A-style agent mesh, an LLM gateway, local RAG and end-to-end observability.**

DeepSeek Infra 是一个**本地优先的 Agentic AI Infra 平台**：一套本机 FastAPI 后端把 LLM 网关、多 Agent DAG 运行时、本地向量 RAG、工具调用运行时、链路可观测性和端云模型路由组装成一个可私有化、多端运行、可观测、可扩展的 Agentic AI 系统，并以标准协议对外互操作——本地工具面经 **MCP**（Model Context Protocol）暴露给任意 MCP 客户端，本地 Agent 经 **A2A** 风格的 Agent Card + 任务生命周期与外部 Agent 互通。桌面端双击打开内嵌 WebView 的本地应用窗口，Android 端打包成 APK，任何 OpenAI 兼容客户端也能把 `base_url` 指向本机 `/v1`。除了你主动发往 DeepSeek / Tavily 的请求，数据都留在本机。

> 想看逐版本变更记录，请见 [CHANGELOG.md](CHANGELOG.md)。本文档描述**当前版本**的架构与用法。

**先验证，再相信**：[实现状态矩阵](docs/IMPLEMENTATION_STATUS.md)（9 个模块各自的代码 / 测试 / Demo 落地程度）· [2 分钟 Demo](docs/DEMO.md) · [Benchmarks](#benchmarks基准与评测) · [部署](docs/DEPLOYMENT.md) · [威胁模型](docs/THREAT_MODEL.md)

![DeepSeek Infra 架构总览](docs/assets/architecture.svg)

## 核心基础设施模块

| # | 模块 | 代码位置 | 职责 |
| --- | --- | --- | --- |
| 1 | **LLM Gateway** | [`infra/gateway/`](deepseek_infra/infra/gateway/) | OpenAI 兼容 `/v1` 门面、模型路由、流式转发、Prompt Cache 友好的上下文管理、请求队列重试与 fallback。 |
| 2 | **Agent DAG Runtime** | [`infra/agent_runtime/`](deepseek_infra/infra/agent_runtime/) | Planner 动态生成执行图、依赖调度、同层并行、Critic 修订环、token 预算护栏、事件持久化与断线重放。 |
| 3 | **Local RAG Data Layer** | [`infra/rag/`](deepseek_infra/infra/rag/) | 文档解析 / 分块 / 本地 embedding / SQLite·sqlite-vec 向量索引 / 混合检索 / 引用回链。 |
| 4 | **Tool Calling Runtime** | [`infra/tool_runtime/`](deepseek_infra/infra/tool_runtime/) | 受控本地工具执行（数学沙箱、文件检索、URL 精读、PPT / Word / PDF / 思维导图生成等），前置 Capability-based **Tool Policy Engine**：schema 校验、按角色的能力权限、风险分级、SSRF / 路径越界 / 敏感写入防护、人工确认、结果 prompt injection 清洗与审计日志。 |
| 5 | **Observability & Trace** | [`infra/observability/`](deepseek_infra/infra/observability/) | 每轮请求的 trace run/span、瀑布图、`/metrics` Prometheus 指标、`/healthz`·`/readyz` 探针。 |
| 6 | **Edge-Cloud Model Router** | [`infra/gateway/edge_inference.py`](deepseek_infra/infra/gateway/edge_inference.py) | 简单任务路由到本地端侧模型，复杂任务走云端 DeepSeek，云端失败可回退本地。 |
| 7 | **MCP Tool Hub** | [`infra/mcp/`](deepseek_infra/infra/mcp/) | 把本地工具面封装成 MCP server（JSON-RPC 2.0：`tools` / `resources` / `prompts`），Claude Desktop、Cursor 等任意 MCP 客户端可直接复用；外接 MCP server 可桥接进本地 Agent 工具面。 |
| 8 | **A2A Agent Mesh** | [`infra/agent_runtime/a2a.py`](deepseek_infra/infra/agent_runtime/a2a.py) | 为每个本地 Agent 暴露 Agent Card 与 A2A 任务生命周期（`message/send`·`message/stream`·`tasks/resubscribe`·`tasks/get`·`tasks/cancel`），支持 artifact chunks 与跨 Agent 委派。 |
| 9 | **Context Taint Firewall** | [`infra/gateway/context_taint.py`](deepseek_infra/infra/gateway/context_taint.py) | 上下文按来源打信任标签（网页 / 文件 / 工具结果 = 不可信），扫描注入 / 密钥外泄 / 工具指令，隔离包装不可信块，污染轮高危工具升级人工确认。 |
| 10 | **Workspace Core** | [`infra/workspace/`](deepseek_infra/infra/workspace/) | Project 2.0、Saved Items、Artifact Hub 与 Markdown / HTML / JSON / ZIP 导出，统一本地 AI 工作台对象模型。 |

> 每个模块落地到什么程度（Status / Code / Tests / Demo，含明确写出的缺口）见 [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)。跨端运行打包（Desktop WebView / Android APK / 单文件 exe / 手机控制台启动器）由 `web/`、`launcher/`、`android_entry.py` 与 `desktop_app.py` 承载。

## Screenshots（截图）

| Trace Waterfall | Agent DAG |
| --- | --- |
| ![Trace Waterfall](docs/assets/trace-waterfall.png) | ![Agent DAG Run](docs/assets/agent-dag-run.png) |

| RAG Citation | MCP Tool Call |
| --- | --- |
| ![RAG Citation](docs/assets/rag-citation.png) | ![MCP Tool Call](docs/assets/mcp-tool-call.png) |

> Screenshots reflect the current DeepSeek Infra UI. See [2-min Demo](docs/DEMO.md) for walking through each feature live.

## 架构分层

总览见第一屏架构图；分层细节、模块职责与数据流的文字版见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 亮点

- **一套后端，多种形态**：同一份 Python 后端既能作为桌面本地应用窗口（内嵌 WebView，不跳外部浏览器）运行，也能打包成 Android APK，或作为本机 / 局域网服务启动。
- **本地优先、数据不出端**：对话历史、草稿、文件缓存、向量索引、长期记忆、追踪与缓存全部保存在本机；DeepSeek / Tavily API Key 可以只用环境变量、不落库。
- **OpenAI 兼容网关**：`POST /v1/chat/completions` + `GET /v1/models`，任何 OpenAI SDK / 工具把 `base_url` 指向本机即可复用整套运行时。
- **标准协议互操作**：本地工具经 `POST /mcp` 以 MCP 协议暴露（不再只是内部工具，Claude Desktop / Cursor / 其他 Agent 客户端可直接接入）；本地 Agent 经 `/.well-known/agent-card.json` 与 `/a2a` 以 A2A 风格协议对外提供任务委派。
- **可观测、可运维**：每轮请求生成 trace，`/metrics` 暴露 Prometheus 指标，`/healthz`·`/readyz` 提供探针。

## 能力详解

### 对话与推理
- 支持 `deepseek-v4-pro` 与 `deepseek-v4-flash`，默认 `deepseek-v4-pro`；快速模式 / 专家模式一键切换，专家模式默认开启深度思考。
- 推理过程与最终回复均流式输出，流式阶段会区分显示「思考中 / 调用工具中 / 搜索中 / Agent 工作中 / 生成中」。
- 支持暂停、中断、继续、重新生成；可编辑历史用户消息后重发，也可以从任意助手回复创建分支。
- Markdown 全面渲染：代码块（行号 / 折叠 / 复制 / 下载 / `vscode://file/...` 打开本地路径）、行内与块级公式（本地 KaTeX，不依赖外部 CDN）、`mermaid` 流程图，以及把表格数值列一键转成柱状 / 折线 / 饼图。

### 多 Agent DAG 协作
- 开启多 Agent 后，Leader 拆解任务，Researcher / Coder / Reasoner / Critic 等角色按声明的 `depends_on` 做拓扑分层、同层并行，再由 Synthesizer 综合成最终回答。
- **可恢复 Agent Run**：每次运行持久化到 `.agent-runs/`，事件带 `runId` / `index` / `createdAt`，刷新页面或断线后可从最后一个事件继续恢复；Activity 面板支持单 Agent 重跑或只重新综合。
- Critic 可点名一个前序 worker 带反馈重跑一轮再综合；token 预算护栏 `MULTI_AGENT_TOKEN_BUDGET` 超额后不再启动后续层，但综合阶段始终执行。

### 联网搜索
- 关闭 / 自动 / 强制三档联网搜索；自动模式由模型决定本轮是否联网。
- 多轮互补搜索 + 结果去重重排 + 本地 `.search-cache` 缓存；模型也可以通过 `web_search` 工具按需检索，并对搜索结果来源做二次精读。
- Tavily API Key 可来自服务端环境变量，也可来自页面设置中的本轮请求。

### 文件理解与文档工作台
- 多文件上传，支持文本 / Markdown / CSV / JSON / 代码 / RTF / HTML / DOCX / XLSX / PPTX / EPUB / PDF，以及 PNG / JPG / WebP / BMP / TIFF / GIF 等图片；流式 multipart 解析，默认单文件最大 200 MB。
- 文件在本地后端解析、分块、缓存，聊天请求只发送 `fileId` 等元数据；提问时按问题从缓存检索相关片段。
- 文档阅读工作台：上传 PDF / 图片 / 文本后点「预览」，宽屏切换成左侧文档对话、右侧原文逐页阅读，支持翻页 / 缩放 / 目录 / 跨页搜索 / 框选区域转图片提问 / 翻译全文 / 一键总结。
- 回答引用回链：模型使用 `[^F1-2]` 这类引用标记时，前端渲染为可点击 pin 并打开对应文件片段预览。

### 图片视觉与 OCR
- **图片视觉理解**：上传图片默认交给 `deepseek-v4-pro` 多模态模型理解，可读图、看图答题、识别公式与图表，而不是只提取纯文字。
- OCR 作为「提取文字」的降级路径：优先调用 DeepSeek API 转写，API 不可用时桌面端回退本地 Tesseract / Windows OCR，Android APK 走内置 ML Kit；公式截图可接入 `pix2tex` 等本地公式 OCR 择优。

### 生成式产物
- DeepSeek function calling 可调用本地工具直接产出可下载文件：`create_pptx` 用 `python-pptx` 渲染真实 `.pptx`、`create_document` 生成排版精美的 `.docx` / `.pdf`、`create_mindmap` 生成可下载 `.svg` 思维导图并在正文内嵌预览。
- 还内建 `python_eval`、`search_files`、`fetch_url`、`web_search`、数据转换、图表、提醒、记忆等工具；终态文件工具成功后直接回传本地下载链接，不再把完整工具结果二次发回模型，保护 prompt cache 命中率。

### Workspace Core（项目 / 保存项 / 产物 / 导出）
- **Project 2.0**：项目成为一级工作台对象，统一聚合文件、对话、项目记忆、保存项、产物、trace 与导出记录；`GET /api/workspace/projects/{projectId}` 返回 `stats` 与子对象列表。
- **Saved Items**：聊天片段、助手回答、文件引用、RAG 片段、网页摘录、媒体说明、生成产物、Trace / Eval 结果都可以保存到项目，并用 `purpose=reference|memory_candidate|export_fragment` 标记后续用途。
- **Artifact Hub**：`.generated/` 中的 pptx / docx / pdf / svg / markdown / csv / json 等产物可注册为项目产物，支持预览、下载、重命名、版本记录与来源追踪。
- **Export**：对话、项目、保存项集合、产物包和证据包可导出为 Markdown / HTML / JSON / ZIP；项目 ZIP 固定包含 `metadata.json`、`conversations/`、`saved-items/`、`artifacts/`、`files/source-files/` 与 `traces/`，并做 API Key / token 脱敏。

### 协议互操作：MCP Tool Hub 与 A2A Agent Mesh
- **MCP-native Tool Hub**：`POST /mcp` 是一个 MCP JSON-RPC 2.0 server（`initialize` / `tools/list` / `tools/call` / `resources/list|read` / `prompts/list|get` / `ping`）。本地 17 个工具（搜索、抓取、文件检索、Python 计算、图表、思维导图、PPT / Word / PDF 生成、记忆、提醒）原样暴露为标准 MCP tools，输入 schema 与风险注解（read-only / destructive / open-world）一并下发；生成的产物（pptx / docx / pdf / svg）以 `generated://<fileId>` resources 暴露，另附 `slides-outline`、`research-brief` 两个 prompts。每个 `tools/call` 都过同一套 Tool Policy 闸门（capability 切片经 `MCP_CAPABILITY` 配置，schema / SSRF / 路径 / 敏感写入防护与结果清洗全部生效）。外接方向由内置 MCP client（`MCP_CLIENT_ENABLED=1` + `MCP_CLIENT_SERVERS`）消费外部 MCP server 的工具目录；v2.2.3 起支持 per-server timeout、retry、短期 circuit breaker，`GET /api/mcp/external/tools` 返回 server health / last error / last refresh / retry count，外部调用写入 `mcp_external` trace span 与 Prometheus 摘要指标。
- **A2A Agent Mesh**：每个本地 Agent 角色（orchestrator / researcher / coder / reasoner / critic）都有自己的 **Agent Card**（`/.well-known/agent-card.json` 做发现，`GET /a2a/agents` 列全部）与 JSON-RPC 任务生命周期：`message/send` 提交任务后台执行、`message/stream` 以 SSE 推送状态与 artifact chunks、`tasks/resubscribe` 按 `afterChunkIndex` 恢复订阅、`tasks/get` / `tasks/cancel` / `tasks/list` 管理任务。任务在角色的 capability 切片内执行（researcher 只有搜索面、reasoner 无工具），快照持久化到 `.a2a/`；`A2AClient` 可向 `A2A_PEERS` 里的外部 Agent 委派任务，`examples/a2a_peer_demo.py` 提供本地 external peer loopback。

### 上下文安全：Taint Tracking 与注入防火墙
- **Context Taint Tracking**：每次请求组装完成后，prompt 按来源分段打信任标签——系统提示 / 用户输入 / 记忆为可信，网页搜索上下文、上传文件内容、外部工具结果为不可信——并对不可信段扫描三类指令：prompt 注入（"ignore previous instructions" / "忽略上述指令"…）、密钥外泄（要求把 API Key 发送出去）、工具调用指令（资料里命令模型调用 `forget_memory` 等）。报告进 `diagnostics.contextTaint`，状态见 `GET /api/taint`。
- **主动防御**：搜索上下文与文件上下文前置确定性的「防注入隔离」声明并红action明确注入行（对 prompt cache 前缀稳定无损）；工具调用参数中出现运行时自身凭证（DeepSeek / Tavily Key、本地 token）一律硬拒绝（`secret_exfiltration_blocked`）；**污染轮升级**——本轮上下文检出注入指令后，高风险 / 敏感写入工具（`fetch_url`、`forget_memory`、`suggest_memory`、`create_reminder`）自动转为待人工确认，直到用户显式批准。

### 端云协同推理
- 可选接入本地端侧模型（`EDGE_INFERENCE_ENABLED=1` + `llama-cpp-python` 或 MLC-LLM 后端 + GGUF 路径）：`edgeMode=auto` 把闲聊 / 概括 / 改写 / 翻译等短任务优先路由到本地模型，代码 / 数学 / 搜索 / 文档生成 / 多 Agent / 图片任务继续走云端。
- 云端连接失败时简单任务可自动回退本地端侧模型；没有云端 API Key 但本地模型可用时，也能进行普通对话。

### 本地数据层与可观测性
- **本地 RAG**：`.file-cache`、`.projects` 和 `.memory` 同步进 `.local-rag/rag.sqlite3`，默认纯 SQLite + 哈希 embedding 零依赖；安装可选依赖后可启用 `sqlite-vec` 向量表与 ONNX Runtime 本地 embedding。
- **链路追踪**：每轮普通聊天、端侧推理和多 Agent DAG 都会生成 `traceId` 写入 `.traces/`，助手消息可打开 trace waterfall 查看各节点耗时、token 与 prompt cache 命中率；`GET /trace/{trace_id}` 提供独立只读瀑布页面，`GET /api/traces/{trace_id}/export.json` 导出脱敏 JSON；`GET /metrics` 聚合成 Prometheus 指标。
- **语义缓存**：无工具、无搜索、无附件的请求会在调用 DeepSeek 前查 `.semantic-cache/`，相似度达阈值时直接返回本地缓存结果。
- **API 网关韧性**：Context Manager 稳定 system prompt 与工具定义前缀，最大化 DeepSeek Prefix Cache 命中；SQLite 请求队列在断网、超时、429 / 5xx 等可重试失败时退避重试，手机息屏或短暂断网后后台 Agent 工作流可等网络恢复再续跑。
- **请求调度 / Backpressure / 限流**：所有上游模型调用先过本地请求调度层——优先级队列（交互 > Agent worker > 后台）、并发上限、令牌桶限流与 backpressure（过载即快速 503 卸载而非无界堆积），耗尽重试 / 被卸载的请求落入持久化 Dead Letter Queue，启动时后台恢复上次崩溃残留的在途请求。默认对正常负载透明，可经 `SCHEDULER_*` 收紧。
- **Evaluation Harness（可评测）**：`evals/` 提供 AI Runtime 回归评测 —— golden 数据集 + CLI runner 对 RAG Recall@K、引用准确率、工具策略、Prompt Injection 防御、Agent 录制回放、延迟与 token/USD 成本自动打分，评分核心是纯函数库 `deepseek_infra/infra/evaluation/harness.py`。CI 通过 `run_offline_eval_suite.py --include-agent --strict` 统一运行稳定离线的 RAG / Tool Policy / Prompt Injection adversarial / Agent Eval，写出 `evals/reports/latest.json` 与 `latest.md`；再用 `compare_eval_baseline.py --strict` 对照 v2.2.6 / agent-v2.2.8 baseline 阻断退化，并用 `run_security_corpus.py --strict` 输出版本化安全语料报告。

### 长期记忆 · Seek 助手
- 用「记住：…」保存偏好 / 项目背景 / 长期任务，用「忘记 …」删除；模型也可在回答中提出记忆建议，需用户确认才写入。支持 `global` / `project:<id>` / `seek:<id>` 作用域与冲突检测；敏感内容会被拦截，不进入长期记忆（`.memory/memories.json`）。
- Seek 助手：创建本地自定义助手（名称 / 指令 / 开场提示 / 参考文件），对话中自动注入；参考文件随消息快照保存。最多 40 个，支持导入 / 导出 JSON。

### 前端体验
- PWA：manifest、图标和 Service Worker 齐全，可安装到手机桌面；支持 Share Target 从系统分享导入文章 / URL / 图片 / 文档。
- 四种视觉风格（ChatGPT / Linear / Notion / Arc）× 浅色 / 深色 / 跟随系统，外加可选 Gemini 皮肤；Web Speech 语音输入与朗读、拖拽 / 粘贴上传、选区引用提问、草稿自动保存、本地提醒、命令面板（`Ctrl/Cmd+K`）与全局快捷键。后端不可用时进入离线模式，仍可查看 / 搜索本地历史。

## OpenAI 兼容网关

把任意 OpenAI SDK 或工具的 `base_url` 指向本机 `/v1` 即可复用整套运行时。`api_key` 传**本地访问 token**（用于本地鉴权）；上游 DeepSeek Key 由服务端配置（`DEEPSEEK_API_KEY`）提供。

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<本地访问 token>")
resp = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

```bash
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <本地访问 token>"
```

- `POST /v1/chat/completions`：支持 `stream` 流式（标准 `chat.completion.chunk` SSE + `[DONE]`）与非流式（`chat.completion`）。
- `GET /v1/models`：列出可用模型（`deepseek-v4-pro` / `deepseek-v4-flash`）。
- **多 Provider**：启用 Ollama（`OLLAMA_ENABLED=1`）后，`/v1/models` 会额外列出 `ollama/<本地模型>`，请求该模型即走本地 Ollama；DeepSeek 专属的工具 / 搜索 / 多 Agent 仍只在 DeepSeek 模型上可用。
- 完整字段见 [docs/API.md](docs/API.md)。

## 协议端点（MCP / A2A）

- `POST /mcp` — MCP JSON-RPC 2.0 端点（本地 token 鉴权）。把 MCP 客户端的 Streamable HTTP server 地址指向它即可使用本地工具面；`GET /api/mcp` 查看 Hub 状态。
- `GET /.well-known/agent-card.json` — A2A 发现：orchestrator 的 Agent Card（仅元数据，不鉴权）。
- `GET /a2a/agents` — 全部本地 Agent Card；`POST /a2a` 与 `POST /a2a/agents/{agentId}` — A2A JSON-RPC（`message/send` / `message/stream` / `tasks/resubscribe` / `tasks/get` / `tasks/cancel` / `tasks/list`，本地 token 鉴权）。
- `GET /api/taint` — Context Taint 防火墙状态。

## 运维端点

- `GET /healthz` — liveness：`{status, version, runtime, provider, auth_enabled}`（不鉴权）。
- `GET /readyz` — readiness：本地存储可达性与上游 Key 配置状态（不鉴权）。
- `GET /metrics` — Prometheus 文本：`ai_requests_total`、`ai_agent_runs_total`、`ai_model_calls_total`、`ai_semantic_cache_hits_total`、`ai_tokens_total`、`ai_run_latency_ms_avg` 等。默认随服务绑定在 `127.0.0.1`。

## Benchmarks（基准与评测）

[benchmarks/](benchmarks/) 提供 4 个可复跑基准（均支持 `--json`）。下表**离线两项是实测数字**（逐次可复现），在线两项依赖你的网络与上游负载，给运行方式不给编造数字：

> **Benchmark 环境**：Windows 11 · Python 3.13 · CPU Intel i7-13700H · RAM 16 GB · SSD · 数据集 95 chunks · runs 10 · warmup 2 · 报告 avg · 默认零依赖 hash embedding 路径

| 基准 | 关键结果（实测） | 复跑命令 |
| --- | --- | --- |
| RAG 检索（离线） | 95 chunks 索引 130 ms；检索 avg 20.2 ms · P95 21.7 ms；**Recall@5 1.000 · MRR 0.917** | `python benchmarks/bench_rag_retrieval.py` |
| 语义缓存（离线） | hash provider：store avg 17.9 ms · lookup avg 8.4 ms；**精确命中 1.00 · 无关误命中 0.00**；改写命中 0.00（hash 相似度约 0.80 < 阈值 0.95，保守不误答是预期行为）。ONNX provider 可用 `--provider onnx` 单独 benchmark 后再决定是否默认启用。 | `python benchmarks/bench_semantic_cache.py --provider hash` |
| 聊天延迟（在线） | 流式 TTFT / 总延迟 avg·P50·P95、token 用量、语义缓存命中分布 | `python benchmarks/bench_chat_latency.py --n 3` |
| Agent DAG（在线） | 端到端延迟、每 Agent 耗时表、token 与估算成本 | `python benchmarks/bench_agent_dag.py` |

与之配套的**质量评测**在 [evals/](evals/)（全部离线可跑）：`python evals/runners/run_offline_eval_suite.py --include-agent --strict` 会统一留下 [latest eval report](evals/reports/latest.md)，当前 baseline 为 RAG Recall@5 1.000 / Citation Accuracy 0.8333、26 个固定攻防用例的 **Tool Policy Pass Rate 1.000 / Prompt Injection Defense Pass 1.000**，以及对抗注入小语料的 `block_rate` / `false_positive_rate` / `bypass_rate` 硬门禁；`run_agent_eval.py --strict` 额外生成 [Agent Eval report](evals/reports/agent-latest.md)，低于 Tool Call Accuracy 0.90 / Agent Success Rate 0.85 / Prompt Regression Pass Rate 0.90 会阻断 CI。`run_security_corpus.py --strict` 生成 [Security Corpus report](evals/reports/security-latest.md)，覆盖 prompt injection、tool policy attack、benign false-positive、SSRF、路径越界与密钥外泄语料。详见 [evals/README.md](evals/README.md)、[docs/EVAL_REPORTS.md](docs/EVAL_REPORTS.md) 与 [docs/AGENT_EVAL.md](docs/AGENT_EVAL.md)。本地安全能力复现最小命令集见 [docs/SECURITY_SMOKE.md](docs/SECURITY_SMOKE.md)。

**发版前一键体检（v2.5.8）**：先 `python scripts/doctor.py --offline` 做运行时体检（Python / 依赖 / .env / 数据目录权限 / static / 端口 / token，PASS / WARNING / FAIL），再 `python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.5.8.json` 生成 Workspace Core evidence；协议与生态 evidence 仍可按需刷新：`python scripts/smoke_mcp_headless_bridge.py`、`python scripts/smoke_a2a_external_peer.py`、第三方 A2A smoke、Edge Router smoke、OpenAI-compatible SDK smoke。随后 `python scripts/preflight_release.py --version 2.5.8` 校验版本徽章 / CHANGELOG / Docker tag / 文档与 eval 报告版本同步，并检查 coverage 80%、offline eval、Agent Eval、baseline compare、injection strict、security corpus、Workspace Core、A2A third-party、Edge Router smoke、Continue.dev MCP、OpenAI-compatible SDK 与 GUI interop 的质量证据，最后 `python scripts/smoke_release.py --offline` 一键编排 doctor + Workspace Core smoke + strict eval + security corpus + Agent Eval + baseline compare（`--with-server --base-url ... --token ...` 额外跑 MCP / A2A smoke）。发布产物会同时生成 `.sha256` 与带 `qualityGates.workspaceCore=PASS` 的 `.manifest.json` 作为 release evidence。详见 [docs/WORKSPACE.md](docs/WORKSPACE.md)、[docs/RUNTIME_DOCTOR.md](docs/RUNTIME_DOCTOR.md) 与 [docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md)。

## 快速开始

### 方式 1（推荐）：本地桌面应用窗口

1. 安装一次 Python 依赖：
   ```powershell
   python -m pip install -r requirements.txt
   ```
2. **Windows** 直接双击 `launch.bat`，**macOS / Linux** 双击或执行 `./launch.sh`，会打开 DeepSeek Infra 本地应用窗口。
3. 在应用右上角设置里填写 DeepSeek API Key（必填）和 Tavily API Key（可选）；也可以先通过环境变量提供 Key。

桌面应用会自动使用带 `desktop=1` 的本地 token 入口完成认证，双击后不需要手动复制 token 链接；如果改用浏览器访问命令行服务，仍使用终端打印的 `?token=...` 地址。需要手动选择端口、局域网模式或查看服务日志时，运行 `python launch.py --gui` 或 `DeepSeekInfra.exe --gui`（旧名 `DeepSeekMobile.exe` 继续兼容）打开旧 GUI 启动器。

### 方式 2：手机本机直接运行

Android 手机上可以用 Termux 或 Pydroid 这类 Python 环境直接跑后端，然后在同一台手机的浏览器里打开本机地址：

```bash
python -m pip install -r requirements-mobile.txt
python launch_mobile.py
```

也可以运行 `python launch.py --mobile`。手机启动器不会导入桌面 GUI 依赖，默认监听 `127.0.0.1:8000`，启动后会打印 `Open on this phone` 地址；如果环境里有 `termux-open-url`，会尝试自动打开浏览器。需要让同一局域网其它设备访问这台手机时，加 `--lan` 监听 `0.0.0.0`。

常用参数：

```bash
python launch_mobile.py --api-key "你的 DeepSeek API Key" --tavily-api-key "你的 Tavily API Key（可选）"
python launch_mobile.py --port 8010 --no-open
python launch_mobile.py --lan
```

### 方式 3：命令行启动（兼容旧用法）

```powershell
cd D:\deepseek
python -m pip install -r requirements.txt
$env:DEEPSEEK_API_KEY="你的 DeepSeek API Key"
$env:TAVILY_API_KEY="你的 Tavily API Key（可选，用于联网搜索）"
python app.py
```

macOS / Linux：

```bash
cd /path/to/deepseek
python -m pip install -r requirements.txt
export DEEPSEEK_API_KEY="你的 DeepSeek API Key"
export TAVILY_API_KEY="你的 Tavily API Key（可选，用于联网搜索）"
python app.py
```

也可以不设置环境变量，在页面右上角设置里临时填写 API Key。启动后终端会打印两个地址：`Computer`（电脑本机访问）和 `Phone`（手机访问，需与电脑在同一 Wi-Fi 或局域网）。

默认情况下所有 `/api/*` 请求都需要本地访问令牌，请使用终端打印的带 `?token=...` 的地址打开应用，浏览器会自动保存认证 Cookie；桌面本地应用窗口会自动用 `?token=...&desktop=1` 完成首屏认证。默认 token 会写入本地 `.auth-token` 并在重启后复用。

### 方式 4：打包成单个 exe 分发

需要把项目分发给完全没装 Python 的电脑时：

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
python scripts/build_exe.py
```

会在 `dist/` 输出单个 `DeepSeekInfra.exe`（旧名 `DeepSeekMobile.exe` 以副本保留兼容；macOS / Linux 同）。双击默认打开本地应用窗口；运行期间产生的 `.auth-token` / `.file-cache` / `.memory` 等数据会写到 exe 同目录。旧启动器可通过 `DeepSeekInfra.exe --gui` 打开。

### 方式 5：打包成 Android APK

仓库内的 `android/` Android Studio 工程可把现有 Python 后端和 Web 前端打进 APK。APK 启动后会在应用私有目录运行 Python 服务，并用内置 WebView 打开 `127.0.0.1` 本机地址，手机上无需再安装 Termux 或 Pydroid。

```bash
cd android
gradle :app:assembleDebug
```

输出位置：`android/app/build/outputs/apk/debug/app-debug.apk`。详细环境、签名和安装说明见 [docs/APK.md](docs/APK.md)。

### 方式 6：Docker / Compose 部署

```bash
cp .env.example .env   # 填写 DEEPSEEK_API_KEY 等
docker compose up -d
curl http://127.0.0.1:8000/healthz
```

镜像为 python:3.12-slim、非 root 运行、内置 `/healthz` HEALTHCHECK；全部运行时数据持久化在一个 `/data` 卷。详见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)（含裸机 / systemd / 反向代理与安全边界）。

## 环境变量

- `DEEPSEEK_API_KEY`：DeepSeek API Key。可不填，改为在页面设置里临时输入。
- `TAVILY_API_KEY`：Tavily 搜索 API Key。可不填，改为在页面设置里临时输入或选择保存到本机浏览器。
- `PORT`：本地服务起始端口，默认 `8000`。
- `HOST=0.0.0.0`：开启局域网 / 手机访问；默认只监听 `127.0.0.1`。
- `DEEPSEEK_INFRA_ROOT=/path/to/data`：**推荐**，指定数据根目录（替代 `DEEPSEEK_MOBILE_ROOT`）。
- `DEEPSEEK_MOBILE_ROOT=/path/to/data`：向后兼容，与 `DEEPSEEK_INFRA_ROOT` 同时设置时后者优先。
- `DEEPSEEK_INFRA_STATIC_DIR=/path/to/static`：**推荐**，指定静态资源目录（替代 `DEEPSEEK_MOBILE_STATIC_DIR`）。
- `DEEPSEEK_MOBILE_STATIC_DIR=/path/to/static`：向后兼容，与 `DEEPSEEK_INFRA_STATIC_DIR` 同时设置时后者优先。
- `AUTH_DISABLED=1`：关闭本地 token 鉴权，仅建议在可信开发环境使用。
- `AUTH_TOKEN=...`：使用固定 token，便于本地测试。
- `AUTH_ALLOWED_HOSTS=host1,host2`：追加允许的 Host 头名称。
- `OCR_ENABLED=1`：默认允许 OCR；未开启时也可在上传失败后点击 OCR 重试。
- `OCR_MODE=fast|balanced|quality`：本地 OCR 增强档位，默认 `balanced`。
- `OCR_PDF_DPI=300`：扫描 PDF 渲染 DPI，限制在 `150..450`，默认 `300`。
- `OCR_MAX_IMAGE_PIXELS=16000000`：OCR 前允许处理的最大图片像素数，超出会等比缩小。
- `OCR_FORMULA_CMD='pix2tex "{image}"'`：可选的本地公式 OCR 命令；未设置时会自动尝试 PATH 中的 `pix2tex` / `latexocr`。
- `OCR_FORMULA_TIMEOUT_SECONDS=120`：公式 OCR 命令超时，限制在 `5..600` 秒。
- `DEEPSEEK_TIMEOUT_SECONDS`：DeepSeek 同步、流式和上下文压缩请求的 socket idle 超时，默认 `180`。
- `MULTI_AGENT_TIMEOUT_SECONDS`：多 Agent 并行层级超时，默认 `3900`；长任务建议与 `DEEPSEEK_TIMEOUT_SECONDS` 一起调高。
- `MULTI_AGENT_TOKEN_BUDGET`：多 Agent 单次运行的 token 预算，默认 `2000000`，设 `0` 不限制。
- `TAVILY_TIMEOUT_SECONDS`：Tavily 搜索请求超时，默认 `45`。
- `UPLOAD_FILE_MAX_BYTES` / `UPLOAD_MAX_BYTES`：单文件 / 单次请求体上限，默认 `200000000` / `220000000`。
- `EDGE_INFERENCE_ENABLED=1` / `EDGE_INFERENCE_PROVIDER` / `EDGE_MODEL_PATH`：可选的本地端侧推理开关、后端与 GGUF 模型路径，详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
- `OLLAMA_ENABLED=1` / `OLLAMA_BASE_URL` / `OLLAMA_TIMEOUT_SECONDS`：可选的本地 Ollama provider 开关、地址（默认 `http://127.0.0.1:11434`）与超时；启用后本地模型经 `/v1` 网关以 `ollama/<tag>` 暴露。
- `TOOL_POLICY_ENABLED=1`（默认开）/ `TOOL_POLICY_ENFORCE_SCHEMA` / `TOOL_POLICY_REQUIRE_CONFIRM` / `TOOL_POLICY_SANITIZE_RESULTS`（默认开）/ `TOOL_POLICY_AUDIT_ENABLED`（默认开）：Tool Policy Engine 开关——是否启用工具调用安全策略、是否把 schema 违例从告警升级为硬拒绝、是否对高风险工具强制人工确认、是否清洗工具结果中的 prompt injection、是否把每条策略决策写入 `.tool-audit/audit.jsonl`。
- `SCHEDULER_ENABLED=1`（默认开）/ `SCHEDULER_MAX_CONCURRENCY`（默认 16）/ `SCHEDULER_MAX_QUEUE_DEPTH`（默认 256，backpressure 卸载阈值）/ `SCHEDULER_RATE_PER_SECOND`（默认 0=不限流）/ `SCHEDULER_RATE_BURST` / `SCHEDULER_ACQUIRE_TIMEOUT_SECONDS`（默认 30）/ `SCHEDULER_DLQ_ENABLED`（默认开）/ `SCHEDULER_ORPHAN_SECONDS`（默认 900）：本地请求调度层——并发上限、队列深度（backpressure）、令牌桶限流速率与突发、准入超时、Dead Letter Queue 开关、启动时回收多久前的在途请求。
- `MCP_ENABLED=1`（默认开）/ `MCP_CAPABILITY=full`（MCP 客户端获得的能力画像）/ `MCP_EXPOSE_RESOURCES`、`MCP_EXPOSE_PROMPTS`（默认开）/ `MCP_CLIENT_ENABLED`（默认关）/ `MCP_CLIENT_SERVERS='[{"name":"docs","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":10}]'` / `MCP_CLIENT_TIMEOUT_SECONDS`（默认 30）/ `MCP_CLIENT_MAX_RETRIES`（默认 1）/ `MCP_CLIENT_RETRY_BACKOFF_SECONDS`（默认 0.25）/ `MCP_CLIENT_CIRCUIT_BREAKER_FAILURES`（默认 3）/ `MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS`（默认 60）：MCP Tool Hub 与外接 MCP client。
- `A2A_ENABLED=1`（默认开）/ `A2A_DEFAULT_AGENT=reasoner` / `A2A_MAX_TASKS`（默认 200）/ `A2A_HISTORY_LIMIT`（默认 20）/ `A2A_PEERS=url1,url2`（外部 A2A Agent 端点）：A2A Agent Mesh。任务执行需要服务端 `DEEPSEEK_API_KEY`。
- `TAINT_ENABLED=1`（默认开）/ `TAINT_HARDEN_SEARCH_CONTEXT`、`TAINT_HARDEN_FILE_CONTEXT`（默认开，给不可信上下文加隔离声明）/ `TAINT_ESCALATE_CONFIRM`（默认开，污染轮高危工具升级人工确认）：Context Taint Tracking 与注入防火墙。

## 安装与依赖

`requirements.txt` 包含后端运行与文件解析所需的依赖，主要有：

- `openpyxl`：读取 `.xlsx`；`pypdf` / `PyMuPDF`：读取 PDF 文字与逐页渲染。
- `python-pptx`：生成 `.pptx`；`reportlab`：生成带内置中文字体的 PDF。
- `multipart`：流式解析 `multipart/form-data` 上传；`defusedxml`：安全解析 `.docx` / `.xlsx` 内部 XML。
- `customtkinter`：桌面 GUI 启动器依赖；手机本机运行改用 `requirements-mobile.txt`，无需安装这一项。
- `pywebview`：桌面端本地应用窗口依赖，用系统 WebView 显示本机界面。

> 注意：正式依赖是 `multipart>=1.3,<2`。如果环境里同时装了占用同名命名空间的 `python-multipart`，上传接口会返回明确的依赖错误；请按 `requirements.txt` 重新安装。

可选依赖按需安装：`requirements-ocr.txt`（本地 OCR）、`requirements-rag.txt`（`sqlite-vec` / ONNX 本地 embedding）、`requirements-edge.txt`（`llama-cpp-python` 端侧推理）、`requirements-build.txt`（PyInstaller 打包）。

图片 OCR 优先用 `DEEPSEEK_API_KEY` 调 DeepSeek API 转写；API Key 缺失或识别不可用时，桌面端才回退本地 Tesseract / Windows OCR。扫描 PDF 需要 Poppler / `pdftoppm` 在 `PATH` 中；Android APK 用 ML Kit 作为本机兜底。

## 本地数据与隐私

主要数据都保存在本机：

- 对话历史 / 未发送草稿 / 自定义 Seek：浏览器 `localStorage`。
- 项目空间 / 文档库 / 保存项 / 产物索引 / 导出记录：`.projects/{projectId}/`。
- 文件分块缓存：`.file-cache`；搜索缓存：`.search-cache`。
- 本地 RAG 向量索引：`.local-rag/rag.sqlite3`。
- 链路追踪：`.traces/traces.sqlite3`；语义缓存：`.semantic-cache/cache.sqlite3`。
- 网关请求队列：`.request-queue/queue.sqlite3`；工具策略审计日志：`.tool-audit/audit.jsonl`；请求调度死信队列：`.scheduler/scheduler.sqlite3`。
- 本地提醒队列：`.reminders/reminders.json`；长期记忆：`.memory/memories.json`。
- 可恢复 Agent Run：`.agent-runs/`；A2A 任务快照：`.a2a/`。
- 生成的文档产物（PPT / Word / PDF / 思维导图）与全局 Workspace 导出：`.generated/`。
- API Key：DeepSeek / Tavily Key 可选择保存在浏览器，也可以只用服务端环境变量。

文件分块缓存会自动清理：默认保留 14 天内缓存并把 `.file-cache` 总量控制在约 500 MB；`.projects/` 是持久 Workspace 数据，只在删除对应项目时移除，不会误删全局 `.generated/`、`.memory/` 或其它项目。服务启动时清理一次，运行期间约每 6 小时后台清理一次。

`.gitignore` 默认排除运行期缓存、长期记忆、项目文档库、本地 RAG / Trace / 语义缓存 / 请求队列、生成文档产物、A2A 任务快照、提醒队列、覆盖率、IDE 配置和本地 `server*.log`。发布或提交前，请不要把 `.file-cache`、`.projects`、`.local-rag`、`.traces`、`.semantic-cache`、`.request-queue`、`.generated`、`.tool-audit`、`.scheduler`、`.a2a`、`.budget`、`.memory`、`.reminders`、`.agent-runs`、`.search-cache` 等本地数据打包进去。发布压缩包建议使用：

```powershell
python scripts/release.py --clean-workspace
```

脚本会生成 `dist/deepseek-infra-<version>.zip`（旧名 `deepseek-mobile-<version>.zip` 以副本保留兼容），并排除本地缓存、日志、虚拟环境和 IDE 文件。

## Roadmap

已完成的能力以 [实现状态矩阵](docs/IMPLEMENTATION_STATUS.md) 为准（含各模块成熟度与明确缺口）；下面是接下来的计划，完成一项勾一项：

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

## 文档

- [CHANGELOG.md](CHANGELOG.md) — 逐版本变更记录。
- [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) — **实现状态矩阵**：10 个模块的代码 / 测试 / Demo 落地程度与缺口。
- [docs/WORKSPACE.md](docs/WORKSPACE.md) — Workspace Core 对象模型、API、导出包结构与 smoke evidence。
- [docs/DEMO.md](docs/DEMO.md) — 2 分钟 Demo 路径（含离线可跑项）。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 分层架构、infra 模块、端云路由与本地数据层。
- [docs/API.md](docs/API.md) — HTTP API、OpenAI 兼容 `/v1` 与鉴权。
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Docker / Compose / 裸机部署与反向代理边界。
- [docs/SECURITY.md](docs/SECURITY.md) — 鉴权、敏感数据与本地安全边界。
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) — 七类威胁 → 缓解实现 → 测试的逐条映射。
- [docs/FRONTEND_MODULES.md](docs/FRONTEND_MODULES.md) — 前端模块拆分。
- [docs/APK.md](docs/APK.md) — Android 打包、签名与安装。
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — MCP / A2A / OpenAI 客户端兼容性矩阵。
- [docs/EVIDENCE_INDEX.md](docs/EVIDENCE_INDEX.md) — MCP / A2A / GUI / eval / release evidence 总索引。
- [docs/EDGE_ROUTER_RUNBOOK.md](docs/EDGE_ROUTER_RUNBOOK.md) — Edge Router / Ollama / GGUF 本地验收步骤。
- [docs/integrations/claude-desktop.md](docs/integrations/claude-desktop.md) / [docs/integrations/cursor.md](docs/integrations/cursor.md) — MCP 客户端配置片段与排障步骤。
- [evals/README.md](evals/README.md) — 评测 harness；[docs/EVAL_REPORTS.md](docs/EVAL_REPORTS.md) — 离线评测报告与回归基线；[docs/AGENT_EVAL.md](docs/AGENT_EVAL.md) — Agent 录制回放规范；[benchmarks/README.md](benchmarks/README.md) — 基准说明。
- [docs/RUNTIME_DOCTOR.md](docs/RUNTIME_DOCTOR.md) — 运行时体检（`scripts/doctor.py`）；[docs/RELEASE_READINESS.md](docs/RELEASE_READINESS.md) — 发版前体检与发布产物证明（`scripts/preflight_release.py` / `scripts/smoke_release.py` / release manifest）。

## 注意事项

手机浏览器可以直接使用 `http://局域网IP:端口`。如果要像正式 App 一样稳定安装到手机桌面，通常需要 HTTPS 部署；本地 HTTP 更适合开发和局域网试用。`/metrics`、`/healthz`、`/readyz` 默认不鉴权，请保持服务绑定在 `127.0.0.1`，或在局域网模式下用反向代理 / 防火墙限制访问。PWA 缓存清理由 `static/sw.js` 的 activate 阶段统一负责。
