# 前端模块索引

适用版本：v2.5.5；模块拆分自 v0.8.2 起。

`static/modules/chat.js` 仍然是聊天主流程和渲染入口，但第一轮拆分已经把不依赖 `state`、不直接操作 DOM 的纯函数移出。后续新增工具函数时，优先放到下面对应模块，避免继续扩大 `chat.js`。

v0.9.1 在设置面板新增思考强度选择，`chat.js` 负责持久化 `deepseek-infra.reasoning-effort` 并在聊天、继续生成、重新生成和编辑后重发时把 `reasoningEffort` 传给后端。
v0.9.2 的上传和交互升级仍集中在 `chat.js`：拖拽、粘贴、文件选择、Seek 参考文件和项目文档上传共用 `validatedUploadFiles()` 预检；图片缩略图、lightbox、toast action、确认弹窗、快捷键面板、live region、焦点陷阱和软键盘安全区也是 DOM/状态耦合逻辑，暂不拆到纯函数模块。`normalizeStoredAttachment()` 负责保留本地图片预览字段。
v0.9.4 的网页来源引用、自动标题、conversationPeek 点击锁和 reasoning/search timeline 也保留在 `chat.js`：`openCitationForMessage()` 按当前 assistant 消息解析 `[^Wn]`，标题生成通过 `/api/title` 异步写回历史项，timeline 只影响本地展示和持久化，不改变后端聊天协议。v0.9.6 的搜索 SVG 命名空间创建、stuck searching 收尾和 `webCitationResults()` URL 去重同样保留在 `chat.js`，因为它们依赖消息状态和 DOM 渲染。
v1.0.0 的主题系统由 `index.html` 的首屏 inline boot script、`chat.js` 的 `state.themeStyle` / `state.themeMode` 和 `styles.css` 的语义 token 共同实现。新组件样式应优先使用 `--bg-*`、`--surface-*`、`--text-*`、`--border-*`、`--accent-*` 等语义 token；`--bg`、`--surface`、`--text`、`--brand` 等旧变量仅作为兼容别名保留。`normalizeThemeStyle()` 和 `normalizeThemeMode()` 放在 `normalize.js`，避免非法本地设置污染根节点 dataset。v1.0.1 在 `markAssistantInterrupted()` 和 `normalizeTimeline()` 中补齐搜索收尾，避免中断或刷新后的历史消息继续显示”正在搜索”。v1.1.1 仅修改 `static/styles.css`，重新校准 ChatGPT、Linear、Notion、Arc 四套主题 token，并同步 system 暗色镜像和主题特定规则。v1.1.5 在 `chat.js` 中新增 `state.agentMode`、`#agentModeButton`、`agentMode` 请求字段和 `agent` timeline step；`message.search` 顶层对象会在读取和保存历史时同步收尾，流式未闭合代码围栏由 `markdown.js` 先按普通文本展示，避免中断后出现黑框。v1.2.6 在设置面板新增 `agentDisplayModeSelect`，`chat.js` 用 `deepseek-infra.agent-display-mode` 保存简洁/详细模式；Agent timeline step 额外持久化 `id`、`notes`、`collapsed`，并用稳定 id 生成 DOM key。v1.2.7 把 agent timeline 的 12 个纯函数（`agentStepId` / `createAgentStepId` / `appendTimelineAgent*` / `normalizeTimeline` / `timelineStepKey` / `shouldCollapseAgentStep` 等）从 `chat.js` 抽到独立模块 `agent_timeline.js`：脱 DOM、无 `window` / `localStorage` 依赖，由 `tests/test_frontend_utils.py` 直接 import 测试。新增 `createAgentStepId(message, phase)` 按 `message.timeline` 里同 phase 的现有 step 数生成 `agent-{phase}-{N}`，修掉 Leader 两轮（拆解 + 综合）共享 id 的 P0；`normalizeTimeline` 加去重兜底，旧 history 里同 id 的两张 Leader 也会被补成 `agent-leader-1` / `agent-leader-2`。折叠策略改为分级：Leader 完成后保留展开、`status === "error"` 永远展开、其他 worker 完成且有内容才折叠。v1.2.8 在同一模块里加 `agentRunSummary(message)` / `agentRunSummarySignature` / `formatAgentDuration`：前两个生成 Activity 顶部"N Agents · 资料 ✓ · 代码 ✕"摘要条数据并算签名供 dataset 去重，第三个把 `durationMs` 渲染成 "850ms / 1.3s / 1m 5s"。`appendTimelineAgent` / `normalizeTimeline` 持久化 `step.durationMs`，刷新后耗时仍能恢复；非法数值（NaN / 负数 / null）一律归一为 null，调用方按"无耗时"渲染。v1.2.9 把 duration 归一化抽成 `normalizeDurationMs()`，修复持久历史里 `durationMs: null` 被 `Number(null)` 恢复成 `0ms` 的问题；摘要条计数文案改为 "N 个 Agent"，失败 chip 增加轻量边框。v1.3.0 新增 `agentExecutionReport(message)`，把 Leader 拆解、worker 摘要/风险和最终回答整理成纯文本执行报告，供 Activity 面板和助手更多菜单复制。

v1.3.4 继续把 Activity 面板状态留在 `chat.js`：`activityAutoDismissedMessageIds` 只记录“用户手动关闭过的当前流式消息”，`maybeAutoOpenActivityPanel()` 会尊重该集合，`openActivityPanel(..., { auto: false })` 则清除记录。`activityTimelineSteps()` 会在 timeline 缺少 reasoning step 时用 `message.reasoning` 补一个 fallback，避免 Leader 思考在 worker 卡片出现后被清空；`messageHasActivity()` 也会把正在流式的 Agent message 视为可打开的 Activity。请求层使用 `message.agentMode || state.agentMode` 在普通 4 分钟和 Agent 75 分钟之间切换。

v1.3.5 不调整前端模块边界；本次主要改后端多 Agent worker 的 prompt/message 排列，并同步 Service Worker 缓存版本到 `deepseek-mobile-v135`。

v1.3.6 仍不调整前端模块边界；本次继续改后端多 Agent worker 的缓存友好排列，并同步 Service Worker 缓存版本到 `deepseek-mobile-v136`。

v1.3.7 继续把诊断面板留在 `chat.js`：`renderDiagnosticsPanel()` 读取 `diagnostics.agentCache`，展示多 Agent 总 hit/miss/rate 和按 Agent 的简表；Service Worker 缓存版本同步到 `deepseek-mobile-v137`。

v1.3.8 继续只调整 `chat.js` 的诊断展示：`formatAgentCacheTotal()` / `formatAgentCacheRate()` / `formatAgentCacheByAgent()` 会识别 `hasData=false` 和 `hitRate=null`，把缺失 usage 显示为“无数据”；Service Worker 缓存版本同步到 `deepseek-mobile-v138`。

v1.4.0 在 `chat.js` 接入可恢复 Agent Run：前端先创建 run，再 attach `/stream?after=N`，持久化 `agentRunId` / `agentRunLastEventIndex`，并通过 `agent_reset`、`final_reset` 恢复 Activity timeline 与最终回答。计划确认模式新增 `.agent-plan-workbench`，允许用户编辑 Agent 计划后再执行；Service Worker 缓存版本同步到 `deepseek-mobile-v140`。

v1.5.1 继续把交互状态留在 `chat.js`：`hasClosablePanelOpen()` 让 Escape 能统一收起设置、Seek、项目、搜索、文件预览、记忆、诊断和 Activity 面板；`focusTrapStack` 让确认框叠在其它面板上时可以恢复到底层焦点陷阱；Activity 面板“复制 Agent 过程”只保留 `onActivityPanelClick` 事件委托，避免直接监听和委托同时触发。搜索 cache 修复在后端请求组装层完成，前端只需要随 Service Worker `deepseek-mobile-v151` 刷新新版脚本。

v1.8.0 仍不新增前端模块；Gateway & Resiliency 完全在后端完成。`chat.js` 只在诊断侧栏展示 `diagnostics.contextManager`、滑动窗口丢弃数、`diagnostics.gatewayResiliency` 的 attempt/retry 统计，并在流式重试等待时显示后端发来的 `system_note`。`loadConfig()` 可读取 `/api/config.gateway`，但聊天请求协议不需要新增字段；Service Worker 缓存版本同步到 `deepseek-mobile-v180`。v1.7.7 的 Trace 入口继续留在 `chat.js`：`loadConfig()` 读取 `/api/config.tracing` 和 `/api/config.semanticCache`，助手消息更多菜单在存在 `diagnostics.traceId` 时显示 `Trace`，点击后复用诊断侧栏读取 `/api/traces/{traceId}` 并渲染 waterfall。语义缓存完全在后端完成，前端只展示 `diagnostics.semanticCache` 的命中/跳过状态；Service Worker 缓存版本同步到 `deepseek-mobile-v177`。v1.7.6 的 Local Data Infra 也在后端完成，前端只通过 `/api/config.localRag` 获取本地 RAG 状态，聊天请求和附件协议不变。v1.7.5 的端侧推理入口继续留在 `chat.js`：`loadConfig()` 读取 `/api/config` 的 `edgeInference`，普通聊天发送前会把“云端 API Key 可用”与“本地端侧模型可用”合并判断，Agent Run、联网搜索、图片理解和标题生成仍走原有云端能力要求。v1.7.0 的流式 Activity 阶段状态继续留在 `chat.js`，用 `message.streamPhase` 区分 `thinking` / `tool` / `searching` / `agent` / `answering`。`startReasoningTick()` 会在请求启动时刷新运行中标题，工具调用、搜索和正文输出阶段不会再停在旧的“思考中”秒数。v1.6.6 的选区引用继续留在 `chat.js`。`scheduleSelectionRefresh()` 在 `mouseup`、`keyup` 和 `touchend` 后刷新浏览器 selection，`chatBubbleForSelection()` 改为按实际 range 命中单条 `.message[data-message-id] .bubble` 判断来源，因此用户消息和助手消息都能引用；触屏 `touchstart` 不再阻断引用操作按钮的后续 click。桌面 WebView 启动鉴权和当前时间 dynamic context 均在后端完成，前端只按既有 `/api/*` 路由工作。v1.6.3 不改变前端模块边界；Windows 本地桌面应用壳只把既有前端装进 pywebview 窗口，仍通过同一套 `/api/*` 路由工作。v1.6.2 的 APK 内 OCR 修复在 Android 原生桥接和后端 OCR 选择层完成。v1.6.1 的联网搜索工具 cache 友好修复在后端工具交换和搜索缓存层完成。v1.6.0 手机本机运行由 Python 启动器完成。

v2.2.0 把 Trace 从应用内侧栏补成独立只读页面：`chat.js` 仍负责在诊断区提供 `Open page` 和 `Export JSON` 入口；`static/trace_viewer.html` 负责页面骨架；`static/modules/trace_viewer.js` 负责加载 `/api/traces/{traceId}`、渲染基本信息、错误表和导出链接；`static/modules/trace_waterfall.js` 提供 span 树、瀑布图和 Agent / Tool / RAG / LLM 耗时汇总。Service Worker 缓存版本同步到 `deepseek-infra-v187`，`APP_SHELL` 必须包含 `trace_viewer.html`、`trace_viewer.js` 和 `trace_waterfall.js`。

## 已拆出的纯函数

| 模块 | 函数 | 说明 |
| --- | --- | --- |
| `static/modules/charts.js` | `chartSvg`、`pieChartSvg`、`parseChartCell` | Markdown 表格图表的 SVG 和数值解析；`renderTableChart` 仍在 `chat.js`，因为它访问 DOM。 |
| `static/modules/speech_text.js` | `speechTextFromMessage`、`speechChunks`、`splitLongSpeechSegment`、`preferredSpeechVoice` | 朗读前文本清理、iOS 友好的短句切片、系统 voice 选择。 |
| `static/modules/stream.js` | `parseStreamEventLine`、`readChatStream` | NDJSON 流解析；通过 `onEvent` 和 `waitUntilResumed` 回调接回主流程。 |
| `static/modules/format.js` | `extensionForLanguage`、`vscodeUriForPath`、`safeFilename`、`fileKindFromName`、`createId`、`quoteAwareContent`、`tailForContinuation` | 文件名、代码下载扩展名、VS Code URI、引用回复和续写尾部裁剪。 |
| `static/modules/normalize.js` | `normalizeTheme`、`normalizeThemeStyle`、`normalizeThemeMode`、`normalizeFontSize`、`normalizeVoiceLanguage`、`normalizeModel`、`normalizeSeekId`、`normalizeStoredAttachment` | 轻量字段规范化；`normalizeModel` 由 `chat.js` 传入当前支持模型集合。 |
| `static/modules/reminder_parse.js` | `parseReminderTime`、`detectReminderFromText` | 客户端提醒短语解析；网络请求和通知轮询仍保留在 `chat.js`。 |
| `static/modules/agent_timeline.js` | `agentStepId`、`createAgentStepId`、`appendTimelineAgent`、`appendTimelineAgentReasoning`、`appendTimelineAgentNote`、`appendTimelineAgentDelta`、`normalizeTimeline`、`timelineStepKey`、`shouldCollapseAgentStep`、`agentStepHasDetails`、`normalizeAgentNotes`、`agentNotesSnapshot`、`agentRunSummary`、`agentRunSummarySignature`、`formatAgentDuration`、`agentExecutionReport` | 多 Agent timeline 的 id 生成、增量合并、折叠规则、刷新后还原、执行摘要条聚合 + 签名、耗时格式化和执行报告导出；脱 DOM 由 `tests/test_frontend_utils.py` 单测。 |
| `static/modules/trace_waterfall.js` | `buildTraceSpanTree`、`spanCategory`、`summarizeByCategory`、`renderCategoryTable`、`renderSpanTree`、`renderTraceWaterfall`、`errorSpans` | 独立 Trace Viewer 的 span 树构建、瀑布条定位、按 Agent / Tool / RAG / LLM 聚合耗时、token、cache hit 和错误数；DOM 写入使用 `textContent`。 |

## 保留在 chat.js 的边界

- `render*` 系列继续留在 `chat.js`，因为它们和 DOM、全局 `state`、事件委托强耦合。
- `voice`、`share target`、`draft`、`reminders` 等子系统暂时只抽纯函数，不移动内部状态。下一轮拆分如果启动，应把各子系统的状态收进模块内部，用回调和公开方法连接主流程。
- `static/sw.js` 的 `APP_SHELL` 必须列出新增模块，否则离线/PWA 缓存会漏文件。
- 动效状态暂时保留在 `chat.js`：新消息用 `freshMessageIds` 标记一次性入场，流式输出用 `pendingStreamingMessageIds` + `requestAnimationFrame` 合并渲染；后续如继续拆 UI 子系统，可把这些移动到独立 motion helper。

## 测试约定

纯函数模块由 `tests/test_frontend_utils.py` 通过 Node 动态导入测试。以后新增纯函数时，优先在这里补低成本单测，再接入 `chat.js`。
