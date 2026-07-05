# Automation Runtime

Applicable version: v2.9.1.

Automation Runtime is the local workflow layer that connects Workspace Core, Skills, Browser Control, Media and export artifacts. It is intentionally small and policy-first: definitions are local JSON records, runs are auditable, and every release gate can be reproduced offline.

## Runtime Model

Automation definitions live under the runtime root in `.automation/automations.json`.

Each definition has:

- `automationId`, `projectId`, `name`, `enabled`
- `trigger`: `manual`, `schedule`, `interval` or `event`
- `condition`: `always`, `project_changed`, `media_ready`, `new_saved_items`, `artifact_created` or `url_changed`
- `action`: one governed action payload
- `output`: project save / artifact creation defaults
- `policy`: daily run limit, browser mode, network permission, confirmation and retry settings

Run history is stored in `.automation/history.json` and linked to Observability traces through `traceId` and `runId`.

## Actions

Supported action types:

- `run_skill`: run a built-in or custom Skill with explicit input and offline defaults.
- `browser_snapshot`: read-only browser snapshot through Browser Safety.
- `browser_check`: read-only URL or fixture change check with stored content hash.
- `project_summary`: create a markdown summary for a project.
- `media_process`: run a media-oriented Skill.
- `create_artifact`: register generated text or markdown as a project artifact.
- `save_item`: save content into Workspace Saved Items.
- `export_conversation`: export a conversation artifact.
- `export_project`: export a project bundle.

Browser actions are denied unless both the global runtime and the automation policy allow them. Private hosts are still blocked by Browser Safety unless the browser runtime explicitly allows them.

`browser_check.fixturePath` is intentionally bounded. Relative paths resolve under `.automation/fixtures`, and absolute paths must stay inside the runtime root, `.automation`, or the repository's automation test fixture tree. Out-of-range fixture reads are rejected before file content is loaded.

Schedule triggers use five cron fields. Automation Runtime supports `*`, numeric values, comma lists, ranges such as `9-17`, steps such as `*/5`, and stepped ranges such as `9-17/2`.

Manual runs, `run_due`, and trigger simulation can receive an optional `now` timestamp so schedule checks, daily run limits and run history use the same deterministic clock.

Retry policy supports `retry.maxAttempts` and `retry.backoffSeconds`. Run evidence records timeout checks, backoff settings and per-attempt failure details.

## API

Automation is exposed through the authenticated HTTP API:

```text
GET    /api/automation
POST   /api/automation
GET    /api/automation/templates
POST   /api/automation/templates/{template_id}
GET    /api/automation/{automation_id}
PATCH  /api/automation/{automation_id}
DELETE /api/automation/{automation_id}
POST   /api/automation/{automation_id}/run
GET    /api/automation/{automation_id}/runs
```

`POST /api/automation` also supports action-style payloads such as `list`, `create`, `run`, `rerun`, `run_due`, `simulate` and `create_from_template`.

## Configuration

Environment variables:

```text
AUTOMATION_ENABLED=1
AUTOMATION_MAX_RUNS_PER_DAY=50
AUTOMATION_MIN_INTERVAL_SECONDS=300
AUTOMATION_ALLOW_BROWSER=0
AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE=1
AUTOMATION_RUN_TIMEOUT_SECONDS=1800
```

Runtime data in `.automation` is gitignored and excluded from release archives.

## Evidence

Offline smoke:

```bash
python scripts/smoke_automation.py --offline --out docs/evidence/automation-v2.9.1.json --version 2.9.1
```

Strict eval:

```bash
python evals/runners/run_automation_eval.py --strict --out evals/reports/automation-v2.9.1.json --version 2.9.1
```

Release preflight has required Automation Runtime evidence since v2.9.0. For v2.9.1, `docs/evidence/automation-v2.9.1.json` must also prove browser check change detection, fixture path blocking, cron step/range matching, daily run limits, retry backoff, timeout evidence, rerun and template creation.
