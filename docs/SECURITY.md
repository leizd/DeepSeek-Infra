# 安全说明

适用版本：v2.6.5。

## 威胁模型

本项目定位为个人、本地优先的客户端。默认假设运行后端的电脑可信，浏览器会话属于同一个用户。默认监听地址是 `127.0.0.1`，如果需要手机或局域网访问，需要显式设置 `HOST=0.0.0.0`。

不要把局域网模式暴露在不可信网络中。服务端环境变量中的 DeepSeek / Tavily Key 会对所有通过本地鉴权的浏览器会话可用。

> 七类核心威胁（网页注入 / 恶意文件 / SSRF / 路径越界 / 密钥外泄 / Agent 工具滥用 / 外部 MCP server）到缓解实现与测试的逐条映射，见 [docs/THREAT_MODEL.md](THREAT_MODEL.md)；攻防回归可离线复跑：`python evals/runners/run_tool_eval.py`。

## 本地鉴权

默认情况下所有 `/api/*` 路由都需要启动 token。启动服务后，请打开终端打印的带 `?token=...` 的地址；后端会设置 `HttpOnly` 的 `auth_token` Cookie，前端后续请求依赖浏览器自动携带 Cookie，不会把 token 写入 `localStorage` 或 `sessionStorage`。未显式设置 `AUTH_TOKEN` 时，服务会把生成的本地 token 保存到 `.auth-token` 并在后续启动复用，避免重启后旧 Cookie 立刻失效；该文件已加入 `.gitignore` 和发布排除。

桌面 pywebview 入口会在 token URL 上追加 `desktop=1`。服务端仍先校验 token；通过后直接返回首页并设置同一个 HttpOnly Cookie，避免 302 跳转导致 WebView 丢 Cookie。这不是免登录入口，也不改变 `/api/*` 鉴权。

可用控制项：

- `AUTH_DISABLED=1`：关闭 token 鉴权，仅建议在可信开发环境使用。
- `AUTH_TOKEN=...`：使用固定 token，便于重复测试。
- `AUTH_ALLOWED_HOSTS=...`：为局域网环境追加 Host 头白名单。

Host 白名单用于降低 DNS rebinding 风险。CORS 预检只允许当前服务端口下的 `127.0.0.1`、`localhost`、`[::1]`、局域网 IP 和显式配置的允许 Host；带 path、query 或 fragment 的伪造 `Origin` 不会被接受。请求日志会脱敏 URL 查询参数中的 `token`，并保留原 URL 的 host/path 结构，便于排查。

所有响应都会带上 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY` 和同源优先的 CSP。CSP 允许本地 KaTeX 字体、同源脚本和样式、KaTeX 所需的内联样式，以及搜索结果 favicon 所需的 `http:` / `https:` 图片。v1.0.0 允许前端从 Google Fonts 加载 Inter 字体 CSS 和字体文件（`fonts.googleapis.com` / `fonts.gstatic.com`）；网络不可用或策略拦截时会自然回退到中文系统字体链。

## Cookie 策略

认证 Cookie 使用：

- `HttpOnly`
- `SameSite=Strict`
- `Max-Age=2592000`（30 天）
- `Path=/`

由于本项目默认通过本地 HTTP 使用，因此没有设置 `Secure` 标记。如果部署到 HTTPS 环境，可以进一步增加 `Secure`。

## PWA Share Target

`POST /share-target` 是给手机系统分享菜单使用的入口。Android Chrome 对“系统分享菜单 → PWA”的 POST 不会携带 `SameSite=Strict` Cookie，因此该入口不要求 `Authorization` 头或 `auth_token` Cookie，只校验请求 `Host` 是否在本地白名单内。它只接收标题、正文、URL 和文件，写入 30 分钟 TTL 的内存缓存并返回 `303 /?share=<id>`；缓存内容必须再由已鉴权的 `/api/share-target` 读取，前端也会要求用户确认后才导入草稿。

这个设计保留了 `/api/*` 的 Strict Cookie 安全收益，并把未鉴权入口限制为单向收件箱：同局域网攻击者最多向分享缓存塞入垃圾内容，不能读取历史、API Key、附件片段、项目文档或模型响应。

## API Key

DeepSeek 和 Tavily Key 可以通过环境变量提供，也可以在浏览器页面中填写。

- 环境变量 Key 存在服务端，对所有已鉴权浏览器会话可用。
- 浏览器填写的 Key 可按用户选择保存在本地 `localStorage`。
- 浏览器填写的 Tavily Key 只会在本轮请求允许联网搜索时随 `/api/chat` 发送；未勾选保存时刷新页面后清空。
- v0.9.4 的 `/api/title` 会使用 DeepSeek Key 生成短标题，并对同一 API key 哈希做 60 秒 12 次的内存限流；超限只影响自动标题，聊天本身不会被中断。

## Seek 参考文件

自定义 Seek 的参考文件复用普通附件上传链路：文件先在本地后端解析、分块并写入 `.file-cache/`，浏览器只保存文件元数据、预览文本和本地 `fileId`。发送消息时，前端会把当前消息快照里的 Seek 参考文件合并到 user 消息附件中，后端只把与当前问题相关的片段注入模型请求。导出自定义 Seek JSON 时会包含这些参考文件元数据；如果资料敏感，请像对待普通附件缓存一样妥善保存导出文件和 `.file-cache/`。

## 项目文档库

项目空间保存在 `.projects/`，其中包含项目名称、文档元数据、预览文本和 chunk 文本。v1.7.6 起，项目文档和长期记忆还会同步写入 `.local-rag/rag.sqlite3` 作为本地 RAG 向量索引；默认哈希 embedding、可选 sqlite-vec KNN 和可选 ONNX Runtime embedding 都在本机运行，不调用第三方 embedding 服务。它是持久资料库，不会被临时 `.file-cache` 清理任务自动删除；删除项目或手动删除 `.projects/{projectId}` 才会移除项目文档，清理 `.local-rag/` 可移除重建型索引。项目文档可能包含长期学习资料、工作文件或隐私内容，发布、分享或备份代码时必须排除 `.projects/` 和 `.local-rag/`。

v1.7.7 新增的 `.traces/traces.sqlite3` 会保存本地 trace run/span，包括请求输入摘要、模型输出摘要、usage、错误文本和耗时；`apiKey`、`tavilyApiKey`、authorization 和 token 会在写入摘要时脱敏，但 prompt、文件片段摘要和模型回答仍可能包含隐私内容。v2.2.0 的 `GET /api/traces/{traceId}/export.json` 会在导出前再做一层递归脱敏和大段文本截断，避免导出的 `trace.json` 包含 API Key、auth token、敏感 URL query 或完整隐私文件内容。`.semantic-cache/cache.sqlite3` 会保存可缓存请求的 prompt 文本、embedding、模型回答和原始 usage，用于离线相似度命中。二者都只在本机保存，不上传到第三方观测平台或 embedding 服务；发布、分享或备份代码时必须排除 `.traces/` 和 `.semantic-cache/`。

v1.8.0 新增的 `.request-queue/queue.sqlite3` 用于 API 网关韧性：云端请求开始前会记录队列 ID、请求类型、模型、消息数、工具数、队列状态、尝试次数、下一次重试时间和最后错误。队列不保存 API Key，但会保存请求指纹与请求摘要；错误文本和模型名仍可能暴露使用习惯或敏感上下文轮廓。它只保存在本机，不上传第三方；发布、分享或备份代码时必须排除 `.request-queue/`。

附件引用回链通过 `/api/file-chunk` 按 `fileId`、`projectId` 和 chunk 编号读取本地片段。该接口仍受 `/api/*` token 鉴权、Host 白名单和 CORS 白名单保护；`fileId` 和 `projectId` 均会做格式校验，避免路径穿越。

## 浏览器侧清理

设置面板中的“清空本地数据”会删除浏览器侧保存的 DeepSeek Infra `localStorage` / `sessionStorage` 项，并调用 `/api/auth/logout` 清除 HttpOnly `auth_token` Cookie。清理后需要重新使用服务端打印的 token 链接进入应用。

- 不建议在共享电脑或不可信网络中保存 API Key。

## 搜索内容与 Prompt Injection

搜索结果属于不可信网页内容。后端会把搜索结果标记为“来源材料”而不是指令，但模型侧 prompt injection 无法完全消除。涉及医疗、法律、金融、政策等高风险内容时，应优先使用官方、原始或权威来源，并在答案中保留来源链接。

## 本地工具调用与 URL 精读

v0.7.2 起 function calling 只开放固定白名单工具，模型不能自定义任意命令。`python_eval` 在隔离 Python 子进程中运行，只允许数学表达式 AST、受控函数、无导入、无文件和网络访问，并设置 2 秒超时；它适合做阶乘、组合数、简单数值检查，不是通用代码执行环境。

`search_files` 只读取本地 `.local-rag`、`.file-cache` 和 `.projects` 索引。它不会扫描任意磁盘路径，也不会把文件原文发给第三方嵌入服务；检索结果仍然会随 DeepSeek 请求发送给模型，因此项目文档和附件应视为会进入本轮模型上下文。

检索锚点：`fetch_url` 的 SSRF 防护会拦截内网或元数据地址，包括 `localhost`、`.local`、私有网段、回环地址、链路本地地址、保留地址、无法解析的 host，以及云元数据地址 `169.254.169.254`。这些规则由 Tool Policy 的静态 URL safety 与 `fetch_url` DNS 后校验共同执行。

`fetch_url` 和 `POST /api/fetch-url` 用于搜索结果二次精读。后端只允许 `http` / `https`，会拒绝 `localhost`、`.local`、私有网段、回环地址、链路本地地址、保留地址和无法解析的 host，降低 SSRF 风险。读取上限为 2 MB，正文缓存写入 `.search-cache`。抓取到的网页内容依旧是不可信文本，可能包含 prompt injection。

`suggest_memory` 只生成待确认的记忆建议，不会自动写入 `.memory`。建议内容仍会经过敏感信息拦截；前端必须由用户点击确认后才调用 `/api/memory` 保存。如果新记忆与同作用域内旧记忆冲突，后端会返回 `memory_conflict`，由用户决定是否替换。

`create_pptx`、`create_document` 和 `create_mindmap` 只写入本地 `.generated/` 目录并通过 32 位随机 id 下载，不允许模型指定磁盘路径或执行命令。SVG 思维导图由后端模板生成，节点文本会 XML 转义，不写入脚本、外链或用户可控的原始 SVG 片段。前端正文预览也只接受本地 `/api/download?id=<32 hex>` 图片块，不把任意外链图片自动嵌入聊天正文。

v1.2.6 的多 Agent 模式仍复用同一套本地工具白名单。Agent 只向前端展示公开摘要，不展示隐藏推理链；Researcher 可联网搜索，Coder 只能使用本地代码/文件工具，Reasoner 和 Critic 默认无工具。Coder / Reasoner 可并行执行，但只共享前序层公开摘要；失败 Agent 会返回降级摘要和风险提示，避免后续综合阶段误把空结果当成成功结论。Agent worker 默认不开放提醒创建、记忆删除和记忆建议等副作用工具，避免并行 Agent 在用户无明确确认时写入本地状态。客户端停止生成或连接断开后，request-level cancel token 会阻止后续流式事件和新工具调度继续写回 UI。v1.3.0 的 Agent 执行报告完全由本地 timeline 和最终回答拼装，只复制当前浏览器已展示的公开内容，不额外读取隐藏推理链或重新请求模型。

v1.3.5 只调整多 Agent worker 的 prompt/message 排列以提升 DeepSeek prefix cache 命中概率，不扩大任何 Agent 工具权限。前序 Agent 摘要仍以“不可信资料”形式追加到历史之后，Synthesizer 和后续 worker 的 prompt injection 边界保持不变；用户停止生成或连接断开后，既有 cancel token 仍会阻止新的工具调度和前端写回。

v1.3.6 继续只调整 worker prompt/message 排列，不扩大工具权限。角色职责和搜索约束虽然从 `systemPrompt` 后移到历史之后，但真实可用工具仍由后端 `allowedTools`、`toolsEnabled` 和 `searchEnabled` 控制：Researcher 才能联网搜索，Coder 只能使用本地文件/代码工具，Reasoner 和 Critic 默认无工具。

v1.3.7 只聚合 DeepSeek 返回的 token usage 统计并展示在本地诊断面板，不新增外部请求、不扩大日志内容，也不改变 Agent 工具权限。`agentCache.byAgent` 只包含缓存命中/未命中 token 数与命中率，不包含 prompt 正文或模型输出。

v1.3.8 延续同一安全边界，只为 `agentCache` 增加 `totalTokens` / `hasData` 并调整本地展示文案。诊断数据仍不包含 prompt 正文、工具结果正文或模型输出。

v1.3.9 仅调整本地诊断面板文案和多行排版，不新增数据字段、不扩大日志内容，也不改变任何 Agent 工具权限。

v1.4.0 新增 `.agent-runs/` 本地持久化目录，用于保存多 Agent run 的状态、事件日志、最终答案和诊断快照。创建 run 时会剔除 `apiKey` / `tavilyApiKey`，发布脚本也会排除 `.agent-runs/`，避免把本地任务过程和运行时状态打包分发。

v1.6.6 的 `desktop=1` 握手只改变首屏 Cookie 建立方式，不开放新路由权限；当前时间 dynamic context 只包含本地时间和 UTC 时间，不含用户隐私数据。v1.6.3 的 Windows 本地桌面应用默认只监听 `127.0.0.1`，并把带 token 的本机地址加载到内嵌 WebView；它不开放新的网络边界，也不绕过 `/api/*` 本地鉴权。v1.6.2 在 Android APK 内新增 ML Kit OCR 依赖，但不把图片或 PDF 发送给 DeepSeek 或外部 Web 服务；识别发生在手机本机的原生 OCR 组件内，结果仍按附件文本写入本地缓存。v1.6.1 不扩大工具权限、不新增外部请求，也不改变 Agent Run 持久化边界；联网搜索工具只是复用既有 `.search-cache` 并稳定传给模型的工具交换字面内容。v1.6.0 手机本机启动器只是在本机 Python 进程里配置 `HOST` / `PORT` / API Key 等环境变量，默认监听 `127.0.0.1` 并继续使用本地 token 鉴权；只有用户显式传入 `--lan` 时才监听 `0.0.0.0`。v1.5.1 搜索提示移动到本轮尾部 dynamic context 只影响 prompt cache 命中概率，不让关闭搜索的请求获得联网工具。Activity 面板复制事件只复制当前 UI 已展示的 Agent 执行报告；Escape 面板关闭和焦点陷阱栈都是本地交互修复，不读取额外数据。

v0.9.6 新增的本地工具继续使用白名单边界：`data_transform` 只做正则抽取、简单 JSON path、CSV/数字摘要，不调用 `exec` / `eval`；`create_reminder` 只写入本地 `.reminders`；`recall_memory` / `forget_memory` 只访问 `.memory`，删除要求非空 substring，并默认限制在全局和当前项目/Seek 作用域；`list_project_files` / `read_file_chunk` 只读取项目文档库和缓存 chunk。`execute_tool_calls()` 只并行运行无副作用工具，提醒、记忆删除和记忆建议保持串行，避免状态写入竞态。

## Tool Policy Engine

v2.1.0 起，上面这些零散的工具安全约束被收敛进一个统一的 Capability-based Tool Policy Engine（`deepseek_infra/infra/tool_runtime/tool_policy.py`）。模型不再直接命中执行器，每个 LLM 工具调用先过策略闸门：**schema 校验 → 能力/权限检查 → 风险分级 → 人工确认（如需要）→ 执行器**，再加结果注入清洗与审计两层横切。它是在既有白名单之上的纵深防御，不放宽任何原有边界。

- **能力画像（capability-based）**：每个 Agent 角色拿到不同工具权限，且这是 `multi_agent.agent_tools_for` 的单一事实源——`researcher`：`web_search` / `compare_search_results` / `fetch_url`；`coder`：`search_files` / `read_file_chunk` / `python_eval`；`reasoner` / `critic`：无工具；主聊天用 `full`。即使 worker 幻觉出能力外的工具，执行期也会被拒绝（offer 层与执行层两道一致）。
- **SSRF 纵深防御**：策略层 `evaluate_url_safety` 做无需 DNS 的静态预判，拦 `localhost` / `.local` / `.internal`、字面私网/环回/链路本地地址、云元数据 `169.254.169.254`、URL 凭证与非 http(s) 协议并尽早拒绝；`fetch_url` 内部解析 DNS 后的权威校验仍是第二道关。
- **路径越界检测**：文件类工具的 `fileId` / `projectId` 经 `evaluate_path_safety` 校验，拒绝 `..`、路径分隔符和非法字符，防止逃出缓存沙箱。
- **敏感信息写入 memory 拦截**：`suggest_memory` 的内容在写入前过 `is_sensitive_memory`，命中（API key / 密码 / token / 身份证等）直接拒绝。
- **人工确认**：高风险工具（如 `forget_memory`，`requires_confirm=True`）在 `TOOL_POLICY_REQUIRE_CONFIRM=1` 时返回 `needs_confirmation` 而非执行，除非请求 `approvedTools` 已预批。
- **工具结果 prompt injection 清洗**：搜索 / 抓取等 `external_output` 工具的外部文本字段会红action 掉常见注入指令（「忽略上述指令 / ignore previous instructions / 输出 system prompt」等），URL、id、score 等非文本字段保持不变；不可信网页文本的注入风险无法完全消除，仍应优先权威来源。
- **外部 MCP server 边界**：`MCP_CLIENT_ENABLED=1` 只连接 `MCP_CLIENT_SERVERS` 显式列出的 Streamable HTTP server；桥接工具统一命名为 `mcp__<server>__<tool>`，进入本地 Agent 工具面前会生成保守风险 profile，每次调用仍先过 Tool Policy，结果按外部不可信输出清洗。v2.2.2 加固让 `/mcp tools/call` 和 Agent 调用共享同一 executor 内部闸门，远端 `isError=true` 不再伪装成成功。v2.2.3 起外部 server 还有 timeout / retry / circuit breaker 与 health API，外部 server 挂掉不会影响本地工具目录。
- **A2A peer 边界**：A2A 任务只在目标 agent 的 capability 切片内执行；`message/stream` 的 artifact chunks 和 `tasks/resubscribe` 只暴露任务产物与状态，不扩大工具权限。取消请求进入 `canceling` 后停止交付最终结果；如果云端请求已在途，结果会被丢弃并在 trace diagnostics 中记录 `discardedResult`。
- **审计日志**：每条决策（放行 / 拒绝 / 待确认）追加写入本地 `.tool-audit/audit.jsonl`（append-only，best-effort，不阻断工具调用），可经 `TOOL_POLICY_AUDIT_ENABLED` 关闭；发布脚本与 `.gitignore` 排除该目录。

默认配置（`TOOL_POLICY_ENABLED=1`，`enforce_schema` / `require_confirm` 关闭，结果清洗与审计开启）下，主聊天用 full 画像、不强制确认，行为与 v2.0.x 一致；能力收窄、强制 schema 与强制确认是按需开启的更严格档位。被策略拦截的工具调用返回 `{"ok": false, "code": "forbidden"|"requires_confirmation", "policy": {...}}`，不会真正执行。

## 请求调度与 backpressure

v2.1.2 起，所有上游模型调用先经过本地请求调度层（`deepseek_infra/infra/gateway/scheduler.py`）的进程内准入控制：优先级队列、并发上限、令牌桶限流与 **backpressure**。这层主要是稳定性/资源边界，而非鉴权边界，但有安全意义：用户连续点「生成」、多个 Agent 同时调模型或上游限流时，等待+在途请求一旦越过 `max_queue_depth` 会被**快速卸载**（503 `{"code":"rate_limited"}`），避免无界排队耗尽本机内存/连接、把瞬时尖峰放大成雪崩。被卸载或耗尽重试的请求写入本地 Dead Letter Queue（`.scheduler/scheduler.sqlite3`），只含 `kind`/原因/尝试次数等元数据，不含 prompt 正文、工具结果或模型输出；发布脚本与 `.gitignore` 排除 `.scheduler/`。准入路径不发起任何新的外部请求，也不放宽 `/api/*` 本地鉴权；默认配置（不限流、并发 16、队列 256）对正常负载透明，仅在压力下生效，可经 `SCHEDULER_*` 环境变量收紧。

## 前端渲染

Markdown 和公式渲染都在本地前端完成，不加载第三方 CDN。公式渲染使用随包自托管的 KaTeX 0.16.45，运行参数固定为 `trust: false`、`throwOnError: false`、`strict: "ignore"`，不允许公式通过可信命令注入外部 HTML。KaTeX 尚未加载或解析失败时会回退为转义后的纯文本占位，避免把模型输出或用户输入当作可执行 HTML 注入页面。

v0.7.4 的 Mermaid 支持先使用内置的轻量 SVG flowchart 渲染器处理常见 `flowchart` / `graph` 语法；如果页面已存在可信的 `window.mermaid` 渲染器，则再尝试交给它渲染。默认不会从 CDN 加载第三方脚本。表格图表使用本地 SVG 生成，不执行模型输出中的脚本。代码块的“VS Code 打开”只在代码开头检测到本地路径时生成 `vscode://file/...` 跳转，是否打开由浏览器和用户本机协议处理决定。

## 文件解析

上传文件在本地解析。当前防护包括：

- 上传体大小限制：默认单文件上限 200 MB，单次 multipart 请求体上限 220 MB；`/api/file-text`、`/api/project-files` 和 `/share-target` 共用同一套校验，超限返回 `upload_too_large`。
- 流式 multipart 解析，避免整包读入内存。
- multipart part 数量、header 大小、字段大小和文件数量限制。
- `multipart` 依赖能力校验，避免被不兼容的同名包覆盖后触发未处理异常。
- DOCX/XLSX/PPTX/EPUB ZIP 单条目和总解压大小限制。
- 文件名清理，避免路径穿越形态。
- 文件缓存 ID 校验，只允许固定十六进制 ID。

PDF 会优先读取可复制文字。OCR 是可选能力，默认关闭，以避免意外的延迟和资源消耗。开启 OCR 后，图片 OCR 会优先把 PNG、JPG、WebP、BMP、TIFF、GIF 等图片或扫描 PDF 页面发送给 DeepSeek API，由 `deepseek-v4-pro` 直接转写文字；如果 API Key 缺失、API 失败或返回空文本，才会回退到本地 OCR 引擎。Windows 桌面端可调用系统 `Windows.Media.Ocr` 兜底，Android APK 可走本机 ML Kit。`OCR_MODE` 只控制本地 OpenCV 预处理、多 `psm` 重试和轻量结构整理。`OCR_FORMULA_CMD` 是用户显式配置或本机 PATH 中存在的本地命令行工具，后端会把临时图片路径传给该命令并读取 stdout；它不走 shell，但该命令本身拥有本机进程权限，应只配置可信工具。扫描 PDF OCR 还会通过 Poppler / `pdftoppm` 把页面转为图片。HTML 会剥离脚本/样式后抽取可见文本，EPUB/PPTX 会在 ZIP 安全校验后读取文本节点。OCR 和解析结果会和其他附件文本一样写入 `.file-cache` 或 `.projects`，因此截图、照片、扫描件和项目资料都应视为敏感数据。

## 本地数据

本项目的主要本地数据：

- 浏览器 `localStorage`：对话历史、设置、可选保存的 DeepSeek / Tavily API Key。
- 浏览器 `localStorage`：未发送草稿、引用回复暂存、对话标签和收藏状态。
- 浏览器 `localStorage`：自定义 Seek 和 active Seek 标识；历史消息会保存 Seek 名称、简介和指令快照。自定义 Seek 最多保存 40 个，名称不可重复，以降低误选和历史混淆风险。导出的 Seek JSON 会包含用户写入的自定义指令，分享或备份前应按本地私人数据处理。
- `.file-cache`：文件分块缓存。
- `.projects`：持久项目空间和项目文档库。
- `.local-rag/rag.sqlite3`：本地 RAG 索引，包含文件 chunk、长期记忆文本片段、embedding 和检索元数据。
- `.traces/traces.sqlite3`：本地请求追踪库，包含 trace/span、输入/输出摘要、耗时、usage、cache hit rate 和错误摘要。
- `.semantic-cache/cache.sqlite3`：本地语义缓存，包含可缓存 prompt、embedding、模型回答、usage、命中计数和更新时间。
- `.search-cache`：搜索结果缓存。
- `.agent-runs`：可恢复 Agent Run 的事件日志、派生快照和最终答案。
- 进程内 `_SHARE_TARGETS`：PWA 分享缓存，默认 30 分钟过期，读取后立即删除。
- `.reminders/reminders.json`：本地提醒队列，包含提醒标题、正文和触发时间。
- `.memory/memories.json`：长期记忆，包含全局、项目和 Seek 作用域。
- `EDGE_MODEL_PATH` 指向的 GGUF / MLC 本地模型权重不由应用复制或上传；开启端侧推理后，简单任务可在本机完成，但模型权重文件本身仍应按用户本地私有数据管理。

这些目录应视为用户本地数据。如果要清理本地痕迹，可以关闭服务后删除上述目录，并使用设置面板的“清空本地数据”清理浏览器侧会话、Key、对话历史和 Seek。

`.gitignore` 已默认排除 `.auth-token`、`.launcher-config.json`、`.file-cache/`、`.projects/`、`.local-rag/`、`.semantic-cache/`、`.traces/`、`.request-queue/`、`.tool-audit/`、`.scheduler/`、`.generated/`、`.budget/`、`.a2a/`、`.memory/`、`.reminders/`、`.search-cache/`、`.agent-runs/`、`.coverage`、`.mypy_cache/`、`.ruff_cache/`、`__pycache__/`、`.idea/`、`server*.log` 和 `pytest-cache-files-*/`。发布或分享压缩包时仍应二次检查，避免把用户附件片段、项目文档库、本地 RAG 索引、语义缓存、trace、请求队列、审计日志、生成产物、长期记忆、提醒、日志、Agent Run 过程或本地 API Key 痕迹打包出去。

## 已知边界

- 本地 HTTP 不提供传输层加密；局域网内其他设备理论上可能观察流量。
- FastAPI/uvicorn 服务默认面向个人本地和可信局域网使用，不应直接作为公网服务暴露。
- 搜索结果和上传文件内容可能包含不可信文本，模型输出仍需用户判断。
