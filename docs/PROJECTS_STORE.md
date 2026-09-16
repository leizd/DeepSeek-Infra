# The projects store, read path (slice E1)

Status: **ported and byte-verified in full (E1 + E2). Nothing is wired.**

This is the last data-layer domain, and it is split in two because the measurement
showed the two halves have very different dependencies.

| | Scope | State |
| --- | --- | --- |
| **E1** | the projects store: id validation, the `normalize_*` family, `read_project`, `public_project`, `list_projects` | **ported, byte-verified** |
| **E2** | `load_cached_file` (the file-cache read path) and the two branch wrappers `list_project_files_tool` / `read_file_chunk_tool` | **ported, byte-verified** |

## Why the split

The two branches are small — about 45 lines together. Their dependency surface is not:

- `read_project` re-normalises **six** collection fields on every read, so the whole
  `normalize_*` family is on the critical path even for a branch that only looks at
  `documents`. That includes `normalize_skill_run`, which has **thirty fields**.
- `read_file_chunk_tool` needs `load_cached_file` from `rag/files.py`.

The good news from the measurement: `load_cached_file` is nearly self-contained —
an id-shape check, a path derivation, a JSON read, and an `lru_cache(64)` keyed on
`(file_id, mtime_ns)`. It does **not** need the RAG chunking/embedding pipeline, so
E2 is a small slice rather than the whole 1,494-line module.

## A real finding: the read path mints random ids

```python
"skillRunId": str(item.get("skillRunId") or item.get("runId") or f"run-{secrets.token_hex(8)}")
```

`normalize_skill_run` and `normalize_saved_items` generate a fresh id whenever a
stored entry has none. Since `read_project` calls them, **reading the same malformed
project twice returns different values**. Measured:

```text
read 1 -> run-d9d3e527ae4f29df
read 2 -> run-5acb6344a2c0e2bf
```

It is not persisted — `read_project` never writes back — so this is a phantom id
rather than data corruption. But it is observable through `public_project`, which is
what `list_projects` returns to the model. This port keeps the behaviour (the ids
belong to a schema the oracle owns) and takes the source through the shared
`entropy::Entropy` trait, so the probe can pin it and compare a stable value.

That is also why `Entropy` moved out of `reminders` into its own module: a *second*
user appeared, and both need the same "no weak fallback" guarantee.

## `OrderedJson` had a real bug, found here

The store records ported so far were flat, so the renderer's flaw never showed.
A project record is not flat, and the comparison exposed it: nested containers were
being written **compactly** where Python's `indent=2` indents at every level. Fixed
by converting nested values into real nodes. This mattered beyond the probe — the
memory store can hold a `source` object, so a write there would have produced
different bytes from the oracle.

The residual limit is stated rather than hidden: nested object **keys** come out
sorted, because `serde_json` here has no `preserve_order` and a nested dict's
insertion order is therefore not recoverable. An explicit order is accepted at the
top level only.

## Two error-shape details

- **`unique_strings(None)` raises.** Python does `list(values)` for a non-list, so
  `None` is a `TypeError`, not an empty result. The port reproduces that — and
  restores the `or []` guard at all six call sites, which is what makes the raise
  unreachable from the ported code, exactly as in the oracle.
- **The `TypeError` has no code.** The oracle raises a bare `TypeError` for a
  non-iterable, so there is no `AppError` code. This port's error type is
  `AppError`, so it reports `invalid_payload` — a **documented** mapping with a
  matching message, not an invented code pretending to be the oracle's. The probe
  compares the message and deliberately does not compare the code, because the
  oracle cannot produce one.

## Detail worth knowing

- `normalize_documents` **drops** a document whose `fileId` is not exactly 32
  lowercase hex characters or whose `projectId` is empty. It does not repair it.
- `_safe_int` goes through `int(str(value))`, so `"12"` is 12 but `"12.7"` and
  `"True"` fall back to the default while `"1_0"` and `"  8  "` parse.
- `public_project` slices `skillRuns`, `savedItems` and `artifacts` to 20 but leaves
  `documents` unsliced.
- `list_projects` reads every valid-named **directory**, so a directory whose name
  is not a valid project id is skipped rather than raising.

## Evidence

- Byte-level parity: **identical MD5 `787f519d69e6b4a295732891fa84777b`**, 76 keys,
  no differences — 11 id shapes, 7 name shapes, 8 document shapes, 13 `_safe_int`
  shapes, 5 `unique_strings` shapes including both raises, 9 skills shapes, 6 skill
  runs (one with all thirty fields), saved items and artifacts with their generated
  ids, 5 tolerant/malformed reads, `require_project` hit and miss, and the
  `list_projects` ordering with an invalid directory and a loose file present.
- `cargo test -p deepseek-policy` → 206 tests, all pass (no new ones yet; E1's
  verification is the probe).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean; `cargo fmt` applied.

## E2: the file-cache read path

`load_cached_file` turned out to be what the measurement promised — an id-shape check,
a path derivation, a JSON read and a cache — so `file_cache.rs` is small and the rest
of that 1,494-line module stays untouched.

Three details that are easy to get wrong, and were:

- **The `lru_cache(64)` only applies without a project id.** A project-scoped read
  always re-reads. The cache key is `(file_id, mtime_ns)`, which is what stops a
  changed file hitting a stale entry, and `FileCache` reproduces both the bound and the
  move-to-front-on-hit behaviour because the whole point of this migration is that a
  difference is stated rather than assumed harmless.
- **`if project_id` tests the raw value, not the stripped one.** A whitespace-only
  project id is *truthy*, so it reaches `project_file_cache_dir`'s shape check and
  fails with a 400 — it does **not** fall back to the global cache. The wrapper
  (`read_file_chunk`) strips before calling and passes `None`, so a blank id from the
  tool *does* use the global path. Two different behaviours for a blank id, one call
  apart; the probe caught my first version collapsing them.
- **`int()` here is the bare one, not the store's `_safe_int`.** So `"3.7"` and `"abc"`
  **raise** rather than falling back, a float truncates toward zero, and `"1_0"` and
  `"  8  "` parse. `python_int` is deliberately a separate function from `safe_int`,
  with the same documented mapping as the projects `TypeError`: a bare Python exception
  becomes `internal`/500 with a matching message rather than an invented code.

Also faithful: `preview` is capped at **500** in the tool projection but **1800** in
the store; `count` is the sum of the *emitted* files, so it is the count after both
caps; and `chunks[index]` that is not an object is a 404, not a skip.

## One open item, stated plainly

`mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` failed **once**
during this slice and passed on every other run — in isolation, serially, and in two
full serial runs (214 tests each). The symptom is a thread panicking inside its scope.

The likely cause is parity, not a defect: `lock_exclusive` faithfully reproduces
`LK_LOCK`'s "retry once a second, give up after ten attempts", so under contention the
gate **errors** after roughly ten seconds where a plain blocking lock would have waited.
The oracle behaves the same way. The test's `.unwrap()` turns that refusal into a panic
instead of a clean assertion.

This is recorded rather than smoothed over. It is not root-caused, it is not a reason to
change the lock semantics, and it should be re-examined before the data layer is wired:

- if it is the retry budget, the fix belongs in the *test* (assert on the error instead
  of unwrapping, or reduce contention);
- if it is not, something else is sharing state between tests and that matters.

## What is left

The data layer is now **complete**: reminders, memory, the shared scorer, and projects.
Nothing is wired, and `Branch::is_ported()` has not been revisited — that, and the round
loop, is the next piece of work.
