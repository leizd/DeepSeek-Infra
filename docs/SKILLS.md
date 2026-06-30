# Skill System

Applicable version: v2.6.5.

DeepSeek Infra v2.6.5 defines a Skill as:

```text
Skill = Prompt + Tools + Input Schema + Output Schema + Memory Policy + Artifact Policy + Project Binding
```

A Skill is not just a prompt template. It has an explicit tool grant, validates input and output, can bind to a project, and can persist outputs into the local workspace.

## Layout

```text
deepseek_infra/infra/skills/
  schema.py        # Skill config and input/output schema validation
  pack.py          # Skill Pack schema, validation, and tool-permission diff
  registry.py      # built-in + custom Skill and Skill Pack registry
  permissions.py   # Skill allowedTools -> ToolPolicy
  runner.py        # Skill execution and project/artifact persistence
  eval.py          # offline Skill / Pack scoring and regression reports
  templates.py     # prompt and offline output helpers
  evidence.py      # Skill artifact index and release evidence

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

User-created Skills are stored in runtime state under `.skills/custom/` and user-created Skill Packs under `.skills/packs/`; neither must be committed.

## Registry

The registry supports listing built-in and custom Skills, creating and editing custom Skills, disabling or deleting Skills, importing and exporting Skill JSON, and validating policies.

HTTP entrypoint:

```text
POST /api/skills
POST /api/skills/{skill_id}/run
```

Common actions: `list`, `builtin`, `get`, `create`, `update`, `disable`, `enable`, `delete`, `import`, `export`, `validate`, `dry_run`, `run`, `list_packs`, `get_pack`, `export_pack`, `import_pack`, `validate_pack`, `delete_pack`, `eval_report`, `list_eval_cases`, `create_eval_case`, and `delete_eval_case`.

## Runner

The Skill Runner flow is:

```text
select Skill
  -> validate inputSchema
  -> load project context when projectBinding.enabled
  -> inject systemPrompt + Skill contract
  -> pass allowedTools into the existing Tool Policy path
  -> run LLM/tool loop or offline smoke path
  -> validate outputSchema
  -> save Skill output, artifacts, project history, and evidence metadata
```

The runner never bypasses Tool Policy. `allowedTools` narrows the tools included in the DeepSeek payload and also narrows the `ToolPolicy` grant used by execution.

## Project Binding

Project Skill state is stored in `.projects/<projectId>/project.json`:

```json
{
  "skills": {
    "enabledPacks": ["pack_study"],
    "enabledSkills": ["skill_study_tutor"],
    "defaultSkill": "skill_study_tutor",
    "recentSkills": ["skill_research_brief"]
  },
  "skillRuns": [],
  "savedItems": [],
  "artifacts": []
}
```

Project export includes Skill bindings, Skill run history, saved Skill outputs, and Skill artifact metadata. `enabledPacks` records which Skill Packs a project has installed; installing a Pack enables its referenced Skills through `POST /api/workspace/projects/{projectId}/skill-packs/{packId}/install`.

## Skill Workbench UI

v2.6.5 adds a local Skill Workbench in the main Web UI:

- Open the `Skills` entry in the sidebar to browse built-in and custom Skills.
- Use the workbench toolbar to search, import Skill JSON, export custom Skills, and enable or disable custom Skills.
- Select `Run` on a Skill to open the Skill Run Panel. The panel maps `inputSchema.properties` into form controls, marks required fields, and submits `projectId`, `offline`, and `persist` parameters through the Skill Web API.
- Open a project to manage `enabledSkills`, `defaultSkill`, and `recentSkills`. Skill runs submitted with a project id update project history and preserve Skill-produced saved items and artifacts.
- After a run, the result preview displays output content, `skillRunId`, linked Saved Items, and linked Artifacts so the output is managed as Workspace data instead of only chat text.

Frontend integration files:

```text
static/index.html
static/modules/skills.js
static/modules/chat.js
static/styles.css
```

## Custom Skill Builder

v2.6.5 adds a Custom Skill Builder inside the Skill Workbench so users can author Skills without hand-writing JSON:

- `New Skill` opens a guided builder for `skillId`, `name`, `description`, `version`, `systemPrompt`, policies, schema fields, and tools.
- `Clone` on a built-in Skill creates a custom editable copy, preserving the source prompt, schemas, tool grants, memory policy, artifact policy, and project binding.
- The visual schema editor supports `string`, `textarea`, `number`, `integer`, `enum`, and `boolean` fields. Each field can set key, title, description, required state, default, enum options, and max length.
- The Tool Permission Picker exposes known local tools with risk labels such as `safe`, `read-only`, `filesystem`, `network`, and `requires approval`. Saving still runs through backend schema validation, and execution still narrows tools through Tool Policy.
- `Preview JSON` shows the final Skill config, `Validate Schema` calls `POST /api/skills` with `action=validate`, and `Dry Run Offline` calls `action=dry_run` with generated sample input before the Skill is saved.
- `Save Skill` creates or updates a custom Skill; `Save & Run` saves and immediately opens the existing run form.
- Evidence screenshots are tracked at `docs/assets/skill-builder.png` and `docs/assets/skill-builder-dry-run.png`.

The authoring API actions are intentionally local-only and do not download third-party Skills:

```json
{ "action": "validate", "skill": { "...": "..." } }
{ "action": "dry_run", "skill": { "...": "..." }, "input": { "...": "..." } }
```

## Skill Packs

v2.6.5 introduces local Skill Packs so a set of Skills can be imported, exported, installed, and bound to projects together. A Skill Pack is a `.skillpack.json` manifest:

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

Each `skills` entry is either a **reference** (only `skillId`, resolved against existing built-in / custom Skills) or an **embedded** full Skill config. Built-in template packs use references; exported packs embed full configs so they stay self-contained.

Built-in Template Library (shipped under `skills/packs/`):

- **Study Pack** — study_tutor, paper_writer, document_reader
- **Research Pack** — research_brief, document_reader, paper_writer
- **Code Pack** — code_review, document_reader
- **Office Pack** — ppt_generator, paper_writer, document_reader

Pack actions on `POST /api/skills`:

```json
{ "action": "list_packs" }
{ "action": "get_pack", "packId": "pack_study" }
{ "action": "export_pack", "packId": "pack_study" }
{ "action": "validate_pack", "pack": { "...": "..." } }
{ "action": "import_pack", "pack": { "...": "..." }, "onConflict": "error" }
{ "action": "delete_pack", "packId": "pack_custom" }
```

Install a Pack onto a project (enables the Pack's Skills and records `enabledPacks`):

```text
POST /api/workspace/projects/{projectId}/skill-packs/{packId}/install
```

### Pack import safety

Importing a Pack never silently overwrites existing Skills. The `onConflict` strategy must be one of:

- `error` (default) — raise when an embedded `skillId` already exists.
- `overwrite` — re-install embedded Skills with the same `skillId`.
- `skip` — leave existing Skills untouched and report them as skipped.

The import summary returns an `allowedTools` permission diff with risk labels (`read-only`, `filesystem`, `network`, `sensitive`, `requires approval`, or the raw risk level) and flags high-risk / requires-approval tools so reviewers can confirm them before running. Skill Packs are **local-only**: there is no remote Skill Marketplace, and the authoring API never downloads third-party Skills.

## Skill Eval Dashboard

v2.6.5 adds a local Skill quality loop. The Workbench `Eval` tab runs offline Skill / Pack evals, shows pass/fail status, average score, case counts, failed cases, and latest run metadata, and exports JSON / Markdown summaries. The Eval Case Builder creates local rule-based cases without hand-editing JSONL.

Eval cases can be defined in `evals/golden/skills/skill_eval_cases.jsonl` or created from the Workbench. A case can include:

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

Scoring is rule-based and offline by default:

- `schemaPass`: input / output schema validation succeeds.
- `toolPolicyPass`: required tools are allowed and denied tools stay blocked by Tool Policy.
- `artifactPass`: generated artifacts match the Skill artifact policy and expected artifact types.
- `projectBindingPass`: project-bound runs write Skill run history and exported metadata.
- `contentPass`: expected keywords, forbidden regex, and required JSON paths match.
- `latencyMs`: elapsed runtime is recorded for report comparison.

The eval runner supports all Skills, one Skill, one Pack, and baseline comparison:

```bash
python evals/runners/run_skill_eval.py --strict --out evals/reports/skills-v2.6.5.json
python evals/runners/run_skill_eval.py --scope skill --skill-id skill_study_tutor --out evals/reports/skills-v2.6.5.json
python evals/runners/run_skill_eval.py --scope pack --pack-id pack_study --out evals/reports/skills-v2.6.5.json
python evals/runners/run_skill_eval.py --baseline evals/reports/skills-v2.6.4.json --out evals/reports/skills-v2.6.5.json
```

Workbench API actions:

```json
{ "action": "eval_report", "scope": "all" }
{ "action": "create_eval_case", "case": { "...": "..." } }
{ "action": "list_eval_cases" }
{ "action": "delete_eval_case", "caseId": "case_id" }
```

## Evidence

Run the local offline checks:

```bash
python scripts/smoke_skills.py --offline
python scripts/smoke_skills_ui.py --offline
python scripts/smoke_skill_builder.py --offline
python scripts/smoke_skill_packs.py --offline
python scripts/smoke_skill_eval_dashboard.py --offline
python evals/runners/run_skill_eval.py --strict
```

The release evidence file is `docs/evidence/skills-v2.6.5.json`.
The Skill Workbench UI evidence file is `docs/evidence/skills-ui-v2.6.5.json`.
The Custom Skill Builder evidence file is `docs/evidence/skill-builder-v2.6.5.json`.
The Skill Packs evidence file is `docs/evidence/skill-packs-v2.6.5.json`.
The Skill Eval Dashboard evidence file is `docs/evidence/skill-eval-dashboard-v2.6.5.json`.
The Skill eval report is `evals/reports/skills-v2.6.5.json`.

Required checks: `skillApiRoutes`, `builtinSkillsLoad`, `customSkillCreate`, `inputSchemaValidation`, `toolPermissionGate`, `artifactPolicy`, `projectBinding`, and `skillExport`.
