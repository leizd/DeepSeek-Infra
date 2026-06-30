# Release Readiness

适用版本：v2.6.5。

v2.6.5 release theme is **Skill Eval Dashboard / Skill Quality Loop**: after v2.6.4 bundled Skills into local Packs, this release adds offline Skill / Pack scoring, Eval Case Builder, regression compare, report export, and a Workbench Eval tab. `deepseek_infra/infra/skills/eval.py` owns the scoring engine; `static/modules/skills.js` renders the dashboard; `scripts/smoke_skill_eval_dashboard.py` records offline UI and report evidence.
## 1. Release Preflight — 版本一致性体检

发版前确认版本号在所有该出现的地方都同步，eval 报告是当前版本，且发布脚本仍排除本地缓存 / 日志 / 密钥：

```bash
python scripts/preflight_release.py --version 2.6.5
```

检查项：

- README 版本徽章是 `2.6.5`。
- `CHANGELOG.md` 顶部有 `## [2.6.5]` 条目。
- `Dockerfile` 示例 tag 是 `deepseek-infra:2.6.5`。
- `docs/IMPLEMENTATION_STATUS.md` 与 `evals/README.md` 的「适用版本」是 `v2.6.5`。
- `docs/EVIDENCE_INDEX.md` 存在且包含 Headless MCP bridge / A2A external peer / A2A third-party peer / Edge Router / Continue.dev MCP / OpenAI-compatible SDK / Workspace Core / Skill System / eval reports 索引。
- `evals/reports/latest.json`、`agent-latest.json`、`baseline-compare-latest.json` 与 `security-latest.json` 的 `version` 是 `2.6.5`，且包含统一 metadata。
- `docs/evidence/headless-mcp-bridge.json` 可解析、版本为 `2.6.5`，且关键 MCP bridge 步骤全为 PASS。
- `docs/evidence/a2a-external-peer.json` 可解析、版本为 `2.6.5`，且关键 A2A external peer checks 全为 PASS。
- `docs/evidence/a2a-third-party-peer.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `peerType=third-party`、`status=PASS` 且八类 A2A checks 全 PASS。
- `docs/evidence/edge-router-smoke.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `status=PASS` 且四类 Edge checks 全 PASS。
- `docs/evidence/continue-dev-mcp.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `status=PASS` 且六类 MCP checks 全 PASS。
- `docs/evidence/openai-compatible-sdks.json` 缺失或版本陈旧时为 WARNING；同版本 evidence 存在时必须 `status=PASS` 且 LangChain/LiteLLM/LlamaIndex 关键 SDK checks 全 PASS。
- `docs/evidence/workspace-v2.6.5.json` 必须存在、版本为 `2.6.5`、`status=PASS`，且 Project / Saved Items / Artifact / Export / secret redaction checks 全 PASS。
- `docs/evidence/skills-v2.6.5.json` 必须存在、版本为 `2.6.5`、`status=PASS`，且 Skill API route / registry / runner / artifact / project binding checks 全 PASS。
- `docs/evidence/skills-ui-v2.6.5.json` 必须存在、版本为 `2.6.5`、`status=PASS`，且 Skill Workbench entrypoint / schema form / project binding / result links / styles / JS syntax / CI syntax gate checks 全 PASS。
- `docs/evidence/skill-builder-v2.6.5.json`、`docs/evidence/skill-packs-v2.6.5.json`、`docs/evidence/skill-eval-dashboard-v2.6.5.json` 与 `evals/reports/skills-v2.6.5.json` 必须存在、版本匹配、`status=PASS`，且 Skill authoring / Pack / Eval checks 全 PASS。
- `quality_gate_evidence` 确认 coverage 80%、offline eval、Agent Eval、baseline compare、injection strict 与 security corpus 全部 PASS。
- CHANGELOG / README / COMPATIBILITY / IMPLEMENTATION_STATUS / RELEASE_READINESS / EVIDENCE_INDEX / `docs/integrations/*.md` 不出现 `???`、`锟斤拷`、`\ufffd` 等乱码。
- `scripts/release.py` 仍排除 `.traces` / `.local-rag` / `.auth-token` / `.env` / `server*.log`。

退出码：`1` 表示有 `FAIL`；GUI、本地模型、第三方生态这类 `WARNING` 不阻断 CI。`--json` 输出机器可读摘要。

实现：[`scripts/preflight_release.py`](../scripts/preflight_release.py)；测试：[`tests/test_preflight_release.py`](../tests/test_preflight_release.py)。

## 2. Release Smoke Suite — 一键编排

把 doctor、Workspace Core smoke、strict 离线评测、security corpus、Agent 评测、baseline compare、可选 MCP / A2A smoke 串成一个命令：

```bash
# 离线，CI 安全：doctor + workspace smoke + strict eval suite + security corpus + Agent eval + baseline compare
python scripts/smoke_release.py --offline

# 带服务：额外跑 MCP / A2A 兼容 smoke
python scripts/smoke_release.py --with-server --base-url http://127.0.0.1:8000 --token <token>
```

`smoke_release.py` 只编排，不持有新逻辑。任意阶段非零退出则整体退出 `1`。可用 `--skip-doctor` / `--skip-workspace` / `--skip-evals` / `--skip-security` / `--skip-agent` / `--skip-compare` / `--skip-mcp` / `--skip-a2a` 裁剪。

实现：[`scripts/smoke_release.py`](../scripts/smoke_release.py)；测试：[`tests/test_smoke_release.py`](../tests/test_smoke_release.py)。

## 3. Release Manifest & Checksum — 发布产物证明

每次跑 [`scripts/release.py`](../scripts/release.py) 不再只产出一个 zip，还会在 `dist/` 下产出三件套：

```text
dist/deepseek-infra-2.6.5.zip
dist/deepseek-infra-2.6.5.zip.sha256
dist/deepseek-infra-2.6.5.manifest.json
```

`manifest.json` 记录发布的关键事实，可独立校验：

```json
{
  "schemaVersion": "release-manifest.v1",
  "version": "2.6.5",
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
    "skillEvalDashboard": "PASS"
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
    "docs/evidence/workspace-v2.6.5.json",
    "docs/evidence/skills-v2.6.5.json",
    "docs/evidence/skills-ui-v2.6.5.json",
    "docs/evidence/skill-builder-v2.6.5.json",
    "docs/evidence/skill-packs-v2.6.5.json",
    "docs/evidence/skill-eval-dashboard-v2.6.5.json",
    "evals/reports/latest.json",
    "evals/reports/agent-latest.json",
    "evals/reports/baseline-compare-latest.json",
    "evals/reports/security-latest.json",
    "evals/reports/skills-v2.6.5.json",
    "docs/EVIDENCE_INDEX.md"
  ],
  "artifact": "deepseek-infra-2.6.5.zip",
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
- run: python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.6.5.json
- run: python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.6.5.json
- run: python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.6.5.json
- run: python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.6.5.json
- run: python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.6.5.json
- run: python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.6.5.json --report-out evals/reports/skills-v2.6.5.json
- run: python scripts/preflight_release.py --version 2.6.5
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

## 11. Workspace Core Evidence（v2.6.5）

`preflight_release.py` 自 v2.5.0 起增加 `workspace_core_evidence` 硬检查。它读取 `docs/evidence/workspace-v2.6.5.json`，确认 Workspace Core 已经用离线 smoke 跑通：

- `projectCreate`
- `savedItemCreate`
- `artifactList`
- `conversationExport`
- `projectExportZip`
- `secretRedaction`

本项是 v2.6.5 的最低交付标准，缺失或失败会让 preflight 返回 `FAIL`。刷新命令：

```bash
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.6.5.json
```

## 12. Skill System Evidence（v2.6.5）

`preflight_release.py` 自 v2.6.5 起增加 `skill_system_evidence` 硬检查。它读取 `docs/evidence/skills-v2.6.5.json`，确认 Skill System 已经完成 Web API 接入与离线核心验收：

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
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.6.5.json
```


## 13. Skill Workbench UI Evidence（v2.6.5）
`preflight_release.py` 自 v2.6.5 起增加 `skill_ui_evidence` 硬检查。它读取 `docs/evidence/skills-ui-v2.6.5.json`，确认 Skill Workbench 前端已经完成本地 UI 接入与离线验收：

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
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.6.5.json
```

## 14. Skill Builder Evidence (v2.6.5)

`preflight_release.py` adds the hard `skill_builder_evidence` check in v2.6.5. It reads `docs/evidence/skill-builder-v2.6.5.json` and verifies the local authoring path:

- Builder entrypoint: `New Skill`, `skillBuilderHost`, and `skillBuilderForm` are present.
- Clone built-in Skill: built-in Skills can become custom editable Skills.
- Visual schema editor: key, title, description, type, required, default, enum, and maxLength generate `inputSchema`.
- Tool permission picker: selected tools carry risk labels and still pass backend schema validation.
- Validate and dry-run: `action=validate` and `action=dry_run` work before saving.
- Save and export: saved custom Skills still use the existing export path.
- UI assets: `docs/assets/skill-builder.png` and `docs/assets/skill-builder-dry-run.png` are present for README / evidence review.

Refresh command:

```bash
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.6.5.json
```

## 15. Skill Packs Evidence (v2.6.5)

`preflight_release.py` adds the hard `skill_packs_evidence` check in v2.6.5. It reads `docs/evidence/skill-packs-v2.6.5.json` and verifies the local Skill Pack path:

- Pack schema validation: `deepseek_infra/infra/skills/pack.py` validates packId / name / description / version / author / skills, with embedded Skill configs validated through `validate_skill_config`.
- Built-in Template Library: Study / Research / Code / Office Skill Packs load from `skills/packs/`.
- Pack import / export: import installs embedded Skills locally; export embeds full Skill configs so packs stay self-contained.
- skillId conflict handling: `onConflict=error` raises, `skip` skips existing Skills, `overwrite` re-installs them.
- Tool permission diff: `tool_permission_summary` labels each allowedTool with risk and flags high-risk / requires-approval tools.
- Project pack binding: `enable_pack_for_project` adds a Pack's Skills to a project and records `enabledPacks`.
- Pack install dry-run: installing a built-in Pack enables its referenced Skills on a project.
- Packs UI tab: `skillPacksButton` / `skillPacksHost` / `skillBuiltinPackList` / `skillCustomPackList` are present and `skills.js` passes `node --check`.
- Pack assets: `docs/assets/skill-packs.png` and `docs/assets/skill-pack-import.png` are present.

Refresh command:

```bash
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.6.5.json
```

## 16. Skill Eval Dashboard Evidence (v2.6.5)

`preflight_release.py` adds the hard `skill_eval_dashboard_evidence` check in v2.6.5. It reads `docs/evidence/skill-eval-dashboard-v2.6.5.json` and `evals/reports/skills-v2.6.5.json`, then verifies the local Skill quality path:

- Eval dashboard entrypoint: `skillEvalButton`, `skillEvalHost`, summary cards, Skill rows, Pack rows, and case list are present.
- Eval Case Builder: local cases can capture `skillId`, input JSON, keywords, required JSON paths, forbidden patterns, expected artifacts, and project binding requirements.
- Skill Eval API actions: `eval_report`, `list_eval_cases`, `create_eval_case`, and `delete_eval_case` are wired through `POST /api/skills`.
- Skill / Pack scoring: schema, Tool Policy, artifact policy, project binding, content, latency, and overall score are emitted for Skill and Pack scopes.
- Regression compare: current and baseline reports can mark new failures, fixed failures, and score regressions.
- Export actions: the Workbench can export JSON, export Markdown, and copy a summary.
- Eval assets: `docs/assets/skill-eval-dashboard.png` and `docs/assets/skill-eval-case-builder.png` are present.

Refresh command:

```bash
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.6.5.json --report-out evals/reports/skills-v2.6.5.json
```

## 17. Evidence Index & Metadata（v2.3.4）

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
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.6.5.json
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.6.5.json
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.6.5.json
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.6.5.json
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.6.5.json
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.6.5.json --report-out evals/reports/skills-v2.6.5.json
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

## 19. Quality Gate Evidence（v2.6.5）

`preflight_release.py` 自 v2.4.2 起增加 `quality_gate_evidence` 硬检查。它聚合以下证据：

- coverage gate：`pyproject.toml` 与 CI 均为 80%。
- offline eval：`evals/reports/latest.json` `status=PASS`。
- Agent Eval：`evals/reports/agent-latest.json` `status=PASS`。
- baseline compare：`evals/reports/baseline-compare-latest.json` `status=PASS`。
- injection strict：`latest.json` 的 `injection.status=PASS` 且 `gateMode=hard`。
- security corpus：`evals/reports/security-latest.json` `status=PASS`。
- Workspace Core：`docs/evidence/workspace-v2.6.5.json` `status=PASS`。
- Skill System：`docs/evidence/skills-v2.6.5.json` `status=PASS`。
- Skill Workbench UI：`docs/evidence/skills-ui-v2.6.5.json` `status=PASS`。
- Skill Builder：`docs/evidence/skill-builder-v2.6.5.json` `status=PASS`。
- Skill Packs：`docs/evidence/skill-packs-v2.6.5.json` `status=PASS`。
- Skill Eval Dashboard: `docs/evidence/skill-eval-dashboard-v2.6.5.json` and `evals/reports/skills-v2.6.5.json` `status=PASS`.

刷新命令：

```bash
python scripts/update_eval_report.py
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.6.5.json
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.6.5.json
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.6.5.json
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.6.5.json
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.6.5.json
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.6.5.json --report-out evals/reports/skills-v2.6.5.json
python scripts/preflight_release.py --version 2.6.5
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
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.6.5.json

# 7. 刷新 Skill System evidence（离线）
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.6.5.json

# 8. 刷新 Skill Workbench UI evidence（离线）
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.6.5.json

# 9. 刷新 Skill Builder evidence（离线）
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.6.5.json

# 10. 刷新 Skill Packs evidence（离线）
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.6.5.json

# 11. Refresh Skill Eval Dashboard evidence (offline)
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.6.5.json --report-out evals/reports/skills-v2.6.5.json

# 12. 版本一致性与质量证据体检
python scripts/preflight_release.py --version 2.6.5

# 13. 运行时体检
python scripts/doctor.py --offline

# 14. 一键 smoke（离线）
python scripts/smoke_release.py --offline

# 15. 打包并生成 manifest + checksum + qualityGates
python scripts/release.py --clean-workspace --version 2.6.5
```

也可以直接用 `python scripts/smoke_release.py --offline` 刷新离线质量证据；本地模型和第三方生态 evidence 需要在具备对应环境时单独补齐。
