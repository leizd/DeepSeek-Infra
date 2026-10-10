# DeepSeek Infra 项目介绍 PPT · 执行计划

## 任务
为 D:\deepseek 仓库（DeepSeek Infra v4.0.3，本地优先 Agentic AI Infra 平台）制作项目介绍 PPT，
使用 kimi-slides 技能（pptd DSL → pptx），场景：技术与工程（项目/架构介绍）。

## 阶段
- Stage 0 — 准备（Orchestrator 本人，已完成）：
  - 读取 kimi-slides 参考：pptd.md / cli.md / slides_categories.md / tech-engineering.md / fonts.md
  - 内容调研：explore 子代理输出 content_brief.md（含来源标注）
  - musepool 灵感：restrained editorial（浅暖灰底 #F2F2F2 + 深海军蓝 #082D4F）+ 暖陶土/石板蓝语义双色 + 网格与斜切符号工艺
- Stage 1 — 设计（Orchestrator 本人）：
  - 产出 deepseek-infra-deck/_design.md（设计系统）、_outline.md（分页大纲）、deepseek-infra-deck.pptd（主入口+主题）
  - 拷贝 docs/assets 真实截图到 deck media/
- Stage 2 — 页面制作（6 个 coder 子代理并行，各 2-3 页 .page）：
  - Writer_P1_2 封面+定位 / Writer_P3_4 数字+架构 / Writer_P5_6 链路+模块 /
    Writer_P7_8 安全+质量 / Writer_P9_10 部署+演进 / Writer_P11_13 截图+路线图+结尾
  - 每个子代理必须先读 _design.md、_outline.md、pptd 参考，再写页
- Stage 3 — 校验与修复（Orchestrator 本人）：
  - `kimi-slides check` 多轮修复；`kimi-slides screenshot` 拼图总览 + 逐页精修
- Stage 4 — 交付（Orchestrator 本人）：
  - `kimi-slides package` 输出 DeepSeek-Infra-项目介绍.pptx

## 质量门禁
- 禁卡片墙、禁蓝紫渐变、禁三等分公式化版式；暖=标题/关键路径，冷=结构/节点
- 所有数字必须来自 content_brief.md（有原文来源），禁止编造
