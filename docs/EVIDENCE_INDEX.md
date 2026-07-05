# 发布证据索引

适用版本：v2.9.0。

本页汇总 DeepSeek Infra v2.3.x 以来的互操作证据、评测报告、v2.4 质量门禁证据、v2.5 Workspace Core 证据与 release artifact，作为证据链的统一入口。所有标 ✅ 的项都有可复现的 smoke / evidence 路径；标 🟡 的项需要人工 GUI、本地模型或真实第三方生态实测。

## 证据矩阵

| 证据 | 文件 | 状态 | 复现方式 |
| --- | --- | --- | --- |
| Browser Control smoke | [docs/evidence/browser-v2.9.0.json](evidence/browser-v2.9.0.json) / [BROWSER_CONTROL.md](BROWSER_CONTROL.md) | PASS offline browser runtime smoke | `python scripts/smoke_browser.py --offline --out docs/evidence/browser-v2.9.0.json --version 2.9.0` |
| Automation Runtime smoke | [docs/evidence/automation-v2.9.0.json](evidence/automation-v2.9.0.json) / [AUTOMATION.md](AUTOMATION.md) | PASS offline automation runtime smoke | `python scripts/smoke_automation.py --offline --out docs/evidence/automation-v2.9.0.json --version 2.9.0` |
| MCP official SDK interop | [docs/integrations/external-mcp-server.md](integrations/external-mcp-server.md) | ✅ 已测试 | `python scripts/smoke_mcp_compat.py --external-server-url <url>` |
| Headless MCP bridge | [docs/evidence/headless-mcp-bridge.json](evidence/headless-mcp-bridge.json) | ✅ 已测试 | `python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json` |
| A2A external peer | [docs/evidence/a2a-external-peer.json](evidence/a2a-external-peer.json) | ✅ 已测试 | `python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json` |
| A2A third-party peer | [docs/evidence/a2a-third-party-peer.json](evidence/a2a-third-party-peer.json) / [a2a-third-party-peer.md](evidence/a2a-third-party-peer.md) | ✅ 第三方证据已测试 | `python scripts/smoke_a2a_external_peer.py --peer-url <third-party-url> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md` |
| Edge Router stabilization | [docs/evidence/edge-router-v2.9.0.json](evidence/edge-router-v2.9.0.json) / [EDGE_ROUTER_RUNBOOK.md](EDGE_ROUTER_RUNBOOK.md) | PASS 离线 dry-run 路由证据 | `python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v2.9.0.json` |
| Context Taint Firewall smoke | [docs/evidence/context-taint-v2.9.0.json](evidence/context-taint-v2.9.0.json) / [SECURITY_SMOKE.md](SECURITY_SMOKE.md) | PASS 离线注入/污染冒烟测试 | `python scripts/smoke_context_taint.py --offline --out docs/evidence/context-taint-v2.9.0.json` |
| Edge Router smoke | [docs/evidence/edge-router-smoke.json](evidence/edge-router-smoke.json) / [edge-router-smoke.md](evidence/edge-router-smoke.md) | ✅ 可选实机冒烟证据 | `python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md` |
| Claude Desktop GUI | [docs/integrations/claude-desktop.md](integrations/claude-desktop.md) | ✅ GUI tested / GUI 实测 | Claude Desktop 0.9.0, commit `54228c4`, Windows 11, 2026-06-28 |
| Cursor GUI | [docs/integrations/cursor.md](integrations/cursor.md) | ✅ GUI tested / GUI 实测 | Cursor 0.48.0, commit `54228c4`, Windows 11, 2026-06-28 |
| Continue.dev MCP | [docs/evidence/continue-dev-mcp.json](evidence/continue-dev-mcp.json) / [continue-dev-mcp.md](evidence/continue-dev-mcp.md) | ✅ 已测试 | Continue.dev 1.2.0, commit `2e2782e`, Windows 11, 2026-06-28 |
| OpenAI-compatible SDK smoke | [docs/evidence/openai-compatible-sdks.json](evidence/openai-compatible-sdks.json) / [openai-compatible-sdks.md](evidence/openai-compatible-sdks.md) | ✅ SDK 冒烟测试 | `python scripts/smoke_openai_compatible_sdks.py --base-url http://127.0.0.1:8000/v1 --model deepseek-v4-pro --out docs/evidence/openai-compatible-sdks.json --markdown docs/evidence/openai-compatible-sdks.md` |
| Workspace Core smoke | [docs/evidence/workspace-v2.9.0.json](evidence/workspace-v2.9.0.json) / [WORKSPACE.md](WORKSPACE.md) | ✅ 离线冒烟测试 | `python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.9.0.json` |
| Media Layer smoke | [docs/evidence/media-v2.9.0.json](evidence/media-v2.9.0.json) / [MEDIA.md](MEDIA.md) | PASS 离线多模态媒体冒烟测试 | `python scripts/smoke_media.py --offline --out docs/evidence/media-v2.9.0.json` |
| Skill System smoke | [docs/evidence/skills-v2.9.0.json](evidence/skills-v2.9.0.json) / [SKILLS.md](SKILLS.md) | ✅ 离线冒烟测试 | `python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.9.0.json` |
| Skill Workbench UI smoke | [docs/evidence/skills-ui-v2.9.0.json](evidence/skills-ui-v2.9.0.json) / [SKILLS.md](SKILLS.md) | ✅ 离线 UI 冒烟测试 | `python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.9.0.json` |
| Skill Builder smoke | [docs/evidence/skill-builder-v2.9.0.json](evidence/skill-builder-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线创作冒烟测试 | `python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.9.0.json` |
| Skill Packs smoke | [docs/evidence/skill-packs-v2.9.0.json](evidence/skill-packs-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线 Skill Pack 冒烟测试 | `python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.9.0.json` |
| Skill Eval Dashboard smoke | [docs/evidence/skill-eval-dashboard-v2.9.0.json](evidence/skill-eval-dashboard-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线 Skill 质量冒烟测试 | `python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.9.0.json --report-out evals/reports/skills-v2.9.0.json` |
| Skill Versioning smoke | [docs/evidence/skill-versioning-v2.9.0.json](evidence/skill-versioning-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线生命周期冒烟测试 | `python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.9.0.json` |
| Skill Analytics smoke | [docs/evidence/skill-analytics-v2.9.0.json](evidence/skill-analytics-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线使用分析冒烟测试 | `python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.9.0.json` |
| Skill Security smoke | [docs/evidence/skill-security-v2.9.0.json](evidence/skill-security-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线安全审查冒烟测试 | `python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.9.0.json` |
| Skill Catalog smoke | [docs/evidence/skill-catalog-v2.9.0.json](evidence/skill-catalog-v2.9.0.json) / [SKILLS.md](SKILLS.md) | PASS 离线本地目录冒烟测试 | `python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.9.0.json` |
| Third-party A2A ecosystem candidates | [docs/integrations/a2a-third-party-plan.md](integrations/a2a-third-party-plan.md) | ✅ 证据路径已关闭 | 保留 LangGraph / CrewAI / Google A2A reference 等候选实现的复现流程与排障说明 |

## 评测报告

| 报告 | 文件 | 状态 |
| --- | --- | --- |
| Offline eval suite | [evals/reports/latest.json](../evals/reports/latest.json) / [latest.md](../evals/reports/latest.md) | PASS |
| Agent eval | [evals/reports/agent-latest.json](../evals/reports/agent-latest.json) / [agent-latest.md](../evals/reports/agent-latest.md) | PASS |
| Baseline compare | [evals/reports/baseline-compare-latest.json](../evals/reports/baseline-compare-latest.json) | PASS |
| Security corpus | [evals/reports/security-latest.json](../evals/reports/security-latest.json) / [security-latest.md](../evals/reports/security-latest.md) | PASS |
| Skill eval | [evals/reports/skills-v2.9.0.json](../evals/reports/skills-v2.9.0.json) | PASS |
| Media eval | [evals/reports/media-v2.9.0.json](../evals/reports/media-v2.9.0.json) | PASS |
| Browser eval | [evals/reports/browser-v2.9.0.json](../evals/reports/browser-v2.9.0.json) | PASS |
| Automation eval | [evals/reports/automation-v2.9.0.json](../evals/reports/automation-v2.9.0.json) | PASS |

## 质量门禁证据（v2.9.0）

| 门禁 | 证据 | 要求 |
| --- | --- | --- |
| Coverage | `pyproject.toml` + CI `pytest --cov --cov-fail-under=80` | >= 80% |
| Offline eval | `evals/reports/latest.json` | `status=PASS` |
| Agent Eval | `evals/reports/agent-latest.json` | `status=PASS` |
| Baseline compare | `evals/reports/baseline-compare-latest.json` | `status=PASS` |
| Injection strict | `latest.json.injection.status=PASS` + `gateMode=hard` | PASS |
| Security corpus | `evals/reports/security-latest.json` | `status=PASS` |
| Browser Control | `docs/evidence/browser-v2.9.0.json` + `evals/reports/browser-v2.9.0.json` | `status=PASS` and session / read / screenshot / links / safety / Media / RAG / audit checks PASS |
| Automation Runtime | `docs/evidence/automation-v2.9.0.json` + `evals/reports/automation-v2.9.0.json` | `status=PASS` and create / manual / schedule / event / skill / browser read-only / export / safety / history / trace / template checks PASS |
| Edge Router | `docs/evidence/edge-router-v2.9.0.json` | `status=PASS` 且 doctor / route-preview / routing / fallback checks 全 PASS |
| Workspace Core | `docs/evidence/workspace-v2.9.0.json` | `status=PASS` 且关键 checks 全 PASS |
| Context Taint | `docs/evidence/context-taint-v2.9.0.json` | `status=PASS` 且 web / file / media injection、tool directive、tainted-turn escalation、risk diagnostics checks 全 PASS |
| Media Layer | `docs/evidence/media-v2.9.0.json` + `evals/reports/media-v2.9.0.json` | `status=PASS` 且导入 / 分片 / RAG / citation / export checks 全 PASS |
| Skill System | `docs/evidence/skills-v2.9.0.json` | `status=PASS` 且关键 checks 全 PASS |
| Skill Workbench UI | `docs/evidence/skills-ui-v2.9.0.json` | `status=PASS` 且关键 checks 全 PASS |
| Skill Builder | `docs/evidence/skill-builder-v2.9.0.json` | `status=PASS` 且关键 checks 全 PASS |
| Skill Packs | `docs/evidence/skill-packs-v2.9.0.json` | `status=PASS` 且关键 checks 全 PASS |
| Skill Eval Dashboard | `docs/evidence/skill-eval-dashboard-v2.9.0.json` + `evals/reports/skills-v2.9.0.json` | `status=PASS` 且 Skill / Pack 评测 checks 全 PASS |
| Skill Versioning | `docs/evidence/skill-versioning-v2.9.0.json` | `status=PASS` 且 Skill / Pack 生命周期 checks 全 PASS |
| Skill Analytics | `docs/evidence/skill-analytics-v2.9.0.json` | `status=PASS` 且运行历史 / 诊断 / 隐私 checks 全 PASS |
| Skill Security | `docs/evidence/skill-security-v2.9.0.json` | `status=PASS` 且审查 / 信任 / manifest / 运行安全 checks 全 PASS |
| Skill Catalog | `docs/evidence/skill-catalog-v2.9.0.json` | `status=PASS` 且目录 / 搜索 / 安装预检 / 安全门禁 checks 全 PASS |
| Runtime doctor | `python scripts/doctor.py --offline` | exit 0 |
| Release preflight | `python scripts/preflight_release.py --version 2.9.0` | exit 0 |
| Smoke release | `python scripts/smoke_release.py --offline` | exit 0 |

## 发布产物

每次发布生成以下 evidence artifact：

| 产物 | 示例 | 用途 |
| --- | --- | --- |
| Release zip | `dist/deepseek-infra-2.9.0.zip` | 可分发源码包 |
| Checksum | `dist/deepseek-infra-2.9.0.zip.sha256` | 校验 zip 完整性 |
| Manifest | `dist/deepseek-infra-2.9.0.manifest.json` | 版本、commit、构建环境、evidence 清单与 `qualityGates` |

构建命令：

```bash
python scripts/release.py --clean-workspace --version 2.9.0
```

## 预检清单

发版前必须通过的 preflight 检查：

```bash
python scripts/preflight_release.py --version 2.9.0
```
关键检查项：

- `docs_encoding_sanity`：Dockerfile / workflows / scripts / `deepseek_infra/**/*.py` / README / CHANGELOG / docs 无 `???`、`锟斤拷`、`鈥`、`鏋`、`杩`、`\ufffd` 等乱码。
- `headless_mcp_bridge_evidence`：`docs/evidence/headless-mcp-bridge.json` 存在、版本匹配、关键步骤 PASS。
- `a2a_external_peer_evidence`：`docs/evidence/a2a-external-peer.json` 存在、版本匹配、关键 checks PASS。
- `a2a_third_party_peer_evidence`：缺失或版本陈旧时 WARNING；同版本 evidence 存在时必须 `peerType=third-party`、`status=PASS` 且关键 checks PASS。
- `edge_router_evidence`：必须存在 `docs/evidence/edge-router-v2.9.0.json`，版本匹配、`status=PASS`，且 doctor / status shape / route-preview API / fake provider / routing policy / fallback / forced-local 409 checks 全 PASS。
- `edge_router_smoke_evidence`：缺失或版本陈旧时 WARNING；同版本实机 evidence 存在时必须 `status=PASS` 且四类 checks 全 PASS。
- `continue_dev_mcp_evidence`：缺失或版本陈旧时 WARNING；同版本 evidence 存在时必须 `status=PASS` 且六类 checks 全 PASS。
- `openai_compatible_sdk_evidence`：缺失或版本陈旧时 WARNING；同版本 evidence 存在时必须 `status=PASS` 且 LangChain/LiteLLM/LlamaIndex 关键 checks 全 PASS。
- `workspace_core_evidence`：必须存在 `docs/evidence/workspace-v2.9.0.json`，版本匹配、`status=PASS`，且项目 / 保存项 / 产物 / 对话导出 / 项目 ZIP / 脱敏 checks 全 PASS。
- `media_layer_evidence`：必须存在 `docs/evidence/media-v2.9.0.json`，版本匹配、`status=PASS`，且 image / PDF / webpage import、segments、RAG indexing、citations、project export 与 secret redaction checks 全 PASS。
- `skill_system_evidence`：必须存在 `docs/evidence/skills-v2.9.0.json`，版本匹配、`status=PASS`，且 Skill API route / registry / runner / artifact / project binding checks 全 PASS。
- `skill_ui_evidence`：必须存在 `docs/evidence/skills-ui-v2.9.0.json`，版本匹配、`status=PASS`，且 Skill Workbench 入口、schema 表单、项目绑定、结果回链、样式、JS syntax 与 CI syntax gate 全 PASS。
- `skill_builder_evidence`：必须存在 `docs/evidence/skill-builder-v2.9.0.json`，`status=PASS`；验证 Builder 入口、克隆、可视化 schema 编辑、工具选择器、校验、离线空跑、保存/导出、截图、样式、JS syntax 与 CI syntax gate。
- `skill_packs_evidence`：必须存在 `docs/evidence/skill-packs-v2.9.0.json`，`status=PASS`；验证 pack schema 校验、内置模板 pack、导入/导出、skillId 冲突处理、工具权限差异、项目 pack 绑定、安装空跑、Packs UI 选项卡、JS syntax、CI syntax gate 与 pack 资产。
- `skill_eval_dashboard_evidence`：必须存在 `docs/evidence/skill-eval-dashboard-v2.9.0.json` 与 `evals/reports/skills-v2.9.0.json`，`status=PASS`；验证 Eval 选项卡、Eval Case Builder、API 操作、Skill / Pack 评分、回归对比、导出操作、截图、样式、JS syntax 与 CI gate。
- `skill_versioning_evidence`：必须存在 `docs/evidence/skill-versioning-v2.9.0.json`，`status=PASS`；验证 Skill 快照、diff、schema 迁移方案、回滚、Pack 版本安装/回滚、评测感知升级门禁、项目绑定迁移、UI 资产、JS syntax 与 CI gate。
- `skill_analytics_evidence`：必须存在 `docs/evidence/skill-analytics-v2.9.0.json`，`status=PASS`；验证运行历史、元数据持久化、使用摘要、故障诊断、项目历史、trace/产物链接、保留清理、隐私脱敏、Runs UI、JS syntax 与 CI gate。
- `skill_security_evidence`：必须存在 `docs/evidence/skill-security-v2.9.0.json`，`status=PASS`；验证 Skill / Pack 审查、prompt 注入与密钥泄露扫描、工具授权风险差异、信任/阻止控制、篡改检测、manifest 导出、运行安全元数据、Security UI、JS syntax 与 CI gate。
- `skill_catalog_evidence`：必须存在 `docs/evidence/skill-catalog-v2.9.0.json`，`status=PASS`；验证本地 catalog manifest、列表、搜索、安装预检、安装/卸载、安全门禁、eval 分数、工具权限摘要、Catalog UI、JS syntax 与 CI gate。
- `gui_interop_evidence`：Claude Desktop / Cursor 已在 v2.4.2 完成 GUI 实测并改为 PASS。
- `baseline_compare_report` / `security_corpus_report` / `quality_gate_evidence`：v2.4 质量门禁证据齐全且 PASS。

## 发版前复现

在干净的 CI / 本地环境依次复现：

```bash
python scripts/smoke_mcp_headless_bridge.py --out docs/evidence/headless-mcp-bridge.json
python scripts/smoke_a2a_external_peer.py --out docs/evidence/a2a-external-peer.json
python scripts/smoke_a2a_external_peer.py --peer-url <third-party-url> --peer-type third-party --out docs/evidence/a2a-third-party-peer.json --markdown docs/evidence/a2a-third-party-peer.md
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
python scripts/smoke_edge_router.py --offline --out docs/evidence/edge-router-v2.9.0.json
python scripts/smoke_workspace.py --offline --out docs/evidence/workspace-v2.9.0.json
python scripts/smoke_media.py --offline --out docs/evidence/media-v2.9.0.json
python scripts/smoke_browser.py --offline --out docs/evidence/browser-v2.9.0.json --version 2.9.0
python scripts/smoke_automation.py --offline --out docs/evidence/automation-v2.9.0.json --version 2.9.0
python scripts/smoke_skills.py --offline --out docs/evidence/skills-v2.9.0.json
python scripts/smoke_skills_ui.py --offline --out docs/evidence/skills-ui-v2.9.0.json
python scripts/smoke_skill_builder.py --offline --out docs/evidence/skill-builder-v2.9.0.json
python scripts/smoke_skill_packs.py --offline --out docs/evidence/skill-packs-v2.9.0.json
python scripts/smoke_skill_eval_dashboard.py --offline --out docs/evidence/skill-eval-dashboard-v2.9.0.json --report-out evals/reports/skills-v2.9.0.json
python scripts/smoke_skill_versioning.py --offline --out docs/evidence/skill-versioning-v2.9.0.json
python scripts/smoke_skill_analytics.py --offline --out docs/evidence/skill-analytics-v2.9.0.json
python scripts/smoke_skill_security.py --offline --out docs/evidence/skill-security-v2.9.0.json
python scripts/smoke_skill_catalog.py --offline --out docs/evidence/skill-catalog-v2.9.0.json
python evals/runners/run_media_eval.py --strict --out evals/reports/media-v2.9.0.json
python evals/runners/run_browser_eval.py --strict --out evals/reports/browser-v2.9.0.json --version 2.9.0
python evals/runners/run_automation_eval.py --strict --out evals/reports/automation-v2.9.0.json --version 2.9.0
python evals/runners/run_offline_eval_suite.py --include-agent --strict --out evals/reports/latest.json --markdown evals/reports/latest.md
python evals/runners/run_agent_eval.py --report-dir evals/reports --strict
python evals/runners/run_security_corpus.py --strict --out evals/reports/security-latest.json --markdown evals/reports/security-latest.md
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json --out evals/reports/baseline-compare-latest.json
python scripts/preflight_release.py --version 2.9.0
```
