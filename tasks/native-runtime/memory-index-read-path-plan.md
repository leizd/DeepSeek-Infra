# Memory index read path — measured 2026-09-18

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


**Status: measurement, then a slice — and the slice is landed and probe-verified.**
The assembly wiring plan's §4 measured that the memory vector bonus is **not bounded** —
7 of 8 queries change order *and set* when the index is live — so wiring
`chat_execution` onto `build_deepseek_request` needed a real provider for
`local_rag.search_memories_index`, not `None`. This file measured what that provider
actually is, so the slice was scoped by evidence rather than by the 2,676-line size of
`local_rag.py`. It is implemented in `rust/crates/deepseek-policy/src/memory_index.rs`
and paired with the oracle by
[`memory_index_parity_probe.py`](memory_index_parity_probe.py) ↔
`examples/memory_index_parity_probe.rs` — see “Landed” at the end of this file.

## What the oracle path actually calls

`retrieve_memories` (`infra/data/memory.py:439`) calls

```python
local_rag.search_memories_index(query, scopes=sorted(allowed_scopes), limit=MEMORY_RETRIEVE_LIMIT * 2)
```

which is `search(query, collection="memory", scopes=scopes, limit=24)`
(`local_rag.py:950`). `search` guards on `LOCAL_RAG_ENABLED` and a blank query, then calls
`_search_db` inside a `try/except Exception` that degrades to `[]`.

`_search_db` (`:812-873`) is the whole read path, and it has **two** branches:

| branch | when | what it needs |
| --- | --- | --- |
| sqlite-vec | `vector_table_ready` (extension loaded) | `rag_vec` virtual table `MATCH` |
| **cosine fallback** | extension absent | `rag_items.embedding` (a JSON column) + `load_candidate_rows` |

## The measurement that scopes it: the fallback is the production path

`sqlite_vec` is **not a dependency** of this repository — not in `requirements.txt`, not in
`requirements-dev.txt`, not in `pyproject.toml`, not in any Compose file, and
`importlib.util.find_spec("sqlite_vec")` is `None` on this host. `initialize_schema` only
creates `rag_vec` `if vec_loaded`, and `load_sqlite_vec` cannot load an absent module.

So in every deployment this repository actually ships, `vector_table_ready` is **false**,
`vector_distances` stays empty, and every score comes from the **cosine fallback over the
`embedding` JSON column**. That is the branch a Rust provider must reproduce first; the
`rag_vec` MATCH branch is a faster equivalent of the same cosine, not a different result.

## The dependency closure, measured

| piece | oracle | already in Rust? |
| --- | --- | --- |
| `hash_text_embedding` | `local_rag.py:166` | **yes** — `attachment_context::hash_text_embedding` |
| `normalize_vector` | `:178` | **yes** — `attachment_context::normalize_vector` |
| `cosine_similarity` | `:193` | **yes** — `attachment_context::cosine_similarity` |
| `parse_embedding` | `:907` | **yes** — `json.loads` + `normalize_vector` |
| `bm25_scores` | `:227` | no — the one real gap |
| `_python_normalize_query` | `:263` | no — small (see below) |
| `load_candidate_rows` | `:876` | no — one `SELECT` with scope/source/project clauses |
| `connect_db` + schema | `:411`, `:432` | no — read-only open; `rusqlite` is already a dependency |
| `row_to_result` | `:915` | no — field mapping plus `metadata` JSON |
| `search` / `_search_db` | `:780`, `:812` | no — the orchestration |

Two switches decide what the *scores* are, and both default to the Python path:

- `DEEPSEEK_RUST_RAG` defaults to **false** (`rust_core/config.py:28`), so
  `_score_chunks_with_rust` returns `None` and the lexical half is Python's `bm25_scores`.
  A Rust provider must implement BM25 itself rather than call the sidecar.
- `LOCAL_RAG_ENABLED` defaults to **true** and the provider to `hash`, so the index is
  populated and live offline (this is what §4 of the wiring plan measured).

## Where the provider belongs, and its ownership boundary

The read is **read-only**: it opens `.local-rag/rag.sqlite3` and never writes. Python
remains the writer (`sync_memories` from `save_memories`) until a `memory` domain is
declared, so this slice does not create a second writer — it satisfies
`one_table_one_authoritative_writer` by construction.

`deepseek-rag` is the right crate for the **pure** halves (`bm25_scores`,
`_python_normalize_query`) — it already owns query normalization, scoring and vector
similarity primitives and has no store. The **store-backed** half (`connect_db`,
`load_candidate_rows`, `_search_db`) belongs beside the other SQLite reads, in
`deepseek-policy` (which already depends on `rusqlite` for `budget_store` and
`file_store`), following the `file_store` precedent.

## Slice plan

1. `deepseek-rag`: port `bm25_scores` and the pure query normalization, with a parity probe
   against the oracle (both are pure and corpus-friendly).
2. `deepseek-policy::memory_index`: read-only open, `load_candidate_rows`, `_search_db`
   (cosine fallback **and** the `rag_vec` branch behind a capability check), `row_to_result`,
   and a `search_memories_index`-shaped entry point.
3. Bind it as the `VectorHits` provider in the wiring slice, and verify the paired
   measurement's 7-of-8 difference **disappears** — the same probe becomes the acceptance
   test, inverted.

## Acceptance

- The pure halves byte-identical to the oracle through a probe pair.
- The store half: a fixture `rag.sqlite3` built by the real Python `sync_memories`, read by
  Rust, returning the **same ordered `(id, score)` list** the oracle returns for the same
  queries — including the query where `m-long` surfaces only through the bonus.
- `memory_vector_bonus_probe.py` re-run with the Rust provider: the 7 differing queries drop
  to 0.

## Landed — and two corrections the work forced

**Step 1 was scoped wrong, and the measurement says so.** `bm25_scores` and
`_python_normalize_query` ended up in `deepseek-policy::memory_index`, not in
`deepseek-rag`. Two reasons, both mechanical: `deepseek-policy` does not depend on
`deepseek-rag`, and `hash_text_embedding` / `normalize_vector` / `cosine_similarity`
already live in `attachment_context` there. Putting the lexical half in a crate the
store cannot see would have meant either a new dependency edge for two functions or a
second copy of the embedding primitives. The pure/store split this file proposed is
real, but the crate boundary it drew is not where the dependency graph puts it.

**The acceptance criterion as written was wrong.** "The 7 differing queries drop to 0"
conflates two different comparisons. The 7-of-8 figure is the *live index versus no
index* difference inside the oracle; a correct Rust provider must reproduce the **live**
side, which leaves that figure at 7. The criterion that actually proves the provider is
that Rust's live path equals Python's live path and Rust's no-index path equals
Python's no-index path — the probe reports both, and `turn::differing = 7 of 8` is now
a **positive** result rather than a target. Measured: **64 keys, byte-identical**
(LF-normalised MD5 `16494f987aa25c24e617eaeda15e33a9`), and all 8 queries agree on both
paths.

The `rag_vec` branch is **not** implemented, and that is a correction to step 2 as
well: `vec0` is an extension loaded into the Python connection, `rusqlite`'s bundled
SQLite has no such module, and the extension is not a dependency of this repository —
so there is no way to evaluate its `MATCH` here, and no way to verify a
reimplementation of what it would return without installing it. The read therefore
refuses with `MemoryIndexError::VectorTableNotReadable` when the table is present
instead of silently serving the fallback. Narrow by construction: `initialize_schema`
creates `rag_vec` only when `sqlite-vec` is importable, so every shipped deployment and
every CI leg takes the complete fallback path.

One defect surfaced on the way, in `parse_embedding`: the oracle has **three** outcomes
(a decode error returns the raw empty list, a non-array normalizes `[]`, an array
normalizes to `dimensions` components) and the port had collapsed two of them. It was
invisible in every score — `cosine_similarity` returns `0.0` for both an empty and an
all-zero vector — and only the probe's `pure::` layer caught it. See
[`../docs/MEMORY_STORE.md`](../../docs/MEMORY_STORE.md).
