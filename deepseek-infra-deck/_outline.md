# DeepSeek Infra 介绍 PPT · 分页大纲（_outline.md）

> 共 13 页，960×540。页面文件名即 pages/ 下文件名。内容事实一律取自 D:\deepseek\content_brief.md。

## pages/01_cover.page（cover）
- 网格肌理背景 + 右侧陶土大斜切（parallelogram）。
- kicker：`LOCAL-FIRST AGENTIC AI INFRA`（11px, accent, letterSpacing 3）。
- 主标题（思源宋体，约 54px，ink）：DeepSeek Infra
- 副题（MiSans 16px, structure）：本地优先的 Agentic AI 基础设施平台
- 一行定位（13px, gray）：LLM 网关 · 多 Agent DAG 运行时 · 本地 RAG · 工具策略引擎 · 端到端可观测，以 MCP / A2A / OpenAI /v1 标准协议互操作
- 底部 meta 行（10px gray）：v4.0.3 · Python 3.10+ · MIT License · 数据不出端

## pages/02_positioning.page（content）
- kicker `01 · 项目定位`；标题：一套本机后端，把 Agentic AI 的整条链路装进私有工作区
- 左侧（约 55% 宽）：一段话定位（取自简报 §1）+ 三个核心卖点（编号 01/02/03，思源宋体大号编号 + MiSans 小标题 + 两行说明）：
  1. 一套后端，多种形态（桌面 WebView / Android APK / 本机或局域网服务）
  2. 本地优先，数据不出端（历史、向量索引、记忆、追踪全在本机；Key 可只存环境变量）
  3. 标准协议互操作 + 可观测（OpenAI /v1 · MCP · A2A；trace + Prometheus + 健康探针）
- 右侧：竖向"协议三联"直角面板或三行协议清单（/v1 · /mcp · /a2a，等宽字体感），不用卡片。

## pages/03_numbers.page（content）
- kicker `02 · 关键数字`；标题：先把数字放在桌上
- 超大数字阵列（思源宋体 56–72px + 11px 标签，网格对齐，无卡片）：
  13+2 核心模块 · 17 个 MCP 工具 · 95% Python 覆盖率门禁（实测 95.25%） · 2599 项测试 · Recall@5 1.000 · MRR 0.917 · 检索 avg 20.2ms · 注入对抗 26 用例全过
- 底部注记（10px gray）：基准环境 Windows 11 · Python 3.13 · i7-13700H · 16GB · SSD · runs 10 · warmup 2；数字来源 README / IMPLEMENTATION_STATUS（v4.0.3）

## pages/04_architecture.page（content）
- kicker `03 · 架构总览`；标题：五层架构——Python 是默认且权威的运行时
- 主体：五层直角嵌套分层图（自上而下）：
  L1 Client Layer（Web/PWA · Desktop WebView · Android APK · OpenAI SDK /v1 · MCP /mcp · A2A /a2a）
  L2 Python Runtime（FastAPI/ASGI：鉴权 · 流式 · 凭据 · 工具执行 · 持久化）
  L3 核心运行时组件（LLM Gateway / Agent DAG + A2A Mesh / 可选 Rust Sidecar——虚线框，5 个确定性委托，超时即回退 Python）
  L4 Local Data & Observability（SQLite 向量 RAG · 长期记忆 · 语义缓存 · Trace · 预算账本）
  L5 显式外部调用（DeepSeek API · Tavily · Ollama 端侧）
- 层间箭头标注协议（HTTP · NDJSON · SSE · JSON-RPC 2.0）；金句侧注：Rust 不读文件、不写索引、不持凭据、不执行工具。

## pages/05_request_flow.page（content）
- kicker `04 · 请求链路`；标题：一次聊天请求，经过哪些关口
- 横向节点链（可两行排布）：POST /api/chat → 鉴权 → traceId → 端云路由 → 记忆检索 → 联网搜索（可选） → 组装请求 → Taint 扫描 → 上下文裁剪 → 语义缓存 → 调度准入 → 队列重试 → DeepSeek API →（有 tool_calls？过 Tool Policy → 清洗 → 回环 ≤5 轮）→ NDJSON 流式输出
- 关键路径（Taint 扫描 / Tool Policy 闸门）用 $accent；缓存命中短路、工具回环用 dash 线；图下注明：多 Agent 模式共享同一 traceId（Planner → 分层并行 → Critic → Synthesizer）。

## pages/06_modules.page（content）
- kicker `05 · 模块全景`；标题：13+2 个基础设施模块，一个 FastAPI 核心
- hub-spoke：中心直角块「FastAPI Core · Python 权威运行时」（ink 底白字），周围放射连接 15 个模块（编号+名称+极简职责，10–11px）：
  01 LLM Gateway / 02 Agent DAG Runtime / 03 Local RAG / 04 Tool Runtime + Policy / 05 Observability & Trace / 06 Edge-Cloud Router / 07 MCP Tool Hub / 08 A2A Agent Mesh / 09 Context Taint Firewall / 10 Workspace Core / 10.5 Memory / 11 Multimodal Media / 12 Browser Control / 13 Automation Runtime / 14 Rust Hybrid（虚线，默认禁用）
- 状态图例：Working（实线 structure）/ MVP release-gated（dash 线）。

## pages/07_security.page（content）
- kicker `06 · 安全设计`；标题：四道纵深防线，逐次生效
- 左侧"防线"纵向分层图（攻击路径自上而下穿过四层）：
  ① Tool Policy Engine（schema 校验 · 角色能力切片 · SSRF/路径越界/敏感写入拦截 · 人工确认 · 审计 .tool-audit）
  ② Context Taint Firewall（来源信任打标 · 注入/外泄/工具指令扫描 · 污染轮高危工具升级人工确认）
  ③ 执行沙箱（python_eval 隔离子进程 + AST + 2s 超时；fetch_url 拒内网限 2MB）
  ④ 密钥外泄硬拒绝（secret_exfiltration_blocked）+ 导出递归脱敏
- 右侧：威胁→缓解→测试映射小表（THREAT_MODEL 七类威胁 → CI：pip-audit / bandit / detect-secrets）。

## pages/08_quality.page（content）
- kicker `07 · 质量证据`；标题：离线在CI里的硬门禁
- 左：横向 bar 图——检索 avg 20.2 / 检索 P95 21.7 / 缓存 lookup 8.4 / 缓存 store 17.9 / 索引 95 chunks 130（ms），主系列 $structure，环境注记贴图下。
- 右：质量门禁清单（10–12px）：RAG Recall@5 1.000 · Citation Accuracy 0.8333 · 精确命中 1.00 / 误命中 0.00 · Tool Policy 26 用例 Pass 1.000 · 注入门禁 blockRate≥0.85 / FPR≤0.10 / bypassRate≤0.15 · Agent Eval 三门禁 · 覆盖率 95%（实测 95.2521%）/ Rust 80%；CI 链：ruff → mypy → pytest --cov → npm check → 安全扫描 → Docker gate。

## pages/09_screenshots.page（content）
- kicker `08 · 界面实录`；标题：眼见为实——当前版本真实界面
- 2×2 直角截图（media/ 下）：trace-waterfall.png / agent-dag-run.png / rag-citation.png / mcp-tool-call.png，各配 10px 图注；等高等宽。

## pages/10_deployment.page（content）
- kicker `09 · 部署形态`；标题：一份后端，七种跑法
- 表格（细线、无厚框）：形态 / 启动方式 / 要点——桌面 WebView（launch.bat/sh，token 自动认证）· 手机本机（Termux launch_mobile.py --lan）· 命令行服务（python app.py）· 单文件 exe（build_exe.py）· Android APK（Chaquopy + 内置 WebView）· Docker Compose（非 root · HEALTHCHECK · 单 /data 卷）· OpenAI 兼容客户端（base_url → 127.0.0.1:8000/v1）
- 底部一行快速开始（等宽感）：cp .env.example .env → docker compose up -d → curl 127.0.0.1:8000/healthz

## pages/11_evolution.page（content）
- kicker `10 · 版本演进`；标题：从 v3.3 到 v4.0.3——一条"可选加速、永不破坏回退"的路
- 横向时间线（主轴 1px structure，节点菱形/圆点）：
  v3.3.0 ADR-0040 Python-first hybrid 合同 → v3.3.2 覆盖率门禁 90→95% → v3.5–3.10 Rust 委托逐版本落地（Gateway/MCP/RAG/binary/缓存） → v4.0.0 Hybrid 稳定版，冻结十端点合同 → v4.0.1 前端安全加固（严格 CSP，Key 不落 localStorage） → v4.0.2 React 19+TS+Vite /ui/ → v4.0.3 React 聊天纵切片（当前）

## pages/12_roadmap.page（content）
- kicker `11 · 路线与边界`；标题：已验证的留下，未完成的写清楚
- 左列「下一步」：React 迁移后续切片（附件/Projects/Skills/Memory/语音/PWA）· 第三方 A2A 实机互验（LangGraph/CrewAI）· Rust 委托渐进扩展（upstream proxy / MCP / Tool Policy）· Edge Router 真实端侧推理 · Workspace Core 深化
- 右列「当前边界（诚实清单）」：Edge Router 为 MVP dry-run gated · 多模块 MVP release-gated · Rust 默认禁用 · 前端 / 与 /ui/ 双轨并存至 parity
- 依据 IMPLEMENTATION_STATUS.md，措辞克制。

## pages/13_closing.page（final）
- 网格肌理 + 小斜切呼应封面。
- 思源宋体结语（28–32px）：把 Agentic AI 的基础设施，放回你自己的机器上。
- 三行下一步（编号）：① docker compose up -d 三分钟起服务 → ② examples/ 四个 Demo（离线 RAG / MCP 回环 / OpenAI 网关 / 多 Agent DAG）→ ③ docs/IMPLEMENTATION_STATUS.md 逐模块核验
- 底部 meta：v4.0.3 · MIT · github 仓库 · 文档见 docs/
