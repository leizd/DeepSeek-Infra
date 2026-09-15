# The memory store and its branches (slice D)

Status: **ported and byte-verified; not wired.** One known gap, stated below.

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

## Where this is deliberately not faithful yet

`retrieve_memories` adds a **vector-search bonus** from
`local_rag.search_memories_index`. `local_rag` is 2,676 lines and belongs to the RAG
slice, so the bonus arrives through an injectable `VectorHits` provider and defaults
to none.

The oracle wraps that call in `try/except Exception` and falls back to an empty map,
so the default reproduces the oracle's **own degradation path** exactly — and the
probe compares that path. But when the vector index is populated the oracle's scores
include a bonus this does not, which can reorder results. **`recall_memory`'s ranking
is verified only for the case where the vector index contributes nothing.** That is
stated here, in the migration matrix, and in the module docs, rather than left to be
discovered.

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

- Byte-level parity: **identical MD5 `4261dd06c31ec2de180601f8d80e5cca`**, 92 keys,
  no differences — 4 text shapes, 13 scope shapes, 3 fingerprints plus two
  cross-scope distinctness checks, 13 sensitive terms, 8 category shapes, 8 conflict
  keys, 6 tool-scope shapes, 6 suggest shapes, the loaded store and its exact bytes,
  the migrated bytes, 5 tolerant-read shapes, 7 recall shapes, 5 forget shapes, the
  delete semantics with their generation counters, and 3 conflict queries.
- `cargo test -p deepseek-policy` → 206 tests, all pass (26 new here, including the
  shared lock and its cross-thread serialization).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean; `cargo fmt` applied.

## What is left

`projects` (slice E) — the only remaining data branch, blocked on
`rag/files.py`'s `load_cached_file`. **`suggest_memory` does not persist anything**:
it builds a suggestion and fires a callback, so `upsert_memory`, `clear_memories`
and `delete_memory_by_id` are not ported and are not needed by these three branches.
Nothing is wired; `Branch::is_ported()` is unchanged.
