# Skill 系统

适用版本：v2.7.2。
Applicable version: v2.7.2.

DeepSeek Infra v2.7.2 对 Skill 的定义如下：

```text
Skill = Prompt + Tools + Input Schema + Output Schema + Memory Policy + Artifact Policy + Project Binding
```

Skill 不仅仅是一个 prompt 模板。它具备显式的 tool grant，会校验输入和输出，可以绑定到项目（project binding），并可将输出持久化到本地 workspace。

## 目录结构

```text
deepseek_infra/infra/skills/
  schema.py        # Skill 配置及输入/输出 schema 校验
  pack.py          # Skill Pack schema、校验及 tool-permission diff
  registry.py      # 内置 + 自定义 Skill 和 Skill Pack registry
  permissions.py   # Skill allowedTools → ToolPolicy
  runner.py        # Skill 执行及 project/artifact 持久化
  analytics.py     # Skill 运行历史、使用统计、诊断、保留策略
  security.py      # Skill / Pack 安全审查、trust store 及签名预备
  catalog.py       # 本地 Skill Catalog、安装预检、项目安装/卸载
  eval.py          # 离线 Skill / Pack 评分与回退报告
  versioning.py    # Skill / Pack 修订历史、diff、迁移、回滚
  templates.py     # prompt 和离线输出辅助工具
  evidence.py      # Skill artifact 索引和发布 evidence

skills/builtin/
  document_reader.json
  research_brief.json
  paper_writer.json
  ppt_generator.json
  code_review.json
  study_tutor.json

skills/packs/
  study.json       # Study Pack
  research.json    # Research Pack
  code.json        # Code Pack
  office.json      # Office Pack
```

用户创建的 Skill 存放在 `.skills/custom/` 运行态目录中，用户创建的 Skill Pack 存放在 `.skills/packs/` 下；两者均不应提交到版本控制。

## Registry

Registry 支持列出内置和自定义 Skill、创建和编辑自定义 Skill、禁用或删除 Skill、导入和导出 Skill JSON、以及校验策略。

HTTP 入口：

```text
POST /api/skills
POST /api/skills/{skill_id}/run
```

常用 action：`list`、`builtin`、`get`、`create`、`update`、`disable`、`enable`、`delete`、`import`、`export`、`validate`、`dry_run`、`run`、`list_packs`、`get_pack`、`export_pack`、`import_pack`、`validate_pack`、`delete_pack`、`eval_report`、`list_eval_cases`、`create_eval_case`、`delete_eval_case`、`list_runs`、`get_run`、`delete_run`、`analytics_summary`、`cleanup_runs`、`redact_run`、`export_runs`、`security_review`、`security_review_pack`、`trust_skill`、`untrust_skill`、`block_skill`、`security_summary`、`catalog_list`、`catalog_get`、`catalog_search`、`catalog_install`、`catalog_uninstall`、`catalog_refresh`、`catalog_export`、`list_versions`、`diff_versions`、`rollback_skill`、`migration_plan`、`list_pack_versions`、`diff_pack_versions`、`upgrade_pack`、`rollback_pack` 和 `eval_upgrade_gate`。

## Runner

Skill Runner 的执行流程如下：

```text
select Skill
  -> validate inputSchema
  -> load project context when projectBinding.enabled
  -> inject systemPrompt + Skill contract
  -> pass allowedTools into the existing Tool Policy path
  -> run LLM/tool loop or offline smoke path
  -> validate outputSchema
  -> save Skill output, artifacts, project history, run analytics, and evidence metadata
```

Runner 永远不会绕过 Tool Policy。`allowedTools` 会限定 DeepSeek payload 中包含的工具，同时也会限定执行时使用的 `ToolPolicy` 授权。

## Project Binding

项目的 Skill 状态保存在 `.projects/<projectId>/project.json` 中：

```json
{
  "skills": {
    "enabledPacks": ["pack_study"],
    "enabledPackVersions": [
      {"packId": "pack_study", "version": "1.0.0", "installedAt": "2026-06-30T00:00:00Z"}
    ],
    "enabledSkills": ["skill_study_tutor"],
    "defaultSkill": "skill_study_tutor",
    "recentSkills": ["skill_research_brief"]
  },
  "skillRuns": [],
  "savedItems": [],
  "artifacts": []
}
```

项目导出包含 Skill binding、Skill run 历史、保存的 Skill 输出、以及 Skill artifact 元数据。`enabledPacks` 保留向后兼容的 Pack id 列表，而 `enabledPackVersions` 记录了 `packId`、`version` 和 `installedAt`；安装 Pack 会通过 `POST /api/workspace/projects/{projectId}/skill-packs/{packId}/install` 启用其引用的 Skill。

## Skill Workbench UI

v2.7.2 在主 Web UI 中新增了一个本地 Skill Workbench：

- 打开侧边栏中的 `Skills` 入口，浏览内置和自定义 Skill。
- 使用 Workbench 工具栏进行搜索、导入 Skill JSON、导出自定义 Skill、以及启用或禁用自定义 Skill。
- 在 Skill 上选择 `Run` 打开 Skill Run Panel。该面板将 `inputSchema.properties` 映射为表单控件，标记必填字段，并通过 Skill Web API 提交 `projectId`、`offline` 和 `persist` 参数。
- 打开项目即可管理 `enabledSkills`、`defaultSkill` 和 `recentSkills`。附带项目 id 的 Skill run 会更新项目历史并保留 Skill 生成的 saved items 和 artifacts。
- 运行完成后，结果预览会展示输出内容、`skillRunId`、关联的 Saved Items 和关联的 Artifacts，使输出作为 Workspace 数据（而非仅聊天文本）被管理。

前端集成文件：

```text
static/index.html
static/modules/skills.js
static/modules/chat.js
static/styles.css
```

## Custom Skill Builder / 自定义 Skill Builder

v2.7.2 在 Skill Workbench 中新增了自定义 Skill Builder，让用户无需手动编写 JSON 即可创作 Skill：

- `New Skill` 打开引导式构建器，配置 `skillId`、`name`、`description`、`version`、`systemPrompt`、策略、schema 字段和 tools。
- 对内置 Skill 使用 `Clone` 可创建一个自定义可编辑副本，保留源 prompt、schema、tool grant、memory policy、artifact policy 和 project binding。
- 可视化 schema 编辑器支持 `string`、`textarea`、`number`、`integer`、`enum` 和 `boolean` 字段。每个字段可设置 key、title、description、required 状态、default 值、enum 选项和 max length。
- Tool Permission Picker 展示已知的本地工具，并附带 `safe`、`read-only`、`filesystem`、`network`、`requires approval` 等风险标签。保存仍需通过后端 schema 校验，执行时仍通过 Tool Policy 限定工具。
- `Preview JSON` 显示最终 Skill 配置，`Validate Schema` 调用 `POST /api/skills`（action=validate），`Dry Run Offline` 在保存前用生成的示例输入调用 `action=dry_run`。
- `Save Skill` 创建或更新自定义 Skill；`Save & Run` 保存后立即打开现有运行表单。
- Evidence 截图跟踪路径为 `docs/assets/skill-builder.png` 和 `docs/assets/skill-builder-dry-run.png`。

创作类 API action 有意设计为纯本地操作，不会下载第三方 Skill：

```json
{ "action": "validate", "skill": { "...": "..." } }
{ "action": "dry_run", "skill": { "...": "..." }, "input": { "...": "..." } }
```

## Skill Packs

v2.7.2 引入了本地 Skill Pack，使一组 Skill 可以一起导入、导出、安装和绑定到项目。Skill Pack 是一个 `.skillpack.json` manifest：

```json
{
  "packId": "pack_study",
  "name": "Study Pack",
  "description": "Skills for study, writing and reading.",
  "version": "1.0.0",
  "author": "builtin",
  "skills": [
    {"skillId": "skill_study_tutor"},
    {"skillId": "skill_paper_writer", "name": "...", "...full Skill config": "..."}
  ]
}
```

每个 `skills` 条目要么是 **reference**（仅 `skillId`，在现有内置/自定义 Skill 中解析），要么是 **embedded** 完整 Skill 配置。内置模板 Pack 使用引用方式；导出 Pack 则内嵌完整配置以保持自包含。

内置模板库（随 `skills/packs/` 发布）：

- **Study Pack** — study_tutor、paper_writer、document_reader
- **Research Pack** — research_brief、document_reader、paper_writer
- **Code Pack** — code_review、document_reader
- **Office Pack** — ppt_generator、paper_writer、document_reader

`POST /api/skills` 上的 Pack action：

```json
{ "action": "list_packs" }
{ "action": "get_pack", "packId": "pack_study" }
{ "action": "export_pack", "packId": "pack_study" }
{ "action": "validate_pack", "pack": { "...": "..." } }
{ "action": "import_pack", "pack": { "...": "..." }, "onConflict": "error" }
{ "action": "delete_pack", "packId": "pack_custom" }
```

将 Pack 安装到项目（启用该 Pack 的 Skill 并记录 `enabledPacks`）：

```text
POST /api/workspace/projects/{projectId}/skill-packs/{packId}/install
```

### Pack 导入安全机制

导入 Pack 绝不会静默覆盖已有的 Skill。`onConflict` 策略必须为以下之一：

- `error`（默认）— 当 embedded 的 `skillId` 已存在时抛出错误。
- `overwrite` — 重新安装具有相同 `skillId` 的 embedded Skill。
- `skip` — 保留已有 Skill 不变，将其报告为已跳过。

导入摘要返回带有风险标签（`read-only`、`filesystem`、`network`、`sensitive`、`requires approval` 或原始风险等级）的 `allowedTools` 权限差异，并标记高风险/需要审批的工具，以便审核者可在运行前确认。Skill Pack 是**纯本地**的：不存在远程 Skill Marketplace，创作 API 永远不会下载第三方 Skill。

## Skill Eval Dashboard

v2.7.2 新增了一个本地 Skill 质量闭环。Workbench 的 `Eval` 标签页运行离线 Skill / Pack eval，显示通过/失败状态、平均分、用例数量、失败用例、最近运行元数据，并可导出 JSON / Markdown 摘要。Eval Case Builder 可创建基于规则的本地用例，无需手动编辑 JSONL。

Eval 用例可定义在 `evals/golden/skills/skill_eval_cases.jsonl` 中，或从 Workbench 创建。一个用例可包含：

```json
{
  "caseId": "study-os-scheduling",
  "skillId": "skill_study_tutor",
  "packId": "pack_study",
  "input": {"topic": "OS process scheduling"},
  "expectedKeywords": ["FCFS", "SJF", "RR"],
  "requiredOutputPaths": ["content"],
  "forbidden": ["ignore previous instructions"],
  "expectedArtifactTypes": ["md"],
  "projectBindingRequired": true
}
```

评分默认基于规则且离线执行：

- `schemaPass`：输入/输出 schema 校验通过。
- `toolPolicyPass`：所需工具被允许，被拒绝的工具依然被 Tool Policy 阻止。
- `artifactPass`：生成的 artifact 符合 Skill artifact policy 和预期的 artifact 类型。
- `projectBindingPass`：绑定项目的运行会写入 Skill run 历史和导出元数据。
- `contentPass`：期望关键词、禁止正则表达式和必需的 JSON 路径均匹配。
- `latencyMs`：记录运行耗时，用于报告对比。

Eval runner 支持评估所有 Skill、单个 Skill、单个 Pack 以及基线对比：

```bash
python evals/runners/run_skill_eval.py --strict --out evals/reports/skills-v2.7.2.json
python evals/runners/run_skill_eval.py --scope skill --skill-id skill_study_tutor --out evals/reports/skills-v2.7.2.json
python evals/runners/run_skill_eval.py --scope pack --pack-id pack_study --out evals/reports/skills-v2.7.2.json
python evals/runners/run_skill_eval.py --baseline evals/reports/skills-v2.6.4.json --out evals/reports/skills-v2.7.2.json
```

Workbench API action：

```json
{ "action": "eval_report", "scope": "all" }
{ "action": "create_eval_case", "case": { "...": "..." } }
{ "action": "list_eval_cases" }
{ "action": "delete_eval_case", "caseId": "case_id" }
```

## Skill Versioning & Migration / Skill 版本管理与迁移

v2.6.6 新增了自定义 Skill 和自定义 Skill Pack 的本地生命周期管理。Builder 保存、自定义 Skill 创建、Pack 导入、回滚检查点和 Pack 升级都会在 `.skills/history/` 下创建修订快照：

```text
.skills/history/<skillId>/<version>-<revisionId>.json
.skills/history/packs/<packId>/<version>-<revisionId>.json
```

每个 Skill 修订记录 `version`、`revisionId`、`createdAt`、`changeSummary`、`schemaHash`、`promptHash` 和 `toolGrantHash`。Pack 修订记录 `packHash`、`skillIdsHash` 和 `toolGrantHash`。Workbench 的 `Versions` 面板可以列出修订历史、将当前 Skill 与选定修订对比、展示 schema 迁移计划、回滚自定义 Skill，以及运行包含 eval 感知的 Pack 升级检查。

版本管理 API action：

```json
{ "action": "list_versions", "skillId": "skill_custom" }
{ "action": "diff_versions", "skillId": "skill_custom", "from": "1.0.0", "to": "current" }
{ "action": "migration_plan", "skillId": "skill_custom", "from": "1.0.0", "to": "current" }
{ "action": "rollback_skill", "skillId": "skill_custom", "version": "1.0.0" }
{ "action": "list_pack_versions", "packId": "pack_custom" }
{ "action": "diff_pack_versions", "packId": "pack_custom", "from": "1.0.0", "to": "current" }
{ "action": "upgrade_pack", "packId": "pack_custom", "version": "1.1.0", "projectId": "proj_..." }
{ "action": "rollback_pack", "packId": "pack_custom", "version": "1.0.0" }
{ "action": "eval_upgrade_gate", "kind": "pack", "itemId": "pack_custom" }
```

迁移计划基于规则且离线执行。它会标记已移除的字段、无默认值的新增必填字段、类型变更、可能的字段重命名，以及引用该 Skill 的 project binding / eval 用例 / 已保存元数据条目的数量。Eval 感知的升级复用 Skill Eval 报告路径，使 Pack 变更可在安装前展示评分、通过率、回退数量和 `low` 或 `review` 建议。

## Skill Run Analytics

v2.7.2 新增了 Skill run 的本地运行历史和使用统计。Runner 将已完成和失败的运行记录在 `.skills/runs/runs.jsonl` 中，仅包含元数据：`skillRunId`、`skillId`、`skillVersion`、`packId`、`projectId`、状态、时间戳、延迟、离线/模型标志、输入/输出摘要、artifact 和 saved-item 数量、`traceId` 以及诊断字段。

Workbench 的 `Runs` 标签页展示：

- 按 Skill 过滤的运行历史。
- 成功/失败率、平均/P50/P90 延迟、热门 Skill/Pack、artifact 数量、saved item 数量和近期趋势。
- 针对 schema 校验、tool policy 拒绝、artifact policy、project binding、LLM/API、超时、取消和未知错误的故障诊断。
- 返回到 trace、项目运行历史、Saved Items 和 Artifacts 的链接。
- 本地留存控制：删除单次运行、清空失败运行、导出历史、以及在保留元数据的同时脱敏摘要。

Analytics API action：

```json
{ "action": "list_runs", "skillId": "skill_research_brief", "limit": 50 }
{ "action": "get_run", "skillRunId": "run_xxx" }
{ "action": "delete_run", "skillRunId": "run_xxx" }
{ "action": "analytics_summary", "scope": "all" }
{ "action": "analytics_summary", "scope": "skill", "skillId": "skill_research_brief" }
{ "action": "analytics_summary", "scope": "pack", "packId": "pack_study" }
{ "action": "cleanup_runs", "status": "failed" }
{ "action": "redact_run", "skillRunId": "run_xxx" }
{ "action": "export_runs" }
```

项目 analytics 端点：

```text
GET /api/workspace/projects/{projectId}/skill-analytics
```

## Skill Security Review

v2.7.2 新增了 Skill 和 Skill Pack 的本地安全审查和签名预备元数据。安全审查仍以本地优先：不存在远程 marketplace、远程签名服务器或基于账户的审批流程。

Workbench 的 `Security` 标签页展示：

- 信任级别：`trusted`、`local-custom`、`needs-review`、`high-risk` 或 `blocked`
- 风险评分和 allowedTools 风险标签
- prompt injection / secret exfiltration / secret file access / network exfiltration 发现
- filesystem、network、sensitive 和 requires-approval 能力
- 最近审查时间戳和 manifest 哈希

Security API action：

```json
{ "action": "security_review", "skillId": "skill_custom" }
{ "action": "security_review_pack", "packId": "pack_custom" }
{ "action": "trust_skill", "skillId": "skill_custom" }
{ "action": "untrust_skill", "skillId": "skill_custom" }
{ "action": "block_skill", "skillId": "skill_custom", "reason": "manual review" }
{ "action": "security_summary", "scope": "all" }
```

Security manifest 是仅含哈希的签名预备记录：

```json
{
  "skillId": "skill_research_brief",
  "version": "1.2.0",
  "contentHash": "sha256:...",
  "schemaHash": "sha256:...",
  "promptHash": "sha256:...",
  "toolGrantHash": "sha256:...",
  "reviewStatus": "trusted",
  "signed": false
}
```

运行行为：

- 内置 Skill 默认保持 trusted，但仍会暴露风险和 manifest 元数据。
- 自定义高风险 Skill 需要 `securityApproved=true` 才可执行，除非已被显式信任。
- 被 blocked 的 Skill 在输入校验、工具执行、artifact 创建或项目持久化之前即被拒绝。
- Skill run analytics 记录 `runSecurityLevel`、`securityReviewId`、`trustedAtRun`、`toolGrantHashAtRun`、`blockedReason` 和 `approvalRequired`。

## Local Skill Catalog

v2.7.2 新增本地 Skill Catalog / Marketplace-lite。Catalog 只索引本机已有的 Skill 和 Pack，不联网下载第三方内容，也不做远程签名服务器或用户账号体系。

Catalog item 记录：

- `itemId`、`kind`、`name`、`description`、`category`、`tags`、`author` 和 `version`
- `trustLevel`、`riskScore`、`signed`、`contentHash`、`schemaHash`、`promptHash` 和 `toolGrantHash`
- `evalScore`、`installCount`、`includedSkills`、`requiredTools`、`artifactTypes` 和 `toolPermissionSummary`

Catalog API action：

```json
{ "action": "catalog_list" }
{ "action": "catalog_get", "itemId": "pack_study" }
{ "action": "catalog_search", "query": "study", "filters": { "trusted": true } }
{ "action": "catalog_install", "itemId": "pack_study", "projectId": "proj_xxx", "dryRun": true }
{ "action": "catalog_install", "itemId": "pack_study", "projectId": "proj_xxx" }
{ "action": "catalog_uninstall", "itemId": "pack_study", "projectId": "proj_xxx" }
{ "action": "catalog_refresh" }
{ "action": "catalog_export" }
```

安装前预检会返回 included Skills、新增 enabledSkills、工具权限摘要、信任状态、风险分数、eval 分数和项目绑定变化。`high-risk` 且未传 `securityApproved=true` 的条目会被拒绝安装，`blocked` 条目始终禁止安装。

## Evidence

运行本地离线检查：

```bash
python scripts/smoke_skills.py --offline
python scripts/smoke_skills_ui.py --offline
python scripts/smoke_skill_builder.py --offline
python scripts/smoke_skill_packs.py --offline
python scripts/smoke_skill_eval_dashboard.py --offline
python scripts/smoke_skill_versioning.py --offline
python scripts/smoke_skill_analytics.py --offline
python scripts/smoke_skill_security.py --offline
python scripts/smoke_skill_catalog.py --offline
python evals/runners/run_skill_eval.py --strict
```

发布 evidence 文件为 `docs/evidence/skills-v2.7.2.json`。
Skill Workbench UI evidence 文件为 `docs/evidence/skills-ui-v2.7.2.json`。
自定义 Skill Builder evidence 文件为 `docs/evidence/skill-builder-v2.7.2.json`。
Skill Packs evidence 文件为 `docs/evidence/skill-packs-v2.7.2.json`。
Skill Eval Dashboard evidence 文件为 `docs/evidence/skill-eval-dashboard-v2.7.2.json`。
Skill Versioning evidence 文件为 `docs/evidence/skill-versioning-v2.7.2.json`。
Skill Analytics evidence 文件为 `docs/evidence/skill-analytics-v2.7.2.json`。
Skill Security evidence 文件为 `docs/evidence/skill-security-v2.7.2.json`。
Skill Catalog evidence 文件为 `docs/evidence/skill-catalog-v2.7.2.json`。
Skill eval 报告为 `evals/reports/skills-v2.7.2.json`。
Skill eval report is `evals/reports/skills-v2.7.2.json`.

需通过的检查项：`skillApiRoutes`、`builtinSkillsLoad`、`customSkillCreate`、`inputSchemaValidation`、`toolPermissionGate`、`artifactPolicy`、`projectBinding` 和 `skillExport`。
Versioning 检查项：`skillVersionSnapshot`、`skillDiff`、`schemaMigrationPlan`、`skillRollback`、`packVersionInstall`、`packRollback`、`evalAwareUpgradeGate` 和 `projectBindingMigration`。
Analytics 检查项：`skillRunHistory`、`runMetadataPersist`、`analyticsSummary`、`failureDiagnostics`、`projectRunHistory`、`traceLink`、`artifactLink`、`retentionCleanup` 和 `privacyRedaction`。
Security 检查项：`securityReview`、`promptInjectionScan`、`secretExfiltrationScan`、`toolGrantRiskDiff`、`trustSkill`、`blockSkill`、`tamperDetection`、`securityManifestExport` 和 `runSecurityMetadata`。
Catalog 检查项：`catalogManifest`、`catalogList`、`catalogSearch`、`catalogInstallPreview`、`catalogInstall`、`catalogUninstall`、`securityGateBeforeInstall`、`evalScoreShown`、`toolPermissionSummary` 和 `catalogExport`。
