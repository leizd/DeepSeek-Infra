# Release Readiness

适用版本：v2.7.1。

v2.7.1 发布主题为 **Media Layer Hardening & Release Polish**：在 v2.7.0 Multimodal Media Layer 之上补强媒体路径边界、上传大小与 MIME 白名单、音视频分片粒度、Skill 媒体上下文容错，以及 2.7.1 release evidence / preflight 路径一致性。
## 1. Release Preflight — 版本一致性体检

发版前确认版本号在所有该出现的地方都同步，eval 报告是当前版本，且发布脚本仍排除本地缓存 / 日志 / 密钥：

```bash
python scripts/preflight_release.py --version 2.7.1
```

检查项：

- README 版本徽章是 `2.7.1`。
- `CHANGELOG.md` 顶部有 `## [2.7.1]` 条目。
- `Dockerfile` 示例 tag 是 `deepseek-infra:2.7.1`。
- `docs/IMPLEMENTATION_STATUS.md` 与 `evals/README.md` 的「适用版本」是 `v2.7.1`。
- `docs/EVIDENCE_INDEX.md` 存在且包含 Headless MCP bridge / A2A external peer / A2A third-party peer / Edge Router / Continue.dev MCP / OpenAI-compatible SDK / Workspace Core / Media Layer / Skill System / eval reports 索引。
- `evals/reports/latest.json`、`agent-latest.json`、`baseline-compare-latest.json` 与 `security-latest.json` 的 `version` 是 `2.7.1`，且包含统一 metadata。
- `docs/evidence/headless-mcp-bridge.json` 可解析、版本为 `2.7.1`，且关键 MCP bridge 步骤全为 PASS。
- `docs/evidence/a2a-external-peer.json` 可解析、版本为 `2.7.1`，且关键 A2A external peer checks 全为 PASS。
- `docs/evidence/a2a-third-party-peer.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `peerType=third-party`、`status=PASS` 且八类 A2A checks 全 PASS。
- `docs/evidence/edge-router-smoke.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `status=PASS` 且四类 Edge checks 全 PASS。
- `docs/evidence/continue-dev-mcp.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `status=PASS` 且六类 MCP checks 全 PASS。
- `docs/evidence/openai-compatible-sdks.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `status=PASS` 且 LangChain/LiteLLM/LlamaIndex 关键 SDK checks 全 PASS。
- `docs/evidence/workspace-v2.7.1.json` 必须存在、版本为 `2.7.1`、`status=PASS`，且 Project / Saved Items / Artifact / Export / secret redaction checks 全 PASS。
- `docs/evidence/media-v2.7.1.json` 必须存在、版本为 `2.7.1`、`status=PASS`，且 image / PDF / webpage import、segments、media-to-RAG、citations、project export 与 secret redaction checks 全 PASS。
- `docs/evidence/skills-v2.7.1.json` 必须存在、版本为 `2.7.1`、`status=PASS`，且 Skill API route / registry / runner / artifact / project binding checks 全 PASS。
- `docs/evidence/skills-ui-v2.7.1.json` 必须存在、版本为 `2.7.1`、`status=PASS`，且 Skill Workbench entrypoint / schema form / project binding / result links / styles / JS syntax / CI syntax gate checks 全 PASS。
- `docs/evidence/skill-builder-v2.7.1.json`、`docs/evidence/skill-packs-v2.7.1.json`、`docs/evidence/skill-eval-dashboard-v2.7.1.json`、`docs/evidence/skill-versioning-v2.7.1.json`、`docs/evidence/skill-analytics-v2.7.1.json`、`docs/evidence/skill-security-v2.7.1.json`、`docs/evidence/skill-catalog-v2.7.1.json` 与 `evals/reports/skills-v2.7.1.json` 必须存在、版本匹配、`status=PASS`，且 Skill authoring / Pack / Eval / Versioning / Analytics / Security / Catalog checks 全 PASS。
- `quality_gate_evidence` 确认 coverage 80%、offline eval、Agent Eval、baseline compare、injection strict 与 security corpus 全部 PASS。
- CHANGELOG / README / COMPATIBILITY / IMPLEMENTATION_STATUS / RELEASE_READINESS / EVIDENCE_INDEX / `docs/integrations/*.md` 不出现 `???`、`锟斤拷`、`\ufffd` 等乱码。
- `scripts/release.py` 仍排除 `.traces` / `.local-rag` / `.auth-token` / `.env` / `server*.log`。

退出码：`1` 表示有 `FAIL`；GUI、本地模型、第三方生态这类 `WARNING` 不阻断 CI。`--json` 输出机器可读摘要。

实现：[`scripts/preflight_release.py`](../scripts/preflight_release.py)；测试：[`tests/test_preflight_release.py`](../tests/test_preflight_release.py)。

## 2. Release Smoke Suite — 一键编排

把 doctor、Workspace Core smoke、Media Layer smoke、strict 离线评测、security corpus、Agent 评测、baseline compare、可选 MCP / A2A smoke 串成一个命令：

```bash
# 离线，CI 安全：doctor + workspace smoke + media smoke + strict eval suite + security corpus + Agent eval + baseline compare
python scripts/smoke_release.py --offline

# 带服务：额外跑 MCP / A2A 兼容 smoke
python scripts/smoke_release.py --with-server --base-url http://127.0.0.1:8000 --token <token>
```

`smoke_release.py` 只编排，不持有新逻辑。任意阶段非零退出则整体退出 `1`。可用 `--skip-doctor` / `--skip-workspace` / `--skip-evals` / `--skip-security` / `--skip-agent` / `--skip-compare` / `--skip-mcp` / `--skip-a2a` 裁剪。

实现：[`scripts/smoke_release.py`](../scripts/smoke_release.py)；测试：[`tests/test_smoke_release.py`](../tests/test_smoke_release.py)。

## 3. Release Manifest & Checksum — 发布产物证明

每次跑 [`scripts/release.py`](../scripts/release.py) 不再只产出一个 zip，还会在 `dist/` 下产出三件套：

```text
dist/deepseek-infra-2.7.1.zip
dist/deepseek-infra-2.7.1.zip.sha256
dist/deepseek-infra-2.7.1.manifest.json
```

`manifest.json` 记录发布的关键事实，可独立校验：

```json
{
  "schemaVersion": "release-manifest.v1",
  "version": "2.7.1",
  "commit": "abc1234",
  "builtAt": "2026-06-28T00:00:00Z",
  "python": "3.12",
  "coverageGate": "80%",
  "qualityGates": {
    "coverage": "80%",
    "offlineEval": "PASS",
    "agentEval": "PASS",
    "injectionStrict": "PASS",
    "baselineCompare": "PASS",
    "securityCorpus": "PASS",
    "workspaceCore": "PASS",
    "skillSystem": "PASS",
    "skillWorkbench": "PASS",
    "skillBuilder": "PASS",
    "skillPacks": "PASS",
    "skillEvalDashboard": "PASS",
    "skillVersioning": "PASS",
    "skillAnalytics": "PASS",
    "skillSecurity": "PASS",
    "skillCatalog": "PASS"
  },
  "evalReport": "evals/reports/latest.json",
  "agentReport": "evals/reports/agent-latest.json",
  "evidence": [
    "docs/evidence/headless-mcp-bridge.json",
    "docs/evidence/a2a-external-peer.json",
    "docs/evidence/a2a-third-party-peer.json",
    "docs/evidence/edge-router-smoke.json",
    "docs/evidence/continue-dev-mcp.json",
    "docs/evidence/openai-compatible-sdks.json",
    "docs/evidence/workspace-v2.7.1.json",
    "docs/evidence/media-v2.7.1.json",
    "docs/evidence/skills-v2.7.1.json",
    "docs/evidence/skills-ui-v2.7.1.json",
    "docs/evidence/skill-builder-v2.7.1.json",
    "docs/evidence/skill-packs-v2.7.1.json",
    "docs/evidence/skill-eval-dashboard-v2.7.1.json",
    "docs/evidence/skill-versioning-v2.7.1.json",
    "docs/evidence/skill-analytics-v2.7.1.json",
    "docs/evidence/skill-security-v2.7.1.json",
    "docs/evidence/skill-catalog-v2.7.1.json",
    "evals/reports/latest.json",
    "evals/reports/agent-latest.json",
    "evals/reports/baseline-compare-latest.json",
    "evals/reports/security-latest.json",
    "evals/reports/skills-v2.7.1.json",
    "evals/reports/media-v2.7.1.json",
    "docs/EVIDENCE_INDEX.md"
  ],
  "artifact": "deepseek-infra-2.7.1.zip",
  "sha256": "...",
  "bytes": 1234567
}
```

`.sha256` 是标准 `<hex>  <filename>` 格式，可用 `sha256sum -c` 校验。`--no-manifest` 可跳过这两个伴生产物；`--dry-run` 只枚举将要打包的文件数，不写 zip / checksum / manifest。

实现：[`deepseek_infra/infra/diagnostics/release_manifest.py`](../deepseek_infra/infra/diagnostics/release_manifest.py)；测试：[`tests/test_release_manifest.py`](../tests/test_release_manifest.py)。

## 4. CI release-readiness job

`.github/workflows/ci.yml` 的 `release-readiness` job 在干净 Ubuntu runner 中跑：

```yaml
- run: python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
- run: python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
- run: python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.7.1.json
- run: python scripts/smoke_media.py --offline --out docs/evidence/media-v2.7.1.json
- run: python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.7.1.json
- run: python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.7.1.json
- run: python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.7.1.json
- run: python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.7.1.json
- run: python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.7.1.json --report-out evals/reports/skills-v2.7.1.json
- run: python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.7.1.json
- run: python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.7.1.json
- run: python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.7.1.json
- run: python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.7.1.json
- run: python evals/runners/run_media_eval.py --strict --out evals/reports/media-v2.7.1.json
- run: python scripts/preflight_release.py --version 2.7.1
- run: python scripts/doctor.py --offline
- run: python scripts/release.py --clean-workspace --dry-run
```

CI 不强制安装真实第三方 A2A server、Ollama 或 GGUF 模型；这些 evidence 缺失时是 WARNING。一旦提交对应 evidence，preflight 会按严格 schema 和 PASS checks 验证。

## 5. Headless MCP Evidence（v2.3.2）

`preflight_release.py` 自 v2.3.2 起增加 `headless_mcp_bridge_evidence` 硬检查。它读取 `docs/evidence/headless-mcp-bridge.json`，确认无 GUI 的 MCP stdio bridge 路径已经自动跑通：

- `bridge.start`
- `mcp.initialize`
- `mcp.tools_list`
- `mcp.tools_call`
- `mcp.policy_denial`

本项是最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
```

## 6. A2A External Peer Evidence（v2.3.3）

`preflight_release.py` 自 v2.3.3 起增加 `a2a_external_peer_evidence` 硬检查。它读取 `docs/evidence/a2a-external-peer.json`，确认一个无 GUI、无 API key 的外部 A2A peer 路径已经自动跑通：

- `agentCard`
- `messageSend`
- `messageStream`
- `tasksGet`
- `tasksCancel`
- `tasksList`
- `artifactChunks`
- `sseFinalEvent`

本项是最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
```

## 7. A2A Third-Party Peer Evidence（v2.4.4）

`preflight_release.py` 自 v2.4.4 起增加 `a2a_third_party_peer_evidence` 可选检查。它读取 `docs/evidence/a2a-third-party-peer.json`，确认 DeepSeek Infra 的 `A2AClient` 路径已经连接到第三方或第三方风格 A2A-compatible peer，并完成完整互操作 smoke：

- `agentCard`
- `messageSend`
- `messageStream`
- `tasksGet`
- `tasksCancel`
- `tasksList`
- `artifactChunks`
- `sseFinalEvent`

本项缺失或版本陈旧时返回 `WARNING`，避免没有第三方生态环境的 CI runner 被阻断；同版本 evidence 文件存在时，统一 metadata、`peerType=third-party`、`status=PASS` 与八类 checks 都必须通过，否则 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md
```

当前 evidence 记录的是 A2A-compatible third-party-style smoke peer；LangGraph / CrewAI / Google A2A reference 等具体实现仍保留在 [a2a-third-party-plan.md](integrations/a2a-third-party-plan.md) 中作为后续候选。

## 8. Edge Router Smoke Evidence（v2.4.3）

`preflight_release.py` 自 v2.4.3 起增加 `edge_router_smoke_evidence` 可选检查。它读取 `docs/evidence/edge-router-smoke.json`，确认 Edge / Ollama / 本地 OpenAI-compatible provider 路径已经记录结构化 evidence：

- `ollamaModelsListed`
- `openaiCompatibleLocalCall`
- `edgeStatusEndpoint`
- `fallbackReady`

本项缺失或版本陈旧时返回 `WARNING`，避免没有 Ollama / GGUF 模型的 CI runner 被强制阻断；同版本 evidence 文件存在时，`status=PASS` 与四类 checks 都必须通过，否则 preflight 返回 `FAIL`。刷新命令：

```bash
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
```

真实 GGUF / MLC 推理仍依赖本地模型文件与可选依赖；本检查只把可复现的本地 provider 路径纳入 release evidence，不把 Edge-Cloud Model Router 升级为 Working。

## 9. Continue.dev MCP Evidence（v2.4.5）

`preflight_release.py` 自 v2.4.5 起增加 `continue_dev_mcp_evidence` 可选检查。它读取 `docs/evidence/continue-dev-mcp.json`，确认 Continue.dev MCP client 路径已经记录结构化 evidence：

- `configLoaded`
- `mcpInitialize`
- `toolsList`
- `lowRiskToolCall`
- `policyDenial`
- `promptInjectionClean`

本项缺失或版本陈旧时返回 `WARNING`，避免没有 Continue.dev GUI 环境的 CI runner 被强制阻断；同版本 evidence 文件存在时，统一 metadata、`status=PASS` 与六类 checks 都必须通过，否则 preflight 返回 `FAIL`。Continue.dev 配置指南与验证 runbook 见 [docs/integrations/continue-dev.md](integrations/continue-dev.md)。

## 10. OpenAI-Compatible SDK Evidence（v2.4.6）

`preflight_release.py` 自 v2.4.6 起增加 `openai_compatible_sdk_evidence` 可选检查。它读取 `docs/evidence/openai-compatible-sdks.json`，确认 LangChain (ChatOpenAI)、LiteLLM、LlamaIndex (OpenAILike) 等 OpenAI-compatible SDK 路径已经记录结构化 evidence：

- `sdks.langchain.modelsList`
- `sdks.langchain.chatCompletion`
- `sdks.langchain.streaming`
- `sdks.litellm.modelsList`
- `sdks.litellm.chatCompletion`
- `sdks.litellm.streaming`
- `sdks.llamaindex.chatCompletion`

本项缺失或版本陈旧时返回 `WARNING`，避免没有安装 LangChain / LiteLLM / LlamaIndex 等可选依赖的 CI runner 被强制阻断；同版本 evidence 文件存在时，统一 metadata、`status=PASS` 与七类 SDK checks 都必须通过，否则 preflight 返回 `FAIL`。SDK smoke 依赖放在 `requirements-sdk-smoke.txt` 中，与默认运行时依赖解耦。

```bash
python scripts/smoke_openai_compatible_sdks.py --base-url http://127.0.0.1:8000/v1 --model deepseek-v4-pro --out docs/evidence/openai-compatible-sdks.json --markdown docs/evidence/openai-compatible-sdks.md
```

## 11. Workspace Core Evidence（v2.7.1）

`preflight_release.py` 自 v2.5.0 起增加 `workspace_core_evidence` 硬检查。它读取 `docs/evidence/workspace-v2.7.1.json`，确认 Workspace Core 已经用离线 smoke 跑通：

- `projectCreate`
- `savedItemCreate`
- `artifactList`
- `conversationExport`
- `projectExportZip`
- `secretRedaction`

本项是 v2.7.1 的最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.7.1.json
```

## 12. Media Layer Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `media_layer_evidence` 硬检查。它读取 `docs/evidence/media-v2.7.1.json`，确认 Multimodal Media Layer 已经完成离线核心验收：

- `imageImport`
- `pdfPageIndex`
- `webpageSnapshot`
- `mediaSegments`
- `mediaToRag`
- `mediaCitations`
- `projectExportIncludesMedia`
- `secretRedaction`

刷新命令：

```bash
python scripts/smoke_media.py --offline --out docs/evidence/media-v2.7.1.json
python evals/runners/run_media_eval.py --strict --out evals/reports/media-v2.7.1.json
```

## 13. Skill System Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `skill_system_evidence` 硬检查。它读取 `docs/evidence/skills-v2.7.1.json`，确认 Skill System 已经完成 Web API 接入与离线核心验收：

- `skillApiRoutes`
- `builtinSkillsLoad`
- `customSkillCreate`
- `inputSchemaValidation`
- `toolPermissionGate`
- `artifactPolicy`
- `projectBinding`
- `skillExport`

刷新命令：

```bash
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.7.1.json
```


## 13. Skill Workbench UI Evidence（v2.7.1）
`preflight_release.py` 自 v2.7.0 起增加 `skill_ui_evidence` 硬检查。它读取 `docs/evidence/skills-ui-v2.7.1.json`，确认 Skill Workbench 前端已经完成本地 UI 接入与离线验收：

- `skillWorkbenchEntrypoint`
- `skillRunSchemaForm`
- `skillApiActions`
- `projectSkillBindingUi`
- `skillRunResultLinks`
- `skillPanelLifecycle`
- `skillPanelStyles`
- `skillJsSyntax`
- `ciSyntaxGate`

刷新命令：

```bash
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.7.1.json
```

## 14. Skill Builder Evidence (v2.7.1)

`preflight_release.py` 自 v2.7.0 起增加 `skill_builder_evidence` 硬检查。它读取 `docs/evidence/skill-builder-v2.7.1.json` 并验证本地创作路径：

- Builder 入口：`New Skill`、`skillBuilderHost` 和 `skillBuilderForm` 存在。
- 克隆内置 Skill：内置 Skill 可变为可编辑的自定义 Skill。
- 可视化 schema 编辑器：key、title、description、type、required、default、enum 和 maxLength 可生成 `inputSchema`。
- Tool 权限选择器：已选工具携带风险标签且仍通过后端 schema 验证。
- 验证与试运行：保存前 `action=validate` 和 `action=dry_run` 正常工作。
- 保存与导出：已保存的自定义 Skill 仍使用现有导出路径。
- UI 资源：`docs/assets/skill-builder.png` 和 `docs/assets/skill-builder-dry-run.png` 存在，用于 README / evidence 审查。

刷新命令：

```bash
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.7.1.json
```

## 15. Skill Packs Evidence (v2.7.1)

`preflight_release.py` 自 v2.7.0 起增加 `skill_packs_evidence` 硬检查。它读取 `docs/evidence/skill-packs-v2.7.1.json` 并验证本地 Skill Pack 路径：

- Pack schema 验证：`deepseek_infra/infra/skills/pack.py` 校验 packId / name / description / version / author / skills，其中嵌入的 Skill 配置通过 `validate_skill_config` 验证。
- 内置模板库：Study / Research / Code / Office Skill Pack 从 `skills/packs/` 加载。
- Pack 导入 / 导出：导入会将嵌入的 Skill 安装到本地；导出会嵌入完整的 Skill 配置，使 pack 保持自包含。
- skillId 冲突处理：`onConflict=error` 会报错，`skip` 跳过已存在的 Skill，`overwrite` 重新安装。
- Tool 权限差异：`tool_permission_summary` 为每个 allowedTool 标注风险级别并标记高风险 / 需审批工具。
- 项目 pack 绑定：`enable_pack_for_project` 将 Pack 的 Skill 添加到项目并记录 `enabledPacks`。
- Pack 安装试运行：安装内置 Pack 会将其引用的 Skill 启用到项目上。
- Packs UI 选项卡：`skillPacksButton` / `skillPacksHost` / `skillBuiltinPackList` / `skillCustomPackList` 存在，且 `skills.js` 通过 `node --check`。
- Pack 资源：`docs/assets/skill-packs.png` 和 `docs/assets/skill-pack-import.png` 存在。

刷新命令：

```bash
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.7.1.json
```

## 16. Skill Eval Dashboard Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `skill_eval_dashboard_evidence` 硬检查。它读取 `docs/evidence/skill-eval-dashboard-v2.7.1.json` 和 `evals/reports/skills-v2.7.1.json`，然后验证本地 Skill 质量路径：

- Eval 仪表板入口：`skillEvalButton`、`skillEvalHost`、汇总卡片、Skill 行、Pack 行和用例列表存在。
- Eval 用例构建器：本地用例可捕获 `skillId`、输入 JSON、关键词、必需 JSON 路径、禁止模式、预期 artifact 和项目绑定需求。
- Skill Eval API 操作：`eval_report`、`list_eval_cases`、`create_eval_case` 和 `delete_eval_case` 通过 `POST /api/skills` 接入。
- Skill / Pack 评分：schema、Tool Policy、artifact policy、project binding、content、latency 和 overall score 针对 Skill 和 Pack 范围输出。
- 回归比较：当前和基线报告可标记新增失败、已修复失败和评分回归。
- 导出操作：Workbench 可导出 JSON、导出 Markdown 并复制摘要。
- Eval 资源：`docs/assets/skill-eval-dashboard.png` 和 `docs/assets/skill-eval-case-builder.png` 存在。

刷新命令：

```bash
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.7.1.json --report-out evals/reports/skills-v2.7.1.json
```

## 17. Skill Versioning Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `skill_versioning_evidence` 硬检查。它读取 `docs/evidence/skill-versioning-v2.7.1.json`，然后验证本地 Skill / Pack 生命周期路径：

- Skill 修订快照：自定义 Skill 创建/更新会保存版本化历史，包含修订元数据和内容散列。
- Skill diff：当前版本和历史版本可比较 prompt、schemas、tools、memory、artifacts、project binding 和 eval summary。
- 迁移计划：schema 变更会标记重命名、必填字段、已删除字段以及现有 project/eval/saved 元数据引用。
- Skill 回滚：自定义 Skill 可从历史恢复，同时保留回滚检查点。
- Pack 版本安装与回滚：自定义 Pack 记录版本化项目绑定，并可回滚到历史修订版。
- Eval 感知的升级门槛：Pack 升级在安装前包含 score、pass rate、regression count 和 recommendation 元数据。
- 版本 UI 资源：`docs/assets/skill-version-history.png` 和 `docs/assets/skill-version-diff.png` 存在。

刷新命令：

```bash
python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.7.1.json
```

## 18. Skill Analytics Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `skill_analytics_evidence` 硬检查。它读取 `docs/evidence/skill-analytics-v2.7.1.json`，然后验证本地 Skill 使用回路：

- Skill 运行历史：已完成和失败的运行均持久化，包含稳定的运行元数据。
- 使用分析：生成 success/failure rate、latency、top Skills/Packs、artifacts、saved items、project binding usage 和趋势摘要。
- 失败诊断：schema 验证失败被分类并包含修复建议。
- 项目链接：project run history、project analytics、trace links 和 artifact links 存在。
- 保留与隐私：失败运行可被清理，运行摘要可在保留元数据的同时被脱敏。
- Runs UI：Skill Workbench 暴露 Runs 选项卡、汇总卡片、运行详情、清理、导出和脱敏控件。

刷新命令：

```bash
python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.7.1.json
```

## 17. Evidence Index & Metadata（v2.3.4）


## 19. Skill Security Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `skill_security_evidence` 硬检查。它读取 `docs/evidence/skill-security-v2.7.1.json`，然后验证本地 Skill 信任路径：

- 安全审查：Skill 和 Pack 审查产出 trust level、risk score、findings、allowedTools risk、approval count 和 manifest hashes。
- 静态扫描：检测 prompt injection、secret exfiltration、secret file access、network exfiltration、hidden tool instructions 和 encoded suspicious text。
- 信任生命周期：覆盖本地信任、取消信任、封禁和篡改检测。
- 签名准备：security manifests 包含 content、schema、prompt 和 tool-grant 散列，`signed=false`。
- 运行元数据：Skill 分析记录 securityReviewId、runSecurityLevel、trustedAtRun、toolGrantHashAtRun、approvalRequired 和 blockedReason。
- Security UI：Skill Workbench 暴露 Security 选项卡、摘要、审查详情、manifest 预览和信任/封禁操作。

刷新命令：

```bash
python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.7.1.json
```

## 20. Skill Catalog Evidence（v2.7.1）

`preflight_release.py` 自 v2.7.0 起增加 `skill_catalog_evidence` 硬检查。它读取 `docs/evidence/skill-catalog-v2.7.1.json`，然后验证本地 Skill Marketplace-lite 路径：

- Catalog manifest：本地目录只索引本机 Skills / Packs，并记录 source、summary 和 local-only 状态。
- Catalog list / search：可列出内置 Skill、内置 Pack、自定义 / imported Pack，并按 query 与 trust filters 搜索。
- Install preview：安装前返回 included Skills、新增 enabledSkills、工具权限摘要、风险分数、eval 分数和项目绑定变化。
- 安全门禁：high-risk 且未批准的条目无法安装，blocked 条目始终禁止安装。
- Project binding：Catalog install / uninstall 会更新项目 enabledSkills、enabledPacks 和 pack version metadata。
- Catalog UI：Skill Workbench 暴露 Catalog 选项卡、摘要卡片、搜索/筛选、预检详情和导出 controls。

刷新命令：

```bash
python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.7.1.json
```

v2.3.4 新增 [`docs/EVIDENCE_INDEX.md`](../docs/EVIDENCE_INDEX.md) 作为所有互操作证据的统一入口，并在 preflight 中检查：

- `docs/EVIDENCE_INDEX.md` 存在。
- 关键证据 JSON（headless MCP bridge、A2A external peer、A2A third-party peer、Edge Router、Continue.dev MCP、OpenAI-compatible SDK、Workspace Core、Skill System、latest eval、agent eval）包含统一 metadata：`version`、`commit`、`generatedAt`、`environment`（含 `os` / `python` / `ci`）、`status`。
- release manifest 包含 `evidence` 列表。

刷新命令：

```bash
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
python scripts/smoke_openai_compatible_sdks.py --base-url http://127.0.0.1:8000/v1 --model deepseek-v4-pro --out docs/evidence/openai-compatible-sdks.json --markdown docs/evidence/openai-compatible-sdks.md
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.7.1.json
python scripts/smoke_media.py --offline --out docs/evidence/media-v2.7.1.json
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.7.1.json
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.7.1.json
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.7.1.json
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.7.1.json
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.7.1.json --report-out evals/reports/skills-v2.7.1.json
python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.7.1.json
python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.7.1.json
python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.7.1.json
python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.7.1.json
python evals/runners/run_media_eval.py --strict --out evals/reports/media-v2.7.1.json
python evals/runners/run_offline_eval_suite.py --include-agent --strict --out evals/reports/latest.json --markdown evals/reports/latest.md
python evals/runners/run_security_corpus.py --strict --out evals/reports/security-latest.json --markdown evals/reports/security-latest.md
python evals/runners/run_agent_eval.py --report-dir evals/reports --strict
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json --out evals/reports/baseline-compare-latest.json
```

## 17. Docs Encoding Sanity（v2.3.4）

`preflight_release.py` 自 v2.3.4 起新增 `docs_encoding_sanity` 硬检查，扫描以下文档是否包含编码乱码：

- `CHANGELOG.md`
- `README.md`
- `docs/COMPATIBILITY.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/RELEASE_READINESS.md`
- `docs/EVIDENCE_INDEX.md`
- `docs/integrations/*.md`

识别模式：连续 `???`、`锟斤拷`、Unicode replacement character `\ufffd`。发现即 FAIL，防止 v2.3.3 的 CHANGELOG 乱码问题再次出现。

## 19. Quality Gate Evidence（v2.7.1）

`preflight_release.py` 自 v2.4.2 起增加 `quality_gate_evidence` 硬检查。它聚合以下证据：

- coverage gate：`pyproject.toml` 与 CI 均为 80%。
- offline eval：`evals/reports/latest.json` `status=PASS`。
- Agent Eval：`evals/reports/agent-latest.json` `status=PASS`。
- baseline compare：`evals/reports/baseline-compare-latest.json` `status=PASS`。
- injection strict：`latest.json` 的 `injection.status=PASS` 且 `gateMode=hard`。
- security corpus：`evals/reports/security-latest.json` `status=PASS`。
- Workspace Core：`docs/evidence/workspace-v2.7.1.json` `status=PASS`。
- Media Layer：`docs/evidence/media-v2.7.1.json` 和 `evals/reports/media-v2.7.1.json` `status=PASS`。
- Skill System：`docs/evidence/skills-v2.7.1.json` `status=PASS`。
- Skill Workbench UI：`docs/evidence/skills-ui-v2.7.1.json` `status=PASS`。
- Skill Builder：`docs/evidence/skill-builder-v2.7.1.json` `status=PASS`。
- Skill Packs：`docs/evidence/skill-packs-v2.7.1.json` `status=PASS`。
- Skill Eval Dashboard：`docs/evidence/skill-eval-dashboard-v2.7.1.json` 和 `evals/reports/skills-v2.7.1.json` `status=PASS`。
- Skill Versioning：`docs/evidence/skill-versioning-v2.7.1.json` `status=PASS`。
- Skill Analytics：`docs/evidence/skill-analytics-v2.7.1.json` `status=PASS`。
- Skill Security：`docs/evidence/skill-security-v2.7.1.json` `status=PASS`。
- Skill Catalog：`docs/evidence/skill-catalog-v2.7.1.json` `status=PASS`。

刷新命令：

```bash
python scripts/update_eval_report.py
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.7.1.json
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.7.1.json
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.7.1.json
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.7.1.json
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.7.1.json
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.7.1.json --report-out evals/reports/skills-v2.7.1.json
python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.7.1.json
python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.7.1.json
python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.7.1.json
python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.7.1.json
python scripts/preflight_release.py --version 2.7.1
```

## 20. GUI Interop Evidence Checklist（v2.3.1）

`preflight_release.py` 自 v2.3.1 起增加 `gui_interop_evidence` 检查，扫描 `docs/COMPATIBILITY.md` 中 Claude Desktop / Cursor 行的状态标记：

- **🟡 状态**：GUI 实机证据尚未填入，检查结果为 `WARNING`。
- **✅ GUI tested 状态**：人工完成 GUI 验证 runbook 并更新矩阵后，检查结果为 `PASS`。

详见 [docs/integrations/claude-desktop.md](integrations/claude-desktop.md) 和 [docs/integrations/cursor.md](integrations/cursor.md)。

## 发版前最小流程

```bash
# 1. 刷新 eval / agent 报告到当前版本
python scripts/update_eval_report.py

# 2. 刷新 headless MCP bridge evidence
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json

# 3. 刷新 A2A external peer evidence
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json

# 4. 刷新 A2A third-party evidence（需要第三方或第三方风格 A2A-compatible peer）
python scripts/smoke_a2a_external_peer.py --peer-url http://<third-party-host>:<port> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md

# 5. 刷新 Edge Router smoke evidence（需要本地 Ollama / Ollama-compatible provider）
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md

# 6. 刷新 Workspace Core evidence（离线）
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.7.1.json

# 7. 刷新 Skill System evidence（离线）
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.7.1.json

# 8. 刷新 Skill Workbench UI evidence（离线）
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.7.1.json

# 9. 刷新 Skill Builder evidence（离线）
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.7.1.json

# 10. 刷新 Skill Packs evidence（离线）
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.7.1.json

# 11. 刷新 Skill Eval Dashboard evidence（离线）
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.7.1.json --report-out evals/reports/skills-v2.7.1.json

# 12. 刷新 Skill Versioning evidence（离线）
python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.7.1.json

# 13. 刷新 Skill Analytics evidence（离线）
python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.7.1.json

# 14. 刷新 Skill Security evidence（离线）
python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.7.1.json

# 15. 刷新 Skill Catalog evidence（离线）
python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.7.1.json

# 16. 版本一致性与质量证据体检
python scripts/preflight_release.py --version 2.7.1

# 17. 运行时体检
python scripts/doctor.py --offline

# 18. 一键 smoke（离线）
python scripts/smoke_release.py --offline

# 19. 打包并生成 manifest + checksum + qualityGates
python scripts/release.py --clean-workspace --version 2.7.1
```

也可以直接用 `python scripts/smoke_release.py --offline` 刷新离线质量证据；本地模型和第三方生态 evidence 需要在具备对应环境时单独补齐。
