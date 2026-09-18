# Assembly wiring plan — measured 2026-09-18

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


**Status: plan of record. Step 3 is now unblocked.** The goal is `chat_execution` building
its upstream body through `build_deepseek_request` instead of the thinner body it builds today.

**Progress**: **Decision A is taken and landed** (`da8c21cf`) — the two patterns are repaired, the
negated-forget guard that repair made necessary is in, and the test that could not fail them is
replaced. **Decision C is answered by measurement and Step 2 is landed**: the memory read half is
ported (`memory_scope_candidates`, `memory_scope_label`, `format_memory_context`,
`upsert_memory`, `clear_memories`, `delete_memory_by_id`, `apply_explicit_memory_command`,
`prepare_memory_state`) with `vector_hits` injected, and the paired measurement shows the vector
bonus is **not bounded** — see §4. **Step 3's third prerequisite is landed too**: the provider
for the memory vector index is `deepseek_policy::memory_index`, paired with the oracle byte for
byte (`memory_index_parity_probe`, 64 keys, `turn::differing = 7 of 8`). Decision B (read-only
wiring vs a `memory` domain declaration) is still open, but it gates only the **write** half.
Step 3 — wiring `chat_execution` — is the next slice, and every prerequisite it named is met.

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

## §1 `prepare_memory_state` — the eight functions, now ported

The oracle is `deepseek_infra/infra/data/memory.py:32`. All eight were absent from both Rust crates
(grep over `deepseek-policy/src` + `deepseek-gateway/src`): `prepare_memory_state`,
`apply_explicit_memory_command`, `format_memory_context`, `memory_scope_candidates`,
`memory_scope_label`, `upsert_memory`, `clear_memories`, `delete_memory_by_id`. **All eight are now
in `deepseek-policy::memory`** and byte-verified by the extended probe pair
(`tasks/native-runtime/memory_parity_probe.py` ↔
`rust/crates/deepseek-policy/examples/memory_parity_probe.rs`, 194 keys, md5
`97187819db5aec787776174f6ac3f3d5`); see `docs/MEMORY_STORE.md` for what the corpus pins and the
two defects it found on the way (the falsy content gate and an `OrderedJson` array-order
regression).

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

## §4 The memory vector index has no Rust provider — measured, and not bounded

`retrieve_memories` calls `local_rag.search_memories_index` (collection = memory) inside a
`try/except Exception` that swallows everything into `{}` — but that is not evidence the index is
never used. `local_rag.search` (`local_rag.py:780-809`) never raises: it returns `[]` when
`LOCAL_RAG_ENABLED` is false, when the query is blank, or on any exception, and it has a **lexical
fallback** (`db_ready()` → `vector_table_ready` → `_search_db`). So hits — and the
`score += max(1, score // 10)` bonus at `memory.py:467` — are real when the index exists.

`deepseek-rag` is a crate of **pure primitives** (chunk validation, citation formatting, query
parsing, scoring, vector similarity, the binary rank protocol, index metadata) with **no store and
no collection** — measured from its public surface, not assumed. So `VectorHits` had no provider
to inject, and passing `None` would drop a bonus the oracle actually applies.

**DECISION C — measured, not chosen** (`tasks/native-runtime/memory_vector_bonus_probe.py`, the
real oracle, paired runs over a corpus). `LOCAL_RAG_ENABLED` defaults to true and the embedding
provider to `hash`, so the index is live **offline** in a default deployment:

| observation | result |
| --- | --- |
| run A executed twice | identical — the difference is the index, not flakiness |
| queries whose retrieved **order** differs | **7 of 8** |
| queries whose retrieved **set** differs | same 7 — `m-long` surfaces only through the bonus (`score 37 → +3`) |

So `None` is **not** bounded: it changes both the order and the membership of the memory context
the assembled request carries. The `file_store` precedent (refuse an index-backed surface with no
Rust provider) cannot be copied verbatim either — memory is enabled by default, so a blanket
refusal would refuse almost every request and *lower* capability below Python, which the migration
rules forbid.

**Recommendation — now implemented.** The memory index read path is
`deepseek_policy::memory_index` (hash embedding + cosine + BM25 over `rag_items`,
**read-only**; Python remains the writer until a `memory` domain is declared), paired with
the oracle by `memory_index_parity_probe.py` ↔ `examples/memory_index_parity_probe.rs`:
**64 keys, byte-identical (LF md5 `16494f98…`)**. The wiring injects it and does **not** pass
`None`.

One narrow refusal remains, and it is not the blanket one this plan rejected: when the
`rag_vec` table exists, the oracle blends `vec0` distances this process cannot evaluate
(`sqlite-vec` is a Python-loaded extension that is not a dependency of this repository),
so the read returns `MemoryIndexError::VectorTableNotReadable` rather than serving the
cosine fallback. That fires only in a deployment that installed the optional extra, so
the default path is unaffected — the opposite of the "refuse almost every request"
outcome the `file_store` precedent would have produced here.

## Recommended order

1. ~~**DECISION A** → fix the two patterns and the test that cannot see them.~~ **Landed**
   (`da8c21cf`).
2. ~~Port the memory read half — `prepare_memory_state` (read-only path),
   `memory_scope_candidates`, `memory_scope_label`, `format_memory_context` — with `vector_hits`
   injected, and run the §4 measurement inside it.~~ **Landed** — the eight functions are ported and
   byte-verified (194-key probe, `97187819…`), and §4 is answered: the bonus is not bounded.
3. ~~A Rust provider for the memory index read path (or the narrow refusal).~~ **Landed** —
   `deepseek_policy::memory_index`, paired and byte-identical (64 keys, `turn::differing = 7 of 8`).
4. **Landed** — `openai_facade::openai_to_internal_payload`, the OpenAI→internal translation the
   route has to start from (§5), paired and byte-identical (56 keys).
   **Next:** wire `chat_execution` onto `build_deepseek_request`, with the forced-search mode
   refusal and the file vector index refusal (already available as `vector_index_not_ready()`);
   §5 lists the five pieces that change has to carry.
5. After a `memory` domain is declared: wire the write half.

Steps 1-4 are additive and inert. Step 5 — the wiring — is the one that changes what the native
route sends, and it is the point at which the 4.9.2 `chat_completions_fast_path` cutover becomes
real.

## §5 The front of the wiring — measured, and it is a divergence, not just a gap

This plan framed the wiring as a *fidelity* improvement: the native body has "no dynamic context,
no memory state, no clock, no assembled system prompt". Measuring the oracle's route shows
something stronger, and it is a compatibility problem rather than an omission.

`POST /v1/chat/completions` is a **facade**. `routes/chat.py:65` calls
`openai_to_internal_payload(body, local_base_url=…)` (`openai_api.py:29`), and only then
`resolve_provider(model).chat(payload)` → `call_deepseek` → `prepare_deepseek_call` →
`build_deepseek_request`. The translation is therefore part of the public contract, and it is
deliberately narrow:

| | forwarded |
| --- | --- |
| `model` | yes, through `MODEL_ALIASES`, after `body.get("model") or settings.default_model` |
| `messages` | yes, **verbatim and unvalidated** — `build_deepseek_request` is what validates them |
| `stream` | yes, Python truthiness, so the string `"false"` is **true** |
| `thinkingEnabled` | **set to `False`** — "deterministic content only, no reasoning tokens" |
| `localBaseUrl` | **set** from `request_base_url(request)` |
| `temperature` | yes, but only for a real number (`isinstance(t, (int, float)) and not isinstance(t, bool)`) |
| `tools`, `tool_choice`, `max_tokens`, `top_p`, `reasoning_effort`, `thinking` | **dropped** |

The last row is the divergence. The native route built its body straight from the OpenAI request
through `request_preparation::prepare_chat_request`, which **forwards** `tools`, `tool_choice`,
`max_tokens`, `top_p` and `reasoning_effort` — all of which the oracle drops — and never sets
`thinkingEnabled` or `localBaseUrl`, which the oracle does. On a public route that is a
compatibility break, not a thinning: a client sending `tools` gets the catalog-tool plus
client-tool mixture the oracle refuses to build, and `temperature` is applied unconditionally
rather than only when `build_deepseek_request` decides the model tier warrants it.

**Landed (`openai_facade`)**: the translation is ported and paired with the real Python function —
`openai_facade_parity_probe`, **56 keys, 12 204 chars, byte-identical**, covering the six forwarded
fields, the seven dropped ones, the falsy-model set, alias normalization (case, underscores,
spaces, unknown, a truthy bool), the `stream` truthiness table, the `temperature` type table and
the two refusals. Seven unit tests pin the same behaviour in-crate.

**Landed (`native_chat`)**: the composition itself — `call_deepseek` → `prepare_deepseek_call`'s
order, which is *validate* → *memory* → *build*. `native_chat_composition_parity_probe`, **15
cases, 298 431 chars, byte-identical** (body, diagnostics, tool names and api key for each). Three
unit tests. The two-step API (`prepare_openai_chat` then `assemble_openai_chat`) exists so the
"validate before memory" ordering is visible at the call site rather than hidden inside a callback.

Three things the composition measurement settled, each of which **removes** work this plan had
assumed:

- **`forced_search_mode` is structurally unreachable on this route.** `search_mode` is
  `payload.get("searchMode") or "auto"` and the facade never forwards `searchMode`, so the
  prefetch branch in `prepare_deepseek_call` is dead. No refusal is owed; adding one would
  introduce a refusal the oracle cannot perform.
- **`web_search` is absent from the composed tool list.** `tools_for_payload` adds it only when
  `search_tool_enabled(payload)` sees `searchEnabled is True`, another field the facade does not
  forward. The route gets the 26-tool catalog minus the search tool — pinned by a unit test.
- **The file vector index refusal is narrower than "has attachments".**
  `expanded_message_content` returns early unless a message carries a non-empty `attachments`
  list, and `search_file_chunks` is consulted only for an attachment with a non-empty `file_id`.
  So only *file* attachments can need the index, not every attachment.

**And one defect it found**, which is the reason the composition needed its own probe rather than
trusting two green probes to compose: `request_assembly::NESTED_ORDERS` had
`("messages", &["role", "content"])`, so the body renderer emitted a tool result as
`role, content, tool_call_id` and a call entry as `function, id, type`, where the oracle writes
`role, tool_call_id, content` and `id, type, function`. Values were identical; only key order
differed. The assembly probe's corpus has no tool-role turn, so it could not see this. Fixed by
making the message order a superset (`role, tool_call_id, content, tool_calls`) that serves all
three oracle message shapes — absent keys are skipped — plus a `tool_calls` entry. The assembly
probe is re-run and unchanged (1 946 077 chars).

**Not landed**: the route still does not call any of it. That is the next slice, and it needs, in
this order:

1. `AssemblyEnv::from_env` — the nine injected fields. Every settings struct has an oracle-matching
   `Default` (`ModelRouterSettings`, `BudgetSettings`, `ContextTaintSettings`,
   `ContextManagerSettings`, `ContextEngineSettings`), the ledger comes from `budget_store` plus
   `LedgerDeps`, the clock from `local_clock::local_now`, and the expander from
   `attachment_context::expanded_message_content` over a `FileContextDeps` built from `FileStore`.
   **Recorded gap:** the *env readers* for those settings are not ported, so a deployment that
   overrides e.g. `CONTEXT_WINDOW_MESSAGES` would get the oracle's default rather than its own
   value. That has to be closed before this is more than a default-configured deployment.
2. `request_base_url` — `routes/chat.py` passes `request_base_url(request)`, which trusts the
   `Host` header only when `host_without_port(host)` is in `allowed_auth_hosts()`, and otherwise
   falls back to `http://127.0.0.1:{port}`.
3. The file-index refusal, taken **before** the expander runs (`FileContextDeps::search_file_chunks`
   returns a bare `Vec<i64>` and so cannot refuse itself), on the requests whose messages carry a
   file attachment, using the available `file_store::vector_index_not_ready()`. Forced search needs
   nothing — see above.
4. The error envelope. `build_deepseek_request` raises `AppError` as
   `{"error": <message>, "code": <code>}` with `AppError.status`, while the route currently answers
   `{"error": {"message": …, "type": "invalid_request_error"}}`. The frozen REST inventory
   (`compat/native-runtime/v1/http/rest_inventory.json`) records the route but no error envelope,
   so this is a compat decision, not a frozen-byte one — and it has to move in the same change
   rather than be discovered afterwards.
5. A real-upstream integration test, since none of the above proves the assembled body reaches
   DeepSeek correctly — only that it matches the oracle's bytes.
