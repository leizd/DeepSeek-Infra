# Exports

Applicable version: v4.0.0-rc.1.

Workspace exports turn local project objects into portable Markdown, HTML, JSON or ZIP bundles. Export builders live in `deepseek_infra/infra/workspace/exports.py`.

## Supported Kinds

- `project`: metadata, conversations, saved items, artifacts and media.
- `conversation`: a single conversation in Markdown, HTML, JSON or ZIP form.
- `saved_items`: selected saved items or the full saved-item list.
- `artifacts`: selected artifacts or the full artifact package.
- `evidence`: trace and eval evidence bundle.

## Redaction

Exports redact obvious API keys, bearer tokens, auth tokens, passwords and secret values in JSON, Markdown, text-like artifact files and project bundle metadata. Binary media is included as-is unless the exporter detects text-like content.

## Provenance

Each export record now includes `includes`, such as project ID, conversation IDs, saved item IDs, artifact IDs and media IDs. The Workspace Provenance Graph uses those links to show which objects were bundled by an export.

## API

```http
POST /api/workspace/exports
GET /api/workspace/exports/{export_id}/downloadprojectId=<projectId>
```

The GA smoke verifies project ZIP structure and redaction with `scripts/smoke_ga.py --offline`.
