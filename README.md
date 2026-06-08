# DeepSeek Infra

![版本](https://img.shields.io/badge/version-2.0.3-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-green)
![许可证](https://img.shields.io/badge/license-MIT-black)

**DeepSeek Infra is a local-first AI runtime platform for building private AI agents across desktop, mobile and local networks.**

DeepSeek Infra 是一个**本地优先的 AI Runtime / Agent 基础设施**：一套本机 FastAPI 后端把 LLM 网关、多 Agent DAG 运行时、本地向量 RAG、工具调用运行时、链路可观测性和端云模型路由组装成一个可私有化、多端运行、可观测、可扩展的 Agentic AI 系统。桌面端双击打开内嵌 WebView 的本地应用窗口，Android 端打包成 APK，任何 OpenAI 兼容客户端也能把 `base_url` 指向本机 `/v1`。除了你主动发往 DeepSeek / Tavily 的请求，数据都留在本机。

> 想看逐版本变更记录，请见 [CHANGELOG.md](CHANGELOG.md)。本文档描述**当前版本**的架构与用法。

## 核心基础设施模块

| # | 模块 | 代码位置 | 职责 |
| --- | --- | --- | --- |
| 1 | **LLM Gateway** | `infra/gateway/` | OpenAI 兼容 `/v1` 门面、模型路由、流式转发、Prompt Cache 友好的上下文管理、请求队列重试与 fallback。 |
| 2 | **Agent DAG Runtime** | `infra/agent_runtime/` | Planner 动态生成执行图、依赖调度、同层并行、Critic 修订环、token 预算护栏、事件持久化与断线重放。 |
| 3 | **Local RAG Data Layer** | `infra/rag/` | 文档解析 / 分块 / 本地 embedding / SQLite·sqlite-vec 向量索引 / 混合检索 / 引用回链。 |
| 4 | **Tool Calling Runtime** | `infra/tool_runtime/` | 受控本地工具执行（数学沙箱、文件检索、URL 精读、PPT / Word / PDF / 思维导图生成等）。 |
| 5 | **Observability & Trace** | `infra/observability/` | 每轮请求的 trace run/span、瀑布图、`/metrics` Prometheus 指标、`/healthz`·`/readyz` 探针。 |
| 6 | **Edge-Cloud Model Router** | `infra/gateway/edge_inference.py` | 简单任务路由到本地端侧模型，复杂任务走云端 DeepSeek，云端失败可回退本地。 |

> 跨端运行打包（Desktop WebView / Android APK / 单文件 exe / 手机控制台启动器）由 `web/`、`launcher/`、`android_entry.py` 与 `desktop_app.py` 承载。

## 架构分层

```
┌─────────────────────────────────────────────────────────────┐
│                        Client Layer                          │
│      Web UI / PWA  ·  Desktop WebView  ·  Android APK         │
└────────────────────────────┬─────────────────────────────────┘
                             │  HTTP · NDJSON · SSE · OpenAI /v1
┌────────────────────────────▼─────────────────────────────────┐
│                 Local AI Runtime  (FastAPI / ASGI)           │
│   Auth · Session · Streaming · /v1/chat · /healthz · /metrics │
└───────┬──────────────────────┬──────────────────────┬─────────┘
        │                      │                      │
┌───────▼────────┐   ┌─────────▼────────┐   ┌─────────▼─────────┐
│  LLM Gateway   │   │  Agent DAG       │   │  Tool Runtime     │
│  + Model Router│   │  Runtime         │   │  + Sandbox        │
└───────┬────────┘   └─────────┬────────┘   └─────────┬─────────┘
        │                      │                      │
┌───────▼──────────────────────▼──────────────────────▼─────────┐
│              Local Data & Observability Layer                 │
│   Vector RAG · Memory · Trace · Semantic Cache · Request Queue │
└───────────────────────────────────────────────────────────────┘
```

## 亮点

- **一套后端，多种形态**：同一份 Python 后端既能作为桌面本地应用窗口（内嵌 WebView，不跳外部浏览器）运行，也能打包成 Android APK，或作为本机 / 局域网服务启动。
- **本地优先、数据不出端**：对话历史、草稿、文件缓存、向量索引、长期记忆、追踪与缓存全部保存在本机；DeepSeek / Tavily API Key 可以只用环境变量、不落库。
- **OpenAI 兼容网关**：`POST /v1/chat/completions` + `GET /v1/models`，任何 OpenAI SDK / 工具把 `base_url` 指向本机即可复用整套运行时。
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

### 端云协同推理
- 可选接入本地端侧模型（`EDGE_INFERENCE_ENABLED=1` + `llama-cpp-python` 或 MLC-LLM 后端 + GGUF 路径）：`edgeMode=auto` 把闲聊 / 概括 / 改写 / 翻译等短任务优先路由到本地模型，代码 / 数学 / 搜索 / 文档生成 / 多 Agent / 图片任务继续走云端。
- 云端连接失败时简单任务可自动回退本地端侧模型；没有云端 API Key 但本地模型可用时，也能进行普通对话。

### 本地数据层与可观测性
- **本地 RAG**：`.file-cache`、`.projects` 和 `.memory` 同步进 `.local-rag/rag.sqlite3`，默认纯 SQLite + 哈希 embedding 零依赖；安装可选依赖后可启用 `sqlite-vec` 向量表与 ONNX Runtime 本地 embedding。
- **链路追踪**：每轮普通聊天、端侧推理和多 Agent DAG 都会生成 `traceId` 写入 `.traces/`，助手消息可打开 trace waterfall 查看各节点耗时、token 与 prompt cache 命中率；`GET /metrics` 把这些聚合成 Prometheus 指标。
- **语义缓存**：无工具、无搜索、无附件的请求会在调用 DeepSeek 前查 `.semantic-cache/`，相似度达阈值时直接返回本地缓存结果。
- **API 网关韧性**：Context Manager 稳定 system prompt 与工具定义前缀，最大化 DeepSeek Prefix Cache 命中；SQLite 请求队列在断网、超时、429 / 5xx 等可重试失败时退避重试，手机息屏或短暂断网后后台 Agent 工作流可等网络恢复再续跑。

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

## 运维端点

- `GET /healthz` — liveness：`{status, version, runtime, provider, auth_enabled}`（不鉴权）。
- `GET /readyz` — readiness：本地存储可达性与上游 Key 配置状态（不鉴权）。
- `GET /metrics` — Prometheus 文本：`ai_requests_total`、`ai_agent_runs_total`、`ai_model_calls_total`、`ai_semantic_cache_hits_total`、`ai_tokens_total`、`ai_run_latency_ms_avg` 等。默认随服务绑定在 `127.0.0.1`。

## 快速开始

### 方式 1（推荐）：本地桌面应用窗口

1. 安装一次 Python 依赖：
   ```powershell
   python -m pip install -r requirements.txt
   ```
2. **Windows** 直接双击 `launch.bat`，**macOS / Linux** 双击或执行 `./launch.sh`，会打开 DeepSeek Infra 本地应用窗口。
3. 在应用右上角设置里填写 DeepSeek API Key（必填）和 Tavily API Key（可选）；也可以先通过环境变量提供 Key。

桌面应用会自动使用带 `desktop=1` 的本地 token 入口完成认证，双击后不需要手动复制 token 链接；如果改用浏览器访问命令行服务，仍使用终端打印的 `?token=...` 地址。需要手动选择端口、局域网模式或查看服务日志时，运行 `python launch.py --gui` 或 `DeepSeekMobile.exe --gui` 打开旧 GUI 启动器。

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

会在 `dist/` 输出单个 `DeepSeekMobile.exe`（macOS / Linux 是 `DeepSeekMobile`）。双击默认打开本地应用窗口；运行期间产生的 `.auth-token` / `.file-cache` / `.memory` 等数据会写到 exe 同目录。旧启动器可通过 `DeepSeekMobile.exe --gui` 打开。

### 方式 5：打包成 Android APK

仓库内的 `android/` Android Studio 工程可把现有 Python 后端和 Web 前端打进 APK。APK 启动后会在应用私有目录运行 Python 服务，并用内置 WebView 打开 `127.0.0.1` 本机地址，手机上无需再安装 Termux 或 Pydroid。

```bash
cd android
gradle :app:assembleDebug
```

输出位置：`android/app/build/outputs/apk/debug/app-debug.apk`。详细环境、签名和安装说明见 [docs/APK.md](docs/APK.md)。

## 环境变量

- `DEEPSEEK_API_KEY`：DeepSeek API Key。可不填，改为在页面设置里临时输入。
- `TAVILY_API_KEY`：Tavily 搜索 API Key。可不填，改为在页面设置里临时输入或选择保存到本机浏览器。
- `PORT`：本地服务起始端口，默认 `8000`。
- `HOST=0.0.0.0`：开启局域网 / 手机访问；默认只监听 `127.0.0.1`。
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
- 项目空间 / 文档库：`.projects/{projectId}/`。
- 文件分块缓存：`.file-cache`；搜索缓存：`.search-cache`。
- 本地 RAG 向量索引：`.local-rag/rag.sqlite3`。
- 链路追踪：`.traces/traces.sqlite3`；语义缓存：`.semantic-cache/cache.sqlite3`。
- 网关请求队列：`.request-queue/queue.sqlite3`。
- 本地提醒队列：`.reminders/reminders.json`；长期记忆：`.memory/memories.json`。
- 可恢复 Agent Run：`.agent-runs/`。
- API Key：DeepSeek / Tavily Key 可选择保存在浏览器，也可以只用服务端环境变量。

文件分块缓存会自动清理：默认保留 14 天内缓存并把 `.file-cache` 总量控制在约 500 MB；`.projects/` 是持久文档库，只在删除项目时移除。服务启动时清理一次，运行期间约每 6 小时后台清理一次。

`.gitignore` 默认排除运行期缓存、长期记忆、项目文档库、本地 RAG / Trace / 语义缓存 / 请求队列、提醒队列、覆盖率、IDE 配置和本地 `server*.log`。发布或提交前，请不要把 `.file-cache`、`.projects`、`.local-rag`、`.traces`、`.semantic-cache`、`.request-queue`、`.memory`、`.reminders`、`.search-cache` 等本地数据打包进去。发布压缩包建议使用：

```powershell
python scripts/release.py --clean-workspace
```

脚本会生成 `dist/deepseek-mobile-<version>.zip`，并排除本地缓存、日志、虚拟环境和 IDE 文件。

## 文档

- [CHANGELOG.md](CHANGELOG.md) — 逐版本变更记录。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 分层架构、infra 模块、端云路由与本地数据层。
- [docs/API.md](docs/API.md) — HTTP API、OpenAI 兼容 `/v1` 与鉴权。
- [docs/FRONTEND_MODULES.md](docs/FRONTEND_MODULES.md) — 前端模块拆分。
- [docs/APK.md](docs/APK.md) — Android 打包、签名与安装。
- [docs/SECURITY.md](docs/SECURITY.md) — 鉴权、敏感数据与本地安全边界。

## 注意事项

手机浏览器可以直接使用 `http://局域网IP:端口`。如果要像正式 App 一样稳定安装到手机桌面，通常需要 HTTPS 部署；本地 HTTP 更适合开发和局域网试用。`/metrics`、`/healthz`、`/readyz` 默认不鉴权，请保持服务绑定在 `127.0.0.1`，或在局域网模式下用反向代理 / 防火墙限制访问。PWA 缓存清理由 `static/sw.js` 的 activate 阶段统一负责。
