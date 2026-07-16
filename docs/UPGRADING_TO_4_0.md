# Upgrading to 4.0

This guide covers the stable 4.0.x Python-first hybrid runtime, including current patch release `4.0.2`. The upgrade is intentionally additive: no mandatory database migration, user-directory deletion, or Rust sidecar deployment is required.

## 4.0.1 to 4.0.2

Stop the 4.0.1 service, install 4.0.2, and restart it with the same configuration and runtime data. Packaged releases already include the React preview assets. Source deployments must run `npm ci --prefix frontend` and `npm run build --prefix frontend` before packaging or starting a deployment that should expose `/ui/`. The stable workspace remains at `/`; `/ui/` is an isolated preview and does not write a new database or replace legacy conversation persistence. Roll back by reinstalling 4.0.1; no data conversion is required.

## 4.0.0 to 4.0.1

Stop the 4.0.0 service, install 4.0.1, and restart it with the same configuration and runtime data. No database or protocol migration is required. Browser credentials previously remembered in `localStorage` are removed; enter them again if needed, and they will be retained only for the current tab session. Reload once while online so the versioned Service Worker can install the complete 4.0.1 app shell before relying on offline mode.

## 3.10.0 to 4.0.0

1. Back up the runtime data directory as you would for any prerelease install.
2. Install 4.0.0 and start the normal Python service. Do not add the optional Rust Compose file unless you already operate the sidecar.
3. Existing JSON-only semantic-cache databases are opened in place. Startup adds nullable BLOB metadata columns idempotently and never performs a full-table rewrite.
4. Existing `embedding TEXT` values remain readable. New rows dual-write the same normalized vector to JSON and `f64le-v1` BLOB storage; JSON is never removed.
5. Confirm every Rust setting remains at its default: `DEEPSEEK_RUST_GATEWAY=0`, `DEEPSEEK_RUST_MCP=0`, `DEEPSEEK_RUST_POLICY=0`, `DEEPSEEK_RUST_RAG=0`, `DEEPSEEK_RUST_RAG_DOCUMENT_PREP=0`, and `DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=json`.

No Rust executable or sidecar is required for ordinary Python-only startup.

## 4.0.0-rc.1 to 4.0.0

Old configuration remains parseable. The legacy Rust delegate flags and policy fallback flag retain their meanings, existing SQLite rows are preserved, and the application does not require deletion or rebuilding of `.semantic-cache`, `.local-rag`, projects, memory, files, or any other user directory. rc.1 is superseded, but its additive data layout is compatible with stable 4.0.0.

## 4.0.0-rc.2 to 4.0.0

This is a metadata-only promotion. No database migration, configuration rewrite, user-directory rebuild, protocol change, or Rust sidecar deployment is required. Stop rc.2, install 4.0.0, and start the same deployment with the same configuration and runtime data.

## Rollback from 4.0.2 to 3.10.0

1. Stop 4.0.2 cleanly.
2. Set all delegate flags to `0` and `DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=json`.
3. Install or start 3.10.0 against the same runtime data directory.

The required `embedding TEXT` column and JSON values are still present, so 3.10.0 can read cache rows created by 4.0.2. Older SQLite readers ignore the added nullable BLOB columns. The `/ui/` preview introduces no backend storage migration. Do not run a destructive schema downgrade. With Rust flags off, operation returns to Python-only.

## Sidecar unavailable

The default service starts successfully because the default deployment is Python-only. If an explicitly enabled delegate cannot reach the sidecar, Gateway request preparation, MCP protocol preparation, Tool Policy evaluation, RAG vector ranking, and RAG document preparation fall back safely to Python. A compact binary vector failure returns directly to Python ranking and must not call the JSON Rust endpoint as a second attempt. Fallback does not delete or rewrite user data.

Run the executable contract before deployment:

```bash
python -m pytest --no-cov tests/test_4_0_upgrade_contract.py
```

The versioned result is recorded in `docs/evidence/upgrade-rollback-v4.0.2.json`.
