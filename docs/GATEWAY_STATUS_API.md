# Native status `/api` (Rust gateway)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

These routes are served by `deepseek-gateway` **before** the Go `/api/*`
proxy. They use already-ported policy/ledger code. Missing stores degrade to
empty views; they do not invent spend or audit rows.

| Path | Source |
| --- | --- |
| `GET /api/tool-policy` | `tool_policy_status` + `read_recent_audit` |
| `GET /api/budget?scope=` | `budget_status` against `.budget/budget.db` |
| `GET /api/rag/status` | `local_rag_status` against `.local-rag/rag.sqlite3` (read-only) |
| `GET /api/gateway/status` | context-manager settings; request queue / scheduler marked `ported: false` |

A missing budget database is an empty `today` (the oracle's own missing-file
path). `GET /api/budget` does not create the file. A missing RAG database is
zero counts; `GET /api/rag/status` never creates `rag.sqlite3`.
`sqliteVecAvailable` is `false` because the native reader does not load the
`sqlite-vec` extension. `GET /api/gateway/status` does **not** invent queue
counts or a job scheduler snapshot.

Other `/api/*` still go to Go (`GO_CONTROL_PROXY_NOT_READY` unless
`GO_CONTROL_ADDR` is set). Production HTTP is still Python.
