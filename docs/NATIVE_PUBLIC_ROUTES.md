# `/api/title`, `/api/download`, `/api/chat`, `/api/file-source`, `/api/file-reader`, `/api/file-chunk`, `/api/file-page-text`, `/api/file-page-search`, `/api/file-page-image`, `/api/file-page-layout`, `/api/file-text` and `/api/project-files`

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported and wired on the native edge.** These routes were
`503 GO_CONTROL_PROXY_NOT_READY` (the Go `/api/*` catch-all with no Go control plane
configured). They are now registered ahead of that catch-all. `/api/file-text`
extracts text, HTML, DOCX, PPTX, XLSX, selectable PDF text, EPUB chapters, and
OCR for an image or a textless PDF when the upload asks for OCR. An image with
OCR off is `415 ocr_required`. A textless PDF with OCR off is `422 ocr_required`.
An OCR pass that recognizes nothing is `422 ocr_empty`. A missing engine is
`415 ocr_unavailable`. Those refusals do not write a cache entry.
`/api/project-files` writes `project.json` only when
`DEEPSEEK_RUNTIME_MODE=python_disabled`. Every other mode is
`409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED` and does not touch the file.

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
- `/api/file-page-search`, `/api/file-page-image` and `/api/file-page-layout`
  are registered on the native edge. Search is covered with the reader routes.
  Image and layout are covered below.

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
- `tasks/native-runtime/file_routes_parity_probe.py` — the filename, disposition and
  media-type corpus (26 names, 21 cached-file shapes, and the 179/180/181/400-character
  caps) against the imported oracle. The same probe now also carries the reader and
  page-text and page-search corpora; the current run is **345 cases** and is
  recorded under `/api/file-page-text` and `GET /api/file-page-search`.

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
- `tasks/native-runtime/file_routes_parity_probe.py` — the reader corpus (56 window
  shapes across seven cached indexes and 42 chunk lookups) against the imported
  oracle. The oracle's own `file_reader_window` runs unmodified; the probe repoints
  `rag_files.FILE_CACHE_DIR`, which is the name `files.py` reads (it imports the constant
  by value, so patching `config.FILE_CACHE_DIR` alone does nothing). The current run of
  this probe is **345 cases**, including the page-text and page-search corpora
  below.

## `/api/file-page-text`

One page of extracted text for the document reader's text pane.
`deepseek_infra/web/server.py` → `deepseek-gateway::file_reader_route::api_file_page_text`,
over `deepseek-policy::file_routes::file_page_text`.

| | |
|---|---|
| Entry | `POST /api/file-page-text` |
| Body | `{fileId, projectId?, page?}` |
| Answer | `{ok, file, page: {index, pageCount, text, hasText}}` |
| Default | `page or 1` |
| `400` | `Invalid page`, `Invalid file id`, `Invalid project id` |
| `410` | uploaded index missing (`file_index_expired`) |

The rules that are easy to get wrong, and are therefore pinned:

- **The page count is raised** to the highest `pageTexts` entry, then floored at 1. A
  request past that count clamps to the last page; it is not a 404.
- **A page with no extracted text falls back to an even character split of the chunk
  text.** For the measured fixture (`"chunk text"` across 5 pages) page 3 is `"k"`.
- **`int()` is not a float parse.** `"1.5"` and `"True"` are `400 Invalid page`.
  `"1_0"` is 10 and then clamped. `2.5` truncates toward zero. `True` is page 1.
- **`hasText` is `bool(page_text.strip())` on the uncapped text.** A file with no pages
  still answers one page whose `hasText` is false.
- **The route only reads.** A success and a refusal leave the cache bytes and mtimes
  unchanged, and an unknown id does not create a file.

### Verification

- `cargo test -p deepseek-gateway --test file_page_text_route` — 7 real-HTTP cases
  through `create_production_app`: the extracted page and the clamp, the chunk-split
  fallback, falsy defaults, `"x"` / `"1.5"` / `"True"` refused with the cache unchanged,
  an empty extraction, a bad id and a missing index, a project-scoped read that does not
  return the global file, and the auth boundary.
- `tasks/native-runtime/file_routes_parity_probe.py` — **345 cases**, **PASS**. The
  `page_texts` section is 8 cached indexes × 16 page values, plus a missing id and a
  malformed id, compared with the imported `file_page_text`. The same run includes
  `page_search` (below). Report: `artifacts/file-routes-parity.json`.

## `GET /api/file-page-search`

Keyword search across extracted pages. `deepseek_infra/web/routes/files.py` →
`deepseek-gateway::file_reader_route::api_file_page_search`, over
`deepseek-policy::file_routes::file_page_search`.

| | |
|---|---|
| Entry | `GET /api/file-page-search?fileId=…&projectId=…&query=…` |
| Answer | `{ok, file, query, pageCount, matches, truncated}` |
| `400` | blank query (`Search query is required`), bad file id, bad project id |
| `410` | uploaded index missing |

The match index is an index into `str.casefold` of the page, then applied to the
original text. `ß` folds to `ss`, so a query of `strasse` hits `Straße`. A query
longer than 200 characters is truncated before the search. `truncated` is true once
200 matches have been kept, which is the oracle's `>=` check. An empty `pageTexts`
list falls back to splitting the joined chunk text. The route only reads.

### Verification

- `cargo test -p deepseek-gateway --test file_page_text_route` includes
  `a_page_search_finds_a_match_and_a_blank_query_writes_nothing`: `page` hits both
  pages of the fixture, a blank query is `400` with the cache bytes and mtimes
  unchanged, and a missing token is `401`.
- The parity probe's `page_search` section (2 indexes × 7 queries, including
  `strasse` against `Straße`) is inside the **345** passing cases.

## `GET /api/file-page-image` and `GET /api/file-page-layout`

`/api/file-page-image` renders one PDF page and caches
`{fileId}.page-{n}-{scaleKey}.png` next to the source. The response is
`image/png` with `X-File-Page`, `X-File-Page-Count` and an inline
`Content-Disposition`. `/api/file-page-layout` returns the word boxes and does
not write a file.

The boxes for unembedded Helvetica match PyMuPDF. The PNG is produced by
`pdftoppm` at `round(scale * 72)` DPI, so its bytes are not MuPDF's pixmap and
its pixel size stays within a few pixels of `ceil(points * scale)`.

| | |
|---|---|
| Image entry | `GET /api/file-page-image?fileId=&page=&scale=` |
| Layout entry | `GET /api/file-page-layout?fileId=&page=` |
| Non-PDF | `415 unsupported_file`, no cache write |
| Bad page or scale | `400 invalid_payload`, no cache write |
| Missing token | `401`, no cache write |

### Verification

- `cargo test -p deepseek-gateway --test file_page_render_route` — one production
  case: the PNG is cached and a repeat read returns the same bytes; the layout
  text is `Hello pdf` / `World`; a text file and a bad page or scale leave the
  cache names unchanged.
- `tasks/native-runtime/file_page_render_probe.py` — **3 PDFs, PASS** against
  unmodified `render_pdf_page_layout` and `render_pdf_page_png`.

## `POST /api/file-text`

Multipart upload. `deepseek_infra/web/server.py` → `deepseek-gateway::file_text_route`,
over `deepseek-policy::file_upload` for text, HTML, DOCX, PPTX, XLSX, selectable PDF text, EPUB, and OCR. The extracted file is written
to `.file-cache/{fileId}.json` and `{fileId}.source`, which `/api/file-reader` and
`/api/file-source` then read back.

| | |
|---|---|
| Entry | `POST /api/file-text` (`multipart/form-data`, field `files`) |
| Answer | `{files, errors, file}` |
| Text / HTML / DOCX / PPTX / XLSX / selectable PDF / EPUB | extracted, chunked, cached |
| Image or textless PDF, OCR on | recognized and cached. Image text is the OCR text. Each textless PDF page is labeled `[PDF 第 N 页 (OCR)]` |
| Image, OCR off | `415 ocr_required`, no cache write |
| Textless PDF, OCR off | `422 ocr_required`, no cache write |
| OCR recognized nothing | `422 ocr_empty`, no cache write |
| OCR engine missing | `415 ocr_unavailable`, no cache write |
| Broken DOCX or PPTX | `422` invalid file or invalid XML, no cache write |
| Broken or unloadable XLSX | `422 Invalid xlsx file`, no cache write |
| Corrupt PDF | `422 Could not extract text from this PDF`, no cache write |
| Corrupt EPUB | `422 Invalid epub file`, no cache write |
| EPUB with no chapter text | `422 No readable text found in this file`, no cache write |
| Empty / binary | `400` empty file, `415 unsupported_file` |

### Verification

- `cargo test -p deepseek-gateway --test file_text_route` — 10 real-HTTP cases through
  `create_production_app`: a text upload, a DOCX upload, a PPTX upload, an XLSX
  upload, a two-page PDF, an EPUB, an OCR image and an OCR PDF come back from
  `/api/file-source` as the original bytes and from `/api/file-reader` as the
  extracted text. Empty, binary, an image with OCR off, a truncated PDF, a
  textless PDF with OCR off, a blank PDF with OCR on, a nav-only EPUB and corrupt
  DOCX/PPTX/XLSX/EPUB leave the cache empty. HTML drops the script. The route
  tests set `OCR_FORMULA_CMD` to `cmd /c exit 1` so pix2tex, whose text changes
  between processes, is not the winning engine.
- `tasks/native-runtime/file_text_parity_probe.py` — **31 cases, PASS** against
  unmodified `extract_uploaded_file`. The PPTX case keeps an empty middle slide in
  the page number (`第 1 页` then `第 3 页`) and strips each text run. The XLSX
  cases cover an openpyxl workbook (strings, numbers, bools, styled dates and an
  empty sheet), shared strings, a bad ZIP, broken content types, and a ZIP that
  has worksheet XML but no `[Content_Types].xml`. The PDF cases cover a two-page
  literal-text file, a PyMuPDF drawing, a textless page and a corrupt header.
  The EPUB case sorts `C.HTML` before `a.xhtml`, drops `nav.xhtml` and
  `toc.xhtml`, and drops script text. The two OCR cases are a 360×140 `HELLO`
  PNG and a one-page PDF of that image. The probe sets `OCR_FORMULA_CMD` to
  `cmd /c exit 1` before importing settings, for the same pix2tex reason. The
  probe does not call the host RAG index.

## `POST /api/project-files`

Multipart upload onto an existing project. `deepseek_infra/web/routes/workspace.py`
→ `deepseek-gateway::project_files_route`, over `deepseek-policy::projects::add_project_files`
and the same extractor as `/api/file-text`. The file cache is
`.projects/{projectId}/files/`. The document list is `project.json`.

| | |
|---|---|
| Entry | `POST /api/project-files?projectId=` (`multipart/form-data`, field `files`) |
| Answer | `{ok, documents}` |
| Python still owns the store | `409 NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED`, no write |
| `python_disabled`, legal file | cached under the project and recorded in `project.json` |
| Read back | `GET /api/workspace/projects/{projectId}` returns the document |
| Missing project | `404 Project not found`, no directory created |
| Unsupported file | `415 unsupported_file`, `project.json` unchanged |
| Missing token | `401` |

The route does not write `.local-rag`. Other project mutations stay
`501 NATIVE_PROJECTS_MUTATIONS_NOT_READY`.

### Verification

- `cargo test -p deepseek-gateway --test project_files_route` — 3 real-HTTP cases
  through `create_production_app`. The success case reads the document back from
  the workspace GET, `/api/file-reader`, and `/api/file-source`. The refusal case
  covers an empty runtime mode, `python_authoritative`, and `go_authoritative`,
  and compares `project.json` bytes.

## CI

`native-probe-parity` runs `title_parity_probe`, `file_routes_parity_probe` and
`chat_stream_events_parity_probe` against their own Rust sides and takes the exit code as
the verdict — each answers `return 0 if not problems else 1`.

The same lane runs `tasks/native-runtime/check_probe_pairs.py`, which drives the older
pairs: both sides are executed, the **parsed values** are compared (a `serde_json` map's key
order depends on whether `preserve_order` is in the build graph, which is not a behavioural
difference), byte equality is reported separately, and a disagreement exits non-zero.
After correcting memory's dependency isolation, the 2026-09-22 isolated rerun found
**38 pairs run, 37 identical, 10 of those byte-identical**, with no unexpected failures.
Both memory examples were rebuilt with `--locked`. Memory is now a strict gate;
the remaining known divergence is:

- `store` — the oracle reads the wall clock (`bm.today()`) while the Rust example pins
  `DAY="2026-09-18"`, so the pair only agreed on the day it was written; the raw row now
  reads `2026-09-22` against `2026-09-18`.

The memory difference was reproducible with a real SQLite index: Python returned four
hits against Rust's three and reordered the scoped context. The Python probe claimed
`local_rag` was absent but allowed its lazy imports once dependencies were installed;
the Rust probe explicitly passed `None`. The Python save path also replaced the host
workspace's memory index with probe records. Imports are now blocked only inside the
extracted no-index namespace, including the save path, without changing either runtime
or disabling RAG elsewhere in the process. Three regressions cover both differing states
and preservation of an existing host index. Rebuilt probe pairs pass **194 memory keys
and 64 live-index keys**, byte-identically in the local build; the live-index pair still
measures **7 of 8** queries changed by the vector bonus. See [MEMORY_STORE.md](MEMORY_STORE.md).

Three pairs are skipped with reasons the harness prints: `browser_engine` (needs a
Chromium; runs in `native-browser-engine`), `oracle` (needs the oracle's application
context), `local_clock` (takes an argument rather than printing a report).
