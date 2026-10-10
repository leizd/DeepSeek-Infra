# Native data-plane HTTP routes

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **`/api/reminders`, `/api/reminders/due` and the `/api/memory` family are
wired on the native edge.** Reads are served; mutations are gated on their store's
cutover. Exact-head CI has not run.

`deepseek-gateway::data_routes` serves the data-plane HTTP surfaces the browser
frontend calls, registered **ahead of** the Go `/api/*` catch-all in
`create_routes`. They are native rather than proxied because the authoritative
writer of these stores is Rust, not Go — routing them through the control-plane
proxy would put a second writer on the same file.

## What is served

### Reminders

| Route | Oracle | Behaviour here |
| --- | --- | --- |
| `POST /api/reminders` `action=list` | `server.reminder_action` | **Served for real.** `{"reminders": [...]}`, tolerant read (missing/malformed/wrong-type all degrade to `[]`, non-object entries dropped). |
| `POST /api/reminders` `action=create` | same | Gated. After the flip: `{"ok": true, "reminder": {...}}`. |
| `POST /api/reminders` `action=delete` | same | Gated. After the flip: `{"ok": true, "deleted": <0|1>}`. |
| `POST /api/reminders` other | same | `400 invalid_payload`, `"Unsupported reminder action"`. |
| `POST /api/reminders` empty body | `read_json_body` → `{}` | Takes the `list` default, as the oracle does. |
| `POST /api/reminders/due` | `server.api_due_reminders` | Gated — it **writes**. After the flip: marks newly-due entries `notified` and persists. |

### Memory

`deepseek_infra/infra/memory/` is a **projection** layer (`schema.py`, `policy.py`,
`store.py`, `search.py`) over the same `.memory/memories.json` the chat turn writes,
so it is the same authoritative store and the same gate. Ported as
`deepseek_policy::memory_schema`.

| Route | Oracle | Behaviour here |
| --- | --- | --- |
| `GET /api/memory` | `routes/memory.py` | **Served.** `{"memories": [...]}` in the v3.0 shape (`memoryId`/`type`/`source`/`confidence`/`expiresAt`) plus the legacy aliases clients still read (`id`/`category`/`legacyScope`/`pinned`). |
| `POST /api/memory` `action=list` | same | Served. |
| `POST /api/memory` `action=add` | same | Conflict check first (see below), then gated. |
| `POST /api/memory` `action=clear` | same | Gated. After the flip: `{"ok": true, "deleted": <count>}`. |
| `POST /api/memory` `action=delete` | same | Gated; scope list is `["global", scope]` when the scope is not global. |
| `POST /api/memory` `action=deletebyid` | same | Gated. |
| `DELETE /api/memory/{id}` | same | Gated. |
| `PATCH /api/memory/{id}` | same | Gated; the oracle's field-wise patch. |
| `GET /api/memory/search` | same | **Served** — a read. `limit` through a bare `int()`. |
| `POST /api/memory/conflicts` | same | **Served** — a read. |
| other action | same | `400 invalid_payload`, `"Unsupported memory action"`. |

## Why the gate is on a route that reads like a query

`POST /api/reminders/due` is not a query. The oracle marks each newly-due entry
`notified`, sets `notifiedAt`, and rewrites the whole file — that is the delivery
poll. `action=create` and `action=delete` rewrite it too. Python reaches the same
file from **three** paths, and both sides reproduce the same temp name
(`reminders.json` → `reminders.tmp`), so two writers can interleave before either
replaces it.

So every mutating action is refused with `NATIVE_REMINDERS_WRITE_NOT_OWNED`
(HTTP 409) while `reminders_store` is Python's — the **same code and reason** the
chat tool loop gives for `create_reminder`. Memory mutations are refused with
`NATIVE_MEMORY_WRITE_NOT_OWNED` on the same terms. This is not a placeholder: the
flip is one environment variable, and the refusal is driven by the same predicate
(`crate::may_write_native_store`) the tool loop uses, which requires **both** that
`DEEPSEEK_RUNTIME_MODE=python_disabled` de-authorises Python **and** that the
domain is declared in `DECLARED_NATIVE_DATA_DOMAINS`. A mode cannot enable a store
nobody declared.

## Behaviours measured against the oracle, not inferred

Three were wrong on the first pass and were corrected by measurement:

1. **Identical content is not a memory conflict.** `memory.py:302` skips a candidate
   whose normalised content equals the incoming content, so `add` with the *same*
   text is the update path, not a 409. A conflict needs the same category, the same
   scope, the same conflict domain and **different** content.
2. **One `add_memory` bumps the fence generation four times, not two.** The oracle
   saves twice — `upsert_memory` persists, then `save_memories(_merge_item(item))`
   persists the public fields it just added — and each save is one fenced scope
   bumping the counter twice.
3. **`public_confidence("nan")` is `1.0`, not the `0.9` default.** The clamp is
   Python's `max(0.0, min(1.0, x))`, and `min` returns its *first* argument unless
   the second compares strictly less — `nan < 1.0` is `False`, so `1.0` survives.
   `"inf"` likewise becomes `1.0` and `"-inf"` becomes `0.0`.

## Verification

`rust/crates/deepseek-gateway/tests/data_routes.rs` — **19 cases, every one driving
`create_production_app`**, so the auth layer and the registration order ahead of the
Go catch-all are inside what is measured.

Reminders (9):

- `a_mutating_action_is_refused_while_python_owns_the_store` — 409 + code, and
  **no store file created**.
- `a_mutating_action_writes_once_python_is_de_authorised` — the same request one
  mode different: reminder stored, 16-hex id, `dueAt` normalised to `+00:00`,
  file content read back, and `.workspace-generation` == `2` (the write really
  passed through the mutation fence).
- `delete_is_refused_then_works_after_the_flip` — refused delete leaves the file
  **byte-identical**; after the flip `deleted == 1` and the file is `[]`.
- `the_due_poll_is_gated_because_it_marks_notified` — refused leaves the file
  byte-identical; after the flip the entry is marked and the marking is
  **persisted**, not merely reported.
- `a_second_due_poll_reports_nothing` — the property the delivery loop needs.
- `a_create_without_due_at_is_the_oracles_400` — oracle message and code, no
  partial store.
- `unknown_action_and_empty_body_match_the_oracle`,
  `list_is_served_and_reads_the_bound_root`,
  `the_routes_require_production_auth`.

Memory (10):

- `memory_mutations_are_refused_while_python_owns_the_store` — all four `POST`
  actions plus `DELETE` and `PATCH`, and **no store file created**.
- `memory_add_writes_once_python_is_de_authorised` — 20-hex content-addressed id,
  v3.0 shape, store on disk, generation `4`.
- `memory_add_reports_conflicts_before_the_gate` — 409 `memory_conflict` with the
  conflict listed, checked **before** the gate.
- `memory_replace_ids_clear_the_conflict` — `replaceIds` suppresses it, so the
  request reaches the gate instead.
- `memory_list_is_served_with_the_v3_shape`,
  `memory_search_serves_and_rejects_a_non_numeric_limit`,
  `memory_conflict_probe_is_a_read`, `memory_unknown_action_is_the_oracles_400`,
  `memory_delete_by_id_after_the_flip`, `memory_edit_after_the_flip`.

Both refusal families were shown **able to fail**: forcing the gate predicate to
`false` turns exactly the gate-dependent cases red (2 for reminders, 2 for memory)
and leaves the rest green.

### Parity probe

`tasks/native-runtime/memory_schema_parity_probe.py` ↔
`rust/crates/deepseek-policy/examples/memory_schema_parity_probe.rs` — the same
corpus through the oracle and the port: **byte-identical**, md5
`d0bbb07505465d8759a9d1943486ec1f`, **164 keys**, 18 935 chars. Covers the scope
and type maps, `storage_scope`, confidence coercion, source-ref sanitisation,
`public_source`, `public_memory`, `assert_memory_safe`, `readable_scopes`,
`skill_can_read_memory`, and the store/search operations against a real temp root.

The probe was shown **not blind**: inverting `public_scope`'s `project:` branch turns
it red on exactly the `public_scope::project:*` keys. The clock is pinned on both
sides and minted ids are masked to `<id>`; both differences are recorded in the
probe's docstring.

Commands:

```
cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-gateway --locked --test data_routes
cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-gateway -p deepseek-policy --locked
cargo +1.85.0-x86_64-pc-windows-gnu clippy -p deepseek-gateway -p deepseek-policy \
  --locked --all-targets --all-features -- -D warnings
cargo +1.85.0-x86_64-pc-windows-gnu fmt --all -- --check

$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
python tasks/native-runtime/memory_schema_parity_probe.py > python.json
cd rust; cargo run -p deepseek-policy --example memory_schema_parity_probe > ../rust.json
```

## Remaining gaps

- The other `/api` data surfaces the frontend calls — projects/files, media, skills,
  traces — are still Python or a Go `501`.
- `memory_store` and `reminders_store` are declared (python → rust, cutover 4.9.4)
  but not yet cut over, so every mutation above still refuses. That is the intended
  state until the cutover lands on both sides at once.
- Production HTTP authority is still Python; this is an opt-in native edge.

