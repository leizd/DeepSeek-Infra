# Assembly wiring plan — measured 2026-09-18

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


**Status: Step 3 is landed; exact-head CI has not run against it yet.** The goal — `chat_execution`
building its upstream body through `build_deepseek_request` instead of the thinner body it built
itself — is met: the route now runs the oracle's order (facade translate, validate, bind, message
rules, memory, build) with the four §5 pieces in place. `continuation.md`'s "The swap landed" section
records what finishing it took, including the one place §5's framing was wrong: the **message rules
cannot** run with the validation, because the oracle defines them over expanded content
(`deepseek_client.py:492`), so they need the expander and the expander needs the file cache. They run
inside `with_env`, before the memory read, which is the oracle's order with the workspace bound.

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

### §3a What changed after §2 was repaired, and what "read-only" was hiding

§2 is fixed (`da8c21cf`), so the parenthetical above is **stale in the direction that matters**:
Python's write half works, which makes a Rust write a genuine second writer rather than a write that
happens to do nothing. That makes "read-only" a claim to *verify*, not a default — and it did not
hold. Measured:

- **The route's refusal is not the only reachable write.** `has_explicit_memory_command` inspects the
  *user's text*. The tool loop is the other path, and it executes `forget_memory`
  (`chat_tool_loop.rs` dispatches it; the module doc's unported seven are `browser_*`, `python_eval`,
  `search_files`, `fetch_url`, `create_mindmap`, `create_pptx`, `create_document` — `forget_memory` is
  not among them). Its branch body deletes: `memory.rs:874` calls
  `delete_memories_by_query(cleaned, Some(&scopes), root, clock)`.
- **Demonstrated, not argued**: `tests/chat_execution.rs`'s
  `chat_route_refuses_the_memory_deleting_tool_instead_of_writing_the_store` seeds a memory under the
  test root and has the stubbed upstream answer with a `forget_memory` tool call. Before the refusal it
  got `{"ok":true,"result":{"deleted":1,"query":"dentist","scopes":["global"]},"tool":"forget_memory"}`
  and the file changed — a turn whose text never matched the command grammar, deleting from the store
  Python owns.
- **Now refused**, with the same code the turn-level refusal uses, because it is the same reason:
  `chat_tool_loop.rs` answers `DispatchOutcome::Denied` with
  `NATIVE_MEMORY_WRITE_NOT_OWNED`. `suggest_memory` needs no refusal — it builds a suggestion and
  writes nothing.

So the read-only option is now actually read-only: both reachable write paths on this route refuse,
and the test above holds the second one.

### §3b What the declaration still owes, and what I did not do about it

The barrier is policy, not machinery, and that is measured rather than quoted: `GO_CONTROL_DOMAINS`
is 28 ids with **no** memory identifier (`memory-ish: []`), and `durable_stores` lists the three
planes only — the memory file is in none of them. `authority.py:77` is what denies a Python write,
and it has nothing to deny here. Python's side has **four** write entry points
(`infra/memory/store.py`, `infra/tool_runtime/tools.py:1481`, `web/routes/memory.py`, and
`infra/data/memory.py`'s own command path), so moving `current_owner` to rust is a Python-side
decommissioning, not a one-line edit.

`release/native_runtime_ownership_v1.json` is `status: accepted` with `approved_by: ["leizd"]`, so an
added domain is an **amendment to a contract that carries your signature**. That is why the entry is
written down here with the reasoning that fixed its two free values instead of appearing in a diff
unexplained — and why it was prepared first and applied only on acceptance:

```json
{
  "id": "memory_store",
  "plane": "data",
  "current_owner": "python",
  "target_owner": "rust",
  "cutover": "4.9.2",
  "durable_store": "rust_data"
}
```

Its two free values were judgement calls, not measurements: `cutover: 4.9.2` matches
`chat_completions_fast_path` — the route that carries the write half — and `durable_store: rust_data`
says the file's durable state belongs to the rust data plane, which the schema then requires agree
with `target_owner: rust`. The schema's other rules are satisfied by construction: ids unique,
`current_owner` python, and the 40-domain production convention that a cutover is named (the only
null cutovers are the three `production: false` reference/client domains).

**Landed.** `domains` 43 → 44. Verified rather than assumed: `validate_ownership` accepts the entry,
`scripts/native_runtime_contract.py --check` reports `"ok": true, "domains": 44`,
`scripts/check_zero_python_runtime.py` still passes 8/8, and the ownership plus zero-python tests are
17 passed. Declaring the domain changes **no runtime behaviour** — `GO_CONTROL_DOMAINS` is what
denies a write, and it still has no memory identifier. It is a statement of intent plus the cutover it
will be judged against, and the two refusals in the route remain what keep the interim honest.

**The stakes, so the decision is not abstract**: at the `chat_completions_fast_path` cutover this
route becomes production-authoritative, and memory-write turns then get a `501` for real users —
a capability regression against Python, which your rules forbid. Nor is "let Python keep doing it"
available indefinitely: the contract's `forbidden` list contains `permanent_python_fallback`. So the
cutover is where the handover has to be recorded, and the refusal is what keeps the interim honest
instead of silently dropping the user's instruction.

**The same question is open for reminders, and it is answered the other way.** `.reminders/reminders.json`
is written by Python (`infra/data/reminders.py`) and by this route's `create_reminder` branch, and
`domains[]` has **no** reminder entry either (`reminder-ish domains: []`). So the two undeclared
stores get opposite treatment — memory is refused, reminders are written, with an existing test
asserting that write as intended. One of those is probably wrong, and which one is a decision-B
question rather than a typo; it is flagged here rather than changed, because the write is asserted
by an existing test that was deliberately written that way.

### §3c The handover body: one choke point, and a mode that has to mean it

"Stopping Python's four write entry points" is not four edits. Measured: every write path into the
store funnels through `memory._save_memories_unlocked` — `upsert_memory`, `save_memories`,
`delete_memories_by_query`, `delete_memory_by_id`, `clear_memories`, and the turn-level command
through the first and third — so the ownership gate belongs at that choke point, where a new caller
cannot forget it. The two delete paths reach it only when they would really delete, which is
deliberate: a no-op is not a write, and denying one would be a wider change than the ownership
question asks for.

**Landed**: `authority.RUST_DATA_DOMAINS` names `memory_store`; `_save_memories_unlocked` calls
`assert_python_writer_allowed("memory_store")` before it creates so much as a directory; and
`check_zero_python_runtime`'s `mechanical_writer_denial` gate now checks the data plane as well as
the control plane (`28 Go control domains and 1 Rust data domain`). Verified: 52 tests over the
memory, ownership and gate files; `check_zero_python_runtime.py` PASS 8/8; `ruff check .` and
`mypy .` clean. The new test is **able to fail** — with the gate's domain string changed to a name no
set contains,
`test_every_memory_write_path_is_denied_once_python_is_de_authorized` goes red.

**The mode condition is the part that needed an argument, and it disagrees with the declared
cutover.** The gate fires in `PYTHON_DISABLED` and **not** in `GO_AUTHORITATIVE`: ADR-0049 hands the
control plane over first ("4.9.3 makes Go control domains authoritative one at a time"), and during
that window the data plane can still be Python's — so `GO_AUTHORITATIVE` says nothing about it, and
denying there would break a deployment that is only half-way across. But `PYTHON_DISABLED` is the
ADR's **4.9.4** ("disables Python production authority by default while retaining an explicit
rollback runtime"), while the declaration's `cutover` for `memory_store` is **4.9.2**. So the
mechanism becomes effective at 4.9.4 and the contract says 4.9.2; one of the two should move, and my
reading is that the **declaration** should say 4.9.4, since that is the first version whose mode
actually stops Python. Changing it edits a contract you approved, so it is flagged rather than made.

**Still open after this**: nothing in the route writes memory any more, and nothing in Python can once
the mode flips — but Rust does not write it either. Filling the route's two refusals with the ported
write half is the other half of the handover, and it has to land *with* the mode flip: ADR-0049 leaves
the prior owner authoritative until its cutover gate passes, and does not permit dual writers.

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

1. ~~`AssemblyEnv::from_env` — the nine injected fields.~~ **Landed** —
   `deepseek-gateway::assembly_env::NativeAssembly`, with `with_root` for callers that already
   resolved the workspace (and for tests). Seven unit tests. Two things it settled:
   - **`DEEPSEEK_INFRA_ROOT` unset is an error, not a degrade.** `chat_tool_loop` treats an unset
     root as "no workspace" and lets the data branches report their disabled path, but the
     assembly cannot: the memory store, the file cache and the budget ledger all live under the
     root, and reading them from anywhere else would be a silent divergence. The refusal names the
     variable.
   - **The file-index refusal is a mechanical flag, not a predicate.** `search_file_chunks`
     returns a bare `Vec<i64>` and so cannot refuse itself; rather than trust a duplicated
     predicate about attachments to stay in step with `expanded_message_content`'s real trigger,
     the injected search sets a flag and `with_env` returns it. A caller that sees `true` refuses.
     The guard fires only when the oracle itself would have called the index — measured:
     `select_file_chunk_indices` returns early and never asks the index unless the chunks exceed
     `min(FILE_FULL_CONTEXT_LIMIT, char_budget)`, so a small attachment does **not** trip it, and a
     test pins both sides.
   **Recorded gap:** the *env readers* for the settings are still not ported, so a deployment that
   overrides e.g. `CONTEXT_WINDOW_MESSAGES` would get the oracle's default rather than its own
   value. That has to be closed before this is more than a default-configured deployment.
2. `request_base_url` — `routes/chat.py` passes `request_base_url(request)`, which trusts the
   `Host` header only when `host_without_port(host)` is in `allowed_auth_hosts()`, and otherwise
   falls back to `http://127.0.0.1:{port}`.
3. The file-index refusal, as returned by `NativeAssembly::with_env` (see item 1). Forced search
   needs nothing — see above.
4. The error envelope. `build_deepseek_request` raises `AppError` as
   `{"error": <message>, "code": <code>}` with `AppError.status`, while the route currently answers
   `{"error": {"message": …, "type": "invalid_request_error"}}`. The frozen REST inventory
   (`compat/native-runtime/v1/http/rest_inventory.json`) records the route but no error envelope,
   so this is a compat decision, not a frozen-byte one — and it has to move in the same change
   rather than be discovered afterwards. `tests/chat_execution.rs` asserts on the captured
   upstream body, so it is also the regression net for the swap.
5. A real-upstream integration test, since none of the above proves the assembled body reaches
   DeepSeek correctly — only that it matches the oracle's bytes.
