# `/api/title`, `/api/download`, `/api/chat`, `/api/file-source`, `/api/file-reader` and `/api/file-chunk`

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported and wired on the native edge.** All six were
`503 GO_CONTROL_PROXY_NOT_READY` (the Go `/api/*` catch-all with no Go control plane
configured); all six are now registered ahead of that catch-all and serve for real.

## The web layer's `truthy` is not Python truthiness

`deepseek_infra/web/http_utils.py:truthy` is a **string parse**:
`str(value or "").strip().lower() in {"1", "true", "yes", "on"}`. So `"false"`, `"0"`,
`"no"` and `"2"` are all **false**, where Python truthiness would call every non-empty
string true. The port lives at `deepseek_policy::core_utils::web_truthy`.

This was a real defect: `/api/download` shipped using `python_truthy`, so
`?inline=false` rendered the SVG in place where the oracle downloads it — and the test
asserted that wrong answer. Both were corrected against the oracle. The same rule
governs `/api/file-source`'s `download` parameter.

## `/api/title`

`deepseek_infra/infra/gateway/title_generator.py` → `deepseek-policy::title` (the pure
half) + `deepseek-gateway::title_route` (the transport and the envelopes).

## What the route does

| | |
|---|---|
| Entry | `POST /api/title` |
| Body | `{userMessage, assistantMessage?, titleModel?, apiKey?}` |
| Answer | `{"title": "..."}` — the sanitised title, possibly `""` |
| Upstream | the configured DeepSeek chat-completions URL, non-streaming, `max_tokens: 60` |
| Credential | `apiKey` from the body, else the server's `DEEPSEEK_API_KEY`; neither → `400 missing_api_key` |
| Rate window | 12 admitted calls per API-key digest per 60 s, then `429 rate_limited` |

## The measured behaviours worth knowing

- **A blank `userMessage` makes no upstream call.** The oracle returns `{"title": ""}`
  before it rate-limits or dials out, so the native route does too — the test asserts
  the scripted upstream received **zero** requests.
- **The upstream timeout is `min(DEEPSEEK_TIMEOUT_SECONDS, 20)`.** A title is
  decoration; it must not hold a turn open for the configured 180 s.
- **An upstream error status is capped at 502** (`min(exc.code, 502)`) and carries the
  provider's own message, extracted by `format_upstream_error`. A body that is not JSON
  becomes the raw text, truncated to 500 characters.
- **`content: null` is an empty title, not the string `"None"`.** The oracle writes
  `str(message.get("content") or "")`, so the `or ""` runs *before* `str`. The parity
  probe caught exactly this on its first run.
- **The rate window is per process**, because the oracle's `_TITLE_RATE_LIMITS` is a
  module-level dict too. A multi-worker deployment therefore has one window per worker
  on both sides; reproducing that rather than "improving" it keeps the two equal.
- **`DEFAULT_UPSTREAM_TIMEOUT_SECONDS` was corrected from 120 to 180.** The oracle's
  default is `180`; the title route's own 20-second cap had hidden the difference.
- **Sanitisation order is the oracle's**: strip wrapping characters, drop one leading
  label (`标题：`/`标题:`/`Title:`/`title:`), collapse whitespace, peel trailing
  punctuation, *then* cut to 24 characters. Peeling before cutting is why a 30-character
  title ending in `。` returns 24 characters rather than 23.

## Verification

- `tasks/native-runtime/title_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/title_parity_probe.rs` — **PASS** over seven
  sections compared against the imported oracle:
  the system prompt, 26 sanitiser cases, 9 truncations, 6 request bodies, 8
  `titleModel` selections, 7 upstream responses, 7 upstream-error bodies.

  ```text
  cargo build -p deepseek-policy --example title_parity_probe
  python tasks/native-runtime/title_parity_probe.py \
      --rust-example rust/target/debug/examples/title_parity_probe.exe
  ```

- `cargo test -p deepseek-gateway --test title_route` — 7 real-HTTP cases through
  `create_production_app` against a scripted loopback upstream that records the bytes it
  received: the oracle's body and headers, the blank-message early return, the
  missing-key `400`, the `503`→`502` cap, the 13th-call `429`, and the auth boundary.

## Remaining gaps

- `/api/chat` (NDJSON, agent mode and cascade) is still the proxy's 503, so the
  frontend's streaming entry is not native yet. `/api/title` was the smaller,
  self-contained half of `routes/chat.py`.
- The native title route does not consult `preflight_chat_payload`; the oracle's title
  path does not either.

## `/api/download`

The `downloadUrl` (`/api/download?id={fileId}`) that `create_document`,
`create_pptx` and `create_mindmap` hand back. `deepseek_infra/web/routes/downloads.py` →
`deepseek-gateway::download_route`, over the already-ported
`deepseek-policy::generated_files`.

| | |
|---|---|
| Entry | `GET /api/download?id=<32 hex>&inline=<anything>` |
| Answer | the file's bytes, with `Content-Disposition` and `Cache-Control: no-store` |
| Missing / malformed / expired id | `404 {"error": "File does not exist or has expired", "code": "not_found"}` |

**The id rule is the security boundary, and it lives in the policy crate.**
`resolve_generated_file` accepts only `[0-9a-f]{32}` and probes the five registered
extensions, so the id never becomes a path component unchecked — `../secret` cannot be
expressed as a valid id at all. The route test writes a file *outside* `.generated/` and
asserts its bytes never appear in any response.

**The disposition is the oracle's, and one case is a Python trap.** `inline` is used only
for an `.svg` **and** a truthy `inline` parameter. Python truthiness means `inline=false`
is *truthy* — a non-empty string — so it renders inline, while `inline=` (empty) is
falsy and downloads. The test pins all four combinations.

| extension | media type | attachment name |
|---|---|---|
| `pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | `presentation.pptx` |
| `docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `document.docx` |
| `pdf` | `application/pdf` | `document.pdf` |
| `md` | `text/markdown; charset=utf-8` | `notes.md` |
| `svg` | `image/svg+xml` | `mindmap.svg` |
| anything else | `application/octet-stream` | `document.<ext>` |

### Verification

- `cargo test -p deepseek-gateway --test download_route` — 6 real-HTTP cases through
  `create_production_app`: the bytes on the wire equal the bytes on disk for every
  registered type, the four `inline` combinations, a traversal attempt leaking nothing,
  the unknown-id envelope, and the auth boundary.
- `cargo test -p deepseek-policy generated_files` — the six MIME/name pairs and the
  lower-cased extension.
- The page-render family (`/api/file-page-image`, `/api/file-page-layout`,
  `/api/file-page-search`) is still the proxy's 503: it needs a PDF/image renderer,
  which is not ported.

## `/api/file-source`

The original uploaded bytes, which the file viewer asks for when the user opens the
upload rather than the extracted text. `deepseek_infra/web/routes/files.py:29` →
`deepseek-gateway::file_source_route`, over the four helpers in
`deepseek-policy::file_routes`.

| | |
|---|---|
| Entry | `GET /api/file-source?fileId=<32 hex>&projectId=<id>&download=<v>` |
| Answer | the original bytes, with `X-Content-Type-Options: nosniff`, `Content-Disposition`, `Cache-Control: no-store` and the media type from the cached index |
| Missing source | `410 {"error": "Original uploaded file has expired or is missing", "code": "file_index_expired"}` — the index exists and the bytes are gone, which is a different repair than an unknown id |
| Bad id | `400 invalid_payload` |

`download` uses the web layer's string parse: `1`, `true`, `yes` and `on` are an
attachment, **everything else** — including `false` and `0` — is inline.

The media type follows the oracle's ladder, in order: a `pdf` kind or
`application/pdf` wins outright; an `image` kind keeps its own `image/*` type unless it
is `image/svg+xml`; a `text/*` that is not `text/html` gets `; charset=utf-8`; a
recognised text-ish `kind` (`txt`, `text`, `md`, `csv`, `json`, `xml`, `log`, `py`,
`js`, `ts`, `css`) becomes `text/plain; charset=utf-8`; the three OOXML types pass
through; anything else is `application/octet-stream`.

The disposition header is the RFC 5987 two-part form, with the ASCII fallback built by
**dropping** non-ASCII characters (`errors="ignore"`). A name like `报告.pdf` therefore
sends `filename=".pdf"` — the extension survives — and only a name with no ASCII at all
falls back to the literal `document`. The percent-encoded part uses Python's `quote`
safe set, which keeps `/`; the name is cleaned first, so a path never reaches either
form.

### Verification

- `cargo test -p deepseek-gateway --test file_source_route` — 6 real-HTTP cases through
  `create_production_app`: the original bytes rather than the index JSON, the `download`
  string parse (six falsy and four truthy spellings), the media-type ladder, `410` for a
  missing source and `400` for a bad id, a project-scoped read from
  `.projects/{id}/files`, and the auth boundary.
- `tasks/native-runtime/file_routes_parity_probe.py` — **103 cases** against the
  imported oracle: 26 filenames through `clean_filename` and both dispositions, 21
  cached-file shapes through `original_file_media_type`, and the 179/180/181/400-character
  caps.

## `/api/chat`

The frontend's streaming entry: `application/x-ndjson`, one compact JSON object per
line. `deepseek_infra/web/routes/chat.py:47` + `web/server.py:chat_event_stream` →
`deepseek-gateway::chat_ndjson`, over `deepseek-policy::chat_stream_events` (the event
protocol) and the same `ToolRoundExecutor` the OpenAI route uses.

### What it serves

The ordinary streaming turn, including the tool-round loop and memory suggestions:

1. the payload is the **internal** shape (not OpenAI), so `localBaseUrl` is injected and
   the payload is validated directly;
2. the upstream is opened with `stream: true`, and each SSE delta becomes an NDJSON
   line **as it arrives** — `reasoning` and `content`;
3. a round's tool calls are merged with the ported `merge_stream_tool_call_deltas`, the
   tools run through `ToolRoundExecutor`, a `memory_suggestion` event is emitted for
   each suggestion the round built, and the loop continues with a fresh upstream
   request;
4. the terminal `done` event repeats the accumulated `content`, `reasoning`, `usage`,
   `finishReason` and `memorySuggestions`;
5. a `finish_reason` of `length` emits the truncation `system_note` before `done`.

### What it refuses, and why refusing is the honest answer

Three branches of the oracle's `stream_deepseek` have no native producer yet. They are
refused with `501 NATIVE_CHAT_BRANCH_NOT_READY` **before any upstream call**, rather
than served by a thinner path that would look like success:

| branch | predicate | why |
|---|---|---|
| agent mode | `payload.agentMode is true` | `stream_multi_agent` is a separate producer with its own event vocabulary (`agent`, `agent_delta`, `run_status`, `agent_plan`, …) |
| cascade | `model_router_cascade_requested(payload)` | draft → gate → refine is non-streaming and replayed as events; the native cascade path is not wired |
| forced search | `forced_search_mode(payload)` | search prefetch is not ported |

The forced-search refusal is **reachable here even though it is not on
`/v1/chat/completions`**: the OpenAI facade never forwards `searchMode`, but `/api/chat`
takes the internal payload, so `searchMode` arrives intact and the oracle's prefetch
branch would run. Silently skipping the prefetch would answer a different question than
the user asked.

The `done` event's `diagnostics` is the oracle's own helper chain over the state this
route has: `diagnostics_with_tools` (`toolCallCount`, `toolNames` sorted and
deduplicated), `diagnostics_with_search` (the round and result counts, **absent** when
there was no search, because `if search_data:` is a truthiness test) and
`diagnostics_with_usage` (`cacheHitTokens`, `cacheMissTokens`, `cacheHitRate`). The
gateway-attempt, semantic-cache, cost and trace blocks are not folded in yet, because
their state (the retry loop, the cache, the budget ledger, the trace store) is not in
this route.

`cacheHitRate` is `round((hit / total) * 100, 1)`, and that rounding is Python's:
half-to-even on the **decimal** value. The direct Rust translation —
`(value * 10).round_ties_even() / 10.0` — is wrong for it, because `1.05 * 10` is
exactly `10.5` and rounds to `1.0` where Python returns `1.1`. `format!("{value:.1}")`
is the same algorithm Python uses, and the parity probe checks it over a tie corpus.

### Verification

- `cargo test -p deepseek-gateway --test chat_ndjson_route` — 7 real-HTTP cases through
  `create_production_app` against a scripted SSE upstream: the oracle's event sequence
  and order, the accumulated `done`, the `length` note, agent mode and forced search
  refused with **zero** upstream calls, the no-user-turn `400`, an upstream failure as
  an HTTP error rather than a `200` stream, and the auth boundary. Every line is also
  checked to be compact JSON.
- `tasks/native-runtime/chat_stream_events_parity_probe.py` — the event bytes, the
  accumulator and the tool-call merge against the imported oracle (see the migration
  matrix for the counts).

## `/api/file-reader` and `/api/file-chunk`

The paginated reader the file viewer scrolls a long extraction with, and the
single-chunk jump. `deepseek_infra/web/server.py` → `deepseek-gateway::file_reader_route`,
over `deepseek-policy::file_routes::{file_reader_window, file_chunk}`.

| | `/api/file-reader` | `/api/file-chunk` |
|---|---|---|
| Body | `{fileId, projectId?, chunkStart?, chunkCount?}` | `{fileId, projectId?, chunkIndex?}` |
| Answer | `{ok, file, window, chunks}` | `{file, chunk}` |
| Defaults | `chunkStart or 1`, `chunkCount or 6` (capped at 12) | `chunkIndex or 0` |
| `400` | `Invalid reader start` / `Invalid reader count` | `Invalid chunk index` |
| `404` | — | `Chunk not found` |

The rules that are easy to get wrong, and are therefore pinned:

- **Display indices are 1-based** in the request and in the answer, while the cached
  chunk's own `index` field is 0-based and is echoed as `index + 1`.
- **A start past the end clamps to the last chunk**, not to an empty window, so the
  reader's "next page" button can overshoot without losing the user's place.
- **An empty chunk list is its own shape**: `chunkStart`, `chunkEnd`, `chunkCount` and
  `totalChunks` are all `0` and `chunks` is `[]`.
- **A non-object entry is skipped by both the payload list and the end index**, which is
  what the oracle's comprehension does — so `chunkEnd - chunkStart + 1` can be less than
  `chunkCount` on a malformed index.
- **`chunkCount` in the file payload falls back to the window's own total**, not to zero,
  when the index does not carry one.

### Verification

- `cargo test -p deepseek-gateway --test file_reader_route` — 6 real-HTTP cases through
  `create_production_app`: the default 1/6 window, a later window's
  `hasPrevious`/`hasNext`, the falsy-value defaults with a per-field `400`, the 12-chunk
  cap, the empty-list shape, the 1-based single chunk with its `404` and `400`, an
  unknown id's `410`, and the auth boundary.
- `tasks/native-runtime/file_routes_parity_probe.py` — **201 cases** against the
  imported oracle, including 56 window shapes across seven cached indexes and 42 chunk
  lookups. The oracle's own `file_reader_window` runs unmodified; the probe repoints
  `rag_files.FILE_CACHE_DIR`, which is the name `files.py` reads (it imports the constant
  by value, so patching `config.FILE_CACHE_DIR` alone does nothing).

## CI

`native-probe-parity` runs `title_parity_probe`, `file_routes_parity_probe` and
`chat_stream_events_parity_probe` against their own Rust sides and takes the exit code as
the verdict — each answers `return 0 if not problems else 1`.

The same lane runs `tasks/native-runtime/check_probe_pairs.py`, which drives the older
pairs: both sides are executed, the **parsed values** are compared (a `serde_json` map's key
order depends on whether `preserve_order` is in the build graph, which is not a behavioural
difference), byte equality is reported separately, and a disagreement exits non-zero.
Measured 2026-09-22: **38 pairs run, 36 identical, 9 of those byte-identical**, with two
known divergences printed on every run:

- `store` — the oracle reads the wall clock (`bm.today()`) while the Rust example pins
  `DAY="2026-09-18"`, so the pair only agreed on the day it was written; the raw row now
  reads `2026-09-22` against `2026-09-18`.
- `memory` — a real divergence, not a key order: the keys match 194/194, but
  `state::remember` reports `hitCount` 4 against 3 and omits `[fact] the sky is blue`, and
  `state::scoped` orders the context list differently.

Three pairs are skipped with reasons the harness prints: `browser_engine` (needs a
Chromium; runs in `native-browser-engine`), `oracle` (needs the oracle's application
context), `local_clock` (takes an argument rather than printing a report).
