# search_files (json_hybrid + read-only RAG)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported, wired into the tool loop, json_hybrid probe-verified locally.**
Exact-head CI has not run against this slice.

`deepseek-policy::search_files` mirrors `tools.search_files` in
`deepseek_infra/infra/tool_runtime/tools.py`. Two retrieval paths are merged by
`(fileId, projectId, chunkIndex)`, keeping the higher score:

1. **json_hybrid** — walk `<root>/.file-cache/*.json` and
   `<root>/.projects/*/files/*.json`, score each chunk with `score_chunk` and
   cosine over the hash embedding. Complete; needs no sqlite.
2. **local_rag** — read-only `search_files_index` over `.local-rag/rag.sqlite3`
   collection `files`, the same cosine+BM25 path `memory_index` uses for
   memories.

## Ownership: this module does not write RAG

The oracle calls `index_file_payload` first, which **writes** `rag_items`.
Python remains that writer. A native search that indexed would be a second
writer of one table. json_hybrid still finds anything sitting in the cache JSON,
which is the source `index_file_payload` itself reads. A missing sqlite file
degrades to json_hybrid only.

When `rag_vec` is present the sqlite path is skipped rather than serving the
cosine fallback (same refusal as the memory index).

## Verification

- `tasks/native-runtime/search_files_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/search_files_parity_probe.rs`
  (json_hybrid; Python `search_files_index` stubbed to `[]` so the probe does
  not require a dual-writer)
- `cargo test -p deepseek-policy search_files`
- `chat_route_searches_cached_files` — the wired loop retrieves `useMemo` from
  a seeded `.file-cache` instead of answering `Tool did not run`
