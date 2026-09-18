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

## The vector bonus: measured, then closed

`retrieve_memories` adds a **vector-search bonus** from
`local_rag.search_memories_index`. The bonus arrives through an injectable
`VectorHits` provider so the turn-state half and the RAG store stay separable.

The oracle wraps that call in `try/except Exception` and falls back to an empty map,
so `None` reproduces the oracle's **own degradation path** exactly — and the probe
compares that path. What was open is whether the bonus is *bounded*: could a
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
not a bounded one.

**That provider now exists** — `deepseek_policy::memory_index` — and the same
measurement is the acceptance test for it; see the next section.

## The index read path (`deepseek_policy::memory_index`)

`search_memories_index` is `search(collection="memory", scopes=…, limit=…)`, and
`_search_db` has two branches. Which one runs is decided by whether `sqlite-vec`
loaded:

| branch | when | what it needs |
| --- | --- | --- |
| `rag_vec` `MATCH` | the extension loaded | the `vec0` virtual table |
| **cosine fallback** | the extension absent | `rag_items.embedding` (a JSON column) |

`sqlite_vec` is not a dependency of this repository — not in `requirements.txt`,
`requirements-dev.txt`, `pyproject.toml` or any Compose file, and
`find_spec("sqlite_vec")` is `None`. `initialize_schema` creates `rag_vec` only
`if vec_loaded`, so the fallback is the branch **every shipped deployment takes**, and
it is the one implemented in full.

The `rag_vec` branch is not reimplemented, because it cannot be: `vec0` is an
extension loaded into the Python connection and `rusqlite`'s bundled SQLite has no
such module. When the table is present the read returns
`MemoryIndexError::VectorTableNotReadable` instead of quietly computing the fallback
— the oracle would have blended `1/(1+distance)` into every score, so the two answers
differ in membership, not just in order. The refusal is narrow by construction: only
a deployment that installed the optional `sqlite-vec` extra can reach it.

The read is **read-only**: it opens `.local-rag/rag.sqlite3` with
`SQLITE_OPEN_READ_ONLY` and never creates the directory, the schema or the `rag_meta`
rows the oracle's `db_ready()` would. Python remains the writer (`sync_memories` from
`save_memories`), so `one_table_one_authoritative_writer` holds by construction — there
is no second writer to fence. A missing database is therefore `None` rather than an
empty index: the oracle's `[]` on a missing database comes from `db_ready()` *creating*
it, and creating it is the one thing a reader must not do.

`bm25_scores` and `_python_normalize_query` live here rather than in `deepseek-rag`
because they are reachable only through this path, and `deepseek-policy` does not
depend on `deepseek-rag`. `hash_text_embedding`, `normalize_vector` and
`cosine_similarity` are reused from `attachment_context` — already probe-verified —
rather than duplicated. `DEEPSEEK_RUST_RAG` defaults to **false**, so the oracle's
lexical half is Python's `bm25_scores`, not the sidecar: BM25 is what is ported.

### A defect the probe found

`parse_embedding` has **three** distinct outcomes in the oracle, and the port had
collapsed two of them. A decode error is a bare `return []` — the **raw** empty list,
not normalized — while a value that is not an array normalizes `[]` to `dimensions`
zeros. The port returned `dimensions` zeros for both.

The difference is invisible downstream, which is exactly why it survived: with an
empty right-hand side `cosine_similarity` returns `0.0` early, and with `dimensions`
zeros it sums to `0.0`, so every score in the corpus was already identical. It is
visible only at the function's own contract — and `str(value or "[]")` is part of that
contract too: an empty string is falsy, so it parses as `[]` and lands in the
*second* branch, not the first. Both are now pinned by `pure::parse-3` and the new
`pure::parse-9` case.

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
- **The index read path**: `tasks/native-runtime/memory_index_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/memory_index_parity_probe.rs`, **64 keys,
  byte-identical** after `tr -d '\r'` (LF-normalised MD5
  `16494f987aa25c24e617eaeda15e33a9`; the Rust `println!` writes CRLF
  on Windows, which is why every probe pair in this repository normalises). The
  fixture is shared rather than duplicated: the Python side builds
  `.local-rag/rag.sqlite3` through the production `save_memories` → `sync_memories`
  write path and the Rust side opens that same file read-only. Pinned: 7 embedding
  shapes, 6 normalization shapes, a 6-document BM25 corpus, 10 `parse_embedding`
  shapes, `store::dimensions`/`vector-table-present`/`memory-rows` (10),
  the ordered hit list and the `id → max(score)` map for all 8 queries, and
  `retrieve_memories` on both paths.
- **The acceptance test the wiring plan asked for, inverted**: the same 8 queries
  report `turn::differing = 7 of 8` — the Rust provider reproduces the oracle's live
  path *and* its no-index path exactly, and the bonus still moves 7 of 8. A provider
  that had quietly returned nothing would have shown `0 of 8`; one that returned the
  wrong bonus would have failed the `retrieve::` comparison.
- The budget corpus **can fail**: with `normalize_memory_text` capping a row at 1200
  characters, reaching 8 000 takes six full rows (used 7 254) plus a 737-character
  row that lands exactly on the budget; the reference output contains the 省略 marker
  (8 140 characters) — the first corpus (3 × 3000) could never have reached it.
- `cargo test -p deepseek-policy` → **400 tests, all pass** (10 of them
  `memory_index`, 2 new here); `cargo test -p deepseek-gateway` → 145 lib + 15
  integration, all pass.
- `cargo fmt --check` clean; `cargo clippy --locked --all-targets --all-features
  -- -D warnings` exit 0 with no diagnostics.
- `ruff check` and `mypy` pass on all three probes.

## What is left

- **Wiring** `chat_execution` onto `build_deepseek_request` (step 3 of
  [`tasks/native-runtime/assembly-wiring-plan.md`](../tasks/native-runtime/assembly-wiring-plan.md)),
  with refusals for forced-search mode and the file vector index. The memory provider
  that step was waiting for is landed, so all three of its named prerequisites are now
  met; what remains is the wiring itself.
- A `memory` domain declaration before any Rust **write** is wired (Decision B).
- `suggest_memory` still does not persist anything: it builds a suggestion and fires
  a callback. The write functions are now ported, but nothing calls them from a
  production path yet.
- The optional `sqlite-vec` deployment still has no Rust read path: reproducing the
  `rag_vec` distances would need the extension itself, or a verified reimplementation
  of what `vec0` returns — a measurement this host cannot make, since the module is not
  installed. That configuration refuses (`VectorTableNotReadable`) rather than
  degrading; the fallback path that every shipped deployment takes is complete.

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
