# Assembly wiring plan — measured 2026-09-18

**Status: plan of record. No production code changed.** The goal is `chat_execution` building
its upstream body through `build_deepseek_request` instead of the thinner body it builds today.

Everything below is a measurement with its evidence, not a roadmap. Where a decision is needed it
is marked **DECISION** and carries a recommendation.

## Why this is a fidelity improvement, not a risk of regression

`request_assembly` has **no production caller**: `grep` over the gateway finds only
`lib.rs`'s module registration, the module's own docs, and one comment in
`request_preparation.rs:710`. `chat_execution` does not import it.

So the native `/v1/chat/completions` path that is wired today sends a body with **no dynamic
context, no memory state, no clock, no assembled system prompt**. Its parity with Python is
absent-by-omission today; the wiring is what closes it. That reframes the whole slice: the
interesting question is not "does wiring risk a regression" but "which surfaces can the native
body honestly reproduce".

## The dependency set

| Surface | State | Evidence |
| --- | --- | --- |
| clock + zone | **ready** | `deepseek-gateway::local_clock` (`f31eea9b`) |
| ledger read | ported | `deepseek-policy::budget_store` behind `LedgerDeps` |
| file cache (`load_cached_file`) | ported | `deepseek-policy::file_store` |
| file vector index (`search_file_chunks`) | **refuses** | `file_store::vector_index_not_ready()`, `NATIVE_FILE_VECTOR_INDEX_NOT_READY`, 501 |
| forced-search prefetch (`search_if_needed`) | **not ported — refuse** | reached only from `forced_search_mode` (`deepseek_client.py:674`) |
| memory state (`prepare_memory_state`) | **not ported** | 8 functions missing, see §1 |

## §1 `prepare_memory_state` — eight functions are missing

The oracle is `deepseek_infra/infra/data/memory.py:32`. Absent from both Rust crates (grep over
`deepseek-policy/src` + `deepseek-gateway/src`): `prepare_memory_state`,
`apply_explicit_memory_command`, `format_memory_context`, `memory_scope_candidates`,
`memory_scope_label`, `upsert_memory`, `clear_memories`, `delete_memory_by_id`.

Already ported, so the gap is narrower than the function list suggests: `empty_memory_state`,
`memory_scope_from_payload`, `retrieve_memories` (with the vector bonus as an injected
`VectorHits`), `delete_memories_by_query`, `load_memories` / `save_memories`, and the file lock.

Two call sites, both Python: `prepare_deepseek_call` (cloud, `deepseek_client.py:668`) and
`build_edge_messages` (edge fallback, `:726`). The edge path is a second consumer of
`build_dynamic_turn_context`, so a later `edge_inference` wiring inherits this work rather than
duplicating it.

## §2 The write half is broken in the oracle, and its test cannot see it

`apply_explicit_memory_command` (`memory.py:508`) contains two patterns that read as `(` where
`(?:` was meant — `(:忘记|…|delete memory)` and `(:请)(:帮我)(:记住|以后记得|remember)`. Measured
against the real patterns (pure regex, no function call, nothing written):

| query | negative guard | forget | remember |
| --- | --- | --- | --- |
| `请帮我记住: 我的生日是3月5日` | no | no | **no** |
| `记住: 我的生日是3月5日` | no | no | **no** |
| `帮我记住：我喜欢深色主题` | no | no | **no** |
| `不要记住: 这是临时的` | **yes** | no | no |
| `删除记忆: 生日` | no | **yes** | no |
| `forget: birthday` | no | **yes** | no |
| `:请:帮我:记住: 原样拼接` | no | no | **yes** |

Consequences, measured:

1. **"记住: X" is silently dropped** — no notice, no save, the user's instruction discarded
   without a word. This is the shape `USER.md` names as unacceptable ("a `200` that quietly
   ignores the user's instruction").
2. The remember branch stores `remember_match.group(1)`, which for the only matching query is
   the literal `:请` — so even when it fires it saves the wrong content.
3. The forget branch takes `forget_match.group(1)` = the **command word**. `delete_memories_by_query`
   is a plain substring test (`lowered_query in lowered_content`, `memory.py:392`), so "删除记忆"
   deletes 0 rows; `if deleted:` means the file is **not written**; and the prompt receives
   `已根据用户要求删除 0 条相关长期记忆。`
4. Reading the first group as the *target* only makes sense if the command alternation were
   non-capturing — i.e. the `(?:` reading is consistent with the rest of the function, and the
   typos are paired rather than independent.

**The covering test cannot detect either.** `tests/test_memory_failure_paths_332.py::test_explicit_english_remember_forget_and_opt_out`
monkeypatches `memory.re` with a `SimpleNamespace` whose `search` returns fabricated
`SimpleNamespace(group=lambda _: "concise replies")` objects. It never runs the real patterns,
never exercises `group(1)` vs `group(2)`, and would pass unchanged with the typos fixed. It is
another instance of the false-pass class already recorded in `continuation.md`.

**DECISION A.** Fix the two patterns (and replace that test with one that runs the real regexes)
before porting — or port the broken behaviour verbatim.

*Recommendation: fix first, as its own slice.* The precedent is `df7dfa13`, where the oracle's
silent drop of blank-content turns was fixed rather than frozen: "it stops silent data loss", not
a capability addition. Porting the typo would freeze "记住: X does nothing" into the Rust contract
and turn the later fix into a parity break.

## §3 Ownership: memory is an unenumerated durable store

- `release/native_runtime_ownership_v1.json` declares durable stores `python_oracle`, `go_control`
  and `rust_data`, and a `domains[]` list — **there is no `memory` domain**, and the memory file
  appears in none of the store entries.
- `GO_CONTROL_DOMAINS` (`infra/native_runtime/authority.py:28`) contains no memory identifier, so
  `assert_python_write_allowed` would **not** deny a write; nothing mechanical stops either side.
- The same file's `invariants` include `one_table_one_authoritative_writer`. Python writes
  `MEMORY_FILE` today (the memory tools, the `/api/*` routes, and the cross-process writer test in
  `tests/test_memory.py`), so a Rust write would make two writers of one store.
- `chat_completions_fast_path` **is** a declared domain — `current_owner: python`,
  `target_owner: rust`, `cutover: 4.9.2`. That is the ownership basis this wiring rests on.

**DECISION B.** Declare a `memory` domain (writer + cutover) before Rust writes it, or land the
wiring read-only.

*Recommendation: read-only, plus a follow-up domain declaration.* It is also free right now, since
the write half is inert-by-bug (§2).

## §4 The memory vector index has no Rust provider

`retrieve_memories` calls `local_rag.search_memories_index` (collection = memory) inside a
`try/except Exception` that swallows everything into `{}` — but that is not evidence the index is
never used. `local_rag.search` (`local_rag.py:780-809`) never raises: it returns `[]` when
`LOCAL_RAG_ENABLED` is false, when the query is blank, or on any exception, and it has a **lexical
fallback** (`db_ready()` → `vector_table_ready` → `_search_db`). So hits — and the
`score += max(1, score // 10)` bonus at `memory.py:467` — are real when the index exists.

`deepseek-rag` is a crate of **pure primitives** (chunk validation, citation formatting, query
parsing, scoring, vector similarity, the binary rank protocol, index metadata) with **no store and
no collection** — measured from its public surface, not assumed. So `VectorHits` has no provider
to inject, and passing `None` would drop a bonus the oracle actually applies.

**DECISION C.** Refuse, or bound the divergence.

*Recommendation: measure before choosing.* The `file_store` precedent is to refuse an index-backed
surface with no Rust provider (`NATIVE_FILE_VECTOR_INDEX_NOT_READY`) even though the oracle degrades
silently — but memory is enabled by default (`memoryEnabled is not False`), so a blanket refusal
would refuse almost every request and *lower* capability below Python, which the migration rules
forbid. The honest next step is a paired measurement: the oracle's `retrieve_memories` over a corpus
of memories and queries, run once with the index live and once with `search_memories_index` forced
to raise. If the retrieved set or its order never differs, `None` is bounded and can be documented;
if it differs, the surface needs its own refusal or a Rust provider.

## Recommended order

1. **DECISION A** → fix the two patterns and the test that cannot see them. Small, independent of
   the wiring, and a production behaviour change, so it needs explicit approval.
2. Port the memory read half — `prepare_memory_state` (read-only path), `memory_scope_candidates`,
   `memory_scope_label`, `format_memory_context` — with `vector_hits` injected, and run the §4
   measurement inside it.
3. Wire `chat_execution` onto `build_deepseek_request`, with two refusals: forced-search mode and
   the file vector index (already available as `vector_index_not_ready()`).
4. After a `memory` domain is declared: wire the write half.

Steps 1 and 2 are additive and inert. Step 3 is the one that changes what the native route sends,
and it is the point at which the 4.9.2 `chat_completions_fast_path` cutover becomes real.
