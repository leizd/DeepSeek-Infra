# The memory store and its branches (slice D)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **the store, the three branches and the turn-state half are ported and
byte-verified; not wired.** The vector-bonus gap is now **measured, not assumed** —
see below.

`memory.rs` mirrors `infra/data/memory.py` plus the `suggest_memory` /
`recall_memory` / `forget_memory` branch bodies, and `file_lock.rs` factors out the
OS lock both this module and the mutation gate need.

## Three layers of exclusion, not one

A memory write passes through all three, and each does a different job:

1. **A process-wide mutex** (`_memory_lock`) — serializes threads in this process.
2. **A cross-process file lock** on `.memory/memories.lock` — the oracle seeds the
   file with a NUL byte on Windows so there is a byte to lock, then locks byte zero.
3. **The workspace mutation gate** — fences against a restore in progress and bumps
   the backup generation twice.

The file lock now lives in one place: `file_lock.rs`, used by both `memory` and
`mutation_gate`, with the `LockFileEx`-retry / `flock` platform split recorded there.

## The bug this slice found, and how

The first version put `mutation_scope` around the **delete** path only, because that
was the path I was looking at. The oracle puts it inside
`_save_memories_unlocked`, so *every* save is fenced — including the migration save
that runs on any write.

The probe caught it immediately, as a generation counter off by exactly two:

```
delete::no-write-generation   Python 6   Rust 4
```

Six means three scopes had already run (the migration save plus two deletes), four
means two. It is a small number, but it is precisely the property the gate exists
for, so a store that skipped it on the migration path would leave a backup free to
accept a package assembled across that write. Fixed by moving the gate into
`save_unlocked`, where the oracle has it.

## Where this is deliberately not faithful yet — and the measurement that settled it

`retrieve_memories` adds a **vector-search bonus** from
`local_rag.search_memories_index`. `local_rag` is 2,676 lines and belongs to the RAG
slice, so the bonus arrives through an injectable `VectorHits` provider and defaults
to none.

The oracle wraps that call in `try/except Exception` and falls back to an empty map,
so the default reproduces the oracle's **own degradation path** exactly — and the
probe compares that path. What was open is whether the bonus is *bounded*: could a
production caller pass `None` and merely lose a tie-break?

It cannot, and the paired measurement shows why. `LOCAL_RAG_ENABLED` defaults to
true and the embedding provider to `hash`, so the index is live **offline** with no
API key: `save_memories` populates it through `sync_memories` and
`search_memories_index` returns real scores. `tasks/native-runtime/memory_vector_bonus_probe.py`
runs the real oracle over a corpus once with that index live and once with
`search_memories_index` monkeypatched to raise — the state this port defaults to:

- run A executed twice → **identical**, so the difference is the index, not
  flakiness;
- **7 of 8 queries retrieved a different order**;
- the **sets differ too**: for query `react`, `m-long` (lexical score zero) is
  retrieved only through the bonus (`score 37 → +3`), so the oracle injects a memory
  this port would not.

**Consequence for the wiring:** a production caller of `retrieve_memories` or
`prepare_memory_state` must inject a provider that reproduces the index, or refuse.
Passing `None` is a visible divergence on every deployment that has memories — it is
not a bounded one — and it is recorded as such in the migration matrix.

## The turn-state half (the request-assembly slice)

`prepare_memory_state` and its collaborators are ported with the same injection
boundary as the clock, the transport and the content expander:

| function | note |
| --- | --- |
| `memory_scope_candidates` | `["global"]`, plus the payload's scope when it is not global |
| `memory_scope_label` | normalization, then the oracle's split-and-rejoin |
| `format_memory_context` | the five header lines, the `[category]`/`[scope]` prefixes, and the 8 000-code-point budget that appends the 省略 marker and **stops** |
| `upsert_memory` | the in-memory record is returned, not the cleaned one, so the notice reports what was saved; `replace_ids` filters before the fingerprint match |
| `clear_memories`, `delete_memory_by_id` | no write when nothing changes |
| `apply_explicit_memory_command` | the repaired grammar (`da8c21cf`): opt-out → negated forget → forget → remember |
| `prepare_memory_state` | runs the command first, so the just-saved memory is retrievable in the same turn; an `AppError` becomes the notice |

The remember branch's `请`/`帮我` prefixes stay **required** — the oracle's own gap
(`记住: X` returns `""`) is kept rather than "fixed", because changing it would be a
product decision and a parity break.

## Two defects this slice found

**A falsy content gate.** `_save_memories_unlocked` writes
`normalize_memory_text(item.get("content") or "")`, so a falsy non-string content
(`0`, `false`, `""`, `null`) is empty and the row is **dropped**, while a truthy
number is stringified (`5` → `"5"`, `true` → `"True"`). The port called
`normalize_memory_text` without that gate, so `content: 0` was kept as `"0"` where
Python dropped it. Fixed in `normalized_content`, and the corpus now carries
`{"content": 0}`, `{"content": true}` and `{"content": false}` through the migration
save and the context formatter.

**A regression in `OrderedJson`'s array rendering.** The nested-order refactor
(`3b7c041b`) changed array elements to take their order from the array's *name*
only, with an empty fallback — so a **top-level array** (`from_value_with_order(&fixture, &KEYS)`,
which is exactly how the store fixtures are written) lost its key order and rendered
alphabetically. No CI job runs these probes, so nothing caught it; the memory probe's
`store::file` observation did, once it was re-run. Fixed by inheriting the enclosing
order when no nested order is registered for the array's name; all 24 runnable probe
pairs were re-run afterwards and are byte-identical, `request_assembly` (the
nested-order consumer) included.

## Evidence

- Byte-level parity: **identical MD5 `97187819db5aec787776174f6ac3f3d5`**, 194 keys
  (was 92), no differences — everything listed below plus the turn-state sections:
  9 candidate payloads, 7 label shapes, 8 context shapes (including the
  budget-crossing corpus), 8 upsert shapes with their file bytes, clear and
  delete-by-id with their file bytes, 14 command shapes with their file bytes, and 9
  turn-state shapes with their file bytes and the final generation counter.
- The budget corpus **can fail**: with `normalize_memory_text` capping a row at 1200
  characters, reaching 8 000 takes six full rows (used 7 254) plus a 737-character
  row that lands exactly on the budget; the reference output contains the 省略 marker
  (8 140 characters) — the first corpus (3 × 3000) could never have reached it.
- `cargo test -p deepseek-policy` → **390 tests, all pass** (33 new here);
  `cargo test -p deepseek-gateway` → 145 lib + 15 integration, all pass.
- `cargo fmt --check` clean; `cargo clippy -p deepseek-policy --locked --all-targets
  --all-features -- -D warnings` exit 0 with no diagnostics.
- `ruff check` and `mypy` pass on both probes.

## What is left

- **Wiring** `chat_execution` onto `build_deepseek_request` (step 3 of
  [`tasks/native-runtime/assembly-wiring-plan.md`](../tasks/native-runtime/assembly-wiring-plan.md)),
  with refusals for forced-search mode and the file vector index — and, per the
  measurement above, a real provider (or a refusal) for the memory vector index
  rather than `None`.
- A `memory` domain declaration before any Rust **write** is wired (Decision B).
- `suggest_memory` still does not persist anything: it builds a suggestion and fires
  a callback. The write functions are now ported, but nothing calls them from a
  production path yet.

## A truthiness detail worth its own note

`_save_memories_unlocked` normalises `source` through two Python `or` chains:

```python
raw_source = item.get("source") or "manual"
source = raw_source if isinstance(raw_source, dict) else str(raw_source or "manual")
```

My first version stringified any number and fell back for anything else, which is
wrong at both ends: `0` and `false` are **falsy** and become `"manual"`, while a
non-zero number and `true` are **truthy** and become `"5"` / `"True"`. Fixed with an
explicit `python_truthy`, which also covers `""`, `[]` and `{}`.

## What else the write path does

`_save_memories_unlocked` doubles as the migration, and the port keeps every rule:

- non-objects and empty content are dropped;
- `id` falls back through `memoryId` → `id` → a **content-addressed**
  `sha256(...)[:20]` fingerprint, so ids do not churn across an upgrade;
- `confidence` is coerced to a float, defaulting to `0.9` and clamped to `[0, 1]`;
- `type` is derived from `type` → `category` → `"fact"`, lowercased;
- `createdAt` / `updatedAt` default to now, through the injected clock;
- the record is capped at `MEMORY_MAX_ITEMS = 400`, with the canonical key order.

Reads stay silent: missing, unreadable, malformed or a wrong top-level type all
degrade to empty, and non-dict entries are dropped.

## Descriptions that would be easy to get wrong

- `normalize_memory_scope` **narrows to `global`** for anything it does not
  recognise — `project:` (empty), `project:a b` (a space), `unknown:x`,
  `PROJECT:abc` (case). It is not an error path, so the round trip is asserted.
- `memory_tool_scopes` splits three ways on the *raw* argument's truthiness: an empty
  scope inherits the request default, an explicit `global` **narrows and does not
  inherit**, and an explicit valid scope wins.
- `recall_memory`'s projection carries exactly five fields, and `list_reminders`'s
  asymmetry has a cousin here: `retrieve_memories` **drops** memories that score
  zero rather than returning them at the bottom.
- Ordering is `(score, updatedAt)` descending, stable, so ties keep file order.

## Evidence

- Byte-level parity: **identical MD5 `97187819db5aec787776174f6ac3f3d5`**, 194 keys
  (was 92), no differences — 4 text shapes, 13 scope shapes, 3 fingerprints plus two
  cross-scope distinctness checks, 13 sensitive terms, 8 category shapes, 8 conflict
  keys, 6 tool-scope shapes, 6 suggest shapes, the loaded store and its exact bytes,
  the migrated bytes, 5 tolerant-read shapes, 7 recall shapes, 5 forget shapes, the
  delete semantics with their generation counters, 3 conflict queries, and the
  turn-state sections listed above.
- `cargo test -p deepseek-policy` → **390 tests, all pass** (33 new here);
  `cargo test -p deepseek-gateway` → 145 lib + 15 integration, all pass.
- `cargo fmt --check` clean; `cargo clippy -p deepseek-policy --locked --all-targets
  --all-features -- -D warnings` exit 0 with no diagnostics.
- `ruff check` and `mypy` pass on both probes.

## What is left

- **Wiring** `chat_execution` onto `build_deepseek_request` (step 3 of
  [`tasks/native-runtime/assembly-wiring-plan.md`](../tasks/native-runtime/assembly-wiring-plan.md)),
  with refusals for forced-search mode and the file vector index — and, per the
  measurement above, a real provider (or a refusal) for the memory vector index
  rather than `None`.
- A `memory` domain declaration before any Rust **write** is wired (Decision B).
- `suggest_memory` still does not persist anything: it builds a suggestion and fires
  a callback. The write functions are now ported, but nothing calls them from a
  production path yet.
