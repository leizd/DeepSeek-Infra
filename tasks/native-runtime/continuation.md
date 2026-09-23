# Native runtime continuation record

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

This file is the session handoff. Historical plans, checkboxes, VERSION, and
`release/native_runtime_5_0_evidence_v1.json` are not completion evidence.
The capability matrix is [`migration-matrix.md`](migration-matrix.md).

## Current continuation checkpoint — 2026-09-22 memory probe isolation

Investigated the two `memory` differences recorded by `c9606a9a`, on base HEAD
`1e82d37c`. Python's supposedly absent `local_rag` was importable with dependencies
installed, so it used a live index while Rust supplied `None`; Python saves also
replaced the host memory index. A real SQLite fixture reproduced exactly
`state::remember` and `state::scoped`; forcing the missing dependency gave 194/194
equal values. The fix confines missing-RAG imports to the extracted namespace and
removes `memory` from `KNOWN_DIVERGENCES`; production runtime code is unchanged.

Validation: three regressions failed before the fix and pass afterward; 32 focused
memory tests, Ruff and mypy pass. Both Rust examples were rebuilt with `--locked`;
194 memory keys and 64 live-index keys match byte for byte in this build. The live
index still changes 7/8 queries, and the fixed probes leave the host index unchanged.
An isolated rerun of all 38 older pairs reports 37 passes, zero unexpected failures
and one remaining known divergence (`store`, fixture clock); 10 are byte-identical.
The legacy `tests/test_memory.py` had the same index-isolation leak: it now uses
`tmp_settings` and gives child writers the temporary root before import. The 32-test
rerun verifies that host memory and index hashes remain unchanged.
Details: [`docs/MEMORY_STORE.md`](../../docs/MEMORY_STORE.md#probe-isolation-correction--2026-09-22).
This closes the probe discrepancy, not the remaining production migration work;
readiness is unchanged. Changes from this investigation are uncommitted.

## Current continuation checkpoint — 2026-09-21 the paginated file reader is wired

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

Implemented:

- **`deepseek-policy::file_routes`** gains `file_reader_window`, `file_chunk`,
  `reader_positive_int`, `reader_file_payload` and `reader_chunk_payload` — the
  paginated reader the frontend scrolls a long extraction with. The rules are the
  oracle's: 1-based display indices, the 12-chunk cap, a start past the end clamped to
  the last chunk, an empty list reported as an all-zero window, a non-object entry
  skipped by both the payload list and the end index, and a per-field `400`
  (`Invalid reader start` / `Invalid reader count` / `Invalid chunk index`).
- **`POST /api/file-reader`** and **`POST /api/file-chunk`**
  (`deepseek-gateway::file_reader_route`) registered ahead of the Go `/api/*`
  catch-all. The falsy-value defaults (`chunkStart or 1`, `chunkCount or 6`) are applied
  at the route, because that `or` is part of the contract.
- `file_routes_parity_probe.py` grew from 103 to **201 cases**: 56 window shapes across
  seven cached indexes and 42 chunk lookups, compared against the oracle's own
  `file_reader_window` and against the `/api/file-chunk` body transcribed from
  `web/server.py`. The oracle's functions run unmodified — the probe repoints
  `rag_files.FILE_CACHE_DIR`, which is the name `files.py` actually reads (it imports the
  constant **by value** at module load, so patching `config.FILE_CACHE_DIR` alone does
  nothing; that was the first, failing, attempt).

Evidence:

- `tasks/native-runtime/file_routes_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/file_routes_parity_probe.rs`: **PASS** over
  **201 cases**. Report: `artifacts/file-routes-parity.json`.
- `cargo test -p deepseek-gateway --test file_reader_route`: **6 PASS** through
  `create_production_app`.
- `deepseek-policy` **524** unit tests, 0 failures (three new reader tests);
  `deepseek-gateway` 190 unit + integration, 0 failures. `cargo fmt --all --check`
  clean; `cargo clippy --all-targets` reports no finding in any file this slice touched;
  ruff and mypy pass the probe.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next, in order: `/api/file-page-text` (needs `normalize_extracted_text` and
`page_texts_for_cache`), `/api/file-text` (multipart upload + extraction) and
`/api/project-files`; then the skills family, `/api/workspace/*`, the diagnostics status
blocks, search prefetch and edge inference for `/api/chat`, and the browser staging 4.
`/api/file-page-image`, `/api/file-page-layout` and `/api/file-page-search` need a
PDF/image renderer, which is a separate decision.

## Previous continuation checkpoint — 2026-09-21 `/api/file-source` is wired, and the web `truthy` helper was wrong

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

Implemented:

- **`deepseek-policy::file_routes`**: `clean_filename`, `content_disposition_header`,
  `original_file_media_type` and `cached_file_source` — the four helpers
  `web/routes/files.py` needs. The RFC 5987 header is built with Python's `quote` safe
  set (which keeps `/`), and the media-type ladder follows the oracle's order.
- **`GET /api/file-source`** (`deepseek-gateway::file_source_route`): the original
  uploaded bytes, with `X-Content-Type-Options: nosniff`, the oracle's disposition
  header, `Cache-Control: no-store` and the media type from the cached index. A missing
  source is `410 file_index_expired`; a malformed id is `400 invalid_payload`.
- **`deepseek-policy::core_utils::web_truthy`**, and a real defect fixed with it. The
  web layer's `truthy` is `str(value or "").strip().lower() in {"1","true","yes","on"}`
  — a **string parse**, not Python truthiness. The `/api/download` route shipped using
  `python_truthy`, so `?inline=false` rendered the SVG in place where the oracle
  downloads it, **and its test asserted that wrong answer**. Both are corrected. This is
  the first defect found by re-reading the oracle rather than by a probe or a test.

Evidence:

- `tasks/native-runtime/file_routes_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/file_routes_parity_probe.rs`: **PASS** over
  **103 cases** — 26 filenames through `clean_filename` and both dispositions (CJK,
  emoji, quotes, both separators, percent/plus/hash/query characters, hidden files), 21
  cached-file shapes through `original_file_media_type`, and the 179/180/181/400-character
  caps. Report: `artifacts/file-routes-parity.json`.
- `cargo test -p deepseek-gateway --test file_source_route`: **6 PASS** through
  `create_production_app` — the original bytes rather than the index JSON, the `download`
  string parse (six falsy and four truthy spellings), the media-type ladder, `410` for a
  missing source and `400` for a bad id, a project-scoped read from
  `.projects/{id}/files`, and the auth boundary.
- `cargo test -p deepseek-gateway --test download_route`: **6 PASS** with the corrected
  `inline` rule (`false`, `0`, `no` and an empty value all download; `1`, `true`, `yes`
  and `on` render in place).
- `deepseek-policy` **521** unit tests, 0 failures. `cargo fmt --all --check` clean;
  `cargo clippy --all-targets` reports no finding in any file this slice touched; ruff
  and mypy pass the new probe. One self-inflicted regression was caught by clippy and
  fixed: a stray edit had merged a doc comment into a `#[test]` attribute, leaving the
  test compiled but not run.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next, in order: `/api/file-reader`, `/api/file-chunk`, `/api/file-text` and
`/api/project-files` (the reader window is ported; the routes are not); then the skills
family, `/api/workspace/*`, the diagnostics status blocks, search prefetch and edge
inference for `/api/chat`, and the browser staging 4. `/api/file-page-*` needs a
PDF/image renderer, which is a separate decision.

## Previous continuation checkpoint — 2026-09-21 `/api/chat` is wired, and its diagnostics block is the oracle's

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

This round finished the terminal event: `/api/chat`'s `done.diagnostics` was a
hand-written `{"tools": {...}}` placeholder, and it is now the oracle's own helper chain.

Implemented:

- **`deepseek-policy::chat_diagnostics`**: `diagnostics_with_tools` (count plus the
  sorted, deduplicated names), `diagnostics_with_usage` (`cacheHitTokens`,
  `cacheMissTokens`, `cacheHitRate`) and `diagnostics_with_search` (the round and result
  counts, **absent** when there was no search — `if search_data:` is a truthiness test,
  not a presence test). `search_round_count` is also here.
- The route folds tools → search → usage, which is the oracle's order, and passes the
  result to `accumulator.done(..)`.
- **`round(x, 1)` was ported twice, and the first version was wrong.** The direct
  translation — `(value * 10).round_ties_even() / 10.0` — returns `1.0` for `1.05`,
  because `1.05 * 10` is exactly `10.5` and half-to-even rounds that to `10`, while
  Python returns `1.1` because the stored double is *above* its decimal tie. Rust's
  `format!("{value:.1}")` uses the same correctly-rounded decimal algorithm Python's
  `round` does, and agrees over the whole tie corpus. The unit test caught this before
  the probe did.

Evidence:

- `tasks/native-runtime/chat_stream_events_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/chat_stream_events_parity_probe.rs`: **PASS**
  over 21 events (byte-identical text), 11 usage merges, 8 streamed tool-call
  sequences, **4 tool-diagnostic cases, 17 usage-diagnostic cases over the `round(x, 1)`
  tie corpus, 5 search-round counts and 6 search-diagnostic cases** — every one compared
  against the imported oracle functions (`diagnostics_with_tools`,
  `diagnostics_with_usage`, `diagnostics_with_search`, `_search_round_count`).
- `cargo test -p deepseek-gateway --test chat_ndjson_route`: **7 PASS**, with the
  terminal event's diagnostics now asserted field by field — including that a turn with
  no search carries **no** `searchRoundCount` key.
- `cargo test -p deepseek-policy --lib`: **516** unit tests, 0 failures.
  `cargo fmt --all --check` clean; `cargo clippy --all-targets` reports no finding in
  either new file; ruff and mypy pass the probe.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image. The gateway-attempt, semantic-cache, cost and trace diagnostics
  blocks are still absent, because their state is not in this route yet.

Next, in order: search prefetch and edge inference for `/api/chat`; then the file/upload
and page-render family, the skills family, `/api/workspace/*`, the diagnostics status
blocks whose Python status functions are not yet ported, and the browser staging 4.

## Previous continuation checkpoint — 2026-09-21 `/api/chat` is wired on the native edge

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

The frontend's streaming entry was the largest remaining `503`. It is now served by
`deepseek-gateway::chat_ndjson` over the protocol ported last round.

Implemented:

- **`deepseek-gateway::chat_ndjson`**: `POST /api/chat` registered ahead of the Go
  `/api/*` catch-all. It takes the **internal** payload (the route injects
  `localBaseUrl`), validates it with the ported `validate_deepseek_payload`, runs the
  message rules and the memory state through `NativeAssembly`, opens the upstream with
  `stream: true`, and turns each SSE delta into an NDJSON line as it arrives. The
  tool-round loop is the OpenAI route's, with `RoundDecision`, `append_tool_exchange`
  and `force_final_answer_without_tools` — so a `browser_*`, `search_files`,
  `create_document` or `reminders` call behaves identically on both routes.
- **Memory suggestions are wired on this route and not the other.** `/api/chat` is the
  one protocol with a `memorySuggestions` channel, so `ToolRoundExecutor` gained
  `run_round_with_suggestions` and `WorkspaceBundle` gained `view_with`; the callback is
  `'static` because the round runs under `spawn_blocking`, and the route closes over an
  `Arc<Mutex<Vec<Value>>>`. `WorkspaceContext::on_memory_suggestion` and
  `memory::suggest_memory` are now `Send + Sync`.
- **Three branches are refused, not degraded.** `agentMode`, the model-router cascade
  and a forced `searchMode` return `501 NATIVE_CHAT_BRANCH_NOT_READY` **before any
  upstream call**. Their producers are unported, and serving a thinner path would look
  like success. The forced-search refusal is reachable here even though it is not on
  `/v1/chat/completions`: the OpenAI facade never forwards `searchMode`, but this route
  takes the internal payload, so it arrives intact and the oracle's prefetch branch
  would run.

Evidence:

- `cargo test -p deepseek-gateway --test chat_ndjson_route`: **7 PASS** through
  `create_production_app` against a scripted SSE upstream — the oracle's event sequence
  and order (`reasoning`, `content`, `content`, `done`), the accumulated `done` totals,
  the `length` truncation note before `done`, agent mode and forced search refused with
  the upstream called **zero** times, the no-user-turn `400`, an upstream failure as an
  HTTP error rather than a `200` stream, and the production auth layer. Every line is
  also asserted to be compact JSON.
- `cargo test -p deepseek-policy -p deepseek-gateway --lib --tests`: 0 failures
  (`deepseek-policy` 511 unit, `deepseek-gateway` 190 unit + integration).
  `cargo fmt --all --check` clean; `cargo clippy --all-targets` reports no finding in
  either new file; frontend `vitest src/api/chatStream.test.ts` 4 passed.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image. The `done` event's `diagnostics` carries only the tool summary;
  the oracle's full chain (gateway attempts, search, semantic cache, usage, cost,
  trace) is not ported.

Next, in order: the `done` diagnostics chain and search prefetch for `/api/chat`; then
the file/upload and page-render family, the skills family, `/api/workspace/*`, the
diagnostics status blocks whose Python status functions are not yet ported, and the
browser staging 4.

## Previous continuation checkpoint — 2026-09-21 the `/api/chat` NDJSON protocol is ported

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below is uncommitted. The goal
remains active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY`
with `exact_head: null`.

`/api/chat` is the frontend's streaming entry and the largest remaining public route.
It is not one slice: the oracle's `stream_deepseek` runs search prefetch, semantic
cache, edge inference, gateway retries with a scheduler lease, a streaming tool-round
loop, budget accounting, trace spans, agent mode and the model-router cascade. This
round ported the **protocol** — the part every one of those branches writes through —
so the route work that follows has a verified encoder instead of an assumed one.

Implemented:

- **`deepseek-policy::chat_stream_events`**: the seven-event vocabulary
  (`system_note`, `search`, `reasoning`, `content`, `memory_suggestion`, `error`,
  `done`), `encode_stream_event` (compact JSON + `\n`, `ensure_ascii=False`), the
  `ChatStreamAccumulator` that grows `content`/`reasoning`/`usage` while emitting each
  delta, `merge_usage_totals`/`usage_int`, and the streaming tool-call merge
  (`merge_stream_tool_call_deltas` / `finalized_stream_tool_calls`).
- **`RawJson`**: `usage` is carried as pre-rendered bytes rather than a
  `serde_json::Value`. This crate compiles `serde_json` **without** `preserve_order`,
  so a `Value` map is key-sorted, and the oracle writes `usage` in the provider's
  insertion order. The parity probe caught the reordering on `done_full`
  (`completion_tokens` before `prompt_tokens`) — the same class of bug the OpenAI SSE
  encoder in `chat_stream.rs` avoids by building its frames by hand.

Three defects were found by reading the oracle and by the probe, not by inspection:

1. **`merge_usage_totals` was ported wrong first.** The oracle sums only the five token
   counters in `USAGE_SUM_FIELDS` (each with a camelCase alias), reads them through
   `usage_int` (`max(0, int(raw))`), and **drops** every other key of the round. The
   first port summed all numeric fields and kept non-numeric ones.
2. **The `done` envelope reordered `usage`** (see `RawJson` above).
3. **A negative tool-call index is a legal key.** The oracle does `int(index_value)`
   with no non-negativity check, so `-4` is stored as `-4`, sorts first, and its
   placeholder id is `call_-3`. The first port filtered negatives out, producing
   `call_1`/`call_2` where the oracle produced `call_-3`/`call_1`. The parity probe
   measured exactly that.

Evidence:

- `tasks/native-runtime/chat_stream_events_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/chat_stream_events_parity_probe.rs`:
  **PASS**. 21 events compared **as text** (which is the contract), including CJK,
  emoji, quotes, tabs, a null id, an empty `content`, a scalar `search`, a
  non-mapping `memory_suggestion`, and the two `done` shapes; 11 usage merges; 8
  streamed tool-call sequences (the raw accumulator **and** the finalized list, so a
  difference in a placeholder id or an appended argument shows up even when the
  finalizer would drop the entry); and the accumulator's totals. The usage/tool-call
  sections are compared as parsed JSON because the Rust probe round-trips them through
  `serde_json::Value`, whose maps are key-sorted — a representation difference, not a
  value one, and the reason the event bytes are compared as text instead.
  Report: `artifacts/chat-stream-events-parity.json`.
- `cargo test -p deepseek-policy --lib chat_stream_events`: **11 PASS** (the byte
  encodings, the always-present `done` fields, the accumulator, the falsy-id rule, the
  memory-suggestion spread — where a suggestion's own `type` wins *and keeps the first
  position*, which is what a Python dict does — the five-counter usage merge, the
  index-order merge, the missing/unparseable index rule, the empty-fragment rule, the
  no-name drop, and the negative index).
- `deepseek-policy` **511** unit tests, 0 failures; `cargo fmt --all --check` clean;
  `cargo clippy --all-targets` reports no finding in the new file (the three
  `deepseek-policy` warnings are the same pre-existing files as previous rounds);
  ruff and mypy pass the new probe.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next, in order: wire the `/api/chat` route for the paths this protocol covers
(non-agent, non-cascade, non-edge, no search prefetch) with the others refused
explicitly rather than silently degraded; then `search_for_client` (whose key order
matters for the `search` event), the file/upload and page-render family, the skills
family, `/api/workspace/*`, and the browser staging 4.

## Previous continuation checkpoint — 2026-09-21 two public `/api` routes stop being 503s

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062`. Everything below (and the browser-engine
checkpoint that follows) is uncommitted. The goal remains active and
`release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY` with
`exact_head: null`.

The evidence file's first blocker says the public edge is incomplete, and the concrete
measurement behind that is the `/api/*` catch-all: any route not registered natively
falls through to the Go proxy, which answers `503 GO_CONTROL_PROXY_NOT_READY` when no
Go control plane is configured. Three of the frontend's routes were in that state and
are now native.

Implemented:

- **`POST /api/title`** — the conversation-title route the frontend calls after the
  first exchange. `deepseek-policy::title` carries the pure half (the prompt, the
  request body, the truncation limits, `_sanitize_title`, `format_upstream_error`, and
  the per-key rate window), and `deepseek-gateway::title_route` carries the transport
  and the oracle's error envelopes. Registered ahead of the `/api/*` catch-all.
- **`GET /api/download`** — the `downloadUrl` that `create_document`/`create_pptx`/
  `create_mindmap` hand back. `generated_files::download_descriptor` was the only
  missing piece (`resolve_generated_file` was already ported); `download_route` reads
  the bytes and writes the two headers. The id rule stays in the policy crate, so the
  traversal boundary cannot be forgotten at the route.
- **`GET /api/taint`** — the context-taint status block. `context_taint` was already a
  complete port; this route only had to be registered. It also needed
  `ContextTaintSettings::from_env`, which now mirrors the oracle's reader including the
  `(4, 200)` clamp on `TAINT_MAX_SEGMENTS`.
- **Measured correction:** `chat_execution::DEFAULT_UPSTREAM_TIMEOUT_SECONDS` read
  `120`; the oracle's default is `180` (`_env_int("DEEPSEEK_TIMEOUT_SECONDS", 180)`).
  Nothing depended on the wrong value — the title route caps its own call at
  `min(timeout, 20)`, which is what hid it — so it is corrected rather than recorded as
  a divergence.
- **Test-race fix:** the browser session registry is process-wide (as the oracle's
  `_sessions` dict is), so the browser unit tests could interleave a
  `reset_sessions_for_tests` between another test's session creation and its first
  action. The engine-backed cases added earlier made that fail intermittently with
  `Browser session not found`; the tests now serialize on a registry mutex. Three
  consecutive full `deepseek-policy` runs are green.

Evidence:

- `tasks/native-runtime/title_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/title_parity_probe.rs`: **PASS**, seven sections
  compared against the imported oracle — the system prompt, 26 sanitiser cases, 9
  truncations, 6 request bodies, 8 `titleModel` selections, 7 upstream responses, 7
  upstream-error bodies. Report: `artifacts/title-parity.json`.
  **The probe found a real bug on its first run**:
  `choices[0].message.content == null` returned `"None"` instead of `""`, because the
  port called `str()` without the oracle's `or ""`.
- `cargo test -p deepseek-gateway --test title_route`: **7 PASS** through
  `create_production_app` against a scripted loopback upstream that records the bytes it
  received — the oracle's body and headers, the blank-`userMessage` early return (zero
  upstream calls), the missing-key `400`, an upstream `503` capped to `502` with the
  provider's own message, the 13th call in the window as `429` with the upstream called
  exactly 12 times, and the production auth layer answering `401`.
- `cargo test -p deepseek-gateway --test download_route`: **6 PASS** — the bytes on the
  wire equal the bytes on disk for all five registered types, the four `inline`
  combinations (including `inline=false` being *truthy*, which is Python), a traversal
  attempt that leaks nothing from outside `.generated/`, the unknown-id `404` envelope,
  and the auth boundary. `generated_files` unit tests pin the oracle's six MIME/name
  pairs.
- `cargo test -p deepseek-gateway --test data_routes`: **28 PASS**, including the two
  new `/api/taint` cases and its auth boundary.
- `cargo fmt --all --check` clean; `cargo clippy --all-targets` reports **no** finding
  in any file this slice touched (the three `deepseek-policy` and five
  `deepseek-gateway` warnings are the same pre-existing files as last round).
  Python `pytest -k title`: 12 passed; `pytest -k "download or generated"`: 67 passed.
  Rust: `deepseek-policy` **500** unit, `deepseek-gateway` **190** unit + integration.
- Still dirty-workspace local qualification. Not exact-head CI, not Evidence Assembly,
  not a packaged image.

Next: `/api/chat` (NDJSON) is the frontend's streaming entry and is still a 503; then
the skills registry/runner family, the file/upload and page-render family
(`/api/file-source`, `/api/file-page-*`, `/api/file-reader`, `/api/file-chunk`,
`/api/project-files`, `/api/file-text`), the `/api/workspace/*` backup/DR surface, and
the diagnostics status blocks whose Python status functions are not yet ported. The
browser staging 4 (image + audit entry + CI lane + revision pin) also remains open.

## Previous continuation checkpoint — 2026-09-21 the browser engine is real, end to end

Same branch `codex/native-a2a-stream-continuation`; HEAD is still
`bae68f0b64067708aac81aaed18492c3917a1062` (the committed ADR-0050 stage 1). All work
below is uncommitted; no commit, push, merge or cleanup was performed. The goal remains
active and `release/native_runtime_5_0_evidence_v1.json` still says `NOT_READY` with
`exact_head: null`.

Implemented (ADR-0050 stages 2 and 3):

- **The CDP engine** (`rust/crates/deepseek-browser/src/engine.rs`, new): spawns a
  headless Chromium with `--remote-debugging-port=0`, reads the DevTools socket off
  stderr, attaches one page in flat mode, and drives every declared action over CDP —
  `open_url` (`Page.navigate` + `Page.domContentEventFired`), `read_page`
  (`innerText` / `documentElement.outerHTML` with the doctype prepended / `title`),
  `extract_links`, `screenshot` (`Page.captureScreenshot`, element clip via
  `DOM.getBoxModel`), `click` (a real `Input.dispatchMouseEvent` at the element's
  viewport centre), `type_text`, `select`, `scroll`, `download`
  (`Browser.setDownloadBehavior` + `Browser.downloadProgress`). `tokio-tungstenite`
  is the one new dependency family; the workspace comment says why a hand-rolled frame
  layer was rejected.
- **The sidecar** (`src/sidecar.rs`): one browser context per session id, the
  oracle's timeouts, no durable store, and the `CloseSession` RPC — added to
  `proto/browser/v1/browser.proto` and regenerated, because without it a closed
  session left a Chromium and a profile directory behind for the engine's lifetime.
- **The seam** (`deepseek-policy::browser_engine`, new): the policy crate declares what
  it needs (`BrowserEngine`), the gateway implements it. The safety gate and the session
  registry stay in the policy crate and run either way; only the controller changes, and
  `controller_kind_for` now selects the engine exactly the way the oracle selects
  Playwright (an engine that answers `Status` with `available: true`).
- **The client** (`deepseek-gateway::browser_engine_client`, new): the generated tonic
  client on a dedicated OS thread with its own single-threaded runtime, because the
  policy seam is synchronous (it runs under `spawn_blocking`) and the client is not. The
  worker is detached and stops when the channel closes. `ToolRoundExecutor` attaches the
  engine to the tool loop; with no engine listening, `browser_*` is the static-controller
  deployment it has always been.
- `deepseek-policy::browser` gains `execute_browser_action_with_engine` and shapes the
  engine's answers into the oracle's per-action envelopes; the original
  `execute_browser_action` is a thin wrapper, so every existing caller and probe is
  unchanged.

Evidence:

- `tasks/native-runtime/browser_engine_parity_probe.py` ↔
  `rust/crates/deepseek-browser/examples/browser_engine_parity_probe.rs`:
  **PASS**, six fixtures, `url`/`title`/`text`/`links` identical, **0 differing HTML
  bytes** after whitespace collapse. Recorded divergences: the download file name
  (oracle: `sample-report.html`; CDP `allowAndName`: a GUID), the screenshot byte
  length (12925 vs 17284 — both PNG), and a missing element (oracle raises its timeout;
  the engine answers `not_found`/404). Report: `artifacts/browser-engine-parity.json`.
- `cargo test -p deepseek-browser --test engine_live`: a real Chrome, every declared
  action, including that a selector which is not in the document is `element_not_found`
  rather than a click at the origin. Gated on `DEEPSEEK_BROWSER_CHROMIUM`.
- `cargo test -p deepseek-gateway --test browser_engine_e2e`: **10 PASS** across the
  real process boundary (gateway client → gRPC → sidecar process → CDP → Chromium →
  loopback HTTP fixture), including the safety gate refusing a private host *before* the
  engine is reached, `not_found` for an unknown session, and `close_session` removing
  both the registry entry and the engine's profile directory.
- Three real defects were found and fixed by these tests rather than by inspection:
  `Page.getLayoutMetrics` was sent without a session id (the browser answers
  `-32601 wasn't found`), the document read dropped the doctype that `page.content()`
  serialises, and `click` returned the pre-click URL.
- Pinned-toolchain checks: `cargo fmt --all --check` clean; `cargo clippy
  --all-targets` reports **no** findings in any file this slice touched (the three
  `deepseek-policy` and five `deepseek-gateway` warnings it does report are pre-existing
  files — `memory_schema.rs`, `presentations.rs`, `workspace_schema.rs`, `a2a_control.rs`,
  `control_proxy.rs` — and `sidecar.rs:207` is the pre-existing `admit`); tests:
  `deepseek-browser` 11, `deepseek-policy` 491, `deepseek-gateway` 190 + 55 integration,
  0 failures; `scripts/native_codegen.py --check` and
  `scripts/check_native_contract_parity.py` pass (47 domains / 9 proto / 14 outputs).
- This is dirty-workspace local qualification on Windows against the machine's Chrome.
  It is **not** exact-head CI, not Evidence Assembly, and not a packaged image: staging 4
  (a Chromium-carrying image, the `scripts/check_native_images.py` entry, a CI lane, and
  a Chromium revision pin) is open, so no release claim is made.

Next: staging 4 for the browser (image + audit entry + CI lane + revision pin); then the
remaining public business APIs, the authoritative Go controller and Rust
worker/provider recovery, and native service/desktop/Android packaging from the matrix.

## Previous continuation checkpoint — 2026-09-20 native project reads

The live workspace advanced externally to HEAD
`b9b31b90c14406c8d306de98f5291d914062d476` on
`codex/native-a2a-stream-continuation`; that commit contains prior Rust policy
work. This continuation preserved the remaining mixed changes and made no
commit, push, merge or cleanup. Current goal remains active; release evidence
still says `NOT_READY` with `exact_head: null`.

Implemented:

- `deepseek-policy::workspace_projects`: Workspace 2.0 project read facade,
  bounded conversations/messages, separate saved-item/artifact store projections,
  filtering, artifact versions and project-scoped memories. Aggregate reads
  tolerate child errors; direct child reads return their validation error.
- `deepseek-gateway::project_routes`: authenticated legacy project list/get and
  Workspace project/list/detail/conversation/saved-item/artifact reads, ahead of
  the Go proxy. Filesystem work uses `spawn_blocking`. JSON body limit is
  2,000,000 bytes, with structured error envelopes.
- Fixed a measured existing schema mismatch: falsey Python values now select
  empty/default titles/content/tags and artifact type fallback. Source-reference
  scalar booleans retain their value.
- Native project mutations explicitly return
  `501 NATIVE_PROJECTS_MUTATIONS_NOT_READY` in every runtime mode; no new writer
  domain or production cutover is claimed. Reads leave durable state unchanged.

Evidence:

- The initial production-router regression failed with 503
  `GO_CONTROL_PROXY_NOT_READY`; after wiring, all 24 data-route tests pass.
- `workspace_projects_oracle.py`: 14 isolated Python storage fixtures and 98
  Rust comparisons. Full children have nonzero counts; corruption, falsey
  values, sorting and 200-conversation/400-message boundaries are covered.
- Pinned Rust 1.85 full policy + gateway tests: **721 passed**, zero failures.
  Strict Clippy (`--all-targets -- -D warnings`) and gateway binary build pass.
- `workspace_projects_read_e2e.py`: **23 PASS checks**, actual Rust executable,
  two independent process starts, `python_disabled`, no Go proxy; expected HTTP
  values, auth, write refusal, unchanged tree before/after process exit and
  unchanged binary hash. Report: `artifacts/workspace-projects-read-e2e.json`;
  full Rust log: `artifacts/workspace-projects-rust-tests.log`.
- Ruff and mypy pass for both new offline harnesses. This is dirty-workspace
  local qualification, not exact-head CI/Evidence Assembly or zero-Python
  default packaging acceptance.

Next: complete the project write ownership decision and mechanical denial,
fenced/serialized read-modify-write, RAG/media deletion cleanup and uploads;
then continue the remaining public APIs, Go/Rust execution, default native
packaging and real-provider/exact-head acceptance from the matrix. Do not reopen
project writes merely because low-level `projects::create_project` and
`delete_project` exist: their side effects and ownership remain incomplete.

## Previous continuation checkpoint — 2026-09-20 A2A hardening and precise coverage

This entry supersedes the prior coverage and A2A test counts below. Same branch
`codex/native-a2a-stream-continuation`, HEAD `57f0595b54071d673799f1093929b783094fa334`.
All changes remain uncommitted; no push/merge or unrelated worktree cleanup.
The full migration goal is active, and release readiness remains `NOT_READY`.

Implemented and verified:

- **31 Python message-oracle cases** now exercise Rust text rendering and Go
  admission from the same fixture. Red regressions caught Rust's `false` ->
  `"false"` mismatch and Go rejecting Python's `true` -> `"True"` case.
  Context fallback, Python truthiness/control-character whitespace, and native
  execution preserve message extensions. The real process harness validates an
  integer above 2^53, null messageId/kind, and nested contextId.
- Every private A2A RPC now rechecks the validity interval of a complete
  previously verified TLS certificate chain. A red regression proved that an
  expired issuing CA had previously retained mutation authority. Both a direct
  alternate-chain test and an actual persistent mTLS connection with a short-lived
  CA prove the fix. This does not add certificate revocation/reload support.
- Actual SQLite corruption/lock tests prove failed initialization releases its
  writer lock, expiry/cancellation cannot be acknowledged without commit, list
  queries do not return partial damaged results, and a corrupted later data page
  cannot partially recover preceding tasks. Restoring that byte lets all 81
  submitted tasks recover through the real store. All damaged files are temporary
  test fixtures; no user task database was touched.
- Removed impossible entropy-error propagation after verifying the pinned Go
  1.27 crypto/rand.Read implementation (fills the buffer or terminates the process).
  The closed string-only status-message encoding no longer propagates impossible
  JSON errors. Dynamic document, filesystem, SQL and transaction errors remain.
- **The Go coverage gate now counts raw profile statements.** A red regression
  showed that the old gate admitted 94.96% when Go printed 95.0%. Duplicate block
  counts are merged and malformed/empty profiles fail closed. No threshold was
  lowered and no source was excluded.

Current local evidence:

- `artifacts/a2a-control-go-coverage.log`: **95.003059% (4658/4903)**, exact 95.0%
  gate PASS after running every handwritten internal/pkg package. Margin is
  narrow; this is not an exact-head CI claim.
- `artifacts/a2a-control-rust-tests.log`: **234 passed**; strict Rust 1.85 Clippy
  and the gateway build passed. `a2a-control-go-race.log`: the updated A2A package
  passed race checks, and `go vet ./...` passed. API/lifecycle race and all 16 Go
  packages passed in the preceding checkpoint; this round did not rerun those
  race packages.
- `artifacts/a2a-control-restart-proof.json`: **9 PASS** against the current
  Go/Rust binary hashes, including coercion/metadata preservation and all previous
  crash/lease/cancellation/no-rerun cases. Five tasks produced exactly five
  controlled loopback provider calls. The harness is offline Python tooling,
  not a Python production listener or storage-provider acceptance test.
- Coverage-gate tests: **13 passed**; Ruff/Mypy passed the four touched Python
  gate/harness files. Message oracle **31/31**, SSE oracle **12/12**, pinned codegen
  drift check, native contract check (**46 domains / 8 proto / 12 outputs**), and
  shadow comparison **8/8** passed.

Next work remains the full matrix: complete A2A peer clients, telemetry,
legacy-task migration, retention/full wire parity; remaining public business
APIs; authoritative Go controller and Rust worker/provider recovery; native
service, desktop and Android packaging; real storage-provider and exact-head
CI/Evidence Assembly acceptance. The default Docker entry still runs Python.

## Current continuation checkpoint — 2026-09-20 durable A2A control

This entry supersedes older A2A process-local/restart-gap statements below.
Branch `codex/native-a2a-stream-continuation`, HEAD `57f0595b54071d673799f1093929b783094fa334`;
all migration changes remain uncommitted, and unrelated dirty/untracked work was
preserved. No push/merge. Goal remains active; readiness is `NOT_READY` with
`exact_head: null`. The default Docker entry still runs `python app.py`.

Implemented: Go-owned SQLite A2A task/history/chunk lifecycle; OS single-writer
exclusion; immutable submission binding; Go-installed epoch plus renewable
execution token/lease; mTLS Protobuf service; Rust public JSON-RPC/SSE bridge and
native executor; shared cursor framing; mechanical denial of Python A2A writes
in native ownership modes. No Rust/Go writes into Python `.a2a` and no local
fallback when native control is missing or unavailable. See `docs/A2A_HUB.md`.

Current evidence (development scope, not release PASS):

- `artifacts/a2a-control-restart-proof.json`: **8 PASS** checks against actual
  Go/Rust binaries, binary SHA256s, and killed process PIDs/exit codes. Completed
  snapshots survive both restarts; resubscribe emits only the missing answer;
  Go crash fails unfinished work and rejects late completion; cancellation
  discards the answer; Rust crash expires the real 45-second lease. The loopback
  HTTP provider observed exactly one call per task, no reruns. Python is only
  the offline harness. This does not prove storage-provider or whole-topology
  zero-Python behavior.
- Gateway full regression: **233 passed**, including the new fail-closed
  configuration regression and **6/6** A2A integration tests. Logs: `a2a-control-rust-tests.log`, `a2a-control-boundary-tests.log`.
  Rust 1.85 strict Clippy and fmt passed (`a2a-control-clippy.log`).
- Go `test ./... -count=1` passed all 16 test-bearing packages
  (`a2a-control-go-all-tests.log`); `vet ./...` passed; `-race` passed
  A2A/API/lifecycle packages with the
  previously verified per-command `libsynchronization.a` link fix. No machine
  environment changes. Log: `a2a-control-go-race.log`. Linux amd64 A2A test
  binary cross-compilation passed (compile only; not a Linux execution claim).
- Full Go coverage runner executed every handwritten internal/pkg package:
  **94.3%**, below the unchanged **95.0%** gate. New A2A package is **87.4%**;
  other packages aggregate **95.083%**. This is an outstanding code/test gate,
  not an environmental blocker and not a passing native-go lane.
- Python A2A + denial/ownership/proto suites passed **63 tests**. The new denial
  tests failed before adding the gate, then passed. Ruff and Mypy passed all
  four touched Python source/test files.
- Pinned codegen drift check, contract validation (**46 domains / 8 proto files /
  12 generated outputs**), shadow comparison **8/8**, and the 12-case Python
  SSE oracle check passed.

Remaining next work: close the Go coverage gap with meaningful fault/recovery
verification; complete A2A peer clients, telemetry, legacy-task migration,
retention and full error/coercion parity; continue remaining public APIs,
Go controller/worker authority and real provider kill/takeover evidence, native
service/desktop/Android packaging, then exact-head CI/Evidence Assembly. Never
mark the whole migration complete from this isolated A2A qualification.

## Current continuation checkpoint — 2026-09-19 data-plane routes

This checkpoint supersedes the historical status paragraphs below for A2A.
Current branch: `codex/native-a2a-stream-continuation`, based on `57f0595b`.
The pre-existing uncommitted migration work was retained. No push or merge.
The session goal is active: the entire Rust/Go migration is not complete.
`Dockerfile` still starts `python app.py`; readiness remains `NOT_READY` with
`exact_head: null`.

### Slices landed in this session (uncommitted)

1. **`/api/reminders` + `/api/reminders/due`** on the native edge — reads served,
   mutations gated on the `reminders_store` cutover. 9 real-HTTP cases.
2. **`deepseek-policy::memory_schema`** — the v3.0 Memory projection layer, paired
   byte-for-byte with the oracle (164 keys, md5 `d0bbb075…`), shown not blind.
3. **The `/api/memory` family** — reads served, mutations gated on the
   `memory_store` cutover. 10 real-HTTP cases.

See the sections below for each slice's evidence. Nothing was pushed.

### Local environment blockers (recorded, not worked around)

- **The Docker daemon is not running on this host** (`npipe:////./pipe/
  dockerDesktopLinuxEngine` missing), so the Three-MinIO / two-Fleet provider
  evidence cannot be produced here. The `container_image_isolation` gate still
  passes statically; the *provider* workloads are what need a daemon.
- No MinIO binary is on `PATH` and no `DEEPSEEK_TEST_S3_ENDPOINT_*` is set.
- **`gofmt -l` reports every Go file on this host**, but it is a checkout artifact,
  not drift: the working tree has CRLF while the committed blobs are LF (verified by
  byte count and by `git show HEAD:…`), and `.gitattributes` only pins the generated
  files. CI runs on Linux where this cannot occur. Reformatting here would rewrite
  every line of files this session never touched.

All three are environment gaps, not code gaps. Everything below was verified
locally without them.

### Implemented and verified in this continuation

- Native `message/stream` and `tasks/resubscribe` on both A2A RPC routes:
  initial public snapshot, resumable progress/answer chunks, terminal status,
  retained JSON-RPC IDs, SSE error events, and no OpenAI `[DONE]` marker.
- Task notifications wake subscribers without polling; disconnect drops the
  subscription without canceling work. Start/cancel/finish share the task lock;
  a queued cancellation prevents execution, and a running cancellation discards
  the late answer before an answer chunk or terminal completion can be published.
- The production router's authentication applies, and disabled A2A rejects
  both ordinary and stream RPC requests. Go's `/api/a2a` and config flags now
  advertise the implemented stream capability.
- A2A hub tests serialize their global-state resets. The baseline had two
  failures caused by parallel reset/runner changes, not by the new stream code.

Evidence (local artifacts are gitignored):

- `artifacts/native-a2a-red.log`: regression first failed because the route
  returned `application/json` instead of SSE.
- `cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-gateway --locked -j 2`:
  **210 passed**, zero failed/ignored (181 lib + 29 integration), recorded in
  `artifacts/native-a2a-gateway-tests.log`.
- The five new integration tests include real loopback HTTP through the
  production gateway, the default native A2A runner, and a controlled local
  upstream. The initial snapshot arrives before the upstream is released.
- `python tasks/native-runtime/a2a_stream_oracle.py`: **12 cases** generated
  directly from the Python oracle's AST; Rust tests compare decoded events for
  three terminal states and four resume cursors. This is semantic parity,
  not a byte-order or full-A2A-parity claim. Ruff and Mypy pass for this helper.
- Gateway `cargo fmt --check` and strict Clippy (`--locked --all-targets
  --all-features -- -D warnings`) pass.
- `go test ./... -count=1 -timeout=600s`: **15 packages pass**;
  `go vet ./...` passes. Logs: `artifacts/native-a2a-go-all.log`.
- API and lifecycle race tests pass with **96.8%** and **98.9%** coverage
  respectively (`native-a2a-go-race-import-fix.log` and
  `native-a2a-go-lifecycle-race.log`). The initial Windows race
  binary could not load (`0xc0000139`): PE inspection proved old GCC 8.1 import
  libraries bound `WakeByAddressSingle`, `WakeByAddressAll`, and `WaitOnAddress`
  to `kernel32.dll`, which does not export them on this host. The scoped fix
  is `CGO_LDFLAGS=C:\Users\12393\.rustup\toolchains\1.85.0-x86_64-pc-windows-gnu\lib\rustlib\x86_64-pc-windows-gnu\lib\self-contained\libsynchronization.a`.
  Do not replace the entire library search path: mixing CRT generations fails
  linking. No system toolchain or persistent environment setting was changed.

### Next work and completion boundary

A2A task/chunk storage is still process-local. Implement restart persistence
and eviction after checking the authoritative store ownership rules, then
prove recovery across actual gateway process death. Peer clients and A2A
trace/disconnect telemetry are also still missing. See `docs/A2A_HUB.md`.
Other public APIs, browser execution, production cutover, default launchers,
desktop/Android packaging, and exact-head CI/Evidence Assembly remain.
Historical matrix entries may be stale; inspect current code before choosing
the next slice. Do not mark the overall goal complete from this local slice.

## Historical git at recovery

| Field | Value |
| --- | --- |
| Branch | `codex/native-runtime-5.0.0-continue` (created from `native-runtime-5.0.0-recovered`) |
| Recovered HEAD | `451ba5ec23ad07e783b68f2a1f8f87ed5f9b8f05` |
| VERSION | `4.8.0` |
| Evidence file | `NOT_READY` (must stay fail-closed until exact-head CI generates it) |
| Production authority | Python (`release/native_runtime_ownership_v1.json`) |

Do not reset, clean, or discard the recovered uncommitted tree. Coverage
profiles (`go/cov`, `go/shadow_cov`, `go/store_cov`, `*.out`) are local
artifacts and must not be committed.

## Evidence classes (do not collapse)

| Class | Meaning |
| --- | --- |
| Documented target | Spec/ADR/roadmap/plan text |
| Implemented, unwired | Native code exists; production entry still Python or fail-closed |
| Wired, unverified | Native entry exists; missing real-process/provider/CI evidence |
| Locally verified | This workspace ran the matching command |
| CI / release verified | Exact-head CI, Evidence Assembly, readiness validator |

## Recovered uncommitted work (this session)

Two in-progress slices were already present. They are not treated as correct
until tests in this session pass.

1. **Go schema v7 verification lifecycle** — `VERIFYING` / `ASSESSING_EFFECT`
   with an immutable boundary; leased execution/recovery persist VERIFYING
   instead of SUCCEEDED; resources stay held.
2. **Go→Rust TLS transport** — Rust-owned server key, Go CA + server-name
   verify, short-lived bearer over TLS only, no plaintext credential fallback.

## What this session implements next

Signed storage-operation grant (worker-execution-plan slice 2): canonical JSON
grant, shared v31 corpus, RPC admission before dispatch.

Not in this slice: durable grant sqlite journal, payload-bytes-in-Rust, MinIO
kill/takeover, production cutover, public edge parity, or readiness PASS.

## Status after this session

| Item | Class | Command / evidence |
| --- | --- | --- |
| Schema v7 + verification primitives | locally verified | `go test ./internal/store -count=1 -timeout=360s` exit 0 (49.937s); `go test ./internal/action -count=1` exit 0 |
| TLS unit + real-process tests | locally verified | worker/protocol tests exit 0; `cargo test -p deepseek-worker --lib --bins --test grpc_service --test tls_transport` exit 0; `TestRustWorkerTLSRealBoundary` exit 0 (0.16s) |
| Clippy worker | locally verified | `cargo clippy -p deepseek-worker --all-targets -- -D warnings` exit 0 |
| Full Go module (no race) | locally verified | `go test ./... -count=1 -timeout=600s` exit 0 (then worker re-run after OPERATION_INVALID mapping) |
| native-go TLS CI wiring | implemented, unverified | `.github/workflows/ci.yml` — needs exact-head CI |
| Storage operation grant v31 + RPC admission | locally verified | `go test ./internal/store -run StorageOperationGrant`; `cargo test -p deepseek-worker --test frozen_storage_operation_grant_v31 --test grpc_service`; `python scripts/native_runtime_contract.py --check` (42 corpora / 31 versions); TLS process test exit 0 |
| Full Go `-race` | not verified this session | store historically hits 600s aggregate timeout |
| Production cutover | documented target | `ErrCutoverNotAuthorized` still enforced |

## Running processes

None started by this continuation unless a later section records a PID.

## Blockers that remain after this slice

1. Durable worker sqlite grant journal (in-memory replay only today) and Go
   coordinator attaching live grants instead of qualification JSON.
2. Outcome/risk verifiers and compensation (not journal primitives).
3. Provider-backed Three-MinIO / two-Fleet kill-and-takeover.
4. Rust edge chat/MCP/A2A/catalog parity; authenticated `/api/*` proxy.
   Non-stream `/v1/chat/completions` now executes natively (see below); SSE,
   tool rounds, MCP and A2A remain fail-closed.
5. Oracle normalization differences - **RESOLVED 2026-09-14** (commit `df7dfa13`).
   The oracle silently dropped blank-content turns, `null` content, non-object
   entries, `tool` turns missing `tool_call_id`, and caller-supplied `system`
   turns. It now refuses the first four (same `ErrorCode` values as Rust) and
   *keeps* `system` turns. The `system` case is the important correction: a
   second measurement on the full assembly path
   (`tasks/native-runtime/oracle_layering_probe.py`) showed the caller's
   instruction never reached the upstream body at all, because
   `normalize_chat_messages` dropped it while `build_deepseek_request` builds
   the authoritative prefix separately from `payload["systemPrompt"]`. Keeping
   the turn is therefore not a capability addition - it stops silent data loss.
   Rust behavior unchanged, as decided.
5. Go production cutover authorization protocol.
6. Default launchers/images still start Python (`launch.py`, `docker-compose.yml`).
7. Exact-head CI and Evidence Assembly.

## Next explicit action

The Python oracle now fails closed on unrepresentable turns instead of silently
dropping them (commit `df7dfa13`, 2026-09-14). Verified: 101 passed across
`test_deepseek_client_failure_paths.py`, `test_gateway_request_preparation.py`
and `test_rust_gateway_request_parity_contract.py`; the only pre-existing failure
was the test that encoded the bug itself. The four measured parity differences
are now closed, with the two layers proven to agree.

Native edge chat has moved from fail-closed to a wired non-stream path
(`6ea4dde3` + `chat_execution.rs`, 2026-09-14). Verified locally:
`cargo fmt -p deepseek-gateway -- --check` clean; `cargo test -p deepseek-gateway
-j 1` -> 77 lib + 4 `chat_execution` real-upstream tests + 2 boundary tests,
all passed.

**SSE streaming is now wired too (2026-09-14, uncommitted).** `chat_stream.rs`
owns upstream SSE decoding (`decode_event`/`decode_chunk`) and downstream OpenAI
SSE encoding (`StreamChunkEncoder`), and `chat_completions` now branches on
`stream`. `request_preparation` no longer refuses `stream: true` — it normalizes
it to a boolean and forwards it, because transport selection is not a
preparation-layer concern. Verified locally:

- `cargo test -p deepseek-gateway -j 1` -> 96 lib + 4 `chat_execution` +
  6 `chat_stream` real-boundary tests, all passed.
- Byte-level parity: `tasks/native-runtime/sse_parity_probe.py` (extracts the
  real `_sse`/`openai_chat_stream` via `ast`) vs `examples/sse_parity_probe.rs`
  over the same two upstream scripts -> identical MD5
  `b9129475b6bae8b1239f4529e0a50932`, 12 frames, no differences.
- `cargo clippy -p deepseek-gateway --all-targets --all-features -- -D warnings`
  -> only the pre-existing `control_proxy.rs:20` `result_large_err` (file
  byte-identical to HEAD; local rustc 1.97.1 vs the declared 1.85).
- See `docs/GATEWAY_SSE_PARITY.md` for the frame contract and the explicit
  non-goals.

Still unwired and each failing closed with its own code: tool-call rounds,
`/api/chat` NDJSON (including `system_note`/`search`/`memory_suggestion`),
semantic cache/memory/context-compression, model router, scheduler leases and
budget ledger. `release/native_runtime_5_0_evidence_v1.json` stays `NOT_READY`.

Wire Go `ExecuteClaimedStorageAction` to sign `control-storage-operation-grant-v1`
from the live claim (no payload bytes in the grant), persist grants in the Rust
worker sqlite journal, then slice 3 provider-backed dispatch.

**Tool-round layer 1 is now implemented and byte-verified (2026-09-14, uncommitted).**
`rust/crates/deepseek-gateway/src/tool_rounds.rs` mirrors the oracle's round
*bookkeeping* only: `ToolCallAccumulator` (streamed `tool_calls` delta merge and
finalization), `normalize_tool_calls_lenient`, `decide_round` (the round/budget
branch), `append_tool_exchange` (message assembly), and
`force_final_answer_without_tools`, plus `tool_names` / `select_tool_calls` /
`tool_call_note` and the three constants.

The public route **keeps refusing** a `tool_calls` turn with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`. Layers 2 (tool execution: the 17 branches
plus `browser_*`) and 3 (policy/sandbox) are not implemented, and wiring layer 1
alone would replace the oracle's terminating tool loop with a permanently
failing one that still answers `200` — the forbidden silent behavior change.
This slice exists to make that refusal precise and to make later enablement a
wiring change rather than a rewrite.

Verified locally:

- Byte-level parity: `tasks/native-runtime/tool_round_parity_probe.py` (extracts
  the real `append_tool_exchange`, `merge_stream_tool_call_deltas`,
  `finalized_stream_tool_calls`, `normalize_tool_calls`, `tool_names`,
  `force_final_answer_without_tools` via `ast`; stubs only the layer-2
  `execute_tool_calls` and the transport `raise_if_cancelled`) vs
  `examples/tool_round_parity_probe.rs` over the same 12 input scripts ->
  **identical MD5 `ca9b072a4826fc470e3ccdc6e436bc58`**, 36 keys, no differences.
- `cargo test -p deepseek-gateway --lib tool_rounds -j 1` -> 24 tests, all pass.
- `cargo fmt --check` clean.

Three real divergences were found and fixed **by the probe**, not by reasoning:

1. an index-less delta lands at the *slot count*, not the highest index plus one
   (oracle: `["first", "third", "five"]`; the original unit test asserted
   `["first", "five", "third"]` and was wrong);
2. `str(item.get("id") or f"call_{i+1}")` stringifies truthy non-strings, so
   `id: 123` becomes `"123"` while `id: 0` / `id: ""` fall back — the first
   implementation read only string ids and silently renumbered them;
3. non-string `arguments` use Python's **default** JSON separators (`{"a": 1}`),
   not `serde_json`'s compact form (`{"a":1}`). This lands verbatim in the
   upstream body and therefore changes the prompt prefix and DeepSeek's prefix
   caching.

Known bounded limitation: this workspace compiles `serde_json` **without**
`preserve_order`, so an `arguments` *object* whose keys are not already sorted
serializes in Rust's sorted order rather than the caller's insertion order.
Enabling `preserve_order` workspace-wide would silently reorder every other Rust
response (only `deepseek-proof` opts in), so it was deliberately not done;
resolving it belongs with argument canonicalization. Recorded in
`docs/GATEWAY_TOOL_ROUND_PARITY.md`.

Next concrete action for this line: implement layer 2 (`execute_tool_call`'s 17
branches + `browser_*`) and layer 3 (`ToolPolicy.evaluate`/`sanitize_result`),
then wire `tool_rounds` into `chat_execution`/`chat_stream` and delete the
`ToolRoundsUnwired` refusal.

**Tool-policy pure core ported and byte-verified (2026-09-15, uncommitted).**
`rust/crates/deepseek-policy/src/tool_policy.rs` mirrors the side-effect-free half
of `deepseek_infra/infra/tool_runtime/tool_policy.py`: the SSRF guard
(`evaluate_url_safety`), the path-escape guard (`evaluate_path_safety`), the
recursive network-argument guard, the secret-exfiltration guard
(`arguments_contain_secret`), the prompt-injection sanitizers
(`sanitize_external_text` / `sanitize_tool_result` /
`sanitize_tool_result_for_external`), `validate_arguments`, `_max_risk`, and the
`ToolMetadata` / capability-profile tables.

This is the gate the oracle applies **before** a tool runs, so it is a
prerequisite for layer 2 (tool execution): porting execution first would mean
running model-chosen side effects with no SSRF, path-escape, secret-exfil, or
injection guard — strictly weaker than the Python being replaced.

Still **not** ported: `ToolPolicy.evaluate` (reads config + audit state) and the
audit writers. Until those land, nothing may execute a tool on this module alone,
and the route keeps refusing tool rounds with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

Verified locally:

- Byte-level parity: `tasks/native-runtime/tool_policy_parity_probe.py` slices the
  contiguous pure region of the oracle (lines 59–563) and `exec`s it (so the
  definitions being compared are the oracle's own, including the import-time
  derivations) vs `deepseek-policy/examples/tool_policy_parity_probe.rs` over the
  same corpus -> **identical MD5 `26c7723c89a4fb59c7ef9e412f1b4b97`**, 158 keys,
  no differences.
- `cargo test -p deepseek-policy --lib tool_policy` -> 30 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean. `cargo fmt` applied.
- `cargo check -p deepseek-gateway --all-targets` -> still compiles.

Two defects the probe caught, both from **guessing** the IP classifier instead of
reading CPython's tables (the first pass was wrong in both directions):

1. false negative — `1:0:0:2::3` was allowed; Python's `is_reserved` covers
   `::/8` (and much more: `4000::/3`, `e000::/4`, …), so the oracle blocks it.
2. false positive — `192.88.99.1` was blocked; that range is in none of Python's
   tables, so the oracle allows it.

Derived facts, now encoded and tested:

- IPv4 `is_global` = `not in 100.64.0.0/10 and not is_private`, so `not is_global`
  adds only the shared range; `is_reserved` is `240.0.0.0/4` (already private).
  Blocking set = 14 ranges.
- IPv6 `is_global` is literally `not is_private`, so `not is_global` adds nothing.
  Blocking set = `_private_networks` ∪ `_reserved_networks` ∪ multicast = 23 ranges.
- IPv4-mapped IPv6 delegates **every** predicate to the underlying IPv4 address,
  so `::ffff:1.2.3.4` is *allowed* while `::ffff:0:1` is blocked — even though
  `_private_networks` lists `::ffff:0.0.0.0/96`. Rust's `to_ipv4_mapped()` matches
  CPython's `ipv4_mapped` exactly.
- `fec0::/10` (deprecated site-local) is allowed by the oracle; `fe00::/9` stops
  at `fe7f::`. Mirroring that hole is correctness, not a bug to "fix".

Message-parity surfaces that look like formatting but are not: the blocked-IP
reason embeds Python's `str(ip)` (so IPv4-mapped must render dotted, not
`::ffff:0:1`), and the enum violation embeds Python's `repr` of the list
(`['x', 'y']`).

Dependency change is minimal: `regex 1.13.0` was already in `Cargo.lock`
transitively, so it is pinned exactly and promoted to a direct dep of
`deepseek-policy`; the lockfile gains one edge and no new crate version.

**Real finding, deliberately not fixed here.** The crate's pre-existing generic
guards (`url_guard.rs` / `path_guard.rs`, behind the gateway's `/policy/*` routes)
are **weaker than the oracle** and are a different model: no
`.local`/`.localhost`/`.internal` suffix check, no trailing-dot strip, URL
credentials are **stripped and allowed** (the oracle denies them), and
multicast/reserved/CGNAT/non-global IPv4 plus the IPv6 reserved ranges are not
checked at all. Tightening them changes a registered route's behavior, so it
deserves its own slice with its own evidence. Exposure is latent — the Rust
gateway is not the production authority — but it should not ship as-is.

Next concrete action for this line: port `ToolPolicy.evaluate` + the audit log
(layer 3b), then implement layer 2 execution against this gate, then wire the
round loop and delete the `ToolRoundsUnwired` refusal.

**Tool-policy engine + audit layer ported and byte-verified (2026-09-15 二轮，uncommitted).**
`deepseek-policy::tool_policy` now also carries the decision engine and the audit
log, completing the policy gate:

- `ToolPolicy` + `ToolPolicyConfig` with the oracle's own defaults,
  `ToolPolicy::new` / `permissive()`, `evaluate`, `_record` semantics
  (counters + `blocked_tools`), `mark_tainted` / `is_tainted`,
  `sanitize_result` (scrubs and taints the turn on a hit), `denial_output`,
  `diagnostics`.
- `ToolPolicyDecision` — deliberately **not** named `PolicyDecision`, because this
  crate already exports a different `PolicyDecision` (the
  `Capability`/`RiskLevel` model behind `/policy/*`). Sharing the name would make
  importing the wrong one an easy, security-relevant mistake.
- `is_sensitive_memory` (extracted from `infra/data/memory.py`, not re-written).
- Audit: `AuditSink` trait with `NullAuditSink` / `InMemoryAuditSink` /
  `JsonlAuditSink`, `build_audit_entry`, `build_external_audit_entry`,
  `normalized_args_hash`, `read_recent_audit`, and a hand-rolled
  `utc_isoformat_seconds` (no date dependency).

**Not ported:** `tool_policy_status` (reads the audit-path global and the config
object) and the `deepseek_infra.core.config` env reader — a config-layer concern,
not a policy one. Nothing may execute a tool until layer 2 exists and is wired;
the route still refuses tool rounds with `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

Verified locally:

- Byte-level parity across **both** regions of the same oracle file: the
  contiguous constants+guards+engine slice (lines 59–901, `exec`ed verbatim with
  only the config globals and audit path rebound) plus the audit functions lifted
  individually and driven against a **real temporary JSONL file** (so the writer
  that ships is the writer measured, not a re-implementation of its entry dict) ->
  **identical MD5 `d51462e06a0e6ccd03db7ed05ab77d71`**, 197 keys, no differences,
  re-confirmed after `cargo fmt`.
- `cargo test -p deepseek-policy` -> 77 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean.
- Dependency: `sha2 0.10.9` was already in `Cargo.lock`; the lockfile gains one
  edge and no new crate version.

Design points worth keeping:

- **The decision order is the contract.** `evaluate` returns on the first failing
  check, so reordering any pair changes which reason a call reports when it fails
  several (unknown → capability → schema → SSRF → path → sensitive → secret →
  confirm → taint → allow).
- **`fetch_url` bypasses the recursive guard** and calls `evaluate_url_safety` on
  `args["url"]` directly; every other network tool goes through
  `evaluate_network_argument_safety`, which **prefixes the offending key**. So the
  same private host yields `ssrf_blocked:private or local ip is not allowed: …`
  for `fetch_url` but `ssrf_blocked:host: …` for `web_search`. My first unit-test
  expectation used the unprefixed form for `web_search` and was wrong; the probe
  settled it. (Third time this pattern has caught me — measure, don't infer.)
- **`denial_output` does not check the action.** Called on an allow it still
  returns a denial-shaped payload with `code: "forbidden"` and
  `error: "… blocked by tool policy (allow)"`. That looks like a bug and is not;
  it has an explicit test so nobody "fixes" it.
- **Best-effort audit is the contract.** The oracle swallows every write error so
  an unwritable log can never break a tool call. The port keeps that but records
  the failure in `last_error()` so it stays observable instead of vanishing.
  Splitting the write behind `AuditSink` is also what keeps `evaluate`
  deterministic enough to compare byte-for-byte, and lets shadow runs capture
  decisions without touching the authoritative log.
- The audit entry is `{"ts", "scope", **decision.to_dict()}` with `sort_keys=True`.
  `ts` is the only non-deterministic field, so the probe injects a fixed clock and
  masks it on both sides; its *shape* is pinned by unit tests with hand-checked
  anchors (epoch, day boundary, Unix 1e9).

Next concrete action for this line: port `tool_policy_status` + the config reader,
then implement layer 2 execution against this gate, then wire the round loop and
delete the `ToolRoundsUnwired` refusal.

**Status endpoint ported, and the `/policy/url` gate aligned to the oracle (2026-09-15 三轮，uncommitted).**

Step 1 of the planned sequence (`tool_policy_status` + config) is done:
`ToolPolicySettings` (the five knobs, config defaults), `ToolAuditPaths::under(root)`
(mirroring `tool_audit_dir = root / ".tool-audit"`), `tool_policy_status`, and
`render_path_like_python` for the `auditLogPath` field. `ToolPolicyConfig::default()`
now reads its four strictness fields *through* `ToolPolicySettings::default()`, so
the engine and the status payload cannot drift apart (asserted by a test).

**The scoping of step 2 turned up something that reordered the work.** The oracle's
own Rust delegation is the risk:

```
execute_tool_call -> _evaluate_rust_policy (tools.py)
                  -> rust_core.policy_client.check_url / check_path
                  -> POST /policy/url, /policy/path (gateway)
                  -> url_guard::validate_url_access  <-- weaker than Python
```

`DEEPSEEK_RUST_POLICY` defaults to **false** (`infra/rust_core/config.py`), so
Python still decides. But flipping it would have moved SSRF decisions onto the
guard flagged in the previous round: `.local` / `.internal` hosts, trailing-dot
localhost, credential-bearing URLs, multicast, reserved, CGNAT and the whole IPv6
reserved set would all have started passing. Wiring execution onto that gate first
would have been the wrong order — the gate had to be correct before anything was
allowed to depend on it.

So `url_guard::validate_url_access` now **delegates to
`tool_policy::evaluate_url_safety`** and maps the oracle's denial reason onto the
crate's codes. The bridge contract is unaffected: `policy_client._parse_response`
requires the `allowed` bool plus non-empty string `code`/`reason`/`decision_id`/
`capability`/`risk_level`, and treats `code` as **opaque** — nothing branches on it.

Two deliberate consequences:

- The oracle reports one `private or local ip is not allowed: …` verdict, so
  loopback, link-local, reserved and multicast now all return
  `PRIVATE_NETWORK_BLOCKED` from this route. `codes::LINK_LOCAL_BLOCKED` is no
  longer emitted *by this guard*. Mirroring the oracle means mirroring its
  collapsing, not inventing a finer taxonomy.
- `UrlPolicy` can only **tighten**. The oracle accepts http(s) only, so listing
  another scheme cannot reintroduce it — there is a test for exactly that.

`path_guard` is deliberately **not** touched: `validate_workspace_path` is
root-containment over a `{root, requested}` pair, while the oracle's
`evaluate_path_safety` is an argument-key scan. Complementary, not
interchangeable; merging them would change what the route means.

Verified locally:

- Byte-level parity: **identical MD5 `bae3a9e5eb30cdd80a7a28b31e1f433b`**, 257
  keys, no differences. The URL corpus is checked **twice** — `url::<label>` (the
  guard) and `guard::<label>` (the route), so the route cannot silently drift from
  the guard it delegates to.
- `cargo test -p deepseek-policy` -> 82 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.
- `cargo check -p deepseek-gateway --all-targets` -> still compiles.

**NOT done, and the honest state of the remaining two steps:**

- **Step 2 (layer 2 tool execution) is not started.** `execute_tool_call` dispatches
  to 17 local branches plus `browser_*`, and those depend on the `search`, `rag`,
  `data` (projects/reminders/memory), `media` (presentations, mindmaps, documents,
  slides) and `browser` packages — several thousand lines with their own
  side-effect and sandbox semantics. It is a multi-slice effort, not one commit.
  A sensible first slice is the **dispatch skeleton + the branches with no external
  package** (e.g. `python_eval`'s sandbox envelope, `data_transform`,
  `list_reminders`), each behind the gate just aligned, with the package-backed
  branches added one at a time.
- **Step 3 (wire the round loop, delete `ToolRoundsUnwired`) is not started** and
  is correctly blocked on step 2 — wiring it now would replace the oracle's
  terminating tool loop with a permanently failing one that still answers `200`.
- `DEEPSEEK_RUST_POLICY` remains **off**, deliberately. Enabling it is an explicit
  cutover that needs `path_guard` aligned and the failure-mode policy reviewed.

**Layer 2 slice 1: the executor seam, with one branch ported (2026-09-15 四轮，uncommitted).**

`rust/crates/deepseek-policy/src/tool_dispatch.rs` ports the *seam* of
`execute_tool_call` in `infra/tool_runtime/tools.py`:

- the envelope contract (success `{"ok": true, "tool", "result"}` + `sanitize_result`;
  the `AppError` and catch-all error arms; the `Unsupported tool:` fallback);
- the normalization the branches rely on — `tool_call_name`,
  `parse_tool_arguments`, `safe_limit`, `is_parallel_safe_tool`, `SERIAL_TOOL_NAMES`;
- the **complete branch inventory** (`Branch`, 18 entries) with `branch_for`
  routing, `is_ported`, and `blocker()` naming the package each unported branch
  waits on — a test asserts no branch is silently missing;
- `generate_chart` + `chart_markdown_table`, the one branch that needs no package.

**Nothing is wired.** `DispatchOutcome::Unported` deliberately carries **no
envelope** and `to_output()` returns `None` for it, so a caller cannot report
success (or even a tidy error) for a tool that was never implemented. 17 of 18
branches remain unported; `python_eval` in particular needs a real sandbox
because the oracle shells out to a Python interpreter, which the migrated runtime
must not do.

Verified locally:

- Byte-level parity: **identical MD5 `9d491ef3f97c9f079ad9d7761a815ec4`**, 49 keys,
  no differences, re-confirmed after the clippy fixes.
- `cargo test -p deepseek-policy` -> 102 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Three orderings the probe pinned, and the bugs they caught:

1. **Parse before gate.** My first draft gated the raw `arguments` value. The
   model sends arguments as a JSON *string*, and the guards read fields inside it —
   so gating the raw string left every argument guard looking at an empty object
   and the SSRF/path checks **silently passed**. The oracle parses first; there is
   now a test that fails if the order is reversed
   (`dispatch("fetch_url", "{\"url\": \"http://169.254.169.254/\"}")` must be
   Denied with `risk = "critical"`).
2. **Gate before branch.** A denial short-circuits; the probe records branch
   invocations, and every denied case reports `branches: []`.
3. **The unknown-tool fallback is a no-policy path.** With a policy attached an
   unregistered name is denied as `unknown_tool` first, so `Unsupported tool:` is
   only reachable without one. The Rust probe example's first version gated every
   case, which made the `no-policy` case deny where the oracle reached the
   fallback — the diff exposed it.

Two behaviours the corpus settled, both from **guessing instead of measuring**
(that is now four times on this project):

- `data[:12]` is applied **before** the point filter, so the cap counts raw items,
  not usable points. My first test asserted 12 points for a 26-item input; the
  real answer is 7.
- `int("7.9")` raises in Python (falls back to the default) while `int(7.9)`
  truncates to 7 — the string and number paths had to be handled separately.

Also reproduced: `python_float_str` for `str(float)` (`1.0` not `1`; signed
zero-padded exponents outside `1e-4..1e16`), because those values are interpolated
into the model-facing markdown table.

Next concrete action for this line: port `execute_tool_calls` (the parallel batch
+ cancellation) and the remaining branches one at a time, each behind the gate,
starting with the ones whose packages are smallest. Only after enough branches
exist does wiring the round loop (and deleting `ToolRoundsUnwired`) become safe.

**Layer 2 slice 2: `data_transform` branch, batch orchestration, shared Python-JSON (2026-09-15 五轮，uncommitted).**

- `tool_transform.rs` ports `data_transform` and its four pure operations
  (`extract_regex`, `json_path`, `csv_summary`, `number_summary`) plus helpers
  (`read_simple_json_path`, `compact_json_value`, `number_summary_payload`, and a
  hand-rolled `csv_read` for Python's default CSV dialect).
- `tool_batch.rs` ports `execute_tool_calls`: selection capped at 6, serial/parallel
  batching (exposed as `plan_batches` data), cancellation at the four points,
  None → cancelled / None → "did not run", and the `role: "tool"` message with
  compact-JSON content truncated to `MAX_TOOL_RESULT_CHARS`. `strip_volatile_tool_fields`
  and `stable_tool_output_for_model` ported; artifact-compaction deferred (those 3
  branches unported, path unreachable, a test pins the pass-through).
- `python_json.rs` owns `dumps_default_separators`/`dumps_compact`/`float_str`/`value_str`
  — the rendering rules `tool_rounds`/`tool_policy`/`tool_dispatch` each had a
  private copy of. `tool_policy::normalized_args_hash` and `tool_dispatch::python_float_str`
  now delegate to it (two duplicates removed; gateway's `tool_rounds` copy noted
  as a follow-up, out of this crate's boundary).

**Honest state of the remaining 15 branches**: `Branch::blocker()` still names each
one's package and a test asserts none is silent. They are blocked on real subsystems
(browser engine, RAG, data layer, media/doc generation, an HTTP client, a real
sandbox for `python_eval`). Wiring the round loop and deleting `ToolRoundsUnwired`
stays blocked on these.

Two parity substitutions recorded in docs:
- JSON-path splitter: Python uses a lookahead the `regex` crate lacks; a plain
  split on `.` is equivalent for every *acceptable* path (well-formed parts have
  digits-only indices, so no dot lives inside brackets; the two disagree only on
  paths that fail the fullmatch and raise "Unsupported JSON path" either way).
- Engine-specific diagnostics: `Invalid JSON: …` / `Invalid regex: …` embed the
  engine's own error text. The prefix is the oracle's and identical; the suffix is
  masked on both sides like the audit `ts`. Divergence on record in the doc and a
  unit test, not behind a green diff.

Verified locally:
- Byte-level parity: **identical MD5 `3f088f27bcf1dda772cf3fb18d318cf5`**, 80 keys,
  no differences (helpers, both ported branches, batch layer, volatile-strip).
- `cargo test -p deepseek-policy` -> 131 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

**Layer 2 slice 3: the search family, callback injected (2026-09-15 六轮，uncommitted).**

The next-smallest dependency after `data_transform`: `web_search` and
`compare_search_results` need no package, only the per-request
`web_search_callback` the gateway owns.

- `tool_search.rs` ports both branch bodies (with their distinct "not enabled for
  this request" errors), `compare_search_results` (two cleaned queries, whitespace
  collapsed / de-duplicated / 500-char cap; one round each; results de-duplicated
  across rounds and capped at 20), and `search_result_key`.
- `ExecutorContext` carries the optional callback, mirroring the oracle's keyword
  arguments. `dispatch` now threads it through — the one signature change; tests
  and the probe example pass a default context, which makes the search branches
  take their "not enabled" path, and that path is compared directly.

**`search_result_key` is deliberately a different projection** from
`tool_policy`'s SSRF host extraction: the guard wants a hostname to classify
against the IP tables, this wants the raw netloc (lowercased, port and userinfo
included) so results differing only in case or fragment collapse to one key.

Two measured behaviours that corrected wrong guesses of mine (**sixth time on this
project that measuring beat reasoning**):

- `urlsplit` strips **leading** C0 controls and spaces but never trailing ones, so
  `"  HTTP://X  "` keys to `"http://x  /"`.
- An **empty** URL is not an empty key: `urlsplit("")` normalises to path `/`, so
  the key is `"/"` — which is why an empty-URL result is **kept**, not skipped.
  Only a non-object entry is dropped.

Verified locally:
- Byte-level parity: **identical MD5 `a6aa9b0ed707966a641940b47fdade55`**, 104 keys,
  no differences.
- `cargo test -p deepseek-policy` -> 141 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Branch status: **4 of 18 ported** (`generate_chart`, `data_transform`,
`web_search`, `compare_search_results`). 14 remain, each with `Branch::blocker()`
naming its package. Nothing is wired; the round loop stays blocked on them.

**Layer 2 / data layer slice A1: the workspace mutation gate (2026-09-15 七轮，uncommitted).**

Prerequisite chosen by the user (A1 over A2). `rust/crates/deepseek-policy/src/mutation_gate.rs`
ports `infra/workspace/mutation_gate.py` — the fence, the exclusive OS lock, and the
durable generation counter. Every memory/reminder write is wrapped in it, so no
data-layer branch could be faithful without it.

**It is not a mutex.** `mutation_scope` (1) asserts no restore owns the workspace
(423, checked twice to close the race with a newly-created fence), (2) takes an
exclusive OS lock for the whole mutation, (3) bumps the generation **before and
after**, fsync'd. The lock and the fence are deliberately separate: a crash
releases the lock, but mutations stay blocked until recovery reconciles the
transaction.

Shape differences, each with a reason: `root: &Path` instead of a `config.ROOT`
global; `LockFileEx` with the oracle's ten-attempts-one-second-apart retry policy
(plain `LockFileEx` would block **forever** where `msvcrt.LK_LOCK` raises); `flock`
on Unix; `Mutex` + thread-local depth instead of `RLock` (Rust's `Mutex` is not
reentrant); hand-written `extern "C"` because this workspace pins deps to what is
already in `Cargo.lock`.

Quirks reproduced rather than fixed: `fsync_directory` stays **best-effort** (the
directory open normally fails on Windows); the lock file is created with `b"0"`
only if absent; temp-file cleanup failure is ignored after a committed replace;
`write_fence` and `bump_generation` build temp names differently (suffix preserved
vs dropped); the unreadable-fence message is **fixed** because the oracle chains
the cause with `raise ... from exc` rather than interpolating it.

Errors: `GateKind` distinguishes the oracle's `AppError` / `RuntimeError` /
`OSError`, and **`code`/`status` are `Option`** — a `RuntimeError` has neither, and
inventing `internal`/500 would let a caller read a programming error as a routine
refusal. That was my first draft's mistake.

Three probe bugs this slice exposed (all mine):
1. `ast.get_source_segment` drops decorators, so `exclusive_gate`/`mutation_scope`
   came back as bare generators, not context managers.
2. `@contextmanager` is **lazy** — `mutation_scope()` alone asserts nothing and
   bumps nothing; the body only runs on `__enter__`. A probe that merely called it
   would have shown a green tick over no behaviour.
3. `_GATE_STATE` is a module-level global, so the nested-different-root check only
   fires within one module instance. Building a second namespace for the "other
   root" gave the inner gate its own thread-local state and — correctly — no error.
   The Rust behaviour was right; the probe was wrong.
4. `json!` reads `[...]` as an array literal, so a `.iter()` chain cannot follow it.

Verified locally:
- Byte-level parity: **identical MD5 `57e0ede25273693e03863bffe024aadb`**, 32 keys,
  no differences — paths, generation read/bump/clamp, fence write/read/clear, both
  refusal paths, the scope's double bump, nesting (same and different root),
  malformed fences, temp-file hygiene, lock-file content.
- `cargo test -p deepseek-policy` -> 155 tests, all pass (14 new), including a
  multi-threaded case asserting the generation ends at exactly `scopes * 2`.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Next: **slice B, the reminders pair** (`create_reminder`, `list_reminders`) — 138
lines, one JSON file, no retrieval, no RAG. `Branch::is_ported()` is unchanged for
every data-layer branch; nothing is wired.

**Data layer slice B: the reminders store and its branches (2026-09-15 八轮，uncommitted).**

`rust/crates/deepseek-policy/src/reminders.rs` ports
`infra/data/reminders.py` plus the `create_reminder_tool` / `list_reminders_tool`
wrappers: the JSON store, `parse_due_at`, `create_reminder`, `list_reminders`,
`delete_reminder`, `due_reminders`. First slice that exercises the slice-A1 gate
from a data path — the probe records the generation advancing **two per create** as
evidence the write really goes through the fence.

**Key order is part of the on-disk contract.** `serde_json` here has no
`preserve_order`, so object keys iterate sorted, while Python dicts keep insertion
order and the store file carries it. Writing from a plain `Value` would give
different bytes for equivalent JSON — and this repository's subject is a backup
system. Added `python_json::OrderedJson` to spell the order out, with a test pinning
the exact expected file text.

**Quirks reproduced, not fixed:** the temp file is
`REMINDERS_FILE.with_suffix(".tmp")`, which *replaces* the suffix
(`reminders.json` -> `reminders.tmp`), so two writers collide on one name; and reads
are silent (missing/unreadable/malformed/wrong-top-level-type all degrade to empty,
non-dict entries dropped).

**`parse_due_at`** reproduces the subset of `datetime.fromisoformat` this module
meets plus Python's `isoformat()`: `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www-D`, `T`/`t`/
space separator, `HH` through `HH:MM:SS.ffffff`, compact time, and `Z`/`+HH`/`+HH:MM`/
`+HHMM` offsets. A **lowercase `z` is rejected** (only an uppercase trailing `Z` is
rewritten). Anything outside the measured set raises the oracle's own message rather
than being guessed at.

**Seventh "guessed instead of measured".** My first ISO-week implementation validated
the week by checking the resulting date's *year* matched the stated year. Wrong in
both directions; `date.fromisocalendar` settled it:

| Input | Oracle | My first version |
| --- | --- | --- |
| `2026-W01-1` | `2025-12-29` (week 1 starts in December) | rejected |
| `2026-W53-1` | `2026-12-28` (2026 has 53 ISO weeks) | rejected |
| `2025-W53-1` | error (2025 has 52) | (would have accepted) |

The rule is: validate against *how many ISO weeks that year actually has*, from the
distance between consecutive week-1 Mondays. The unit test asserted the wrong
expectation too and now carries the measured values.

**Non-determinism is injected.** `secrets.token_hex(8)` and `int(time.time()*1000)`
arrive through an `Entropy` trait; production uses the OS CSPRNG (`BCryptGenRandom` /
`/dev/urandom`) and **fails loudly rather than falling back** to a weaker source,
since `secrets` is explicitly the secure option and a reminder id reaches the model
in tool output.

Verified locally:
- Byte-level parity: **identical MD5 `a636cd4590cff26d7809e8866fb3e500`**, 74 keys,
  no differences — 41 date forms, 8 create shapes, the exact store bytes, generation
  and lock file, temp hygiene, 8 status variants over two store states, 5 tolerant-read
  shapes, delete outcomes.
- `cargo test -p deepseek-policy` -> 171 tests, all pass (16 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Next: slice C, the scorer (`query_tokens` / `score_chunk` / `utc_now_iso` /
`latest_user_query`) — pure, and shared with `search_files`, so one port unblocks two
branches. `due_reminders` is ported and unit-tested but not yet in a compared corpus
(calling it mutates the store past the probe's last observation).

**Data layer slice C: the retrieval scorer (2026-09-15 九轮，uncommitted).**

`core_utils.rs` ports `query_tokens`, `score_chunk`, `utc_now_iso` and
`latest_user_query` from `core/utils.py`. Shared by the memory branches **and** the
RAG `search_files` branch, so one port serves two.

**A measured defect in the oracle.** `query_tokens` ends with
`sorted(tokens, key=len, reverse=True)[:80]` over a **set**. Python's sort is stable,
so equal-length tokens keep the set's iteration order, which depends on
`PYTHONHASHSEED`; when more than 80 tokens survive, *which* 80 are kept changes every
run. Measured:

    100 equal-length tokens, seed 1 -> x70,x17,x04,x11,...
    100 equal-length tokens, seed 2 -> x78,x43,x17,x67,...
    weighted case: seed 1 -> score 360; seed 5 -> score 390

The score ranks memories, so this leaks into tool output. The port therefore orders
by **length descending, then lexicographically** — deterministic where the oracle is
not. That is a deliberate divergence: there is no single oracle behaviour to
preserve, and CPython's set order is impossible to reproduce by construction. It
narrows a varying result to a fixed one and weakens nothing.

The probe matches that reality instead of hiding it: token lists are compared
**sorted**; inputs where more than 80 tokens survive report **only the count** (the
subset itself differs run to run); and a unit test pins the determinism as a property
of this port.

Signature difference, documented: `utc_now_iso()` reads the clock and takes no
argument in the oracle; this port is `utc_now_iso(epoch_seconds)`, so the clock is
supplied and can be pinned. The probe compares the *rendering* for four epochs.

Details that are easy to conflate: the tokenizer's character classes need **two or
more** characters while the weight is `max(2, min(len, 10))`; CJK bigrams are added
**on top of** the run itself; and a `set` dedupes windows, so `"中" * 60` yields
exactly two tokens.

Also fixed: a unit test asserted `query_tokens("Rust   OWNERSHIP") == ["rust",
"ownership"]`, the wrong order — `ownership` is longer and comes first. Same mistake
class as the previous six; the ordering rule now has its own test.

Verified locally:
- Byte-level parity: **identical MD5 `2170fb900a543b67163f1417d8af2c15`**, 37 keys,
  no differences — 13 tokenizer inputs, 2 capped inputs, 10 scoring inputs with token
  lists, 4 epoch renderings, 8 `latest_user_query` payloads.
- `cargo test -p deepseek-policy` -> 180 tests, all pass (9 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Next: slice D, the memory triple (`suggest_memory`, `recall_memory`, `forget_memory`)
— store + scorer + the fingerprint/category/conflict/sensitive logic. Nothing is
wired; `Branch::is_ported()` is unchanged for every data-layer branch.

**Data layer slice D: the memory triple (2026-09-15 十轮，uncommitted).**

`memory.rs` ports `infra/data/memory.py` plus the `suggest_memory` /
`recall_memory` / `forget_memory` branches; `file_lock.rs` factors out the OS lock
that both this module and the mutation gate need (the platform split now lives in one
place). A memory write passes through **three** layers, each doing a different job: a
process-wide mutex, a cross-process file lock on `.memory/memories.lock`, and the
workspace mutation gate.

**The bug this slice found, and how.** The first version put `mutation_scope` around
the *delete* path only, because that was the path I was reading. The oracle puts it
inside `_save_memories_unlocked`, so **every** save is fenced — including the
migration save. The probe caught it as a generation counter off by exactly two:

    delete::no-write-generation   Python 6   Rust 4

Six means three scopes had run (migration + two deletes), four means two. Fixed by
moving the gate into `save_unlocked`, where the oracle has it.

**A truthiness detail.** `_save_memories_unlocked` normalises `source` through two
Python `or` chains. My first version stringified any number and fell back otherwise,
which is wrong at both ends: `0` and `false` are falsy and become `"manual"`, while a
non-zero number and `true` become `"5"` / `"True"`. Fixed with an explicit
`python_truthy` covering `""`, `[]` and `{}` too.

**One deliberate gap, stated everywhere it matters.** `retrieve_memories` adds a
vector-search bonus from `local_rag.search_memories_index`. `local_rag` is 2,676 lines
and belongs to the RAG slice, so the bonus arrives through an injectable `VectorHits`
provider defaulting to none. The oracle wraps the call in `try/except Exception` and
falls back to an empty map, so the default reproduces the oracle's **own degradation
path** and the probe compares that. But when the vector index is populated the
oracle's scores include a bonus this does not. **`recall_memory`'s ranking is verified
only where the vector index contributes nothing** — recorded in the docs, the matrix
and the module docs.

Also ported faithfully from the write path (it doubles as the migration): non-objects
and empty content dropped; `id` falls back `memoryId` -> `id` -> a **content-addressed**
`sha256(...)[:20]`; `confidence` default 0.9 clamped to [0,1]; `type` derived from
`type` -> `category` -> `"fact"`; timestamps through the injected clock; cap 400.
Reads stay silent on corruption.

Verified locally:
- Byte-level parity: **identical MD5 `4261dd06c31ec2de180601f8d80e5cca`**, 92 keys, no
  differences (text/scope/fingerprint/sensitive/category/conflict helpers, tool scopes,
  suggest, the loaded and migrated store bytes, tolerant reads, recall, forget, delete
  semantics with generation counters, conflict queries).
- `cargo test -p deepseek-policy` -> 206 tests, all pass (26 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Last data slice is E (`projects`, blocked on `rag/files.py`'s `load_cached_file`).
`suggest_memory` does not persist: it builds a suggestion and fires a callback, so
`upsert_memory`, `clear_memories` and `delete_memory_by_id` are not ported and are not
needed here. Nothing is wired.

**Data layer slice E1: the projects read path (2026-09-16，uncommitted).**

The last data domain, split in two because the measurement showed the halves have very
different dependencies. **E1 is done**: the projects store — `validate_project_id`, the
whole `normalize_*` family, `read_project`, `public_project`, `list_projects`.
**E2 is not started**: `load_cached_file` plus the two branch wrappers.

`read_project` re-normalises **six** collection fields on every read, so the normaliser
family is on the critical path even for a branch that only looks at `documents` — and
`normalize_skill_run` alone has **thirty fields**. That is why a ~45-line pair of
branches needs a store-sized slice.

**A real finding: the read path mints random ids.** `normalize_skill_run` and
`normalize_saved_items` generate `f"run-{secrets.token_hex(8)}"` / `f"saved-…"` whenever a
stored entry has none, and `read_project` calls them — so **reading the same malformed
project twice returns different values**. Measured: `run-d9d3e527ae4f29df` then
`run-5acb6344a2c0e2bf`. Not persisted (read never writes back), so it is a phantom id, but
it is observable through `public_project`, which `list_projects` returns to the model. The
port keeps the behaviour and takes the source through the shared `entropy::Entropy` trait.
That is why `Entropy` moved out of `reminders` into its own module — a second user appeared.

**`OrderedJson` had a real bug, exposed here.** Store records ported so far were flat, so
nested containers were being written **compactly** where Python's `indent=2` indents at
every level. A project record is not flat. Fixed by converting nested values into real
nodes — and this mattered beyond the probe, since a memory `source` object would have hit
the same bug. Residual limit stated rather than hidden: nested object **keys** come out
sorted, because `serde_json` here has no `preserve_order`.

**Two error-shape details.** `unique_strings(None)` **raises** in the oracle (`list(None)`
is a `TypeError`), so the port reproduces that and restores the `or []` guard at all six
call sites — which is what makes the raise unreachable from ported code. And that
`TypeError` has **no code**; this port reports `invalid_payload` with a matching message, a
documented mapping rather than an invented code, so the probe compares the message and
deliberately not the code.

Verified locally:
- Byte-level parity: **identical MD5 `787f519d69e6b4a295732891fa84777b`**, 76 keys, no
  differences (11 id shapes, 7 name shapes, 8 document shapes, 13 `_safe_int` shapes, 5
  `unique_strings` shapes incl. both raises, 9 skills shapes, 6 skill runs incl. one with
  all thirty fields, saved items and artifacts with generated ids, 5 tolerant reads,
  `require_project` hit and miss, `list_projects` ordering with an invalid dir and a loose
  file).
- `cargo test -p deepseek-policy -- --test-threads=1` -> 206 tests, all pass.
  **Note:** `mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` is flaky
  under the default parallel harness (passes in isolation and serially, twice). This crate
  already has a known class of process-level shared-state interactions; run the suite with
  `--test-threads=1` when it matters.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` -> clean.

Next: **E2** — `load_cached_file` (self-contained: 32-hex id check, `PROJECTS_DIR/<id>/files`
path, JSON read, `lru_cache(64)` keyed on `(file_id, mtime_ns)`, `file_index_expired` 410)
and the two wrappers. Then the data layer is complete and `Branch::is_ported()` can be
revisited. Nothing is wired.

**Data layer slice E2: the file-cache read path and the two branch wrappers (2026-09-16，uncommitted).**

`file_cache.rs` + the `list_project_files` / `read_file_chunk` branches in `projects.rs`.
The measurement held up: `load_cached_file` really is an id-shape check, a path
derivation, a JSON read and a cache, so the rest of that 1,494-line RAG module stays
untouched. **The data layer is now complete** — reminders, memory, the shared scorer,
projects.

Three details that are easy to get wrong, and were:

1. **The `lru_cache(64)` only applies without a project id** — a project-scoped read
   always re-reads. Key is `(file_id, mtime_ns)`, which is what stops a changed file
   hitting a stale entry. `FileCache` reproduces the bound and move-to-front-on-hit.
2. **`if project_id` tests the RAW value, not the stripped one.** A whitespace-only
   project id is truthy, so it reaches `project_file_cache_dir`'s shape check and fails
   with a 400 — it does **not** fall back to the global cache. The wrapper
   (`read_file_chunk`) strips first and passes `None`, so a blank id from the tool *does*
   use the global path. **Two different behaviours for a blank id, one call apart**; the
   probe caught my first version collapsing them.
3. **`int()` here is the bare one, not the store's `_safe_int`.** `"3.7"` and `"abc"`
   **raise** rather than falling back; a float truncates toward zero; `"1_0"` and
   `"  8  "` parse. `python_int` is deliberately separate from `safe_int`, with the same
   documented mapping as the projects `TypeError`.

Also faithful: `preview` is capped at **500** in the tool projection but **1800** in the
store; `count` sums the *emitted* files (after both caps); a `chunks[index]` that is not
an object is a 404, not a skip.

Verified locally:
- Byte-level parity: **identical MD5 `5baaaba2565bed0542b652627891039d`**, 29 keys, no
  differences — 4 file-id shapes, missing/malformed/scalar indexes, the project-scoped
  path, the blank-id 400, the `project_file_cache_dir` path, 13 chunk cases (default,
  explicit, zero, negative, out of range, non-dict chunk, missing/非-list `chunks`,
  project-scoped, invalid project id), and `list_project_files` named/missing/invalid-id
  plus the full `list_projects` payload shape with its two caps.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` -> **214 tests, all pass**, run
  twice.

**One open item, stated plainly.** `mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly`
failed **once** during this slice and passed on every other run — in isolation, serially,
and in two full serial runs. The symptom is a thread panicking inside its scope. Likely
cause is **parity, not a defect**: `lock_exclusive` reproduces `LK_LOCK`'s "retry once a
second, give up after ten attempts", so under contention the gate **errors** after ~10s
where a plain blocking lock would have waited — the oracle does the same. The test's
`.unwrap()` turns that refusal into a panic. **Not root-caused.** If it is the retry
budget the fix belongs in the test, not the lock semantics; if it is not, something else
is sharing state between tests, and that matters. Re-examine before wiring.

**Gate fidelity fix: poisoning is a failure mode the oracle does not have (2026-09-16，uncommitted).**

Follow-up to the open item recorded in E2. The intermittent failure of
`concurrent_scopes_serialize_and_count_exactly` did **not** reproduce on demand (5 further
full serial runs, all 214-green), so instead of shrugging I looked for a failure mode this
port has and the oracle does not. There was one:

**`std::sync::Mutex` poisons; Python's `threading.RLock` does not.**

`exclusive_gate` acquired `PROCESS_LOCK` with `.lock().map_err(...)?`, so once **any**
thread panicked while holding that mutex, every later acquisition in the process returned
an error — and `mutation_scope(...).unwrap()` in the test would panic with exactly the
observed shape. That was a real fidelity gap whether or not it explains this failure.

Fixed: recover from poisoning (`unwrap_or_else(PoisonError::into_inner)`) instead of
reporting it. The same applies to `MEMORY_LOCK` / `STORE_LOCK`; those call sites already
bound the whole `LockResult` and so were tolerant by accident of style — now documented as
intentional, because it is load-bearing.

**Narrowed, not closed.** Eleven subsequent full runs pass. If it recurs, the remaining
candidate is the gate's `lock_exclusive` retry budget (ten attempts a second apart,
faithful to `msvcrt.LK_LOCK`, but it means the gate *errors* under sustained contention
where a plain blocking lock would wait) — in which case the fix belongs in the test, not
in the lock semantics.

**Wiring-surface measurement (for the next slice).** `deepseek-gateway` already depends on
`deepseek-policy`, but only uses `PolicyDecision`/`codes` — it does **not** reference
`tool_dispatch` or `is_ported`. The `Branch` enum already carries all seven data variants
with `tool_name()` and `branch()` mappings. But **nothing executes them**: no caller
anywhere invokes `reminders::create_reminder` or `projects::list_project_files`. So wiring
is not "flip `is_ported()`" — it needs an executor plus routing, and end-to-end
verification. That is its own slice.

---

## E7 (2026-09-16): the gateway wiring — `dispatch()` has a production caller

**HEAD before this slice: `d92953bb` (main). The slice follows the seven data branches
being wired into the dispatcher (`432318d1`).**

The executor-plus-routing slice the measurement called for. Three pieces:

1. **`rust/crates/deepseek-gateway/src/chat_tool_loop.rs`** — the non-streaming tool
   round loop, mirroring `call_deepseek`'s loop body in the oracle's order:
   `exchange_turn` → `merge_usage_totals` → lenient `tool_calls` normalization →
   `decide_round` → `execute_tool_calls` (runner = `dispatch`) →
   `append_tool_exchange`; `force_final_answer_without_tools` at budget exhaustion;
   `final_answer` from the last turn plus the merged usage.
   - `WorkspaceBundle` (root + `FileCache` + `SystemEntropy` + `SystemClock`) is the
     oracle's module globals as one injectable object; the root comes from
     `DEEPSEEK_INFRA_ROOT`, and unset ⇒ the data branches answer
     "not enabled for this request", never a silent no-op.
   - `ToolRoundExecutor::from_env` builds the policy the oracle's
     `build_tool_policy` produces for main chat: `ToolPolicyConfig::default()`
     (capability `full`, `enforce_schema`/`require_confirm` off, `sanitize` on,
     `TOOL_POLICY_ENABLED` default on with `_env_bool` spellings) plus the
     process's `DEEPSEEK_API_KEY`/`AUTH_TOKEN` as the secrets blocklist. One
     policy object lives across the request — counters accumulate like the
     oracle's single `tool_policy`. The per-call lock recovers from poisoning
     (`PoisonError::into_inner`) for the same reason the stores do.
   - Execution runs on `spawn_blocking` (the data branches take OS file locks);
     a panicked blocking task resolves every selected slot through the batch
     layer's own "did not run" envelope rather than inventing results.

2. **`chat_execution.rs` reworked around turns** — `UpstreamTurn` +
   `turn_from_payload` (extraction does not refuse `tool_calls`; that is the
   loop's data), `exchange_turn` (the POST), `merge_usage_totals` +
   `usage_int` upgraded to Python `int()` coercion semantics (numeric strings,
   float truncation, bool), `final_answer` (keeps the facade's pre-existing
   empty-content refusal, now also covering the budget-exhausted partial turn).
   `ToolRoundsUnwired` / `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` is **deleted** from
   the non-streaming path; the SSE path keeps refusing in-band via
   `STREAM_TOOL_ROUNDS_NOT_READY` (streaming round continuation is its own seam).

3. **The route is actually reachable** — `/v1/chat/completions` now prepares the
   raw body through `prepare_chat_request` instead of re-encoding through the
   typed `ChatCompletionRequest` struct, which silently dropped every field it
   did not enumerate — including `tools`, without which the model could never
   have called anything and the loop would have been dead code on arrival.
   Malformed JSON → 400 "request must be valid JSON"; a malformed-typed field
   now surfaces as the preparation layer's own 400 instead of axum's 422.

**Honest state.** Eleven of eighteen branches execute for real. The other seven
(`browser_*`, `python_eval`, `search_files`, `fetch_url`, `create_mindmap`,
`create_pptx`, `create_document`) resolve to the visible `Tool did not run`
envelope — a degradation against the Python oracle for those tools, on an
opt-in sidecar, stated in the loop's module docs and pinned by a boundary test.
Also absent with owners: the web-search provider, `mcp__*` bridging, artifact
terminal handling, and the loop's surrounding machinery (semantic cache, memory
retrieval, scheduler, traces, budget ledger). Divergences kept on purpose are
listed in `docs/GATEWAY_TOOL_DISPATCH.md` (empty-content refusal, env-injected
root, `""` vs `null` assistant replay, no `memorySuggestions` channel).

**Verification.**
- `cargo test -p deepseek-gateway -j 1` → 129 lib + 7 `chat_execution` boundary
  (four new: continuation through dispatch, data branch against the workspace
  incl. the fence files landing under `DEEPSEEK_INFRA_ROOT`, budget exhaustion
  incl. the `MAX_TOOL_ROUNDS + 2` turn count and `tool_choice: "none"`, unported
  branch honesty) + 6 `chat_stream` + 2 control-boundary — all pass.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → 223 pass.
- `cargo clippy -p deepseek-gateway -p deepseek-policy --all-targets -- -D warnings`
  → only the pre-existing `control_proxy.rs:20` `result_large_err` (byte-identical
  to HEAD; local rustc 1.97.1 vs declared 1.85). One same-class local-toolchain
  lint (`unnecessary_sort_by` in `python_json.rs`) fixed mechanically — the two
  sort forms are identical.
- `cargo fmt --all -- --check` clean.

**Next.** The streaming tool loop (SSE round continuation interleaved with
`system_note`), the web-search provider behind `ExecutorContext.web_search`, and
the `schema_for_tool` catalog.

**Bug fix: the streamed tool-call accumulator coerced values the Rust way, not Python's
(2026-09-16，uncommitted).**

Found while checking whether E7's `ToolCallAccumulator` needed anything before the
streaming loop is built on it. It did — and the accumulator is **committed code from an
earlier slice**, so these are pre-existing bugs, not fallout from E7.

The oracle reads the delta index through a bare `int()`:

```python
index = len(accumulator) if index_value is None else int(index_value)
```

This port read it with `as_i64()`, which is strictly narrower. Measured against the real
`merge_stream_tool_call_deltas` before changing anything:

| delta | oracle | this port (before) |
| --- | --- | --- |
| `"index": "2"` | slot **2** | `len(accumulator)` = 0 |
| `"index": true` | slot **1** | `len(accumulator)` = 0 |
| `"index": 2.7` | slot **2** | `len(accumulator)` = 0 |
| `"id": 123` | `"123"` | placeholder `call_1` |

**The index decides which tool call a fragment lands in.** Sending three of those to
`len(accumulator)` merges the arguments of unrelated calls into one slot — a wrong tool
invocation, not a cosmetic difference. The id case is the same class the lenient
normalizer already guards with a comment ("Reading only string ids here would silently
renumber such calls"); the accumulator had the gap.

Fixed by using Python's semantics rather than Rust's: `python_int_opt` for the index,
`python_truthy` + `value_str` for `id` / `type` / `function.name` / `function.arguments`.
The slot key widened from `usize` to `i64` because `int()` accepts a negative index and
Python's dict holds one; `sorted()` then orders it first, which the new test pins.

**Consolidation this forced, and that is the real win.** `deepseek-policy` now has one
implementation of each Python coercion in `core_utils`, used by three call sites:
`python_int_opt` (the file-cache read path maps its failure to the documented 500; the
accumulator falls back to the running slot count) and `python_truthy` (the stores,
the file cache, the accumulator). `file_cache::python_int` and `projects::is_truthy`
delegate, so their committed APIs are unchanged. Two small corrections fell out of
writing the shared version: `"1__0"` and a non-finite float are both rejected by Python's
`int()` and were previously accepted.

Verified:
- Three new gateway tests pin the measured divergences and the negative-index ordering
  (`the_index_coerces_the_way_pythons_int_does`,
  `a_negative_index_orders_before_the_others`,
  `a_non_string_id_is_stringified_and_a_falsy_one_is_ignored`), plus one in
  `core_utils` for the shared coercion.
- `cargo test -p deepseek-gateway -j 1` -> 132 lib + 7 + 6 + 2, all pass.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` -> 224 pass.
- `cargo clippy` -> only the pre-existing `control_proxy.rs:20`; `cargo fmt --check` clean.

**Not reachable from DeepSeek's own API today** — it sends `index` as a JSON number and
`id` as a string, so the two implementations agree in practice. That is exactly why it
was worth fixing rather than noting: the divergence is invisible until a provider
changes shape, and then it corrupts tool-call assembly instead of failing.

**Streaming slice, step 1: the SSE decoder now yields every delta a chunk carries (2026-09-16，uncommitted).**

Prerequisite for the streaming round loop, and a real divergence on its own.

The oracle's per-chunk body in `stream_deepseek` does everything **in one pass** —
it does not short-circuit:

```python
choices = chunk.get("choices") or []
if not choices: continue
delta = choices[0].get("delta") or {}
if choices[0].get("finish_reason"): round_finish = str(...)
if isinstance(chunk.get("usage"), dict): round_usage = chunk["usage"]
merge_stream_tool_call_deltas(stream_tool_calls, delta.get("tool_calls"))   # always
if delta_reasoning: ... forward reasoning ...
if delta_content:   ... forward content  ...
```

This port's `decode_chunk` checked `chunk_has_tool_calls` **first** and returned
`UpstreamDelta::ToolCalls`, dropping the same chunk's `content`, `reasoning`,
`finish_reason` and `usage`. Confirmed by reading the oracle, not by inference.

That is not cosmetic: the dropped `content` is the text `append_tool_exchange` replays
to the provider as the round's assistant message. A round-ending chunk that also
carried prose would have lost it.

**Fix.** `decode_event` / `decode_chunk` return `Vec<UpstreamDelta>` in the oracle's
order, and `UpstreamDelta` gained payloads: `ToolCalls(Value)` (the fragments the
accumulator needs), `Usage(Value)` and `FinishReason(String)`. `forward_line` iterates
and, when a tool round appears, forwards the chunk's content **first** and emits the
refusal **last** — the earlier ordering would have put the refusal ahead of text the
model did produce, which reads as the text being the problem.

An existing test asserted the old behavior under a name that defended it
(`a_tool_call_chunk_is_typed_as_tool_calls_even_with_content`, "forwarding the prose is
the silent-flattening failure the refusal exists to prevent"). That reasoning was
wrong: the oracle forwards the prose too. The test is replaced by one that pins the
oracle's behavior, plus two more for the ordering and for the round-ending chunk's
`usage` / `finish_reason`.

**The refusal itself is unchanged and still loud.** Streaming clients still get
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` on a tool round; what changed is that they get the
round's text first. The loop body (the `for tool_round in range(max_tool_rounds + 2)`
structure, the `system_note`s, `append_tool_exchange` and the next upstream request) is
the next step — the body is an `async_stream` generator, so awaiting a new upstream
mid-stream is already possible.

Verified:
- `cargo test -p deepseek-gateway -j 1` -> 134 lib + 7 + 6 + 2, all pass (3 new, 1
  replaced).
- **SSE byte-parity holds**: `tasks/native-runtime/sse_parity_probe.py` against the Rust
  example, identical MD5 `b9129475b6bae8b1239f4529e0a50932`. Note the corpus could not
  have caught this divergence — it has no chunk carrying both `content` and
  `tool_calls`, and it could not, because this transport refuses on a tool round where
  the oracle continues. The unit tests are the right level for it.
- `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` -> only the
  pre-existing `control_proxy.rs:20`; `cargo fmt --check` clean.

**Streaming slice, step 2: the round loop. The refusal is gone (2026-09-16，uncommitted).**

`streaming_response` now runs the tool rounds, so `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` is
deleted rather than kept as a seam. Streaming clients get the same round continuation the
non-streaming path has had since E7.

The shape mirrors `stream_deepseek`'s `for tool_round in range(max_tool_rounds + 2)`:

- each round streams one upstream turn, forwarding `content` as it arrives while
  accumulating the `tool_calls` fragments into the existing `ToolCallAccumulator`;
- at the end of the round `finalize()` + `decide_round` decide: no calls → the stop frame
  and the loop ends; budget spent → `force_final_answer_without_tools` and one more turn;
  otherwise → `executor.run_round` + `append_tool_exchange` and **a fresh upstream request
  opened from inside the generator** (a response body is single-shot, so every further
  round is a new request);
- `decode_event`'s per-chunk deltas are handled inline rather than through a helper,
  because the generator has to `yield` between them and a helper cannot yield on its
  behalf. That deleted `forward_line` and the refusal constant, and an obsolete test.

The round decision, the exchange assembly, the tool execution and the usage merge are the
**same functions** the non-streaming loop calls, so the two transports cannot drift.

**What is deliberately not emitted.** The oracle's `system_note`s (`正在调用本地工具…`, the
budget notice, the `finish_reason: "length"` truncation notice) never reach this endpoint:
`openai_chat_stream` maps only `content`, `done` and `error`. Same for the per-round `usage`
— the facade's frames carry no usage field. Both are still decoded, so the loop is not
reading a shape it cannot see, but they have no wire effect here.

**Verification.** The new `streaming_continues_a_tool_call_round` drives the real
`/v1/chat/completions` route against a per-request stub upstream and asserts four things:
both rounds' content arrives in order; there is no error frame; the reminder was actually
written under `DEEPSEEK_INFRA_ROOT`; and **the second upstream request carries the
exchange** — the assistant `tool_calls`, the `tool_call_id` and the tool result content.
That last assertion is the one that would catch a loop that ran but replayed nothing.

- `cargo test -p deepseek-gateway -j 1` -> 133 lib + 7 + 6 + 1 + 1, all pass.
- `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` -> only the pre-existing
  `control_proxy.rs:20`.

**The intermittent gate failure recurred, and the poisoning fix was not the cause.**
`mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` failed once more
(223 passed, 1 failed, `--test-threads=1`) and then passed three runs in a row. That was
the honest label's payoff: it was recorded as "narrowed, not closed" precisely because the
poisoning gap was a real fidelity bug but never proven to be *this* failure. Now it is
disproven as the sole cause. Not captured this time (the reruns were green); the next
occurrence needs the panic message, which the earlier note never managed to record.

**Root cause of the intermittent gate failure: the lock file was reopened to seed it
(2026-09-16，uncommitted).**

Three rounds of this. Round 1 recorded it as "narrowed, not closed" after fixing a mutex
poisoning gap; round 2 saw it recur, which disproved poisoning as the sole cause. This
round captured the panic, and the cause was in the port all along.

**The evidence.** Reproduced on the 7th of 15 full serial runs:

```
thread '<unnamed>' panicked at mutation_gate.rs:741:58:
called `Result::unwrap()` on an `Err` value: GateError { kind: RuntimeError,
  message: "另一个程序已锁定文件的一部分，进程无法访问。 (os error 33)", code: None, status: None }
```

`os error 33` is `ERROR_LOCK_VIOLATION`: Windows refuses a **write-mode open** of a byte
range that another handle has locked, and refuses writes into it.

**The mechanism.** `exclusive_gate` created the lock file and then **reopened it for
write** to seed the byte:

```rust
if let Err(error) = OpenOptions::new().create_new(true).write(true).open(&target) { … }
else {
    let mut file = OpenOptions::new().write(true).open(&target)?;   // ← reopen
    file.write_all(b"0")?;
}
```

Between the create and the reopen, another thread can reach the OS lock on byte 0. The
reopen-for-write then fails with error 33, the code reports `GateError::misuse`, and
`mutation_scope` returns `Err` — which the test unwraps. The lock file only exists once
per workspace, so the window only opens while the file is being created, which is why it
took the full suite (many workspaces) and roughly one run in seven to hit.

**The fix is the oracle's shape, not a guess.** `_lock_file`/`exclusive_gate` in
`infra/workspace/mutation_gate.py`:

```python
try:
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
except FileExistsError:
    pass
else:
    os.write(descriptor, b"0")     # the SAME descriptor, then closed
    os.close(descriptor)
with _PROCESS_LOCK:
    ...
    with target.open("r+b") as handle:   # the only open for locking
        _lock_file(handle)
```

So the byte is written through the handle that created the file, and there is exactly one
other open — inside the process lock, for locking. **The reopen was an invention of this
port.** Fixed by writing through the creating handle.

**The regression test had to be able to fail.** `racing_first_scopes_never_fail_on_the_lock_file`
removes the lock file every round and races eight threads through `mutation_scope`, 25
rounds, because the window only opens during creation. Verified in both directions: it
passes with the fix, and it fails with the pre-fix reopen restored.

**What this round changes about the record.** Rounds 1 and 2 both said "not root-caused",
and that was right to say — the poisoning fix was a real fidelity bug, and describing it
as *the* cause would have been a plausible story standing in for evidence. The lesson is
the one already in the notes from the object-store work: a fixed bug is not a fixed
symptom until the symptom stops.

**The numbers.** Failure rate before: 1 in 7 full serial runs (run 7 of 15). After: **0 in
15**. The regression test, run against the pre-fix code restored temporarily: **failed on
run 2 of 5** at the thread's assert — so it is a test that can fail, not decoration. Run
against the fix: 5 of 5 green. Both directions measured, not asserted.

`cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` -> clean
(the only output is a transient `deepseek-core` incremental-artifact copy warning, which
is not a lint). `cargo fmt --all -- --check` -> clean.

**Tool catalog ported, generated rather than transcribed (2026-09-16，uncommitted).**

`schema_for_tool` and the web-search provider were the two remaining items. Measuring
them showed the catalog is the **shared** dependency — `schema_for_tool`,
`tool_parameter_schemas`, `agent_tool_definitions` and `tools_for_payload` all sit on it —
so it came first, and the web-search provider is its own slice (below).

**The 28 definitions are not typed out by hand.** They are the oracle's own
`json.dumps(available_tool_definitions(), ensure_ascii=False, indent=2)` bytes, 40,634 of
them, committed as `rust/crates/deepseek-policy/assets/tool_catalog_v1.json` and embedded
with `include_str!`. Hand-copying 40 KB of descriptions and JSON Schema is where typos
live, and a typo inside a `parameters` block would silently change what the model may
send. The probe's `asset::json` case is the guard: it compares the embedded text with the
oracle's rendering, so the committed bytes cannot drift without the diff failing.

**Three divergences caught by reading the oracle instead of guessing.** The appended
external-MCP definition is not what a reasonable guess produces:

```python
tools.append({
    "type": "function",
    "function": {
        "name": profile.bridged_name,     # a profile field, not derived from `tool`
        "strict": True,                   # easy to miss entirely
        "description": f"[External MCP: {profile.server}] {schema_desc}",
        "parameters": parameters,
    },
})
```

and `parameters` is `raw_schema if raw_schema.get("type") == "object" else
{"type": "object", "properties": raw_schema}` — the **whole** raw schema goes under
`properties`, not its entries. My first version derived `mcp__{tool}` as the name, omitted
`strict`, and spread the free-form schema's keys into `properties`. All three are now the
measured shape.

**The external arms are injected, with a documented default.** `schema_for_tool`'s
`mcp__` branch and `agent_tool_definitions`' appending both need `infra.mcp.bridge`. They
take an injected provider / profile list, and `None` reproduces the oracle's own
`except Exception: pass` degrades-to-local arm — so the default is the oracle's behaviour
rather than an invented one.

Verified:
- **Catalog parity holds**: identical MD5 `a2fa62de54c6f95e85065f6c008b8e58`, 18 keys, no
  differences — the asset bytes, the 28 names in declaration order, the schema index
  (count, sorted names, four full schemas), eight `schema_for_tool` cases including the
  trim, the unknown name and the empty string, and `agent_tool_definitions` with no
  bridge.
- Six unit tests cover the arm the probe cannot reach: an `mcp__` name with a bridge, a
  non-object profile schema (rejected by `schema_for_tool`, wrapped by
  `agent_tool_definitions`), and an unknown external name.

**The web-search provider is measured and left, on purpose.** It is not pure:
`_perform_web_search` needs `search_single_round` (a real Tavily HTTP call),
`tavily_api_key`, a per-request result cache, a citation counter, a turn limit and a
shared `search_budget`. `ExecutorContext.web_search` is already the injection point, so
the Rust side has the seam — what is missing is the HTTP integration and its config, which
is a connector-shaped slice that needs either a live key or a stub upstream to verify. Say
that plainly rather than half-wiring it.

**Also measured while sizing this, now unblocked:** `search_tool_enabled` and
`tools_for_payload` are pure and depend only on the catalog plus `search_mode`. They are
the natural companions to this slice whenever the search provider lands.

**Tavily search layers 1+2 ported: query planning, normalization, ranking, cache (2026-09-16，uncommitted).**

`search.rs` now carries everything the `web_search` tool branch needs from
`infra/tool_runtime/search.py` **except the HTTP call**. The boundary is a dependency
closure, not taste:

- **`format_search_context` / `format_search_failure_context` are not in it.** They build
  the *prompt context* at request-assembly time; the tool branch returns a compiled tool
  result and never calls them. Porting them would be porting a different consumer.
- **`search_tavily` / `search_tavily_with_retry` are not in it either** — but their retry
  *policy* is ([`should_retry_tavily_error`], [`simplified_retry_query`]). Only the request
  itself is missing, which is the next slice.

**One divergence, caught by the probe.** `domain_from_url` is
`urlsplit(url).netloc.lower().removeprefix("www.")` — and `netloc` is the **whole
authority, userinfo and port included**. Extracting just the host reads as the obvious
cleanup and is wrong: for `https://user:pw@Host.COM:8443/x` the oracle returns
`user:pw@host.com:8443` and my first version returned `host.com`. The probe diff was a
single line out of 97 keys. It is now the whole netloc.

This matters beyond the field itself: `search_result_score` feeds `domain` into
`TRUSTED_DOMAIN_HINTS` with a `contains` check and `rerank_search_results` uses it as the
per-domain diversity key, so a narrowed domain changes both ranking and the
two-per-domain cap.

**A recurring trap, hit a third time.** `serde_json`'s `json!` does not accept a **block
expression** as a value, so `"retryQuery": { let v = ...; if ... { v } else { json!("") } }`
fails with `unexpected end of macro invocation`. The fallbacks have to be hoisted into
`let` bindings first. Same class as `.iter()` on a temporary and `&"x".repeat(n)` in a
`Vec<&'static str>`: the macro's accepted grammar is narrower than the expression grammar.

**What is reproduced rather than tidied:** `search_cache_key` lowercases while its callers
pass the raw query (so the cache is case-insensitive by construction);
`save_search_cache` **prunes before writing**; the temp file is `with_extension("tmp")`,
which replaces `.json` rather than appending; `search_result_score`'s weights (score × 20,
title token +8, body token +3, trusted domain +10, official-docs +6, empty snippet −8) and
`rerank`'s two-per-domain cap run after the sort.

Verified:
- **Search parity holds**: identical MD5 `a425954350aeea8c6d935c476bda169e`, 97 keys, no
  differences — six query shapes through nine distinct functions, nine `should_search_for_query`
  cases across four modes, six intents, five URL authorities, three `normalize_search_response`
  shapes, eight per-result scores, the reranked URL order, the full aggregation (status,
  joined answer, reason, result URLs, normalized rounds), two compactions, round statuses,
  round ordering, and two cache round-trips.

**About the pasted credentials.** A live Tavily key and what appears to be an upstream API
key were pasted into the chat. Neither was written to any file (verified with a repo-wide
grep), neither was persisted as an environment variable, and all probe artifacts were
deleted. They still appear in this conversation's transcript, so both should be **rotated**
regardless of what this session did with them.

**What is left.** The HTTP layer: `search_tavily` (request body assembly, the `TAVILY_URL`
POST, `AppError` mapping for a missing key and for upstream failure) plus
`search_tavily_with_retry`, and a shared clock for `load_search_cache` /
`cleanup_search_cache` / `save_search_cache` (their `now_epoch` parameter is already there,
so only the wiring is missing). Verification for that slice is a **stub upstream**, because
the live path measured ~5% availability — one clean `http=200` in roughly forty attempts,
amid 308/405/400/301/502/522 from the proxy and its intermediaries. A real-call check stays
a one-off confirmation, not a regression test.

**Tavily HTTP layer ported, with the transport injected (2026-09-16，uncommitted).**

`search.rs` now carries `search_tavily`, `search_tavily_with_retry`, `format_upstream_error`
and the request-body assembly. That completes the module's non-cryptographic surface: what
is still missing is only the **client**, not the logic.

**The transport is a parameter, not a call.** `search_tavily(query, api_key, transport)`
takes a `dyn Fn(&str, &[u8], &[(&str, &str)]) -> TransportOutcome`, so the whole path —
body assembly, header construction, status mapping, response normalization, retry policy —
runs offline. That is what made the parity probe possible without a network, and it is why
the measured ~5% link availability does not block this slice.

`TransportOutcome` has three arms on purpose: `Response` (any status, with its body),
`Failure { reason, timed_out }` (the request never completed — the oracle's `URLError`
branch), and `Rejected(AppError)`. The third exists **for the probe**: the Python probe
drives the retry policy by raising an `AppError` from a stubbed `search_tavily`, so without
it the Rust side would be comparing "error mapping **and** retry policy" against Python's
"retry policy alone". The arm makes the layers line up.

**A real divergence, caught by the probe.** `format_upstream_error` is:

```python
message = error.get("message") or error.get("type")
if message: return str(message)
```

The `or` tests the **values' truthiness**, so `{"error": {"message": "", "type": "x"}}`
returns `"x"`. My first version checked the key's presence and then whether the rendered
text was empty, which fell through to the raw text instead. Fixed to filter both lookups
through `python_truthy`.

**A byte-level detail worth naming: `json.dumps` defaults to `ensure_ascii=True`.** The
request body sends `{"query": "\u6700\u65b0\u6d88\u606f"}`, not the raw UTF-8. Escaping is
CPython's exactly — BMP as a lowercase `\uXXXX`, an astral character as a lowercase
surrogate **pair** (`\ud83d\ude00`). Added `dumps_default_separators_ascii` /
`escape_non_ascii` to `python_json` for it. This is not cosmetic for a request body: the
bytes are what leave the process.

The body's **key order** is the oracle's dict-merge order — `query`, then the options in
their insertion order, then the filters — and `search_depth` / `include_answer` /
`include_raw_content` are *updated in place* by the intent rules, so they keep their
positions rather than moving to the end. `tavily_request_body_json` renders that order
explicitly, so the probe compares the bytes rather than a re-serialization.

**Two probe-side fixes, so the comparison is honest.** Python's f-string renders an enum
*member* (`ErrorCode.UPSTREAM_TIMEOUT`), not its value, so the stub messages had to use
`code.value`. And the Python fake replaces the whole `search_tavily`, so it has to apply
`normalize_search_response` itself — otherwise the two sides are compared at different
layers and the response bodies diverge for a reason that is not the implementation's.

Verified:
- **Search parity holds**: identical MD5 `b5077e1e730dfac3ffde3e024c6094cf`, **113 keys**
  (up from 97), no differences. The HTTP additions are five request bodies — including a
  600-character query, which exercises the `[:500]` truncation — six
  `format_upstream_error` inputs, and five retry-policy drives (first-call success, retry
  after a timeout, retry after a 503, both attempts failing, and a missing key that must
  **not** be retried).

**What is left.** A concrete `Transport` (a `reqwest::blocking` client honouring
`TAVILY_TIMEOUT_SECONDS`), the shared clock for the three cache functions, and the
`ExecutorContext.web_search` callback that binds them — the seam already exists, so this is
wiring rather than logic. Verification stays a **stub upstream**; a live call is a one-off
confirmation, since the measured link was one clean `http=200` in roughly forty attempts.

**Scoping: `format_search_context` is one link in an unported, security-bearing pipeline
(2026-09-17，measured not started).**

Both remaining `format_*` functions were previously listed as "the next slice". Measuring
the call path says they are not a slice of their own — they are the last step of a pipeline
whose other links, including a security module, are unported. Writing them alone would be
inert code with no consumer.

The pipeline, from `deepseek_client.py`:

```python
search_data = search_if_needed(payload, progress_callback=…, system_note_callback=…)
...
if search_data and search_data.get("results"):
    # Context Taint firewall: web content is untrusted — isolation-wrap and
    # scrub the per-turn search context before it joins the prompt.
    payload = {**payload, "searchContext": context_taint.harden_search_context(
        format_search_context(search_data))}
elif search_data and search_data.get("status") == "error":
    payload = {**payload, "searchContext": format_search_failure_context(search_data)}
prepared = build_deepseek_request(payload, stream=stream, memory_state=memory_state,
                                 validated=validated)
```

**Measured size of the missing links:**

| link | size | notes |
| --- | --- | --- |
| `search_if_needed` | ~35 lines | gates on `searchEnabled is True` **and** `forced_search_mode`; raises `INVALID_PAYLOAD` on an empty query; emits up to four `system_note`s |
| `search_multiple` | ~45 lines | **parallel** rounds (`ThreadPoolExecutor`, `SEARCH_ROUND_LIMIT` workers) — the only concurrent part of the search module |
| `format_search_context` / `_failure_context` | ~55 lines | the two functions originally scoped as "next" |
| **`context_taint.harden_search_context`** | **383-line module, 18 public items** | a **taint firewall**: `sanitize_external_text`, `UNTRUSTED_CONTENT_GUARD`, `taint_enabled()`, feature flags |
| `searchContext` → `build_deepseek_request` | — | **the consumer does not exist in Rust**; the gateway passes the prepared body through |

**Why this is a separate vertical slice, not an extension.** `searchContext` is consumed by
`build_deepseek_request`, which the Rust gateway does not own — the route prepares the raw
body and forwards it. So the pipeline's output has nowhere to go until the request-assembly
layer exists, and that layer is where the earlier recorded layering lesson lives
(`build_deepseek_request` composes the system turn from `payload["systemPrompt"]`).

**Recommendation.** Treat this as its own slice with the taint firewall as its centre, not
as a tail of the tool-round work. The ordering that keeps every step verifiable:
1. the pure predicates and the two formatters (byte-parity, offline) — inert until 3, so
   they commit safely;
2. `search_multiple`'s parallel shape, which is the part with real concurrency semantics;
3. `harden_search_context` and `sanitize_external_text` against the reference's own tables
   — this is a security boundary, so it needs the same treatment the IP-block sets needed:
   read the reference's data, do not rebuild the predicate from intuition;
4. the `searchContext` injection once `build_deepseek_request` exists to consume it.

Nothing here was started.

**Search-prefetch slice 1: the pure predicates and the two formatters (2026-09-17).**

Step 1 of the order recorded above, landed in `deepseek-policy::search`:
`search_mode`, `forced_search_mode`, `search_tool_enabled`,
`format_search_context`, `format_search_failure_context`. Byte-verified offline;
inert until the assembly layer exists, so nothing calls them yet.

**This slice resumed an interrupted working tree, and the interruption was not
clean.** The three modified files had never run: the Rust example failed to
compile (five `cannot find function` errors) and the Python probe crashed with
`AttributeError: …search has no attribute 'search_mode'`. The recovery found two
defects before anything was green:

1. **The predicates live in `gateway/deepseek_client.py`, not `search.py`.** The
   probe now extracts them verbatim from that file via `ast` (the SSE-probe
   pattern), so the definitions being compared are the oracle's own. The Rust
   port stays in this crate's `search` module because its consumers are the tool
   catalog and `tools_for_payload`; the placement is recorded in the module docs.
2. **`python_str` matched `str(x or "")` on the rendered text, not the raw
   truthiness.** A numeric `0` came through as `"0"` — an off-mode spelling —
   so `{"searchMode": 0}` made `search_mode` return `"0"` where the oracle
   returns `"auto"` (its `or` fallback is `"auto"`, which neither forces nor
   disables), **flipping `search_tool_enabled`**, and made
   `should_search_for_query` refuse where the oracle falls through to text
   matching. The same root cause would render a `Tavily 摘要` line for
   `{"answer": 0}`. Fixed through `python_truthy` + `python_json::value_str`;
   the corpus now pins every one of those shapes (mode `0`/`true`, answer `0`,
   title/citation/raw_content `0`, error `true`/`0`). This was a defect in
   **committed** code (`should_search_for_query` shares `python_str`), not just
   the interrupted WIP.

Two divergences measured and recorded rather than compared:

- A **non-dict result entry** (or a non-array `results`) makes the oracle raise
  `AttributeError`/`TypeError` and fail the request; the port renders through
  the fallbacks. Unreachable from the wired pipeline —
  `normalize_search_response` / `aggregate_search_rounds` guarantee dict entries
  in a list — and reachable only from a hand-corrupted cache file, where the
  oracle's own behaviour is an uncontrolled 500. Pinned by a unit test so the
  tolerance is a recorded decision, and the corpus case that would have crashed
  the Python probe was dropped. The *failure* formatter keeps its non-dict round
  entry, because there the oracle guards with `isinstance` and both sides agree.
- The query line renders containers through `value_str`'s JSON quoting — the
  crate's standing `repr` approximation, unreachable from the wired pipeline
  where `query` is always a string.

Verified locally:

- Byte-level parity: **identical MD5 `0f3877807ce994f0d3dbe852293c47ee`**, 157
  keys (up from 113), no differences, re-confirmed after `cargo fmt`.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → **237 tests, all
  pass** (6 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  → clean; one real find fixed (`useless_vec` on the example's corpus, which had
  never been clippy'd). The only other output is the transient `deepseek-core`
  incremental-copy warning, which is not a lint.
- `cargo check -p deepseek-gateway --all-targets` → still compiles.
- `cargo fmt --all -- --check` → clean.

Next per the recorded order: slice 2, `search_multiple`'s parallel shape (a
`ThreadPoolExecutor` over `SEARCH_ROUND_LIMIT` rounds — the module's only
concurrency), then the taint firewall against the reference's own tables, then
the `searchContext` consumer.


### Slice 2 landed; the lib-test harness regressed with it (2026-09-17, measured)

`search_multiple` is ported and committed (`e2354851`). It is the module's only
concurrency: cache gate, query planning, per-round "searching" announcements,
`as_completed`-style collection, the two error arms, the cache write, the
progress callback. Parity is byte-identical — 2461 lines, md5
`84b6f0f6b7c90b2d2a07f08d138659ae` on both sides; the probe grew the whole
`multi::` family (first run, announcements, cache hit, expiry, reversed
completion order, API error, worker exception, empty query list). `cargo fmt
--check` and `cargo clippy --all-targets` are clean.

**The harness no longer starts, and it did at slice 1.** The previous entry in
this file records `cargo test -p deepseek-policy -j 1 -- --test-threads=1` →
237 tests all pass, and that text came in with slice 1 (`6e6519cf`, 11:06). At
12:46 the same command dies before running anything:

    error: test failed, to rerun pass `-p deepseek-policy --lib`
      process didn't exit successfully: ... (exit code: 0xc0000139,
      STATUS_ENTRYPOINT_NOT_FOUND)

What was measured, not assumed:

- Of the 193 imported symbols in that binary, exactly one is unsatisfiable: it
  binds `WakeByAddressSingle` to `KERNEL32.dll`.
- This Windows build's `kernel32` exports **none** of `WakeByAddressSingle`,
  `WakeByAddressAll`, `WaitOnAddress`. Verified at the loader's own API with
  `GetProcAddress` (a five-line C probe): all three MISSING in kernel32, all
  three PRESENT in kernelbase. Two other checks agree (`grep` for the name in
  the DLL is 0 for kernel32, 1 for kernelbase; `objdump -p` the same).
- The other seven test binaries in `target/debug/deps` — deepseek-core's and
  deepseek-gateway's among them — bind those three to
  `api-ms-win-core-synch-l1-2-0.dll` (which the loader maps to kernelbase) and
  start normally. So does this crate's own `search_parity_probe` **example**
  after a clean rebuild.
- `cargo clean -p deepseek-policy` followed by a relink reproduces the bad
  binding, so it is not a stale artifact.

**The causal picture, stated honestly.** The example links the same lib code —
including `search_multiple` and its threads — and binds the api-set, so the
ported logic is not what breaks the import. What the lib-test target adds over
the example is the `#[cfg(test)]` code plus `libtest`, and it is one of those
that flips which of the two competing `__imp_WakeByAddressSingle` stubs the
linker takes (raw-dylib stubs carry their own DLL name, and `ld` keeps the
first). Slice 1's harness booted, slice 2's does not, and slice 2's only new
code is `search_multiple` plus its tests — so the correlation points at the new
test code, but **causality was not isolated**. Next diagnostic round: relink and
run with slice 2's unit tests removed, which separates "the new tests pull it"
from "the crate now links something else".

This is written down rather than fixed because it is not the ported logic, and
because guessing at the linker would be exactly the kind of change this project
does not want: the verification for slice 2 is the probe, which does pass.


### Isolation done: the harness failure is a link-shape fragility, not a test bug

The previous entry left the next round as "relink with slice 2's unit tests removed".
That was done, by disabling tests one at a time with `#[cfg(any())]` in front of the
`#[test]` attribute (the source was byte-restored afterwards; `git diff` is empty).
Each round relinked and ran the lib harness:

| new tests enabled | harness |
| --- | --- |
| none | **boots: 237 passed, 0 failed** |
| `an_empty_query_aggregates_without_searching` | dies, 0xc0000139 |
| `a_cache_hit_returns_without_searching` | dies, 0xc0000139 |
| those two together | dies, 0xc0000139 |
| all five | dies, 0xc0000139 |

With all five disabled the count is exactly 237 — slice 1's number — and
`search_multiple` is still in the lib, so the *library* code is not what breaks the
import. Enabling **any one** of the five is enough, and the two single tests are
trivial: no worker thread is spawned, no panic occurs, the transport is a closure that
is never called. So it is not the content of a test. What flips the import is the
lib-test target acquiring a reference to `search_multiple` at all — i.e. **the test
binary growing changes which competing import stub the linker keeps**. The same
reference in the **example** target binds the api-set and runs, so the outcome is
target-dependent, not source-dependent.

Where the stubs come from, checked: rustc's self-contained
`lib/rustlib/x86_64-pc-windows-gnu/lib/self-contained/libsynchronization.a` does
provide `WakeByAddressSingle`, and inside it the DLL name is
`api-ms-win-core-synch-l1-2-0.dll` — the correct one. `/d/mingw64`'s copy of the same
archive agrees. `libkernel32.a` (both the toolchain's and mingw64's) does **not** define
the symbol at all. So the KERNEL32 binding that appears in the failing binary is not
coming from those archives; it comes from a crate-level raw-dylib stub, i.e. some object
compiled against `kernel32.dll`, and which stub wins is decided by link order. Pinpointing
that object needs the std sources, and `rust-src` is not installed on this machine —
that is where this stopped, deliberately, rather than guessing further.

**Consequences.** (1) There is no "guilty test" to rewrite; the fragility will resurface
whenever this target's object set changes. (2) Slice 2's verification stays the probe,
which is byte-identical. (3) If the harness is wanted back, the options are a link-level
workaround (`RUSTFLAGS` with an explicit stub order or `-C link-self-contained`), a
toolchain pin, or installing `rust-src` to name the offending object first — none of
which belongs in a migration commit.

Corrects the earlier framing in this file: the regression did arrive with slice 2, but
it is not caused by slice 2's code or tests; slice 2 is what made the test binary big
enough to expose it.


### Slice 3 landed: the taint firewall's string layer (`591d0c38`)

`context_taint.rs` now carries the part of `gateway/context_taint.py` that has no I/O and no
consumer-dependent shape: the guard constant and the trust/source/marker vocabulary, both
pattern tables, the sensitive-tool alternation, `scan_text`, and the active hardening
(`harden_search_context`, `file_context_guard_line`, `escalation_enabled`) with
`ContextTaintSettings` at the oracle's defaults.

**Both tables are read off the reference, not rebuilt** — the lesson from the IP-block sets:

- the sensitive-tool list is *derived* from `TOOL_METADATA` with the oracle's own predicate
  (`requires_confirm || sensitive_sink || risk == "high"`), so a tool profile change cannot
  desynchronise the two. It comes out as eight names, in table order, and the alternation is
  order-bearing because alternative branches are tried left to right.
- the injection count is `tool_policy::sanitize_external_text`, the Tool Policy Engine's own
  sanitizer, because the oracle shares that table between the two modules.
- the exfiltration verb list keeps the oracle's deliberate exclusion of `提交`: it trips on
  benign advisory prose like `不要提交到仓库`, while genuine exfiltration in this corpus uses
  `发送` / `上传` / `发到`. The corpus pins that case, plus a 70-character gap (one past the
  pattern's `{0,60}` lifetime), a newline inside the gap, and `web_search` not matching the
  sensitive alternation — all four must *not* fire.

Verification is `tasks/native-runtime/context_taint_parity_probe.py` against
`examples/context_taint_parity_probe.rs`: 35 texts plus six flag combinations, 87 keys,
**byte-identical**, md5 `adff8e2723c890fe8c969d21e8d1fa0c`. The Python side imports the
oracle module directly rather than re-executing extracted source, because `context_taint`
only pulls `core.config` and `tool_policy` and both import cleanly — so the tables under
test are literally the oracle's own objects. `cargo fmt --check` and
`cargo clippy --all-targets` are clean (exit 0).

**Deliberately left out, as the honest boundary**: `_risk_level`, `classify_request_messages`,
`build_taint_report`, `report_is_tainted`, `taint_status`. That is the diagnostics half, and
its consumer — the gateway's diagnostics assembly and the `/api/taint` route — does not exist
in Rust. It is inert in a way this layer is not: `harden_search_context` is exactly what
slice 4's `searchContext` injection calls.

No unit tests were added with the module: the lib-test harness still cannot start on this host
(the link-shape finding above), so such tests could not be run, and the probe is the
verification that actually executes. That is a real gap to close once the harness boots.

Remaining: slice 4 — the `searchContext` injection into `build_deepseek_request`, which has to
exist first — and, separately, `search_if_needed`, which is what eventually calls
`search_multiple`.


### Slice 4 landed: the per-turn context, and the reader of `searchContext` (`4056e3c9`)

`dynamic_context.rs` carries `build_dynamic_turn_context` — the function that reads
`payload["searchContext"]` — plus everything it splices in: `format_current_time_context`,
`format_context_summary_context`, `format_memory_notice`, `format_slides_skill_context`,
`presentation_intent_requested` (over the already-ported `latest_user_query`),
`append_context_to_latest_user`, and the constants (`CURRENT_TIME_CONTEXT_HEADER`,
`CONTEXT_SUMMARY_MAX_CHARS = 12 000`, `WEB_SEARCH_SYSTEM_HINT`, the three slides
name/reference/guidance strings).

This closes the loop the earlier scoping note described: `harden_search_context` had a string
with nowhere to go, and this is the thing that puts it in the prompt. The ordering is the
whole design — the search context goes **after** the stable prefixes, so switching search on
and off does not invalidate the prompt cache behind it.

**The one real design decision: the clock is injected, not read.** The oracle calls
`datetime.now().astimezone()` and renders the machine's local zone. Rust's standard library
has no local-timezone support, and this workspace has **no time crate at all** — only
`std::time` epoch arithmetic. So `LocalNow` carries the instant, the offset and the zone name,
following the two precedents already in this tree: `utc_now_iso(epoch_seconds)`, whose doc
says "the clock is a parameter so callers can pin it", and the injected search transport.
**Resolving the OS zone is not implemented**, deliberately and visibly: faking it would be
worse. The oracle's naive-datetime arm (`tzinfo is None` → assume UTC, then convert to the
*machine's* local zone) has no counterpart for the same reason, and is excluded from the
corpus because its output is host-dependent.

Three details that would each be a silent divergence if "cleaned up":

- **Two spellings of the same instant coexist.** `format_current_time_context` renders UTC as
  `…Z`; `core_utils::utc_now_iso` renders `…+00:00`. The oracle replaces the suffix in exactly
  one of the two places, so `isoformat_seconds` (new in `core_utils`, sharing
  `civil_from_days` with `utc_now_iso`) appends the offset and leaves the choice to its caller.
- **The slides text is transcribed with `concat!` and explicit `\n`,** not as a multi-line raw
  string: a raw string takes its line endings from the source file, so a CRLF checkout would
  silently change every prompt byte those constants feed. Git confirmed the risk is live —
  committing these files printed `LF will be replaced by CRLF the next time Git touches it`.
- **A landmine is recorded for the assembly slice.** `append_context_to_latest_user` appends
  `{"role": …, "content": …}` and the oracle's body serializes in insertion order, but
  `serde_json::Map` here is a `BTreeMap`, so `json!` emits `content` first. Whoever writes the
  body builder must not let `json!` decide the order of the message it injects.

Verification: eight pinned instants (UTC, +08:00, −05:00, +05:30, −09:30, epoch 0, and two
day-rollover cases), the full assembly over fourteen payload/memory/tools combinations, both
12 001-character truncation paths, and the append cases — **byte-identical**, 39 keys, md5
`e3a065e999df38e421de8a17f74cfef6`. The Python probe imports the oracle modules directly and
stubs `format_current_time_context` for the assembly cases, because the oracle's builder reads
the machine clock; the mirror of that stub is the injected clock on this side. `cargo fmt
--check` and `cargo clippy --all-targets` are clean, and **both** this probe and slice 3's were
re-run after formatting so the committed bytes are the verified bytes (`e3a065e9…`,
`adff8e27…`).

**The honest remaining boundary.** The reader exists, but `build_deepseek_request` — the body
assembly that would actually consume `build_dynamic_turn_context` — still does not exist in
Rust: the gateway prepares the raw body and forwards it. So nothing injects into a request yet,
and this slice is inert in the same recorded sense as slices 1–3. Also outstanding: the OS
timezone resolution, `search_if_needed`, and the taint diagnostics half. No unit tests came
with this module, for the same reason as the last one.


### `build_deepseek_request` was next, and measuring says it is not a slice (`9b3a7825`)

The obvious next move after slice 4 was the assembly function that would finally consume
everything: `build_deepseek_request`. Measuring it first, the way the earlier scoping pass
should have, says **do not start it as one slice** — and the shapes below are what that
judgement rests on.

The function itself is only ~123 lines (`deepseek_client.py:240`–362), but it is a
convergence point, not a unit. Its dependency closure, measured:

| collaborator | size | ported? |
| --- | --- | --- |
| `model_router.py` (`route_request`, `is_auto_request`) | 279 lines | no |
| `budget_manager.py` (`budget_policy_from_payload`, `should_downgrade`, `budget_scope`) | 371 lines | no — and it owns a **ledger**, so it is not pure |
| `context_manager.py` (`manage_request_body`, `merge_context_manager_diagnostics`) | 137 lines | no |
| `validate_deepseek_payload` + `_validate_request_messages` + `normalize_chat_messages` | ~110 lines | partly — the gateway has its own `prepare_request`/`normalize_*`, but not these |
| `chat_payload.count_payload_attachments` | 32 lines | no |
| `empty_memory_state`, `_has_image_content`, `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`, `TOOL_PARALLEL_SYSTEM_HINT` | ~75 lines | no |
| `context_taint.build_taint_report` | 39 lines | no — until this commit |

So the closure is ~1,200 lines across six subsystems, one of which is stateful. That is a
milestone. The useful thing to do with a milestone is find its slices, and the first one was
already sitting there: **line 357 needs `build_taint_report`**, which is exactly what slice 3
deferred on the note that its consumer did not exist. Measuring the consumer turned it up, so
this commit ports the diagnostics half and **`context_taint.py` is now complete**.

What the classification half turned on, all pinned by the corpus:

- **`len()` counts characters, and `_segments_for_user` uses a found index as a length.** A CJK
  prefix before the file marker inflates the trusted-prefix segment if the index is treated as
  bytes; the corpus pins the case that would catch it (中文提问… → `chars: 4`).
- **The arm order in `tool_message_source` is the contract**: `browser_` and `mcp__` before the
  metadata table, and `search_files` reaches the RAG arm only when the payload says `local_rag`.
- **`segments_for_per_turn_system` inserts at 0 and 1** — that is what puts the media segment
  first and the trusted prefix before the web segment.
- The serialization landmine recorded for slice 4 applies to this block too: `build_taint_report`
  builds its object in insertion order and the caller splices it into `diagnostics`; `json!` here
  yields sorted keys, so the diagnostics serializer must own that order.

Verification: 176 keys, byte-identical, md5 `49e390b4c0c9f2ab499337326b308404` — 29 message
lists, 4 settings tuples × 6 bodies, the `taint_status` block and 8 risk combinations, on top of
slice 3's 87 keys (the earlier cases are still in the same probe, now 176). The first comparison
**failed**, and the cause was the probe corpus rather than the port: the Python body list indexed
one case off from the Rust one, and only the Python side needed changing to make the hashes agree.
That is the method working — the diff localised the fault before it became a story about the port.

**Sequence for the milestone, in the order that keeps each step verifiable.** None of these is
started:
1. the remaining pure collaborators (`empty_memory_state`, `_has_image_content`,
   `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`,
   `count_payload_attachments`, `TOOL_PARALLEL_SYSTEM_HINT`) — small, and each has an oracle
   function to compare against;
2. `context_manager` (137 lines) — pure but with the sliding-window semantics that make the
   body's bytes; needs its own probe over windowed bodies;
3. `model_router` (279 lines) — pure tier selection, but it needs a model catalog to compare
   against, so measure that dependency before assuming;
4. `budget_manager` (371 lines) — **last, and only with a store port**: it reads a ledger, so it
   is the one piece here that is not a pure function, and the ledger's own port would have to
   come first;
5. the assembly itself (`build_deepseek_request`), once its collaborators exist, with the
   diagnostics serializer that owns key order.

Until step 5 lands, every slice so far remains inert in exactly the recorded sense: verified
and unwired.


### The harness works again, and the earlier mechanism note was wrong (2026-09-17)

`cargo test -p deepseek-policy --lib` runs again: **242 passed; 0 failed** — 237 from before
plus the five slice-2 tests that had never been able to execute. The fix is one flag:

```
RUSTFLAGS="-C link-self-contained=yes" cargo test -p deepseek-policy
```

**Correction first.** The earlier entry here explained the failure as "two competing raw-dylib
stubs and `ld` keeps whichever it sees first". That was wrong. Asking the linker directly settles
it:

```
RUSTFLAGS='-C link-arg=-Wl,--trace-symbol=__imp_WakeByAddressSingle' \
  cargo test -p deepseek-policy --lib --no-run

warning: linker stderr: D:/mingw64/bin/../lib/gcc/x86_64-w64-mingw32/8.1.0/../../../../x86_64-w64-mingw32/lib/../lib/libkernel32.a(dqifs01464.o): definition of __imp_WakeByAddressSingle
```

The provider is **`/d/mingw64`'s `libkernel32.a`** — the *system* MinGW's import library, GCC 8.1.0,
from 2018, on `PATH` as `gcc`. rustc's `windows-gnu` target uses `gcc` as its linker driver, and
that driver injects its own library search path, so an import library built for a Windows era when
`kernel32` did export the futex APIs is searched — and its ordinal-era stub `dqifs01464.o` wins
`__imp_WakeByAddressSingle` from libstd's own stub. On this Windows build `kernel32` exports none of
`WakeByAddressSingle` / `WakeByAddressAll` / `WaitOnAddress` (verified with `GetProcAddress`: all
three live only in `kernelbase`), so the import is unsatisfiable and the loader stops with
`0xc0000139`.

Three things this re-explains, and one it does not:

- **Why the API-set stub never appeared to be the provider.** It *is* rustc's provider: every
  `WakeByAddress*` stub inside `libstd` (members `api-ms-win-core-synch-l1-2-0.dlls0000{0,1,2}.o`)
  has a four-byte, **all-zero** `.idata$7` — the DLL-name field. rustc does not name the DLL in the
  stub; the descriptor is chosen at link time. So a *different* archive can satisfy the symbol
  first, which is exactly what the old system import library does.
- **Why `cargo clean -p` and relinking never helped.** The offending archive is outside the target
  directory.
- **Why only the lib-test target died.** The symbol is undefined in several objects; which archive
  wins depends on the order the linker walks them, which differs per target. The examples and the
  other crates' test binaries happened to resolve it from libstd.
- **What is still unexplained, and is now moot:** why this target in particular. With the flag the
  ambiguity is gone, so there is nothing left to chase.

`-C link-self-contained=yes` makes rustc use its own bundled `rust-mingw` libraries (which ship
the correct-era import libs) instead of the system MinGW's, so the symbol resolves from libstd's
stub and the import points at the API set that the loader maps to `kernelbase`.

**It is committed, scoped as narrowly as the cause allows.** `rust/.cargo/config.toml` now carries

```toml
[target.x86_64-pc-windows-gnu]
rustflags = ["-C", "link-self-contained=yes"]
```

Scoped to the triple rather than `[build] rustflags` because the failing combination is specifically
`windows-gnu` plus a system MinGW on `PATH` — nothing about MSVC builds or other targets should
inherit the workaround. The file also carries the reasoning inline, since a config that changes link
inputs deserves its own explanation next to it. It requires the `rust-mingw` component (present here,
and installed by default for windows-gnu host toolchains).

Verified after adopting it, with no `RUSTFLAGS` in the environment:

- `cargo test -p deepseek-policy -j 1` → **242 passed, 0 failed**, in 0.77 s with the *same* artifact
  hash as the env-var run — so the config produces the same fingerprint as `RUSTFLAGS` did, and
  adopting it costs no rebuild;
- `cargo test -p deepseek-core -j 1` → 8 passed after a rebuild under the new flags, so the flag does
  not regress a crate that was already linking fine.

One caveat that survives, and is written into the config file: **`RUSTFLAGS` in the environment takes
precedence over the config**, so a stray value there silently overrides these flags and brings the
failure back along with a full rebuild. Picking one mechanism and staying with it still matters.

The consequence for the record: the "no unit tests came with the module, because the harness cannot
start" note that appears against slices 3, 4 and 5 is now **expired** — the harness starts, and
those modules can carry tests.


### The test debt is paid (`3b55437d`)

The note above said the harness starting again meant slices 3, 4 and 5 could carry tests. They do
now: 40 added, 242 → **282 passing**. `context_taint` gets the bulk, since it is the security
boundary and every "obvious" simplification in it is a divergence — the guard wrapping rather than
replacing, the `提交` exclusion, the gap neither crossing a newline nor sixty characters, the
character-counted CJK prefix, the arm order in `tool_message_source`, the per-turn split, the
media tail's position, and the report's cap-versus-totals behaviour. `dynamic_context` pins the
`Z`/`+00:00` pair that must not be unified, the cache argument (search off is byte-identical up to
the hint), the joins, and the falsy drops.

One expectation was wrong on the first run: the truncation test asserted five segments where both
implementations produce six. **The port was right and the arithmetic was mine** — and the corrected
assertion now carries the oracle's own sequence rather than a recomputed number. Same lesson as the
taint probe's mis-indexed corpus two slices ago: check the expectation before believing a failure.

These tests are the fast local net; the parity probes stay the cross-language evidence, since they
compare against the oracle rather than against expectations written by hand.


### Milestone step 1: the pure collaborators are ported (`11a08fff`)

The sequence recorded above said to take the assembly's pure leaves first. They are in, in a new
`request_shaping` module plus two functions in `memory`:

| what | where it came from |
| --- | --- |
| `TOOL_PARALLEL_SYSTEM_HINT` | `deepseek_client.py` |
| `normalize_reasoning_effort`, `tools_for_payload`, `forced_artifact_tool_name`, `should_force_create_pptx`, `has_create_pptx_tool`, `mindmap_intent_requested`, `has_image_content` | `deepseek_client.py` |
| `count_payload_attachments` | `chat_payload.py` |
| `empty_memory_state`, `memory_scope_from_payload` | `data/memory.py` |

That shrinks the closure between here and a working `build_deepseek_request` to four things:
`context_manager` (137 lines), `model_router` (279), `budget_manager` (371, ledger-backed and
therefore last), and the assembly itself with the serializer that owns the diagnostics key order.

What the corpus and the 13 new tests pin, each of which reads like a tidy-up waiting to happen:

- **`tools_for_payload` composes two filters and their order shows.** The allow-list is applied
  first, then the search tools are dropped — so naming `web_search` in `allowedTools` still loses
  it when search is off. A non-list `allowedTools` is ignored rather than treated as empty.
- **`forced_artifact_tool_name` needs availability *and* permission**, and with no allow-list the
  permitted set *is* the available one.
- **`normalize_reasoning_effort` is case-sensitive** — `MEDIUM` falls back like any unknown — and
  `"  high  "` is stripped before the membership test.
- **`memory_enabled` is `is not False`**, so `0` and `""` read as *enabled* while only the boolean
  `false` disables it; a malformed scope id is silently narrowed to `global`.
- **`memory_scope_from_payload` reads the latest user message only** and stops there either way, so
  an older `projectId` never leaks forward.
- **`mindmap_intent_requested` fires on `什么是 mindmap？`** with no create verb, because the
  oracle's verb alternation contains `map` and `mindmap` contains it. Left as-is, with the reason in
  the code: this is the oracle's behaviour, and tightening it would be a divergence, not a fix.

Verification: the probe pair replays six corpora and matches byte for byte — 86 keys, md5
`4bc6167d94f761c2a4178c70e2cdac6e`. `tools_for_payload` is compared as the **sequence of function
names**, not as whole definitions: the definitions are the tool catalog's own subject and are
covered there, and re-comparing them here would bury this probe's actual subject. Tests are at 295,
`cargo fmt --check` and `cargo clippy --all-targets` are clean, and the Rust probe was re-run after
formatting so the committed bytes reproduce the hash.


### Step 2 measured: `context_manager` is not a slice either (`0882b1b0`)

`context_manager` was the next recorded step. Measuring it first: its 137 lines depend on
`context_engine` (347 lines, entirely unported), whose identity half needs **SHA-1** — and this crate
depends on `sha2`, not `sha1`. So the work was split at the seam the dependency graph already has:

- **done here**: the token half of the engine — the heuristics, the three estimators, the body
  breakdown, the per-model window lookup, `available_input_tokens`, the budget plan with its
  recommendation ladder, and `token_trim`;
- **blocked on a decision**: `base_context_id` / `build_context_diff` / `build_engine_diagnostics`,
  which need the SHA-1 either hand-rolled (a hash implementation in-tree) or via a new dependency;
- **then**: `context_manager` itself, which is mostly ordering and diagnostics once the engine exists.

What the 168-key corpus and 12 new tests pin, beyond the arithmetic:

- an empty **object** message still pays the four-token structural overhead; only a non-object
  message costs nothing (measured: the oracle returns 4 for `{}` — my first test said 0 and was
  wrong, not the port);
- the trailing system message is `dynamic` only when it is last *and* there is more than one message;
- the CJK ranges include Fullwidth forms, so CJK-keyboard punctuation is not miscounted as Latin;
- `round(x, 1)` is ties-to-even on both sides, which `format!("{:.1}")` mirrors;
- `estimate_tools_tokens` measures a serialized tool array whose **key order differs** between
  `serde_json` and Python — and the estimate is deliberately insensitive to that, since reordering
  keys changes neither length nor CJK count. The string is not exposed, so nothing can start
  comparing it byte-for-byte;
- `token_trim` never touches the leading or trailing system message, keeps at least
  `min_keep_messages` of the middle, and returns the caller's list untouched when the budget is zero.

Verification: byte-identical, md5 `9c50e3cc29c8493f3c057fc3a3b79a07`; tests 295 → **307**; `fmt
--check` and `clippy --all-targets` clean, with the probe re-run after formatting. Two more of my
expectations were wrong on the first run and were corrected against the oracle rather than by
changing the port — the same failure mode as the taint corpus index and the five-versus-six segment
count, which is now three for three: **my arithmetic about the oracle is the weak link, so
expectations get taken from the oracle.**


### The context engine is whole (`3960be2c`), and the SHA-1 decision went to a dependency

The identity half was blocked on a decision worth recording: `base_context_id` needs SHA-1, this
crate depends on `sha2`, and the two ways out were hand-rolling the primitive or adding the crate.
Measurements that decided it:

- **`sha1` was not in the lockfile at all**, not even transitively — so the addition is a real one,
  not a free promotion of something already present;
- **every existing fingerprint in the crate delegates to a RustCrypto digest**
  (`memory.rs:195`, `search.rs:735`, `tool_policy.rs:1330` all call `Sha256::digest`), so writing a
  primitive by hand would have introduced a practice this codebase does not have.

So `sha1 = "0.10"` sits next to `sha2 = "0.10"` in the workspace table. What the corpus and four new
tests pin:

- **tool order is part of the prefix identity** — the parts string is the leading system content, the
  model, then the tool names *in order*, so a swap changes the id. That is the value's whole purpose:
  revealing accidental prefix churn.
- **an unnamed tool contributes nothing**, so a tool with an empty name, a non-dict `function`, or a
  bare string leaves the parts string untouched and the id equal to an empty body's.
- the dynamic block's `chars` counts characters, not bytes.
- the two ids asserted in the unit tests are **taken from the oracle**, which doubles them as a
  known-answer test of the digest path.

Verification: 204 keys byte-identical, md5 `0b430da00b2467c6a99e632880b4f38d`; tests 307 → **311**;
`fmt --check` and `clippy --all-targets` clean, probe re-run after formatting.

`context_engine` is now complete, which leaves `context_manager` as the only piece of this subsystem
— and it is mostly ordering plus diagnostics assembly, since both halves it depends on exist.


### The context subsystem is complete (`7c377889`)

`context_manager` was the last piece, and it went in small because both halves it depends on
already existed. What is worth recording is less the port than two mistakes in my own verification:

**The first corpus could not have caught a broken token-trim.** The manage bodies carried a model
that is *in* the window table, so the table's 131 072 beat the small patched default window and the
token-aware pass **never ran** — meaning the probe would have reported "parity holds" whether that
path worked or was a no-op. Switching the corpus to a model outside the table made the path
reachable, and the reference now shows the discrimination: 4 messages dropped with trim on, 0 with
it off. The same mistake was in the unit test, where the fix was to empty the table explicitly. This
is the strongest form of the recurring lesson — not "my expected value was wrong" but **"my corpus
could not tell the difference"**, which is worse because it reads as a pass.

**And the lint I introduced.** The settings tuple in the new probe tripped `type_complexity`, which
is a warning rather than a deny, so `clippy` still exited 0 — the diagnostic was there and my filter
was hiding it. It is fixed with a `SettingsCase` alias. Worth remembering: "clippy exit 0" and "no
diagnostics" are not the same claim, and it is the second one that was being asserted in these
messages.

Traps the corpus and seven tests pin: the sort is by `(name, type)` and **stable**, `toolOrder`
lists only named tools while `toolCount` counts every entry, both system ends are pinned and the
count window's budget floors at one, the engine block appears only while the engine is on, and
`merge_context_manager_diagnostics` **moves** the engine block out and copies a **zero**
`requestMessageCount` (a truthiness test would drop it). One measured divergence is kept and
documented: the oracle's `tool_name` raises on a non-dict tool where this port returns an empty name.

Verification: 341 keys byte-identical, md5 `ba5343c49b1b6aeef25fec1724b0d911`; tests 311 → **318**;
`fmt --check` clean and `clippy --all-targets` exit 0 with no diagnostics in the new files.

What is left before `build_deepseek_request` can exist: `model_router` (279 lines, pure, needs its
model catalogue measured first), `budget_manager` (371, ledger-backed and therefore last), the
validation/normalisation set (~110), and then the assembly itself with the diagnostics serializer
that owns key order.


### The model router landed, and the new check earned its keep (`b04772bc`)

Same shape as the last two: `model_router.py` is 279 lines and depends on `edge_inference`'s 529, of
which it uses **four names**. So the slice is the router plus that surface — the three query-shape
patterns and the two payload readers — with the edge-routing half (providers, quantisation,
local-versus-cloud) left for its own slice. The patterns are exported as **text** as well as
compiled, and the probe compares the strings: they carry CJK literals, and a wrong character
transcribed blind would otherwise surface only as a mysterious routing difference.

What the 251-key corpus and eleven tests pin, in the order they would bite:

- complexity tests run in the oracle's order — the complex pattern beats a short length, and the
  simple pattern only counts within 400 characters, so `解释` + 500 characters is `neutral`;
- `is_auto_request` mixes a case-fold with an identity check: `model: " AUTO "` opts in while
  `autoRoute: 1` does not;
- capability reads the **attachment** (`imageData: data:image/…`), not content parts — the test
  asserts both directions against `request_shaping::has_image_content` so the pair cannot collapse;
- an explicit model is normalised, checked against the supported list, then overridden by vision
  unless it is already the refine model;
- auto routing walks complexity → the soft cost cap (off at zero) → the default, and the tier falls
  back to the model name for anything that is neither draft nor refine;
- cascade is refused for agent and vision turns; the quality gate scores `1 - 0.34` per reason with
  one uncertainty marker passing and two failing.

**The "no diagnostics" standard caught a real lint this time.** Appending the test modules left the
`text_or_empty` helper after them — `items after a test module`, a warning that does not change
clippy's exit code. Under the old "exit 0" claim it would have shipped. Both files were reordered.
Same class of miss as `type_complexity` last round, and the reason the assertion was changed.

Verification: 251 keys byte-identical, md5 `ebad857e793f240bba7fa0d1c2f5a894`; tests 318 → **329**;
`fmt --check` clean; `clippy --all-targets` exit 0 with no diagnostics.

What is left before `build_deepseek_request`: `budget_manager` (371, ledger-backed and therefore
last), the validation/normalisation set (~110), the `edge_inference` edge-routing half (~430), and
then the assembly with the diagnostics serializer that owns key order.


### The message layer landed, and the corpus nearly failed to be able to fail (`01d252b1`)

`normalize_chat_messages` plus its two validators and the tool-call helpers. The layer is
**fail-closed** by design and the oracle's docstring records why: an earlier revision silently
skipped every turn it could not represent, so the caller's instruction reached the model as if it
had never been written — a `200` whose answer ignored what the user had said. Every unrepresentable
turn raises here, with its index and the codes the gateway's own preparation layer returns.

**The content expander is injected.** `expanded_message_content` reaches the file index through
`build_attachment_context`, which is I/O, so the pure layer takes the expander as a parameter — the
same move as the clock and the transport. Attachment *parts* are still covered, because
`_image_content_parts` is pure and is ported.

Three notes on verification, in order of how much they cost:

1. **The first corpus for the check layer could not have failed.** It reused the message sets, none
   of which contains more than 40 messages, so the `context_compression_required` path was
   unreachable and the probe would have reported parity whether that rule worked or not. It has its
   own corpus now, and the reference shows the discrimination: 41 messages without a summary is a
   **409**, with a summary it is fine, and exactly 40 is fine either way. This is the second
   instance of "a corpus that cannot fail reads as a pass" after the token-trim one.
2. **One measured divergence.** A JSON *object* as tool-call `arguments` is re-serialized in sorted
   key order here where the oracle keeps insertion order — `serde_json::Map` is a `BTreeMap` and
   `preserve_order` is off workspace-wide on purpose, and the order is gone at parse time. The wire
   format sends arguments as a string, so it is unreachable from the wired path; documented, pinned
   by a test, and the corpus uses an already-sorted object so the probe compares behaviour rather
   than that gap.
3. **A test case of mine was wrong, not the port.** The api-key-fallback case omitted `messages`,
   which the oracle rejects too — the probe showed both sides agreeing before the test changed.

The "no diagnostics" assertion earned its keep for the second slice running: the helper landed after
the test module again, a warning that does not move clippy's exit code.

Verification: 121 keys byte-identical, md5 `7c7860c9a70b4b4f07f0cff7503cdda7`; tests 329 → **337**;
`fmt --check` clean; `clippy --all-targets` exit 0 with no diagnostics.

What is left: `budget_manager` (371, ledger-backed and therefore last), the attachment-expansion path
behind the file index, and then `build_deepseek_request` itself with the diagnostics serializer that
owns key order. The `edge_inference` edge-routing half is **not** in this closure — only its four
consumed names were ever needed.


### The budget manager's pure half, and a float rendering that was wrong at real magnitudes (`04645d0c`)

`budget_manager` was the last leaf before the assembly, and its ledger is SQLite -- so the slice
line is the one the oracle's own docstring draws: pricing, cost arithmetic, the policy and its
payload override, the in-memory `ToolBudget`, the scope key and the cost diagnostic are pure; the
database (`connect_db`, `record_spend`, `daily_spend`, `over_daily_budget`, `should_downgrade`,
`budget_status`) is a store and its own slice. 209 keys byte-identical, md5
`08a4887495a2a11398f75daeae21393f`.

**The finding outlived the port.** Serializing `estimate_cost` in the probe meant comparing
serde_json's float rendering against Python's, and they disagree on *every* float below `1e-4`:
serde_json writes `4.93e-5` as `0.0000493` and `1e-6` as `1e-6`, where Python writes `4.93e-05` and
`1e-06`. A single request's cost is exactly that size -- a fraction of a cent -- and
`diagnostics["costUsd"]` is served to the caller. The crate already had the right renderer
(`python_json::float_str`, with `1e-05`/`1e+16` pinned), but three containers --
`dumps_default_separators`, `dumps_compact`, `OrderedJson::render` -- sent numbers through
serde_json's own `to_string` and bypassed it. Fixed in a commit of its own (`36fc2de7`), because
eight modules consume those renderers; the two probes with hashes on record were re-run
(`request_messages` still `7c7860c9...`, `memory` byte-identical). This was visible at all only
because the corpus uses **real costs** rather than round numbers -- the third time in this
migration that the corpus's composition decided whether the probe could see anything.

**Two asymmetries recorded, not smoothed over.** `diagnostics_with_cost` reads a non-dict as `{}`
where the oracle's bare `dict(...)` raises -- unreachable, and the module doc says so, explicitly
so that nobody later "unifies" it with `cost_from_usage`, which *does* guard in the oracle. And
`BudgetPolicy::to_value` cannot carry the oracle's `to_dict` insertion order (`maxTotalTokens,
maxAgentTokens, maxSearchCalls, maxToolCalls, maxEstimatedCostUsd, policy`) because a
`serde_json::Map` is a `BTreeMap` -- and the payload reaches the wire as
`diagnostics["budgetPolicy"]`, where Python's response `json.dumps` does not sort. The serializer
that serves it must be handed the order; the assembly slice owns that decision, and the module doc
now states it.

**Corpus traps pinned**: a usage value that is present but cannot convert is *skipped* (so a later
spelling can still win) rather than read as zero; a present negative limit is *floored* while an
unconvertible one *falls back* -- two directions, two assertions; an unknown `budgetPolicy` cannot
be turned on by a payload; the scope cap is 120 code points, which is what keeps a 200-character
Chinese scope from becoming an unbounded ledger key.

Verification: 209 keys byte-identical, md5 `08a48874...`; tests 337 -> **347**; `fmt --check` clean;
`clippy --all-targets` exit 0 with no diagnostics (one `explicit_auto_deref` found and fixed).

What is left before `build_deepseek_request` itself: the SQLite ledger (`should_downgrade` is the
assembly's consumer), the attachment-expansion path behind the file index, and then the assembly
with the diagnostics serializer.


### The two blocks before the assembly: attachment expansion and the budget ledger (`2e7f395e`, `592a05ce`)

`build_deepseek_request` needs two things that are not pure, and both now exist as pure halves
with their I/O injected -- the same boundary the clock, the transport and the content expander
already had.

**`attachment_context`** (`2e7f395e`) is `rag/files.py` 69-283 plus
`chat_payload.expanded_message_content`: the chunk selector with its scoring and embedding
helpers, the two formatters, and the orchestration. `load_cached_file` (the file index),
`local_rag.search_file_chunks` (the vector index) and the embedding pipeline arrive as
parameters, and the `context_taint` guard line is handed in rather than re-derived. Every
budget and truncation counts **code points**, because the section boundaries are part of the
prompt. 69 keys byte-identical, md5 `347c7fb29c4c8cc73fe6ca0bc8f7c93e`; tests 347 -> 359.

**`budget_ledger`** (`592a05ce`) is the behaviour `budget_manager.py` 183-371 wrap around
`connect_db`: the spend view, the row shaping, the four threshold checks, `should_downgrade`,
`record_request_spend`, `budget_status`. The SQL statements, the connection and its pragmas are
the store's slice, so they arrive as `LedgerDeps`. Its defining property is that **failure is
data**: the oracle swallows every database error into `_last_error` and returns the empty view,
so these functions return the message and the caller owns the state -- which is why
`budget_status` reads the ledger twice and reports the newest failure. 75 keys byte-identical,
md5 `0e22a7129ec62c918f52ac0117532c93`; tests 359 -> 367.

What the two slices had in common, beyond the boundary: **each corpus was wrong on the first
pass in a way that would have read as a pass.** The selector's indexed rows used a `file_id`
the search stub did not know, so the branch that reads the vector index was never entered; and
its budget-exhausted row used two attachments where the share shrinks multiplicatively
(`remaining -> remaining / left`), so the "not sent" row cannot appear before the
`max(8_000, ...)` floor binds -- about fifteen attachments. Both were caught by asking what
the corpus was supposed to be able to **fail** on, not by reading the diff. That is the fourth
and fifth instance of this class in the migration.

Remaining before the assembly: the two stores (the SQL and connection behind `LedgerDeps`; the
file and vector indexes behind `FileContextDeps`), and then `build_deepseek_request` itself
with the diagnostics serializer that owns key order.

---

### The closure list above is closed: the stores and the assembly landed (`13136490`, `de2bd60a`), and its probe is now clean (`3b7c041b`, `02976415`)

Three commits were not yet in this record. `13136490` is the two stores behind the injected
reads: `budget_store` (`connect_db` + the schema DDL, which is itself a contract because SQLite
stores the statement text, so it is written with `concat!` and explicit `\n` -- a multi-line
literal would have taken CRLF from the checkout) and `file_store` (`load_cached_file` with its
four failure codes, whose messages render **into the prompt**). `de2bd60a` is
`build_deepseek_request` itself, with the key orders **measured** by making the Python probe
publish every envelope and nested block's key order. Its first run reported "95 of 292 rows
differing, and every one of them is inside a tool schema".

**That last part was wrong, and closing it was the whole slice.** 45 rows differed inside tool
schemas; **50 differed on `contextDiff.delta` elements**, which are not tool schemas at all.
Fixing both then surfaced three more divergences that the same rows had been masking -- the
first-difference sampling only ever saw the earliest one. The probe pair now reports **0 of 406
rows differing**, md5 `adfbfd903b6e98dd83bbb75f7d323985`.

**The tool-schema order had to move into the data.** Measured from the asset:
`parameters.properties` takes **25 distinct orders** across the catalog -- each tool declaring
its own property sequence while reusing the same names -- and property objects order `type`
before `description`. `NESTED_ORDERS` matches by key **name**, so no table can express that.
`python_json` gained `loads` (structural scanning there; leaves decoded by `serde_json`, because
a `Deserialize` visitor was ruled out after reading the source: with workspace-unified
`arbitrary_precision`, `deserialize_any` delivers numbers as a private map). `tool_catalog`
gained the ordered trees plus `ordered_tool_definition`, which serves a definition only when it
is byte-equal to the catalog's own -- drift keeps the generic rendering. The strongest check is
the new test that re-renders the whole 40 KB asset from the parsed trees and asserts it equals
the committed bytes.

**The taint block needed its builder to keep the bytes.** `sources` accumulates in
first-appearance order over the **untruncated** segment scan, and the visible `segments` are
capped at `max_segments` -- once truncation bites, the order is not re-derivable from the
report. `build_taint_report_ordered` now builds the tree (the `Value` API delegates to it and is
unchanged); the assembly carries it on `PreparedDeepSeekRequest::taint_ordered` and substitutes
it while the two views agree, the same guard shape as the tool substitution. Its probe
reproduces its recorded hash `49e390b4c0c9f2ab499337326b308404` after the refactor.

**Three divergences the old corpus could not see** (each inside a row already differing for the
tool-schema reason):

1. `temperature`: the oracle's `max(0, min(float(t), 2))` keeps **the integer** at the clamps --
   `3.5 -> 2`, `-1 -> 0`, `False -> 0` -- where the port rendered `2.0` / `0.0`. Measured on
   the oracle, ported as comparisons, extracted into `clamped_temperature` so a unit test pins
   the whole table.
2. taint `segments` elements and `sources` order (above).
3. `modelRouter.reasons` elements: `{"router": ..., "decision": ...}` -- one more table row.

**And the corpus could not have caught any of them.** It never reached the forced `tool_choice`
object (no payload carried a PPT/mindmap intent), never turned search on, never narrowed
`allowedTools`, and never clamped the temperature low. It now does: +3 payloads, +2 axes -- a
memory-state `{}`, which also exposed that the port used `unwrap_or_else` where the oracle's
`memory_state or empty_memory_state(payload)` is Python's `or` with its falsy fallback, and the
non-pro ledger short-circuit. 292 -> **406 rows**.

Verified: `cargo test -p deepseek-policy -p deepseek-gateway -j 1` -> 383 + 139 lib tests plus
7 + 6 + 1 + 1 integration tests, all passing; `cargo fmt --check` clean; clippy exit 0 with **no
diagnostics in the new code** (the two `needless_borrow` warnings in `python_json::build` and
`control_proxy.rs`'s `result_large_err` are byte-identical at HEAD -- local stable 1.97 versus
the CI pin 1.85; do not "fix" either, and do not read exit 0 as "no diagnostics").

What the assembly still is **not**: wired. The production chat path (`chat_execution`) builds its
own thinner body today; this oracle-shaped assembly is what a production caller switches onto
next, together with the file vector index (`local_rag`; refused via
`NATIVE_FILE_VECTOR_INDEX_NOT_READY`/501), the OS local timezone and `search_if_needed`.

**Unpushed**: this slice adds `3b7c041b` and `02976415` on top of the six already recorded.

### The batch's first CI run went red in two lints, and neither had a local signal

Run `35310488045` on `56b189eb`: 4 of its jobs failed, and both causes were lints in code
that had never been through CI. Everything else passed -- `rust-coverage`, `native-go`, and
the parity / S3 / federation e2e jobs among them -- so the red is fully explained by:

- **rust**: clippy 1.85 with `-D warnings` rejects `needless_borrow` twice in
  `python_json::build` (the `&name` spellings from `de2bd60a`; local stable 1.97 only warns
  about them) and `needless_lifetimes` in `budget_ledger`'s test helper. Reproduced locally
  with the exact CI invocation (`cargo +1.85.0-x86_64-pc-windows-gnu clippy --locked
  --all-targets --all-features -- -D warnings`), fixed, and re-run to exit 0.
- **test (3.10 / 3.11 / 3.12)**: ruff first -- an unused `sqlite3` import and an unused
  `failures` dict in the two newest probes -- and, behind it (ruff masks mypy), mypy 2.0's
  `Cannot infer type of lambda` for the default-argument idiom passed to a typed
  `Callable[[], Any]`; replaced with `functools.partial`, which binds the loop value the same
  way. Both probes' outputs are byte-identical before and after (`65c1e23c…` / `0e22a712…`).

**The trap worth recording**: the two `needless_borrow` sites had been judged "pre-existing"
and deliberately left alone -- on the evidence that they were byte-identical at local `HEAD`.
But local `HEAD` included nine unpushed commits, so byte-identity there only proved they were
older than the last *local* commit; it said nothing about whether CI had ever tolerated them.
The baseline for "will CI accept this" is `origin/main`, and the check is the 1.85 invocation
above -- not the local stable, which merely warns.

Verified before re-pushing: 1.85 full-workspace `cargo test --locked --all` exit 0;
`ruff check .` and `mypy .` pass; both probe pairs unchanged.

**The re-run is green.** `35312303606` on `59914dce`: **all 35 jobs succeeded** -- `rust`, the
three `test` legs, `rust-coverage`, `native-go`, and every parity / S3 / federation e2e job.
The ten commits from `2e7f395e` through `59914dce` are CI-verified at that HEAD.

### The clock stopped being the reason the assembly cannot be wired (`f31eea9b`)

The assembly has been complete and unwired since `de2bd60a`, and the prerequisite named there
was the OS local zone: `dynamic_context` carries `LocalNow` as data precisely because "Rust's
standard library has no local-timezone support", and every path through
`build_dynamic_turn_context` needs it unconditionally. That is now resolved, and it was the
**only** hard prerequisite -- the other candidate in that list is not one (below).

**`deepseek-gateway::local_clock`** asks the OS the way CPython does, because that is what the
parity target *is*. `datetime._local_timezone()` consults no tz database: on Windows
`tm_gmtoff`/`tm_zone` do not exist, so it falls back to `time.timezone` / `time.altzone` and
`time.tzname[tm_isdst]`, which the UCRT fills from `GetTimeZoneInformation`; on POSIX it reads
`localtime_r`'s `tm_gmtoff` and `tm_zone` directly. The FFI is declared rather than
dependenc-ised, following `deepseek-policy::file_lock` (`#[link(name = "kernel32")]` on Windows,
a bare `extern "C"` on Unix), so the dependency graph is unchanged -- and a time crate would not
have closed the gap anyway, since `chrono` and `time` expose the offset but not the zone's own
name, which is what gets printed into the prompt.

**The measurement that mattered.** On this zh-CN Windows 11, `GetTimeZoneInformation` reports
`Bias = -480`, `StandardBias = 0`, `StandardName = 中国标准时间`, and CPython's `tzname()`
returns that same string. **Not** `China Standard Time` -- which is what the example in
`dynamic_context` led a reader to expect, and what a hand-written name table would have
produced: a prompt that looks right and diverges on every request. That example is corrected,
and so is `search.rs`'s module header, which still claimed the request-assembly layer "does not
exist yet" and listed three links (the taint firewall, `search_multiple`, the consumer) that are
in fact ported.

**The second measurement changed the plan.** `search_if_needed` had been recorded alongside the
zone as a pre-wiring prerequisite. It is not one: it is reached only under
`forced_search_mode(payload)` (`deepseek_client.py:674`), and `searchContext` is written only
inside that same branch -- so the ordinary path never needs it. Its two callbacks
(`progress_callback`, `system_note_callback`) have no destination at all on
`/v1/chat/completions` (measured in `search_provider`), and its body is a live Tavily fetch, so
a native path that reaches forced-search mode must **refuse** rather than port it. Porting it
now would be porting dead orchestration.

Verified:
- `tasks/native-runtime/local_clock_parity_probe.py` with `examples/local_clock_parity_probe.rs`:
  both sides resolve the host's zone and render the same pinned instant through the oracle's own
  `format_current_time_context` -- **byte-identical** (`diff=0`; `offset_seconds` 28800,
  `tzname` 中国标准时间, `is_daylight` false on both).
- **Negative control run**: with the offset sign flipped the pair reported `offset_seconds:
  -28800` and `2026-09-17T22:50:44-08:00`, so the probe is able to fail. Reverted before the
  commit.
- 6 unit tests on the Windows bias mapping -- the daylight branch and a non-zero `StandardBias`
  are pinned there because no host in reach exercises them, and they run on Linux CI too, where
  the Windows read is `cfg`-ed out.
- `cargo fmt -p deepseek-gateway -- --check` clean; full-workspace clippy (1.85, `--locked
  --all-targets --all-features -- -D warnings`) exit 0 with **no diagnostics**; `ruff check .`
  and `mypy .` pass (884 files).

**Not verified**: the Unix read compiles in CI's Linux job but was not run here -- the paired
probe only exercised the Windows path.

**Pushed and green**: `24f7263f`, `f31eea9b`, `af6e8e52`, `4903540c` — see the CI round below.

**Next explicit action**: measured, and the earlier claim here that the wiring slice is "blocked on
nothing" was **wrong** — see [`assembly-wiring-plan.md`](assembly-wiring-plan.md). Wiring
`chat_execution` onto `build_deepseek_request` still needs the memory state, and
`prepare_memory_state` is unported (8 functions). The measurement also found two things that are
not wiring work at all: the oracle's explicit-memory-command parser is broken by a pair of
`(?:` → `(` typos (so "记住: X" is silently dropped), and the test that covers it monkeypatches
`memory.re` and therefore cannot see it. The plan records both, the ownership gap (memory is not a
declared domain, and `chat_completions_fast_path`'s cutover is 4.9.2), and the four decisions the
next slices need.

### The batch went red on a managed-document rule, and the re-run is green (`0b697839`)

Run `35318257112` on `4903540c`: 4 jobs failed of 35 -- `docs` and all three `test` legs -- from
**one** cause. `assembly-wiring-plan.md` is a tracked Markdown file, so it is a *managed document*:
`scripts/update_docs_language_nav.py --check` (the `docs` job) requires the language switcher block
and `tests/test_docs_language_navigation.py` asserts both its presence and that its targets resolve.

That was my omission, and the cost ratio is the lesson: local verification had covered
`ruff` + `mypy` for the new probe but not the two commands the `docs` job runs, nor the docs test
that reads them. One missing four-line block killed four jobs 21 seconds in. The rule is now in the
project memory: for any new tracked Markdown, run `scripts/update_docs_language_nav.py` (it inserts
the block), then `--check`, then `scripts/check_doc_links.py`, then
`pytest tests/test_docs_language_navigation.py`.

Nothing else was wrong: `rust` passed, which matters because that job compiled the new Unix FFI for
the first time, and `native-protocol`, `rust-coverage`, `native-go` and every parity / e2e job passed.

Fixed in `0b697839` with the repo's own script (it edited exactly one file).

**The re-run is green.** `35321343935` on `0b697839`: **all 35 jobs succeeded**, `docs` and the three
`test` legs among them.

**A push trap worth writing down**: `git push` then hung for ~14 minutes with no output. This shell
carries the persistent `HTTPS_PROXY=http://127.0.0.1:7897/`, which git inherits, and an unhealthy
Clash node hangs instead of failing. `gh` keeps working throughout because it uses the API path, so
"`gh` is fine" is not evidence the push landed. The remote ref proved it had not -- and the direct
push (`timeout 120 env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY git push origin main`) went through
immediately. Check `git ls-remote --heads origin main` before believing either way.

### Decision A taken: the memory-command grammar was two `?` from working (`da8c21cf`)

`apply_explicit_memory_command` (`infra/data/memory.py:508`) is the write half of
`prepare_memory_state`, and the wiring plan's §2 measured it as broken. The repair is the `?` in each
`(?:` that had been written `(:`, and it has three consequences:

1. **Nothing that used to match stops matching.** `删除记忆: X`, `forget: X`, `不要再记得: X`,
   `不再记住: X`, `取消记住: X`, `delete memory: X` matched before and still do; they now delete
   against the text after the colon. While the alternation captured, `(.+)` was group 2 and the code
   reads group 1, so the target was the literal **command word** -- `删除记忆: X` answered
   `已根据用户要求删除 0 条相关长期记忆。` and wrote nothing.
2. **`忘记: X` becomes reachable.** It had required a literal `:` in front of it, which is why every
   natural phrasing failed to match and nothing was ever saved.
3. **That reachability needs a guard, and this part is not a typo repair.** A negated forget --
   `不要忘记: X`, `别忘记: X`, `don't forget: X` -- contains the bare `忘记:` substring, so it would
   land in the delete branch and destroy the memory the user asked to keep. Measured with the guard
   removed: `别忘记: 牙医预约` returned `已根据用户要求删除 1 条相关长期记忆。` and the row was gone.
   A negated forget is now recognised first and routed to *remember*, which is what the sentence
   means. Eight lines, and they are the difference between a repair and a new way to lose data.

**The accept-set of the repaired grammar, measured** -- and the reason this is a decision rather than
a finished story:

| input | result |
| --- | --- |
| `请帮我记住: A` | saved |
| `记住: B` / `帮我记住: C` / `以后记得: D` / `remember: E` | `""` -- still dropped |
| `不要忘记: F` / `don't forget: G` | saved |
| `忘记: X` / `forget: X` / `删除记忆: X` | deleted, against `X` |
| `不要再记得: H` | `""` -- shadowed by the `不要…记得` guard |
| `不要删除记忆: I` | reaches the delete branch |

The remember branch's prefix is **required**: `(?:请)(?:帮我)` never had a `?`, so the only phrasing
that works is `请帮我记住: X`. Making those prefixes optional is a one-token change that *adds*
accepted phrasings -- a product decision, left open. The last two rows are pre-existing gaps this
repair does not touch; both are now in the function's docstring.

**The test could not fail, which is why none of it was visible.**
`test_explicit_english_remember_forget_and_opt_out` monkeypatched `memory.re` with a
`SimpleNamespace` whose `search` returned fabricated `SimpleNamespace(group=lambda _: …)` objects: it
never ran the patterns, never distinguished `group(1)` from `group(2)`, and it asserted a result for
`"forget concise replies"`, a phrasing the grammar never accepted. Replaced by
`test_explicit_memory_commands_are_parsed_by_their_real_patterns`, which calls the function on real
phrasings against a temporary memory directory and also closes the empty-input early return that was
uncovered.

Verified:
- **Both negative controls, each restored before the commit**: reverting the two patterns makes the
  new test fail at its first remember assertion; disabling the negated-forget branch makes
  `别忘记: 牙医预约` delete instead of save.
- `tests/test_memory_failure_paths_332.py` + `tests/test_memory.py` -> 24 passed; `test_memory.py`
  alone -> 13 passed; `memory.py` line coverage over those files 92.90% -> 94.48%.
- `ruff check .` and `mypy .` pass (884 files).
- **The full local suite is not a usable gate on this host**, which is worth recording rather than
  glossing: it runs ~5x slower than CI; the 16 storage files cannot provision MinIO (no `minio`
  binary in `bin/`, no Docker daemon); and a combined run reports **35 failures that all pass when
  their file is run alone** (19 in `test_files.py`, 8 in `test_memory.py`, 4 in
  `test_presentations.py`, 1 in `test_search.py`, 3 in backup files) -- local cross-file isolation
  artifacts, not code. CI ran those same files green on `0b697839`, so CI is the arbiter for the
  full gate. Two traps found while trying: `pytest --cov` is blocked by the sandbox's safe-delete
  hook unless `COVERAGE_FILE` points outside the repo, and `-v` is overridden by the project's
  pytest config into per-file dots.

**Pushed**: `da8c21cf` (the repair) and `1b856bec` (this record); both are on `origin/main`.

**The batch is green** (run `35330894171` on `1b856bec`, after a re-run of one job): **35 of 35 jobs
succeeded** -- including `docs`, all three `test` legs, `rust`, `rust-coverage`, `native-go` and
every parity / e2e job. The first attempt was 34 green and one red, and the red was a **flaky
threshold assertion, not this change**:
`tests/test_backup_458_storage_control_plane.py::test_qos_reserves_p0_bandwidth_and_enforces_independent_target_buckets`
returned `0.747967004776001` against `>= 0.75` at line 1314 -- a rate-accounting assertion missing by
0.27%, in a storage-QoS test that has nothing to do with memory parsing. The evidence that it is
flaky rather than broken: the same code passed `test (3.10)` in the previous run (`0b697839`, all 35
green) and passed `test (3.11)` and `test (3.12)` in the failing run; `gh run rerun --failed` then
passed it with no code change.

**Two CI-harness traps recorded while getting here**, both of which cost time and will cost it again:
`gh run watch --exit-status` returns non-zero on a *network* error too (`failed to get run: … … unexpected EOF`,
the proxy hop), while the run is still going -- so the conclusion must come from
`gh run view --json status,conclusion` and the per-job `conclusion` (an empty string there means
in progress, not failed). And on this host the full local suite cannot stand in for CI at all; see
the previous section.

---

## The memory turn-state half landed, and the vector bonus turned out not to be bounded

**Branch `main`, HEAD `dd9b5cdb`** (one commit ahead of `origin/main` — the last push was
`1b856bec`; nothing in this section is pushed yet). Working tree carried only the files below.

This is step 2 of [`assembly-wiring-plan.md`](assembly-wiring-plan.md): the memory read half the
request assembly waits for. The wiring plan had recorded `prepare_memory_state` as the last hard
prerequisite for wiring `chat_execution` onto `build_deepseek_request`; eight functions were
missing. All eight are now ported into `deepseek-policy::memory`, byte-verified, and the plan's
open **Decision C** — whether the un-ported vector bonus is bounded — is **answered by
measurement**.

### Decision C: measured, and the answer is no

`LOCAL_RAG_ENABLED` defaults to **true** and the embedding provider to **`hash`** — so unlike the
API-key-gated paths, the memory vector index is **live offline in a default deployment**:
`save_memories` populates it through `sync_memories`, and `search_memories_index` returns real
scores. The plan's proposed measurement was run for real
([`memory_vector_bonus_probe.py`](memory_vector_bonus_probe.py), the actual modules, a scratch root
via `DEEPSEEK_INFRA_ROOT`):

| observation | result |
| --- | --- |
| run A (index live) executed twice | **identical** — the difference is the index, not flakiness |
| queries whose retrieved **order** differs from run B | **7 of 8** |
| queries whose retrieved **set** differs | the same 7 |

The set result is the one that matters. For query `react`, `m-long` has a lexical score of **zero**
and is retrieved *only* through the bonus (`hit score 37 → +3`); with the index forced to raise,
it is absent. So `None` is **not** a tie-break divergence that can be documented away: it changes
which memories reach the prompt.

**Consequence recorded in the matrix and the plan:** the wiring slice now has a third
prerequisite — a Rust provider for the memory index read path (bounded: hash embedding + cosine +
BM25 over `rag_items`/`rag_vec`, **read-only** while Python remains the writer), or a narrow
refusal that only fires when the index is populated. The `file_store` precedent cannot be copied
verbatim, because memory is enabled by default and a blanket refusal would refuse nearly every
request.

### What was ported, and the two defects found on the way

The eight functions — `memory_scope_candidates`, `memory_scope_label`, `format_memory_context`,
`upsert_memory`, `clear_memories`, `delete_memory_by_id`, `apply_explicit_memory_command`,
`prepare_memory_state` — with the same injection boundary the clock and the transport already use
(`vector_hits` as a `dyn Fn` provider). The repaired grammar from `da8c21cf` is ported with it: the
opt-out guard first, then the *negated forget* (which must beat the forget branch or it deletes the
memory the user asked to keep), then forget, then remember. The remember branch's required
`请`/`帮我` prefixes are kept as the oracle's own gap, not "fixed" — that is a product decision.

Two real defects surfaced, neither by reading the diff:

1. **A falsy content gate.** The oracle writes `normalize_memory_text(item.get("content") or "")`,
   so `content: 0` is empty and the row is **dropped**; the port passed the value straight in, so
   `0` became `"0"` and the row survived. `normalized_content` now applies the truthiness gate, and
   the migration corpus carries `0` / `true` / `false`.
2. **An `OrderedJson` regression that no gate could see.** The nested-order refactor (`3b7c041b`)
   made array elements take their order from the array's *name* with an empty fallback, so a
   **top-level array** — exactly how the store fixtures are written — rendered alphabetically. No
   CI job runs these probes, so it sat there; the memory probe's `store::file` observation caught it
   the moment the probe was re-run. The fix restores inheritance of the enclosing order when no
   nested order is registered; all **24 runnable probe pairs** were re-run afterwards and are
   byte-identical, `request_assembly` (the nested-order consumer) included.

### The corpus that could not fail, again — twice

- The first budget corpus was `3 × 3000`-char rows. `normalize_memory_text` caps a row at **1200**
  characters, so `used` peaked at ~3 600 of 8 000 and the 省略 path was **never reached** — the
  probe would have reported parity whether that branch worked or was a no-op. My unit test failed
  for the same reason and exposed it. The corpus is now six full rows (7 254) plus a 737-char row
  that lands **exactly** on the budget and a row after it that crosses; the reference output
  contains the marker (8 140 chars). Sixth instance of this class in the migration.
- The same cap invalidated the "exact boundary" case for the same reason.

My own unit-test expectations were wrong twice more (I indexed `load_memories()[0]` and forgot that
**pinned** rows sort first) — corrected against the oracle-derived probe rather than by touching
the port. That is the recurring lesson, unchanged: **expectations get taken from the oracle.**

### Verification

- Probe pair byte-identical: **194 keys** (was 92), md5 **`97187819db5aec787776174f6ac3f3d5`**,
  re-run after `cargo fmt` so the committed bytes reproduce the hash. Includes 8 upsert shapes,
  clear/delete-by-id, **14 command shapes**, and **9 turn-state shapes**, each with its file bytes
  and the final generation counter.
- `cargo test -p deepseek-policy` → **390 passed** (33 new); `cargo test -p deepseek-gateway` →
  145 lib + 15 integration passed.
- `cargo +1.85.0-x86_64-pc-windows-gnu test --locked --all` → **exit 0**, whole workspace,
  including the real Go→Rust boundary integration tests.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.
- `ruff check` and `mypy` pass on both probes (the new probe needed explicit `list[dict[str, Any]]`
  annotations to satisfy mypy's overload resolution).
- Docs gates: `update_docs_language_nav.py --check` PASS (199 files), `check_doc_links.py` OK.

### Two pre-existing probe breakages found while re-running the suite (not mine)

Recorded rather than fixed, since both are legacy measurement tools with newer replacements:

- `oracle_parity_probe.py` fails with `NameError: name 'AppError' is not defined`: it extracts
  `normalize_chat_messages` from `deepseek_client.py`, and `df7dfa13` introduced `AppError` into
  that function *after* the probe's last edit (`6ea4dde3`). `request_messages_parity_probe.py` is
  the superseding probe and is byte-identical (121 keys).
- `local_clock_parity_probe` takes the epoch as an argument on the Rust side while the Python side
  prints its own; it passes when paired that way (verified: identical).

### Not done, and the next executable task

**Not pushed.** The four changed files plus one new probe are local only; exact-head CI has not run
against them. The new `docs/MEMORY_STORE.md` and the two task Markdown edits are managed documents
(the `docs` job's language switcher is verified present).

**Next slice:** the memory index read path in Rust (hash embedding + cosine + BM25 over
`rag_items`/`rag_vec`, read-only) **or** the narrow refusal — then step 3, wiring `chat_execution`
onto `build_deepseek_request` with the three refusals and a real memory-state provider. Decision B
(a `memory` domain declaration) still gates wiring the **write** half.

---

## The memory index read path landed, and its acceptance criterion had to be corrected

**Branch `main`, HEAD `cd8cca08`** (committed by the workspace, not pushed — `origin/main` is still
`1b856bec`). The turn-state half of the previous section is in that commit; the index read path is
the uncommitted set below.

This closes the third prerequisite step 3 of
[`assembly-wiring-plan.md`](assembly-wiring-plan.md) was waiting for. The plan and the module
skeleton were already in the tree when this session started; what was missing was **verification**,
and an unverified provider is exactly what the migration rules do not count.

### What was verified, and the one defect it found

`tasks/native-runtime/memory_index_parity_probe.py` ↔
`rust/crates/deepseek-policy/examples/memory_index_parity_probe.rs`: **64 keys,
byte-identical** after `tr -d '\r'`. The fixture is **shared**, not duplicated — Python builds
`.local-rag/rag.sqlite3` through the production `save_memories` → `sync_memories` path and Rust
opens that same file read-only, so the schema and the column types are part of what is compared.
`.local-rag/rag.sqlite3`, note, is written by Python and read by Rust: no second writer.

The defect only the `pure::` layer could see: `parse_embedding` has **three** outcomes in the
oracle (a decode error returns the bare `return []`, a non-array normalizes `[]` to `dimensions`
zeros, an array normalizes to `dimensions` components) and the port had collapsed the first two.
It is invisible in every score — `cosine_similarity` returns `0.0` for an empty *and* for an
all-zero vector — so all 8 queries and their 24-key ordering were already identical while the
function was wrong. `str(value or "[]")` is part of the contract too: an empty string is falsy and
lands in the *second* branch. Fixed, and `pure::parse-9` now pins the empty-string case.

### The acceptance criterion in the plan was wrong, and the measurement says so

`memory-index-read-path-plan.md` asked for "the 7 differing queries drop to 0". That conflates two
comparisons. The 7-of-8 figure is the *live index versus no index* difference **inside the
oracle**; a correct Rust provider reproduces the **live** side, which leaves the figure at 7. The
probe therefore reports both paths — `retrieve::` with the provider and `retrieve-none::` without
— and **`turn::differing = 7 of 8` is now a positive result**, not a target. A provider that
silently returned nothing would have reported `0 of 8`; one that computed the wrong bonus would
have failed the `retrieve::` comparison. The plan records the correction.

### What the provider deliberately does not do

The `rag_vec` branch is **not** reimplemented. `vec0` is an extension loaded into the Python
connection, `rusqlite`'s bundled SQLite has no such module, and `sqlite-vec` is not a dependency of
this repository (not in `requirements*.txt`, `pyproject.toml` or any Compose file;
`find_spec("sqlite_vec")` is `None`). `initialize_schema` creates `rag_vec` only `if vec_loaded`,
so **every shipped deployment and every CI leg takes the cosine fallback**, which is complete.

When the table *is* present the read returns `MemoryIndexError::VectorTableNotReadable` instead of
quietly serving the fallback — the oracle would have blended `1/(1+distance)` into every score, so
the two answers differ in membership, not just in order. This is the narrow refusal the wiring plan
asked for: it fires only in a deployment that installed the optional extra, which is the opposite
of the blanket refusal the `file_store` precedent would have produced.

The read is read-only in the strict sense: `SQLITE_OPEN_READ_ONLY`, and it does not create the
directory, the schema or the `rag_meta` rows the oracle's `db_ready()` would. A missing database is
therefore `None` rather than an empty index, because the oracle's `[]` there comes from `db_ready()`
*creating* it — the one thing a reader must not do.

One structural fix came with it: `memory::VectorHits` was `dyn Fn(…) -> …` with no lifetime, so its
object bound defaulted to `'static` and no provider could borrow the store it reads. It now carries
a lifetime (`VectorHits<'a>`), which is what lets the wiring build a per-request provider rather
than leaking or `Rc`-ing one.

### Verification

- **Both probe pairs byte-identical**: `memory_index` **64 keys / ** (new);
  `memory_parity_probe` re-run and unchanged at **194 keys**, LF-normalized md5
  `97187819db5aec787776174f6ac3f3d5`.
- `cargo test -p deepseek-policy` → **400 passed** (10 in `memory_index`, 2 new here);
  `cargo test -p deepseek-gateway` → pass.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics** — one `useless_vec` in the
  in-flight test code was fixed to get there.
- `ruff check` and `mypy` pass on the new probe.

### Two traps worth recording

- **The `docs` job's rule applies to any new tracked Markdown**, and
  `memory-index-read-path-plan.md` already carries its language switcher — verified, not assumed.
- **`memory_parity_probe.py` writes GBK on this host.** Its Python side has no stdout
  reconfiguration, so a bare `python … > python.json` on Windows produces bytes that cannot be
  decoded as UTF-8 and compares as "different" against a correct Rust output. The pair is
  byte-identical under `PYTHONIOENCODING=utf-8`. The new probe pins `reconfigure(encoding="utf-8")`
  itself so its documented command works as written.

### A workspace hazard for the next session

Between two consecutive `git status` calls in this session the same 7 files moved from unstaged to
staged, then appeared as commit `cd8cca08` (19:41), and `memory_index.rs` +
`memory-index-read-path-plan.md` appeared with mtimes this session did not produce. A `codex`
process was resident throughout and idle by 19:46. Whatever the exact cause, **treat this working
tree as possibly having a second writer**: re-check `git status` and file mtimes before staging,
rebasing or force-pushing, and prefer adding to the existing task files over rewriting them.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

**Next slice — step 3, now unblocked:** wire `chat_execution` onto `build_deepseek_request` with
the memory provider bound (not `None`), plus the forced-search mode refusal and the file vector
index refusal (`vector_index_not_ready()`). Decision B (a `memory` domain declaration) still gates
wiring the **write** half.

---

## The OpenAI facade translation landed, and the wiring stopped being a fidelity question

**Branch `main`, HEAD `276d21a7`** (the memory index read path, committed by the previous round;
`origin/main` is still `1b856bec`). Working tree carried only the files below.

Before writing any wiring code, the oracle's route was measured rather than assumed — and the
measurement changed what the slice *is*.

### The measurement: `/v1/chat/completions` is a facade, and the native route disagrees with it

`routes/chat.py:65` is `payload = openai_to_internal_payload(body, local_base_url=…)`, then
`resolve_provider(model).chat(payload)` → `call_deepseek` → `prepare_deepseek_call` →
`build_deepseek_request`. So the OpenAI body is translated **first**, and the translation is part
of the public contract.

`openai_to_internal_payload` (`openai_api.py:29`) is narrow: it forwards `model` (after
`body.get("model") or settings.default_model`, then `MODEL_ALIASES`), `messages` **verbatim and
unvalidated**, `stream` (Python truthiness — the string `"false"` is *true*), `thinkingEnabled:
False`, `localBaseUrl`, and `temperature` only when it is a real number. It **drops** `tools`,
`tool_choice`, `max_tokens`, `top_p`, `reasoning_effort` and `thinking`.

The native route does the opposite. `request_preparation::prepare_chat_request` builds the upstream
body straight from the OpenAI request, so it **forwards** those six fields and **omits**
`thinkingEnabled`/`localBaseUrl`. That is a visible divergence on a public route (§五.11), not the
omission the plan had recorded — a client sending `tools` gets a body the oracle never builds, and
`temperature` is applied unconditionally instead of only when `build_deepseek_request` decides the
tier warrants it.

### What landed

`deepseek-gateway::openai_facade::openai_to_internal_payload`, with `payload_canonical_json` for
probes and diagnostics (`json.dumps(..., ensure_ascii=False, sort_keys=True)` — Python's default
separators, so a rendering comparison does not test `serde_json`'s compact default).

Paired with the real Python function:
`openai_facade_parity_probe.py` ↔ `examples/openai_facade_parity_probe.rs`, **56 keys, 12 204
chars, byte-identical**. The corpus is written to reach every branch and is labelled per case, so a
diff names the behaviour that moved:

- the six forwarded fields and the seven dropped ones, including "drops everything at once";
- the falsy-model set (`""`, `null`, `0`, `false`, `[]`, `{}`) all falling back to the default
  **before** normalization, and a truthy `true` normalizing to the literal `"True"` — which is what
  `str(True)` does and which no alias matches, so it passes through;
- alias normalization: case, underscores, spaces, surrounding whitespace, unknown passthrough;
- the `stream` truthiness table, `"false"` → `true` included;
- the `temperature` type table, `bool` excluded explicitly (a `bool` *is* an `int` in Python);
- `messages` forwarded verbatim — blank content, a `tool` turn, a non-object entry and
  `content: null` all survive this layer, which is what keeps validation single-sourced in
  `build_deepseek_request`;
- both refusals as `{message, code, status}`.

Seven unit tests pin the same behaviour in-crate.

### One probe bug worth recording

The first run showed **42 of 56 cases differing** — all of them only in whitespace inside the
canonical rendering. Python's `json.dumps(..., sort_keys=True)` uses the default `", "` / `": "`
separators; `serde_json::to_string` is compact. The fix is `python_json::OrderedJson::
render_default_separators`, which this repository already had for exactly this reason — the same
class of trap as the `4.9e-05` float rendering recorded in the budget slice. A probe that compares
*renderings* has to render both sides the same way; only then does a diff mean a behaviour change.

### Verification

- `cargo test -p deepseek-gateway` → **152 lib tests passed** (7 new) plus every integration target
  (7 + 6 + 1 + 1); `cargo test -p deepseek-policy` unchanged.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.
- `ruff check` and `mypy` pass on the new probe.
- Probes re-run for regressions: `openai_facade` byte-identical (56 keys); `memory_index` and
  `memory_parity` unchanged.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

The route still does not call `openai_facade` — this slice is additive and inert, like the three
before it. The wiring is now the **only** thing between here and a native body that matches the
oracle, and §5 of [`assembly-wiring-plan.md`](assembly-wiring-plan.md) lists the five pieces it has
to carry, in order:

1. `AssemblyEnv::from_env` — nine injected fields. Every settings struct has an oracle-matching
   `Default`; the ledger comes from `budget_store` + `LedgerDeps`, the clock from
   `local_clock::local_now`, the expander from `attachment_context::expanded_message_content` over
   a `FileContextDeps`. **Recorded gap:** the settings' *env readers* are not ported, so only a
   default-configured deployment would agree.
2. `request_base_url` — `Host` trusted only when `host_without_port(host)` is in
   `allowed_auth_hosts()`, else `http://127.0.0.1:{port}`.
3. The two refusals, taken **before** the expander runs (`search_file_chunks` returns a bare
   `Vec<i64>` and so cannot refuse itself), on requests that would actually consult the file index.
4. The error envelope: `build_deepseek_request` raises internal `AppError` codes where the route
   currently answers with `PreparationError` codes. Both move in one change.
5. A real-upstream integration test — matching the oracle's bytes does not prove the assembled body
   reaches DeepSeek correctly.

Decision B (a `memory` domain declaration) still gates wiring the **write** half.

---

## The composition landed, and it found a key-order defect two green probes could not

**Branch `main`, HEAD `918885cc`** (the OpenAI facade translation, committed by the previous
round; `origin/main` is still `1b856bec`). Working tree carried only the files below.

Last round ported the facade and noted that "two probes can each be right and still compose
wrongly". This round built the composition and ran that check — and it was not a hypothetical.

### What landed

`deepseek-gateway::native_chat`, the `call_deepseek` → `prepare_deepseek_call` composition:

```
openai_to_internal_payload  →  preflight_deepseek_payload  →  prepare_memory_state  →  build_deepseek_request
```

in **that** order, measured from `deepseek_client.py:1502` and `:651`. The two-step API
(`prepare_openai_chat` then `assemble_openai_chat`) exists so "validate before memory" is visible
at the call site instead of hidden inside a callback — `call_deepseek` validates before
`prepare_memory_state` runs, and the memory command path *writes*, so the order is observable.

Paired with the oracle: `native_chat_composition_parity_probe.py` ↔
`examples/native_chat_composition_parity_probe.rs`, **15 cases, 298 431 chars, byte-identical** —
body, diagnostics, tool names and api key for each. Plus three unit tests.

### The defect it found

`request_assembly::NESTED_ORDERS` carried `("messages", &["role", "content"])`, and the body
renderer appends any key the list does not name in **sorted** order. So a tool result rendered

```
{"role": "tool", "content": "[expanded]", "tool_call_id": "call-1"}     ← Rust
{"role": "tool", "tool_call_id": "call-1", "content": "[expanded]"}     ← oracle
```

and a call entry rendered `{"function": …, "id": …, "type": …}` where the oracle writes
`{"id": …, "type": …, "function": …}`. Identical values, different bytes — a real body-level
difference on a public route, and invisible to `request_assembly_parity_probe` because its corpus
has no tool-role turn. The tool-*call* path is exactly where the native route is now most active
(the loop runs rounds), so this was not a corner.

Fixed by making the message order a **superset** that serves all three oracle shapes — absent keys
are skipped, so one list covers all of them:

| shape | oracle order | served by |
| --- | --- | --- |
| plain | `role, content` | ✓ |
| assistant + tool calls | `role, content, tool_calls` | ✓ |
| tool result | `role, tool_call_id, content` | ✓ |

with the list `["role", "tool_call_id", "content", "tool_calls"]`, plus a new
`("tool_calls", &["id", "type", "function"])` entry. `request_assembly_parity_probe` was re-run
afterwards and is **unchanged at 1 946 077 chars**, so the fix is a strict improvement rather than
a trade.

### Three measurements that remove planned work

- **`forced_search_mode` is structurally unreachable on this route.** It is
  `search_mode(payload) in {"on","force","true","1"}` and `search_mode` is
  `payload.get("searchMode") or "auto"` — a field `openai_to_internal_payload` never forwards. The
  prefetch branch in `prepare_deepseek_call` is dead here, so **no refusal is owed**. Adding one
  would be the `search_budget` mistake the `search_provider` docs already record.
- **`web_search` is absent from the composed tool list.** `tools_for_payload` adds it only when
  `search_tool_enabled` sees `searchEnabled is True`, also never forwarded. The route gets the
  26-tool catalog minus the search tool; a unit test pins it.
- **The file-index refusal is narrower than "has attachments".**
  `expanded_message_content` returns early unless a message carries a non-empty `attachments`
  list, and `search_file_chunks` is consulted only for an attachment with a non-empty `file_id`.
  Only *file* attachments can need the index.

### A test expectation I got wrong, again

The first version of the tools test asserted the composed list **contains** `web_search`. It does
not — that is the measurement above. Corrected against the measured list rather than by touching
the composition; the recurring lesson is unchanged, and this time the failing assertion *was* the
measurement.

### Verification

- `cargo test -p deepseek-policy` → **400 passed**; `cargo test -p deepseek-gateway` → **155 lib
  tests** (3 new) plus every integration target (7 + 6 + 1 + 1).
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.
- `ruff check` and `mypy` pass on the new probe.
- Probe pairs re-run: `native_chat_composition` byte-identical (15 cases); `request_assembly`
  unchanged (1 946 077 chars); `openai_facade`, `memory_index`, `memory_parity` unchanged.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

The route still does not call any of it — `openai_facade` and `native_chat` are both additive and
inert. `assembly-wiring-plan.md` §5 lists the **five** pieces the swap has to carry, revised by
this round's measurements:

1. `AssemblyEnv::from_env` — nine injected fields; settings `Default`s match the oracle, ledger
   from `budget_store` + `LedgerDeps`, clock from `local_clock::local_now`, expander from
   `attachment_context::expanded_message_content` over a `FileContextDeps`. **Recorded gap:** the
   settings' *env readers* are not ported, so only a default-configured deployment would agree.
2. `request_base_url` — `Host` trusted only when `host_without_port(host)` is in
   `allowed_auth_hosts()`, else `http://127.0.0.1:{port}`.
3. The **file-index** refusal only (forced search owes nothing), taken before the expander runs,
   because `search_file_chunks` returns a bare `Vec<i64>` and cannot refuse itself.
4. The error envelope: `build_deepseek_request` raises `AppError` as
   `{"error": …, "code": …}` + `AppError.status`, while the route answers
   `{"error": {"message": …, "type": …}}`. The frozen REST inventory records the route but **no**
   error envelope, so this is a compat decision — and it moves in the same change.
5. A real-upstream integration test; matching the oracle's bytes does not prove the body reaches
   DeepSeek correctly.

Decision B (a `memory` domain declaration) still gates wiring the **write** half.

---

## The assembly environment landed — the last prerequisite before the route swap

**Branch `main`, HEAD `a017e402`** (the OpenAI-chat composition, committed by the previous
round; `origin/main` is still `1b856bec`). Working tree carried only the files below.

`deepseek-gateway::assembly_env::NativeAssembly` binds the nine `AssemblyEnv` fields from the
server environment: the five settings structs at the oracle's own `Default`s, a real
`BudgetStore` + `LedgerDeps` over `<root>/.budget`, the real `FileStore` and
`attachment_context::expanded_message_content` expander, and the OS zone through
`local_clock::system_local_now`. Seven unit tests.

`with_env` is a closure rather than a returned struct because `AssemblyEnv` borrows a ledger and
an expander that borrow *their* stores — the whole graph has to live on one stack frame, and that
frame is `with_env`.

### Two decisions the tests forced, both measured

**`DEEPSEEK_INFRA_ROOT` unset is an error, not a degrade.** `chat_tool_loop::ToolRoundExecutor`
treats an unset root as "no workspace" and lets the data branches report their disabled path. The
assembly cannot: the memory store, the file cache and the budget ledger all live under the root,
so reading them from anywhere else would be a silent divergence. The refusal names the variable.

**The file-index refusal is a flag, not a predicate.** `search_file_chunks` returns a bare
`Vec<i64>` and cannot refuse itself, and a duplicated predicate about attachments could drift from
`expanded_message_content`'s real trigger. So the injected search sets a `Cell` flag and
`with_env` returns it; a caller that sees `true` refuses. The guard therefore fires exactly when
the oracle itself would have called the index.

My first two tests were wrong and the code taught me why — worth recording because it narrows the
condition further than the plan assumed:

- the attachment field is **`fileId`**, not `id`, and the cached document must exist:
  `build_attachment_context` calls the search only from the `Ok(cached)` arm of
  `load_cached_file`;
- even then, `select_file_chunk_indices` returns early and **never asks the index** unless the
  chunk text exceeds `min(FILE_FULL_CONTEXT_LIMIT, char_budget)` — 60 000 characters here. So a
  small attached file is served in full and must **not** trip the guard, and refusing on "has a
  file attachment" would refuse requests the oracle answers completely.

Both sides are now pinned: a 70 000-character attachment trips the flag, a short one does not.

### Verification

- `cargo test -p deepseek-gateway` → **162 lib tests** (7 new) plus every integration target
  (7 + 6 + 1 + 1); `cargo test -p deepseek-policy` → 400 passed.
- Workspace `fmt --all -- --check` exit 0; workspace clippy (1.85, `--locked --all-targets
  --all-features -- -D warnings`) **exit 0 with no diagnostics**.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against any of this.

The route still does not call any of it. Every prerequisite is now landed and probe-verified, so
the swap itself is the next slice and `assembly-wiring-plan.md` §5 lists its four remaining
pieces: `request_base_url`; the refusal the flag drives; the error envelope (**the tests in
`tests/chat_execution.rs` capture the upstream body and are the regression net**); and a
real-upstream integration test. The recorded env-reader gap for the settings stays open and is
noted as the boundary of what this assembly guarantees.

Decision B (a `memory` domain declaration) still gates wiring the **write** half.

### The swap landed: the route composes through the assembly

`chat_completions` no longer builds its own body. It runs the oracle's order — facade translate,
validate, bind the assembly, message rules, memory, build — and the four pieces §5 listed are in:
`request_base_url`, the `rag_vec` refusal, the `{"error", "code"}` envelope, and
`tests/chat_execution.rs` re-derived as the regression net.

The slice arrived in the working tree half-written, so this records what finishing it took. The
last piece was not a typo.

**It did not compile.** Three errors, all interruption artefacts: `ModelRouterSettings` missing from
two scopes, and the real one — the closure still called
`native_chat::prepare_openai_chat(&raw, &base_url, env)` after the facade and the validation had been
moved *before* the workspace binding. Deleting that stale call and threading the router through the
remaining call sites was most of it; the probe's call site and two unused `let env` in the tests were
the rest.

**The message rules cannot run where the comments said they do, and that is measured.** Both the
module doc and `PreparedOpenAiChat`'s said the preflight runs before the workspace is bound, so a
request with no user turn answers `400` rather than `500`. Running the whole preflight there needs
`preflight_deepseek_payload`, which needs the content expander — and the oracle defines the message
rules over `normalize_chat_messages`, whose first act is
`content = expanded_message_content(message)` (`deepseek_client.py:492`). A blank turn is only blank
*before* expansion. Forcing the rules early with a plain-content expander is **measurably wrong**:
`native_chat_composition_parity_probe` diverges at `case::blank-content-turn`, where the oracle
accepts the turn and the plain expander answers `invalid_message_content`. So the validation half
stays pre-binding (credential, model, `messages` — no store needed) and the route runs
`validate_request_messages` with the **real** expander inside `with_env`, before the memory read:
the oracle's order, with the workspace bound. The cost is exact and stated — on a process with no
`DEEPSEEK_INFRA_ROOT`, a request that fails a message rule answers `500` instead of `400`. That is
the boundary, not a claim.

**The regression net was stale, not broken.** Three `lib.rs` cases asserted the old route's own
errors — `503 NATIVE_CHAT_UPSTREAM_CREDENTIAL_MISSING` for a missing credential, and its own
`a user message is required` / `context compression is required` wording. The oracle answers
`400 missing_api_key` (validation checks the credential **first**, `deepseek_client.py:198-201`,
with `AppError`'s default status) and its own two sentences, so the cases now assert those.
`tests/chat_execution.rs` needed the same for the body it captures: the assembled body leads with the
catalog tools' parallel-call hint, carries the client's own turn, and takes the per-turn context as
the trailing system message, which moves a round's assistant/tool pair from index 1/2 to 3/4. Its
cases also need a workspace root now — the guard installs one — and `DEEPSEEK_API_KEY` is forced, so
a developer shell cannot silently change what they test.

**Verified**: `cargo test -p deepseek-gateway` → 162 lib plus 7 + 6 + 1 + 1 integration, all passed;
`cargo test -p deepseek-policy` → 403 passed; workspace `fmt --check` and clippy (1.85,
`--locked --all-targets --all-features -- -D warnings`) clean; `native_chat_composition_parity_probe`
**byte-identical** again (315 124 B, `diff` empty after `tr -d '\r'`), which is the evidence that
dropping the plain-expander attempt restored parity.

**The batch is green**: run `35411153854` on `6e0af1ef` — **all 35 jobs succeeded**, `rust-docker`,
`evidence-assembly`, `rc-readiness` and `release-package` among them.

The first attempt (`35409221685` on `5d247be9`) was 31 green and four not: `rust-docker` failed, and
`evidence-assembly`, `rc-readiness` and `release-package` fell with it. That is one root cause, not
four — `evidence-assembly` and `rc-readiness` both need `rust-docker`, and the gate's step is a bare
`exit 1` under "Require every upstream Evidence gate", so it reads like an independent failure and is
not; `release-package` was skipped behind them. Worth knowing before diagnosing the next one.

The cause was the same class of stale contract as the `lib.rs` cases, in Python:
`scripts/smoke_rust_sidecar.py` expected `POST /v1/chat/completions` to answer `503` with a *nested*
`NATIVE_CHAT_UPSTREAM_CREDENTIAL_MISSING`. The wired route answers `400 missing_api_key` — the
oracle's own validation error, in the oracle's flat `{"error": <message>, "code": <code>}` envelope.
The requirement the script encodes (fail closed, never a fabricated completion) is unchanged and
still asserted; `tests/test_rust_docker_config.py`'s stubbed sidecar now answers with the new
envelope, so the script's new expectations are exercised end to end rather than assumed (9 passed).
`worker-execution-plan.md` carried the same stale row for this case; the rest of that document's
drift — its "Still on the Python path" list still names memory retrieval, context compression and the
model router, all of which are ported — is left alone and flagged rather than silently rewritten.

### Decision B, advanced: "read-only" was hiding a live writer

Decision B asked whether to declare a `memory` domain or land the wiring read-only. The read-only half
is already landed, so the honest next step was to *verify* that claim rather than repeat it. It did
not hold.

**The route's refusal is not the only reachable write.** `has_explicit_memory_command` inspects the
*user's* text. The tool loop is the other path, and it executes `forget_memory`: it is not among the
seven unported branches (`chat_tool_loop.rs`'s module doc names them), and its body deletes —
`memory.rs:874` calls `delete_memories_by_query(cleaned, Some(&scopes), root, clock)`. A model that
calls the tool on its own reaches the store with no command in the turn at all.

**Demonstrated, then fixed.**
`chat_route_refuses_the_memory_deleting_tool_instead_of_writing_the_store` seeds a memory under the
test root and has the stubbed upstream answer with a `forget_memory` tool call. Before the refusal it
got `{"ok":true,"result":{"deleted":1,"query":"dentist","scopes":["global"]},"tool":"forget_memory"}`
and the file changed — a turn whose text never matched the command grammar, deleting from the store
Python owns. The loop now answers `DispatchOutcome::Denied` with the same code the turn-level refusal
uses, `NATIVE_MEMORY_WRITE_NOT_OWNED`, because it is the same reason. `suggest_memory` needs no
refusal: it builds a suggestion and writes nothing.

**The declaration was prepared, not applied on my own initiative.** `release/native_runtime_ownership_v1.json` is
`status: accepted` with `approved_by: ["leizd"]`, so an added domain amends a contract that carries
your signature; §3b of the plan holds the exact entry (`memory_store`, python → rust,
`durable_store: rust_data`, and a cutover that was written as 4.9.2 and later moved to 4.9.4 — see the
section below for why the mode, not the analogy, decides it), with its two judgement values flagged as
such. The measured context for
that decision: the barrier is policy, not machinery (`GO_CONTROL_DOMAINS` is 28 ids with no memory
entry, and the memory file is in no `durable_stores` entry), and Python has **four** write entry
points — so moving `current_owner` is a Python-side decommissioning, not a one-line edit. The stakes
are concrete: at the `chat_completions_fast_path` cutover the `501` becomes a capability regression
against Python, and the contract's `forbidden` list contains `permanent_python_fallback`, so the
cutover is where the handover has to be recorded.

**The amendment was then accepted and applied.** `domains` 43 → 44, with `validate_ownership`
accepting the entry, `scripts/native_runtime_contract.py --check` reporting `"ok": true, "domains":
44`, `scripts/check_zero_python_runtime.py` still PASS 8/8, and the ownership plus zero-python tests
at 17 passed. It changes no runtime behaviour: `GO_CONTROL_DOMAINS` is the mechanical denial and it
still has no memory identifier, so the declaration is intent plus the cutover it will be judged
against. What remains open is the handover itself — stopping Python's four write paths and filling
the route's two refusals with the ported write half — which is a slice of its own.

**Verified**: `cargo test -p deepseek-gateway` → 162 lib plus 8 + 6 + 1 + 1 integration, all passed
(the new case included); `cargo test -p deepseek-policy` → 403 passed; workspace `fmt --check` and
clippy (1.85, `--locked --all-targets --all-features -- -D warnings`) clean.

**Not pushed**, and the ownership handover itself is not started — the hole fix is a refusal, and the
declaration is intent. Both commits sit on `main` ahead of origin.

### The handover body: the Python side is now mechanically stoppable

`memory_store` is declared, so "how do Python's four write entry points stop" had to be answered
mechanically rather than by convention. It is one edit, because they are one choke point:
`upsert_memory`, `save_memories`, `delete_memories_by_query`, `delete_memory_by_id`,
`clear_memories` and the turn-level command (through the first and third) all pass through
`memory._save_memories_unlocked`, so `assert_python_writer_allowed("memory_store")` sits there, ahead
of the directory creation. The two delete paths reach it only when they would really delete — a no-op
is not a write — and the test asserts that in both directions.

`authority.py` grew `RUST_DATA_DOMAINS`, and the gate is deliberately **not** symmetric with the Go
one: control domains are denied in `GO_AUTHORITATIVE` and `PYTHON_DISABLED`, data domains only in
`PYTHON_DISABLED`, because ADR-0049 hands the control plane over first ("4.9.3 ... one at a time") and
the data plane can still be Python's during that window. `check_zero_python_runtime`'s
`mechanical_writer_denial` gate now verifies both halves — `28 Go control domains and 1 Rust data
domain`.

**A mismatch that was flagged, and is now settled by moving the declaration.** The gate fires in
`PYTHON_DISABLED`, which ADR-0049 places at **4.9.4**, while the declaration said `memory_store` cuts
over at **4.9.2** — so the mechanism was effective one version after the contract claimed. The
**declaration moved**: `memory_store` now cuts over at **4.9.4**, the first version whose mode actually
stops Python. Verified: `validate_ownership` accepts it, `scripts/native_runtime_contract.py --check`
reports `"ok": true`, `check_zero_python_runtime.py` PASS 8/8, and the ownership plus gate tests are 17
passed. 4.9.4 has exactly one member — this domain — which is the point: the data-plane handover is
tied to the version that de-authorizes Python, not to the one that moves the listener.

**Verified**: 52 tests across the memory, ownership, gate and docker-config files;
`scripts/check_zero_python_runtime.py` PASS 8/8; `ruff check .` and `mypy .` (888 files) clean. The
new denial test is **able to fail**: disarming the gate's domain string turns it red, which is how it
was checked, and the file was restored byte-identically afterwards. `ruff format` is deliberately not
run — the repository does not use it, and CI checks only `ruff check .`.

**Not pushed**, and the Rust write half is deliberately not filled: ADR-0049 leaves the prior owner
authoritative until its cutover gate passes and does not permit dual writers, so it lands *with* the
mode flip.

### The other half of the handover: the refusals became a flip

Both route refusals are now driven by one predicate instead of being constants, which is what turns the
handover into a **flip** rather than a rewrite.

`lib.rs`'s `native_owns_memory_store()` (with the testable `memory_store_owner_is_native(mode)` under
it) is true exactly when `DEEPSEEK_RUNTIME_MODE=python_disabled` — the same signal
`authority.py`'s gate reads. It is deliberately **not** `DEEPSEEK_GO_CONTROL=1`: that is the *control*
plane's mode, the ADR hands the control plane over one domain at a time (4.9.3) while the data plane can
still be Python's, and reading it as data-plane ownership would put two writers on one file. A test
pins that reading, and the mode table with it.

While it is false the turn-level refusal and the tool-level denial behave exactly as before — the tests
written for them did not change — and while it is true the turn calls the oracle's own
`prepare_memory_state` (command first, then retrieval, so a memory saved this turn is retrievable in it)
and `forget_memory` dispatches for real. One environment variable decides which side writes; the store
never has two.

**Verified from both sides.** `chat_route_saves_the_memory_once_the_mode_de_authorises_python` and
`chat_route_runs_the_memory_deleting_tool_once_the_mode_de_authorises_python` are the mirrors of the two
refusal cases — same bodies, one mode different — and both are **able to fail**: forcing the predicate
to `false` turns exactly those two red and leaves the other eight green, which is how it was checked.
Totals: gateway 163 lib plus 10 + 6 + 1 + 1 integration; policy 403 passed; `fmt --check` clean; clippy
clean on lib and test targets.

**A host quirk worth recording**: three clippy runs in a row failed on this machine with
`error: failed to write D:/deepseek/rust/target/debug/examples/*.rmeta: os error 5`, reported as
"could not compile … due to 1 previous error" for files that had no compile error. Disk has 20 GB free
and a manual write into the same directory succeeds, so it is intermittent file contention, not
permissions or space. `CARGO_INCREMENTAL=0` is the lighter workaround (it also made `cargo check`
clean); deleting `target/debug/incremental` is the heavier one.

**Not pushed**: six commits now sit on `main` ahead of origin, and the flip is inert until the mode is
set — nothing runs differently today.

### The reminders write is refused, and the refusal is export-proof

The question "which of the two undeclared stores is treated wrongly" answered itself once measured: it
is the reminders write, the one the route performed unconditionally. `.reminders` sits exactly where
`.memory` sat before its declaration (undeclared — `remind` appears nowhere in the contract,
`GO_CONTROL_DOMAINS` or the command codes — and written by Python from three paths, one of them the
*delivery* poll `due_reminders`), and ADR-0049's "it does not permit dual writers" forbids the native
side writing it. On the owner's call the treatment is now the same as memory's: **refuse now, declare at
the cutover.**

The gate grew a second condition rather than a second constant. `python_is_de_authorised()` is the
deployment-wide mode (ADR-0049's 4.9.4), and `may_write_native_store(domain)` is that **and** the
domain's presence in `DECLARED_NATIVE_DATA_DOMAINS`. That split is the whole point: `reminders_store` is
not in the list, so `create_reminder` answers `NATIVE_REMINDERS_WRITE_NOT_OWNED` **whatever the mode
says**, and one case asserts it twice — refused by default, and still refused with
`DEEPSEEK_RUNTIME_MODE=python_disabled`. No environment variable can enable a store nobody has declared,
which is the mechanical half of "declare it at the cutover". A second test pins the list as a **subset**
of the contract's python → rust data domains, so it cannot invent ownership (nine domains carry
`durable_store: rust_data`, and most belong to other planes).

**Three cases had asserted the reminder write**, and each was updated rather than deleted, keeping its
own purpose: the integration case now asserts the refusal and that no store appeared; the streaming case
still proves a tool round is *continued* rather than failing the turn, with the refusal as the replayed
tool result; and `chat_tool_loop`'s unit test proves workspace injection by seeding the store under the
injected root and reading it back, which is stronger evidence than the write it used to lean on. Both
refusal assertions were shown **able to fail**: disarming the gate turns the integration case and the
streaming case red, and nothing else.

**Correction worth recording**: my first version of the contract-pinning test asserted the writable list
*equals* the contract's python → rust data domains. That was wrong and it failed immediately — the
contract declares nine such domains, most of them other planes' stores. A subset is the correct
invariant, and it is the one the Python side already used.

**Verified**: `cargo test -p deepseek-gateway -p deepseek-policy` → 164 lib plus 10 + 6 + 1 + 1
integration and 403 policy, all passed; `fmt --check` clean; clippy clean on lib and test targets.

### The reminders cutover, all three pieces

The declaration landed on the owner's word, and cutting a store over takes **three** edits rather than
the two it looks like — because a store has two writers and each needs its own gate:

1. `release/native_runtime_ownership_v1.json` gained a `reminders_store` domain (python -> rust,
   `cutover: 4.9.4`, `durable_store: rust_data`). `domains` 44 → 45, and 4.9.4 now has exactly two
   members: `memory_store` and `reminders_store`.
2. `lib.rs`'s `DECLARED_NATIVE_DATA_DOMAINS` gained `"reminders_store"`. Without it the Rust side keeps
   refusing a store it now owns, and the flip would never happen.
3. `authority.RUST_DATA_DOMAINS` gained the same domain, and `reminders._write_reminders` now calls the
   gate. **Without this the flip is asymmetric** — Rust starts writing while Python is still allowed to,
   which is precisely the dual writer ADR-0049 forbids, and it would bite hardest in the
   `DEEPSEEK_LEGACY_PYTHON=1` rollback where the Python server *is* running.

All three are inert today (the default mode is `python_authoritative`), so nothing runs differently; what
they buy is that the flip is now one environment variable, symmetric on both sides, for both stores.

**Tests moved with it.** The integration case flipped from "refused even when the mode is set" to the
memory tests' two-sided shape — refused by default, written once the mode flips — and the
"undeclared-here stays refused" property moved onto `s3_minio_streaming`, a domain the contract *does*
declare python -> rust and that this gateway must still refuse. Python gained
`tests/test_reminders.py::test_every_reminder_write_path_is_denied_once_python_is_de_authorized`, which
covers all three write paths at once (creation, the delivery poll's marking, deletion) and asserts the
store is byte-identical afterwards. Each refusal assertion was shown **able to fail**: disarming a gate
turns exactly the corresponding case red, and nothing else.

**Verified**: `cargo test -p deepseek-gateway -p deepseek-policy` → 164 lib plus 10 + 6 + 1 + 1
integration and 403 policy; `pytest` over the reminders, memory, ownership and gate files → 33 + 17
passed; `ruff check .` and `mypy .` (888 files) clean; `check_zero_python_runtime.py` PASS 8/8, now
reporting `all 28 Go control domains and all 2 Rust data domains`; the contract CLI reports
`"ok": true`.

**A process note worth keeping**: an append executed by this host's shell tool can run **twice** (the
sandboxed pass and the escalated retry), and `cat >>` is not idempotent — the test above was appended
twice, which `mypy` caught as `no-redef` and `ruff` as a redefinition. Guard repeats with
`grep -q … ||`, or edit by unique anchor instead of appending.

---

## fetch_url landed: DNS-time SSRF, locked HTTP, and the loop no longer says it did not run

**Branch `main`, HEAD `57f0595b`** (reminders cutover). This slice is uncommitted on top of that.

`fetch_url` was the first remaining tool branch whose dependencies were already in the tree: the
static URL guard is aligned, `reqwest::blocking` is how the search provider talks to Tavily, and
the tool loop was already the production caller. It was resolving to `Tool did not run`. That is
now a real branch.

### What landed

`deepseek-policy::fetch_url` ports `resolve_public_url` / `ensure_public_address` /
`fetch_public_url` / the cache / `extract_html_text` (the shipped fallback; `trafilatura` is not a
production dependency). DNS and HTTP are injected as `FetchContext` callbacks, same shape as the
search transport, so the policy crate stays free of TLS.

`deepseek-gateway::fetch_provider::locked_http_get` is the connection the oracle's
`LockedHTTPConnection` performs: connect to the pinned address, send `Host: host_header` and the
oracle User-Agent/Accept, disable redirects (the policy crate re-resolves `Location`), and
**disable ambient HTTP(S)_PROXY**. The last of those hung the first TCP test on this host — the
shell carries `HTTPS_PROXY=http://127.0.0.1:7897/`, and reqwest would have sent the "locked"
request through Clash. The oracle does not. `.no_proxy()` is the fidelity fix, not a test hack.

A hostname that is already an IP is checked with `ensure_public_address` before DNS is consulted.
That is equivalent for literals (`getaddrinfo("127.0.0.1")` returns `127.0.0.1`) and is what stops
a stub DNS from laundering a redirect to `http://127.0.0.1/admin` into a public stand-in.

### Verification

- Probe pair byte-identical: **30 keys / 3818 chars**, LF-normalized md5
  `844b896fa61715c0663273d8d8a13abb`. Covers resolve accept/refuse, address block set,
  HTML extract, cache hit (one HTTP for two fetches), redirect revalidation, oversize body,
  HTTP 503 status cap.
- `cargo test -p deepseek-policy` → **415 passed** (12 new in `fetch_url` + the dispatch
  "not enabled rather than unported" case).
- `cargo test -p deepseek-gateway` → **166 lib** + **11 + 6 + 1 + 1** integration, including
  `chat_route_refuses_a_private_fetch_url_target` (wired loop refuses `http://127.0.0.1/admin`
  instead of `Tool did not run`) and the locked TCP test (connects to 127.0.0.1, `Host:
  example.com`, oracle UA). The unported-branch case now uses `search_files`.
- `cargo +1.85.0-x86_64-pc-windows-gnu clippy -p deepseek-policy -p deepseek-gateway
  --locked --all-targets --all-features -- -D warnings` clean. Local stable (1.97) still
  fires the pre-existing `control_proxy.rs` `result_large_err`; CI's 1.85 does not.
- `ruff check` / `mypy` pass on the new probe. Docs language nav PASS (200 files); doc links OK.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run against this.

Six tool branches remain: `search_files` (scorer + file cache already ported — the next
dependency-satisfied slice), `browser_*`, `python_eval` (real sandbox, must not shell out to
CPython), `create_mindmap` / `create_pptx` / `create_document` (media; high-risk, validate
early rather than last). Then MCP/A2A, skills, automation, OCR, launchers, and the zero-Python
cutover.

`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`. This slice does not change
that: production HTTP is still Python-authoritative; the native gateway is still an opt-in
delegate.

---

## search_files landed: json_hybrid is the production path, RAG sqlite stays Python-written

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted `fetch_url` slice.

`search_files` was the next remaining tool whose dependencies were already in the tree:
`query_tokens` / `score_chunk`, the file-cache layout, and the read-only RAG cosine+BM25
path from `memory_index`. It was resolving to `Tool did not run`.

### What landed

`deepseek-policy::search_files` ports the oracle's two retrieval paths and merges them
by `(fileId, projectId, chunkIndex)` keeping the higher score:

1. **json_hybrid** — walk `.file-cache/*.json` and `.projects/*/files/*.json`. Complete.
2. **local_rag** — read-only `search_files_index` over collection `files`. `MemoryIndex`
   grew a collection parameter so files and memories share one reader.

**It does not call `index_file_payload`.** That function writes `rag_items`. Python is
still the writer; indexing from the native search would be a second writer of one table.
json_hybrid still finds anything sitting in the cache JSON, which is the source
`index_file_payload` itself reads. A missing sqlite file degrades to json_hybrid only.
When `rag_vec` is present the sqlite path is skipped (same refusal as the memory index).

`compact_snippet` windows by **code point**, matching `len(str)` / `s[start:end]`. A first
draft used `str::find` byte offsets and would have sliced CJK wrong.

### Verification

- Probe pair byte-identical: **3423 chars**, LF-normalized md5
  `133204b547c51707467ed66ca058b21b`. Python `search_files_index` stubbed to `[]` so the
  comparison is the json_hybrid path without a dual-writer. Covers snippet windows
  (ASCII + CJK), empty/blank query, two-index merge, corrupt cache skip, no-hit.
- `cargo test -p deepseek-policy` → **419 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **12 + 6 + 1 + 1** integration,
  including `chat_route_searches_cached_files`. The unported-branch case now uses
  `python_eval`.
- Clippy 1.85 GNU `--locked -D warnings` clean on policy + gateway.
- `ruff` / `mypy` pass on the new probe.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Five tool branches remain. Next is **`create_mindmap`**: pure SVG, no python-docx /
python-pptx / reportlab, so it is the media path that can be proven native without
waiting on Office libraries. Then `create_pptx` / `create_document` (those libraries
are the high-risk remainder), `browser_*`, `python_eval`.

---

## create_mindmap landed: SVG is byte-identical, generated-file store is unique-id creates

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted `fetch_url` + `search_files`
slices. This is the first media tool, chosen because it is pure SVG and does not
need python-docx / python-pptx / reportlab.

### What landed

`deepseek-policy::mindmaps` ports layout, CJK/ASCII tokenization, wrapping, XML
escaping and SVG rendering. `generated_files` ports `store_generated_file` /
`resolve_generated_file` / cleanup / `_safe_filename`. Ids are
`Entropy::new_file_id` (`secrets.token_hex(16)`, 32 hex chars).

`.generated` is unique-id creates with a 6-hour TTL, not a durable
read-modify-write table, so it is not a declared ownership domain.

### Verification

- Probe pair byte-identical including the SVG: **5979 chars**, LF-normalized md5
  `be02bc5fc34cccdf49bc7752bc743c8a`. Covers empty title/nodes, the sample
  outline, XML escaping, and `title`/`name` aliases.
- `cargo test -p deepseek-policy` → **424 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **13 + 6 + 1 + 1** integration,
  including `chat_route_creates_a_mindmap_svg`.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Four tool branches remain: `create_pptx` / `create_document` (Office/PDF
libraries — high-risk), `browser_*`, `python_eval` (must not shell out to
CPython). Then MCP/A2A, skills, automation, OCR, launchers, zero-Python cutover.

---

## create_document landed: content model is byte-identical, files are valid OOXML/PDF

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted fetch_url / search_files /
create_mindmap slices. This is the high-risk media path that needed a native
docx+pdf writer without python-docx / reportlab.

### What landed

`deepseek-policy::documents` ports format aliases, section/table normalization
(including ragged-row padding to `max(headers, rows)`), MD5 theme selection
(`int(hex, 16) % 6` on the full 128-bit digest — a first draft truncated to
`usize` and picked the wrong theme), the outline/note envelope, and writers:

- **docx**: uncompressed OOXML zip (`[Content_Types].xml`, rels, `word/document.xml`,
  numbering, footer PAGE field). CJK is UTF-8 in the XML; Word uses 微软雅黑.
- **pdf**: PDF 1.4 with `/STSong-Light` + `/UniGB-UCS2-H`, the same CID approach
  as reportlab `UnicodeCIDFont`. CJK is UTF-16BE hex in the content stream.

The Office/PDF **bytes** are not python-docx/reportlab fingerprints. Those
libraries are not a frozen protocol. Tests assert magic, zip membership, and
that title/headings survive in the payload.

### Verification

- Content-model probe byte-identical: **4798 chars**, md5
  `a09bbde438e3ca1e86f79c0c7e15c953`.
- `cargo test -p deepseek-policy` → **428 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **14 chat_execution**
  (including `chat_route_creates_a_docx_document`) + 6 stream + 1 + 1.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Three tool branches remain: **`create_pptx`** (last media; python-pptx),
`browser_*`, `python_eval` (must not shell out to CPython). Then MCP/A2A,
skills, automation, OCR, launchers, zero-Python cutover.

---

## create_pptx landed: content model is byte-identical, files are valid 16:9 OOXML

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted media slices.
This finishes the three `create_*` artifact tools.

### What landed

`deepseek-policy::presentations` ports `create_presentation`: refusals, `content`
→ bullets, MD5 deck theme, layout picker (quote / cards / process / comparison /
summary / requested layout), automatic agenda at ≥4 content slides, outline/note,
and a 16:9 OOXML zip writer (`ppt/slides/slideN.xml`, blank master/layout, CJK
via 微软雅黑).

The `.pptx` **bytes** are not python-pptx fingerprints. Tests assert zip magic,
slide XML membership, and that the title survives. `create_presentation_from_text`
stays a slides-skill path, not this tool branch.

`zip_store` moved into `generated_files` so docx and pptx share one STORE-method
writer.

### Verification

- Content-model probe byte-identical: **3676 chars**, md5
  `d360f5e45f605284e51c13dbcdeba9ea`.
- `cargo test -p deepseek-policy` → **432 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **15 chat_execution**
  (including `chat_route_creates_a_pptx_deck`) + 6 stream + 1 + 1.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Two tool branches remain: **`browser_*`** (needs a browser engine) and
**`python_eval`** (must not shell out to CPython). Then MCP/A2A, skills,
automation, OCR, launchers, zero-Python cutover.

---

## python_eval landed: in-process AST sandbox, no CPython child

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted tool slices.

The oracle's `python_eval` is not a full interpreter: it is `sys.executable -I
-c PYTHON_EVAL_RUNNER`, an AST allowlist + `eval(..., {"__builtins__": {}})`.
The port parses, validates and evaluates that same allowlist in-process. It
does **not** fork CPython.

### Verification

- Probe byte-identical: **3458 chars**, md5 `b545a8f9c49c5fbbb6e2010c595ae3f2`.
  Covers factorial/arithmetic/math/compare/min/max/sum/pow/round/abs/len/
  bool-if/tuple/subscript/gcd/comb, plus empty/oversize/import/unknown/open/div0.
- `cargo test -p deepseek-policy` → **435 passed**.
- `cargo test -p deepseek-gateway` → **166 lib** + **16 chat_execution**
  (including `chat_route_evals_a_python_expression`). Unported case is now
  `browser_click`.
- Clippy 1.85 GNU `--locked -D warnings` clean.

Integer overflow on huge factorials is a documented bound (i128); the probe
corpus does not hit it.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

One tool family remains: **`browser_*`** (Playwright). Then MCP/A2A, skills,
automation, OCR, launchers, zero-Python cutover.

---

## browser_* landed: safety gate + static HTML controller, no Playwright

**Branch `main`, HEAD `57f0595b`**, on top of the uncommitted tool slices.

`browser_*` was the last unported dispatch family. The oracle already has a
**StaticController** fallback when Playwright is missing. Native ports that
path plus the safety policy, not Chromium.

### What landed

- `browser_safety`: `evaluate_action` / `evaluate_url_safety` (disabled-by-default,
  private hosts, credentials, high-risk click, password fields, confirmation).
- `browser`: in-memory sessions, `execute_browser_action`, static HTML parse of
  approved `file://` fixtures.
- Dispatch: all **18/18** branches now run. Unported `Tool did not run` is no
  longer the live path for a catalog tool.

### Honest gaps

- Playwright is not ported.
- Static controller does **not** `urlopen` public HTTP (Python's StaticController
  does). Allowed `https://example.com` then fails closed with a visible error.
- Media/RAG snapshot writes stay Python (`indexed: false`).

### Verification

- Safety probe byte-identical: **2252 chars**, md5
  `ae657d74bdda48155bea65a6b20c6993`.
- `cargo test -p deepseek-policy` → **440 passed** (fixture `file://` open).
- `cargo test -p deepseek-gateway` → **166 lib** + **16 chat_execution**
  including `chat_route_blocks_a_private_browser_url`.
- Clippy 1.85 GNU `--locked -D warnings` clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run.

Next: Playwright engine **or** MCP/A2A / Go control-plane cutover / desktop and
Android zero-Python launchers. The chat-tool inventory is no longer the bottleneck.

---

## Native `/mcp` hub landed: tools/list + tools/call through dispatch

**Branch `main`, HEAD `57f0595b`**.

The native gateway's `POST /mcp` used to answer `native MCP tool execution is
not wired` for `tools/list` and `tools/call`. It now implements the Python Tool
Hub's JSON-RPC methods for **local** tools.

### What landed

- `deepseek-policy::tool_catalog::mcp_tools` — MCP shape + risk-card annotations
- `deepseek-gateway::mcp_hub` — `initialize` (with instructions), `ping`,
  `tools/list`, `tools/call` (policy-gated `execute_call_sync`), resources,
  prompts
- `ToolRoundExecutor::execute_call_sync` — one-call path for the hub

External `mcp__*` is a **tool error**, not a fake success.

### Verification

- `mcp_hub` unit tests: list includes `python_eval`/`create_pptx`; `2+2` → `4`
- `mcp_initialize_and_tools_call_are_native` on `POST /mcp`
- `cargo test -p deepseek-policy` → **440 passed**
- `cargo test -p deepseek-gateway` → **169 lib** + **16 chat_execution** + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. A2A `/a2a` still `not wired`.
Playwright, Go cutover, desktop/Android, exact-head CI remain.

---

## Native A2A mesh landed: Agent Cards + task lifecycle

**Branch `main`, HEAD `57f0595b`**.

`POST /a2a` used to answer `native A2A execution is not wired`. It now
implements the Python mesh's JSON-RPC methods for discovery and tasks.

### What landed

- `deepseek-gateway::a2a_hub` — orchestrator + researcher/coder/reasoner/critic
  Agent Cards (protocol 0.3.0)
- `GET /.well-known/agent-card.json`, `GET /a2a/agents`,
  `POST /a2a`, `POST /a2a/agents/{id}`
- `message/send`, `tasks/get|cancel|list`, `agent/getAuthenticatedExtendedCard`
- Injected task runner; default **fails the task** (`native A2A task runner is
  not attached`) instead of inventing an answer

Streaming SSE (`message/stream`) is not ported.

### Verification

- `agent_cards_cover_orchestrator_and_workers` (researcher tags include
  `web_search`)
- `message_send_runs_injected_runner` (`echo:hello` completes)
- `cargo test -p deepseek-gateway` → **172 lib** + **16 chat_execution** + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. No default upstream
`call_deepseek` on the native runner. Go `/api` still
`GO_CONTROL_PROXY_NOT_READY`. Playwright, launchers, exact-head CI remain.

---

## Native A2A runner landed: capability-scoped chat loop

**Branch `main`, HEAD `57f0595b`**.

`POST /a2a` `message/send` without an injected runner now queues a native job
and runs `a2a_runner::run_native_a2a`: system profile + capability-scoped
OpenAI tools + `execute_chat_with_tool_rounds`. Missing `DEEPSEEK_API_KEY`
fails the task (`A2A upstream is not configured`) instead of inventing text.

### Verification

- researcher tools = `web_search`, `compare_search_results`, `fetch_url`;
  reasoner has none
- `message_send_without_runner_queues_native_work`
- `cargo test -p deepseek-gateway` → **175 lib** + **16 chat_execution** + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. SSE `message/stream` unported.
Go `/api` still `GO_CONTROL_PROXY_NOT_READY` unless `GO_CONTROL_ADDR` is set
(and Go still does not serve public `/api`). Playwright, launchers, exact-head
CI remain.

---

## Go public `/api` landed: control status + honest 501

**Branch `main`, HEAD `57f0595b`**.

`deepseekd` now serves a public control-plane edge:

- `GET /api/control/status` — same JSON as `/healthz` (shadow, Python mutation
  authority, `productionMutation: false`)
- `GET /api/cutover/status?domain=` — read-only cutover record
- other `/api/*` → `501 GO_API_NOT_IMPLEMENTED` (not a fake success)
- `POST /api/cutover/transition` is **not** public; mutation stays `/internal`

### Verification

- `TestHealthzIsShadowAndReadOnly` also hits `/api/control/status` (GET 200,
  POST 405)
- `TestPublicAPIUnimplementedPathsFailClosed`
- `go test ./internal/api` coverage **96.8%**
- `go test ./...` ok

### Not done

**Not pushed.** Production HTTP is still Python. Native gateway still needs
`GO_CONTROL_ADDR` to forward `/api`. Remaining Python `/api` (config, chat,
tools, …) is 501 on Go. Playwright, launchers, exact-head CI remain.

---

## Go `/api/config` subset + mcp/a2a flags

**Branch `main`, HEAD `57f0595b`**.

`deepseekd` now serves a Go-owned **read subset**, not Python's full config blob:

- `GET /api/config` — `owner=go`, version, runtime, `hasServerKey`/`hasSearch`
  booleans (never the keys), default model, searchModes, mcp/a2a hub flags
- `GET /api/mcp` — protocol `2025-06-18`, `nativeHub`, `externalBridge: false`
- `GET /api/a2a` — protocol `0.3.0`, `streaming: false`

OCR/RAG/budget/toolPolicy are omitted, not faked. POST `/api/config` is 405.

### Verification

- `TestPublicConfigIsAGoOwnedSubset` / `TestPublicConfigReadsEnvFlags`
- `TestPublicMcpAndA2AStatusAreNativeHubFlags`
- `go test ./internal/api` coverage **96.8%**
- `go test ./internal/lifecycle` coverage **98.9%**

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`
writes, launchers, exact-head CI remain.

---

## Native `GET /api/tool-policy` (Rust, not Go proxy)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/tool-policy` is now served by the native gateway **ahead of**
the Go `/api/*` catch-all, using the already-ported `tool_policy_status` +
`read_recent_audit`. Settings knobs read `TOOL_POLICY_*` via
`ToolPolicySettings::from_env`. A missing audit log is `[]`, not an error.

`GET /api/policies` still `GO_CONTROL_PROXY_NOT_READY` without `GO_CONTROL_ADDR`.

### Verification

- `tool_policy_status_is_native_not_go_proxy` (28-card catalog, bad `limit` → 200)
- `cargo test -p deepseek-gateway` → **176 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`,
launchers, exact-head CI remain.

---

## Native `GET /api/budget` (Rust ledger, not Go proxy)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/budget` is served by the native gateway using
`budget_status` + `BudgetStore`. Missing `.budget/budget.db` is an empty
`today` (oracle path); the GET does not create the file.

### Verification

- `budget_status_is_native_not_go_proxy` (`scope=global` and `scope=agent`)
- `cargo test -p deepseek-gateway` → **177 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`
(gateway/scheduler/RAG status), launchers, exact-head CI remain.

---

## Native `GET /api/rag/status` (read-only `rag.sqlite3`)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/rag/status` is served by the native gateway using
`memory_index::local_rag_status`. The handle is **read-only** and never
creates `.local-rag/rag.sqlite3`. Missing DB → zero counts.
`sqliteVecAvailable` is `false` (native does not load `sqlite-vec`).

### Verification

- `local_rag_status_reads_the_fixture_and_does_not_invent_a_db`
- `rag_status_is_native_not_go_proxy`
- `cargo test -p deepseek-gateway` → **178 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`
(gateway/scheduler), launchers, exact-head CI remain.

---

## Native `GET /api/gateway/status` (context manager, honest queue gaps)

**Branch `main`, HEAD `57f0595b`**.

Python `GET /api/gateway/status` is served by the native gateway with the
ported context-manager knobs. Request queue and job scheduler are
`ported: false` — no invented `counts` / DLQ.

### Verification

- `gateway_status_is_native_not_go_proxy`
- `cargo test -p deepseek-gateway` → **179 lib** + 16 chat + 6 stream
- Clippy 1.85 GNU `--locked -D warnings` clean

### Not done

**Not pushed.** Production HTTP is still Python. Playwright, remaining `/api`,
launchers, exact-head CI remain.

---

## Native `/api/reminders` + `/api/reminders/due`: the data-plane HTTP edge, gated

**Branch `codex/native-a2a-stream-continuation`, HEAD `57f0595b`** plus the
pre-existing uncommitted slices. This slice is uncommitted on top.

The frontend calls `/api/reminders` on every reminder read and write, and it was
reaching the Go `/api/*` catch-all (`501 GO_API_NOT_IMPLEMENTED`) or Python.
`deepseek-gateway::data_routes` now serves it natively, registered **ahead of**
the Go catch-all because the store's authoritative writer is Rust — proxying it
to Go would put a second writer on one file.

### What landed

`data_routes.rs` mirrors `server.reminder_action` and
`server.api_due_reminders` exactly: the `list` / `create` / `delete` shapes, the
`{"error", "code"}` envelope, `read_json_body`'s empty-body `{}` → `list`
default, and the `Unsupported reminder action` 400. It calls the already-ported
`deepseek_policy::reminders` — no store logic was re-implemented.

**The gate is the point, and it is not a placeholder.** `POST
/api/reminders/due` reads like a query and **writes**: the oracle marks newly-due
entries `notified` and rewrites the file. `create` and `delete` do too, Python
reaches the same file from three paths, and both sides reproduce the same temp
name (`reminders.json` → `reminders.tmp`), so two writers can interleave. Every
mutating action is therefore refused with `NATIVE_REMINDERS_WRITE_NOT_OWNED`
(409) — the **same code and reason** the chat tool loop gives for
`create_reminder` — while `list` is served for real, because refusing a read the
frontend needs would be a capability regression rather than a correctness guard.

The refusal is driven by the existing `crate::may_write_native_store`, which
requires **both** `DEEPSEEK_RUNTIME_MODE=python_disabled` **and** the domain's
presence in `DECLARED_NATIVE_DATA_DOMAINS`. So the cutover is one environment
variable and needs no code change, and a mode alone cannot enable a store nobody
declared.

### Verification

`rust/crates/deepseek-gateway/tests/data_routes.rs` — **9 cases, all green**,
each driving `create_production_app`, so the auth layer and the registration
order are inside what is measured. Three are the ones that matter:

- the write is **real**: after the flip the reminder is in
  `.reminders/reminders.json` under the bound root, and `.workspace-generation`
  is exactly `2` — proving the write went through the mutation fence, not around
  it. A route that reported success without storing anything fails this.
- the refusal is **byte-exact**: a refused delete leaves the seeded file
  byte-identical, and a refused create leaves **no file at all**.
- the flip is **symmetric**: the same request one mode different stores the
  reminder for real.

Both refusal assertions were shown **able to fail** — forcing the gate predicate
to `false` turned exactly those two red and left the other seven green.

- `cargo +1.85.0-x86_64-pc-windows-gnu test -p deepseek-gateway -p deepseek-policy --locked`
  → **184 lib** + **9 data_routes** + 16 chat_execution + 6 stream + 5 + 1 + 1,
  and **441 policy**, all passed.
- Clippy 1.85 GNU `--locked --all-targets --all-features -- -D warnings` clean;
  `fmt --all --check` clean.
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**;
  `python scripts/native_runtime_contract.py --check` → `"ok": true`, 45 domains
  / 42 corpora / 31 versions; the reminders + ownership + gate tests pass;
  `go build ./...` and `go vet ./...` clean; docs language nav clean.

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run. Production HTTP is still Python, and
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`.

Remaining `/api` data surfaces the frontend calls: **memory** (`/api/memory`,
`/api/memory/search`, `/api/memory/conflicts`), projects/files
(`/api/projects`, `/api/project-files`, `/api/file-*`), media, skills, traces,
and the workspace backup/DR surface (184 Python routes total). The next
dependency-satisfied slice is **memory**: the store, the triple, the index read
path and the turn-state half are already ported and byte-verified, so only the
HTTP projection and its gate are new — the same shape as this one.

---

## The memory v3 layer and its `/api/memory` routes

**Branch `codex/native-a2a-stream-continuation`, HEAD `57f0595b`**, on top of the
uncommitted `/api/reminders` slice. Still uncommitted.

The slice the previous record named as next, and it landed as predicted: the store
was already ported, so the new work was the v3.0 **projection** layer plus the HTTP
routes and their gate.

### What landed

`deepseek_infra/infra/memory/` is not a second store. `schema.py`, `policy.py`,
`store.py` and `search.py` all delegate to `infra/data/memory.py` — the same
`.memory/memories.json` the chat turn writes. So this is a **projection** over one
authoritative store, and it is gated identically.

`deepseek_policy::memory_schema` ports it: the public vocabulary (`public_scope`,
`storage_scope`, `public_type`, `legacy_category`), the source sanitisation
(`normalize_source_ref`, `public_source`), `public_confidence`, `public_memory`,
the policy pair (`assert_memory_safe`, `readable_scopes`, `skill_can_read_memory`),
and the store/search operations (`list_memories`, `add_memory`, `edit_memory`,
`delete_memory`, `search_memories`, `memory_context_for_skill`).

`data_routes` gained the whole family: `GET`/`POST /api/memory`,
`DELETE`/`PATCH /api/memory/{id}`, `GET /api/memory/search`,
`POST /api/memory/conflicts`. Reads are served; every mutation is refused with
`NATIVE_MEMORY_WRITE_NOT_OWNED` while Python owns the store, and flips with the
same `DEEPSEEK_RUNTIME_MODE` the reminder routes and the tool loop read.

### The deadlock this slice found, and why it was structural

`edit_memory` must hold the store's process lock across the read, the patch and the
write — the oracle holds `_memory_lock` across all three so a concurrent upsert
cannot interleave. **Python's `RLock` is reentrant and Rust's `Mutex` is not**, so
the first version deadlocked on its own thread: it took `memory_process_lock()` and
then called `load_memories()`, which locks the same mutex again. It hung rather than
failing, which is why the symptom was a 10-minute test timeout and a stuck
`deepseek_policy-*.exe` holding the output binary.

Fixed by exposing the **unlocked** read/write pair (`load_unlocked_for_caller` /
`save_unlocked_for_caller`) for callers that already hold the guard, with the reason
documented at both ends. The unlocked write still takes the mutation fence — the
process lock and the fence are different guards.

### Three measured corrections

Every one of these was a wrong guess of mine, caught by running the oracle rather
than by reasoning:

1. **Identical content is not a memory conflict.** `memory.py:302` skips a candidate
   whose normalised content equals the incoming content, so `add` with the same text
   is the update path, not a 409. My first test asserted a conflict for identical
   text and failed; a conflict needs the same category, scope and conflict domain
   with **different** content.
2. **One `add_memory` bumps the fence generation four times, not two.** The oracle
   saves twice (`upsert_memory`, then `save_memories(_merge_item(item))` for the
   public fields), and each save is one fenced scope bumping twice. Measured against
   the oracle: it also reports `4`.
3. **`public_confidence("nan")` is `1.0`, not the `0.9` default.** The clamp is
   Python's `max(0.0, min(1.0, x))`, and `min` returns its *first* argument unless
   the second compares strictly less — `nan < 1.0` is `False`. `f64::clamp` cannot
   express this, so the pair is spelled out. The probe caught it on its first run.

### Verification

- **Byte-level parity probe pair** —
  `tasks/native-runtime/memory_schema_parity_probe.py` ↔
  `deepseek-policy/examples/memory_schema_parity_probe.rs`: **identical**, md5
  `d0bbb07505465d8759a9d1943486ec1f`, **164 keys**, 18 935 chars. It was shown
  **not blind**: inverting `public_scope`'s `project:` branch turns it red on exactly
  the `public_scope::project:*` keys, and the file was restored byte-identically.
- `cargo test -p deepseek-gateway -p deepseek-policy --locked` → **184 lib** + **19
  data_routes** + 16 chat_execution + 6 stream + 5 + 1 + 1, and **456 policy**
  (up from 441), all passed.
- The gate assertions were shown **able to fail**: forcing
  `may_write_native_store("memory_store")` to `false` turned exactly the two
  gate-dependent cases red and left the other seventeen green.
- Clippy 1.85 GNU `--locked --all-targets --all-features -- -D warnings` clean;
  `fmt --all --check` clean.
- `python scripts/check_zero_python_runtime.py` → **PASS 8/8**;
  `python scripts/native_runtime_contract.py --check` → `"ok": true`.
- `ruff` and `mypy` clean on the new probe (mypy found three real annotation bugs
  that are fixed).

### Not done, and the next executable task

**Not pushed.** Exact-head CI has not run. Production HTTP is still Python;
`release/native_runtime_5_0_evidence_v1.json` remains `NOT_READY`.

The remaining `/api` data surfaces the frontend calls, in dependency order:

1. **projects/files** — `projects` and `file_cache` are already ported and
   byte-verified (`list_projects`, `list_project_files`, `read_file_chunk`), so the
   routes are the same shape as this slice. The frontend calls `/api/projects`,
   `/api/project-files`, `/api/file-*`.
2. **skills**, **traces**, **media** — the ports do not exist yet.
3. The **workspace backup/DR** surface is the largest block (~90 routes) and is
   Go-owned in the target topology, so it belongs with the Go control API rather
   than here.

## Browser engine (ADR-0050 stages 1-4) and the public data routes

Work that was in the tree uncommitted. Stage 1 (`bae68f0b`) is this file's earlier
entry; stages 2-4 and the public-route helpers are recorded here, verified rather than
asserted.

### The engine

- `rust/crates/deepseek-browser`: `engine.rs` (spawn headless Chromium, `--remote-
  debugging-port=0`, read the DevTools socket off stderr, one page session in flat mode)
  behind `sidecar.rs` (one browser per gateway session id, unknown session is
  `not_found` rather than an implicit create, fences validated, no durable store, no
  second safety policy). `CloseSession` was added to the proto so a closed session
  releases its Chromium and profile.
- The gateway seam: `deepseek-policy::browser_engine` declares what the policy crate
  needs, `deepseek-gateway::browser_engine_client` implements it over the generated
  tonic client, `ToolRoundExecutor` attaches it to the tool loop, and
  `playwright_available()` remains the single switch — no engine configured means the
  static controller answers.

### Two defects found by running the probes, not by reading them

1. **`file_routes` had two wrong test expectations**, both of which asserted behaviour
   the oracle does not have. Measured by running the oracle:
   `normalized_page_texts(...)` over the nine-entry list returns **2** survivors, not 3
   (the list's own `pages[2]` assertion duplicated `pages[1]`, which is what a
   copy-paste looks like), and `page_text_from_cached_chunks({"chunks":[{"text":"chunk
   text"}]}, requested_page=3, page_count=5)` returns **`"k"`** — `per_page = 10 // 5 =
   2`, so page 3 is `text[4:6]`. The Rust implementation was already right; the tests
   now say what the oracle does.
2. **`scroll` did nothing, and the parity probe was racing.** Chromium animates a wheel
   and `page.mouse.wheel` returns before it lands, so reading `window.scrollY`
   immediately measures the race: two consecutive runs gave `oracle 900 / engine 0`
   then `oracle 0 / engine 900`. The engine now settles the position before answering
   (a deliberate divergence from `mouse.wheel`, recorded in the spec) and the probe
   settles both sides before reading. The pair passes on repeat runs.

### Verification

- `browser_engine_parity_probe.py --rust-example …` → **PASS**, six fixtures,
  `problems: []`, twice in a row: `url`/`title`/`text` identical, `links` identical
  after blob-UUID normalisation, 0 differing HTML bytes after whitespace collapse.
- `title_parity_probe` **PASS**; `file_routes_parity_probe` **PASS** (201 cases);
  `chat_stream_events_parity_probe` **PASS** (21 events) — all four driving their own
  Rust side and writing a comparison report.
- `cargo test -p deepseek-policy -p deepseek-browser --locked`: **529** policy lib +
  9 sidecar + 2 listener + 2 live engine (`the_engine_drives_a_real_browser_through_the_declared_actions`,
  `a_browser_that_cannot_be_started_is_reported_not_panicked`), all passed, against a
  real Chromium.
- `cargo test -p deepseek-gateway --test browser_engine_e2e` → **1 passed**,
  `the_gateway_reaches_a_real_browser_through_the_sidecar`.
- `cargo fmt --all --check` clean; clippy `--all-targets --all-features -D warnings`
  clean; `check_zero_python_runtime` PASS 8/8; `native_runtime_contract --check` `ok`
  (`proto_files: 9`); docs nav PASS (214) and links OK.
- The new image-audit rule was shown **able to fail**: deleting the `browser` stage
  yields `missing required stage 'browser'`.

### Not done

- **The image was written, not built**: no Docker on this machine, so
  `rust/Dockerfile`'s `browser` stage and the compose service are verified by CI
  (`rust-docker`, `native-browser-engine`), not locally.
- The container sandbox remains an operator decision: the image and the compose service
  deliberately do not set `DEEPSEEK_BROWSER_NO_SANDBOX`, and a container with neither
  user namespaces nor that opt-in fails closed to the static controller.
- Three of the four new probe pairs are not CI steps.
- The `/api` surfaces this file listed earlier (skills, traces, media; the Go-owned
  workspace backup/DR block) are still unported.
