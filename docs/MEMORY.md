# Memory

Applicable version: v3.4.0.

Memory is a first-class Personal AI Runtime module in `deepseek_infra/infra/memory/`. It keeps the legacy local JSON store compatible while exposing a stable public object shape for workspace, skills and automations.

## Object Shape

```json
{
  "memoryId": "mem_...",
  "scope": "global | project | skill | automation",
  "type": "preference | fact | instruction | summary | artifact_ref",
  "content": "Short durable memory text",
  "source": {"kind": "chat | saved_item | project | automation | manual", "refId": "source-id"},
  "confidence": 0.9,
  "createdAt": "2026-07-05T00:00:00Z",
  "updatedAt": "2026-07-05T00:00:00Z",
  "expiresAt": null
}
```

Legacy fields `id`, `category`, `legacyScope` and `pinned` are still returned for older clients.

## Scopes

- `global`: available across the local runtime.
- `project`: isolated to `project:<projectId>`.
- `skill`: isolated to `skill:<skillId>`.
- `automation`: isolated to `automation:<automationId>`.

Skills read memory only when their `memoryPolicy.read` allows it. Successful project-bound automation runs write a project-scoped summary memory with `source.kind=automation`.

## API

- `GET /api/memory`
- `POST /api/memory` with `action=list|add|clear|delete|deletebyid`
- `GET /api/memory/searchq=...&projectId=...`
- `PATCH /api/memory/{memory_id}`
- `DELETE /api/memory/{memory_id}`

Sensitive memory candidates containing obvious secrets, API keys, passwords or tokens are blocked before persistence.
