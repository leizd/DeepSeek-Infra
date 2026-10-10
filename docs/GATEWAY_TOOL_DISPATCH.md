# Tool dispatch parity (executor seam, slice 1)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **seam ported and byte-verified; all 18 branches now run.**
`browser_*` is the safety gate + static HTML controller (see
[`BROWSER.md`](BROWSER.md)). This document is the seam's original record;
later branch slices have their own docs.

This is the first slice of layer 2 (tool execution). It ports the *seam* — the
envelope contract, the normalization the branches rely on, the branch inventory,
and the ordering — plus the one branch that is genuinely self-contained.

## Where this sits

The oracle funnels every model tool call through one function:

```
execute_tool_call(tool_call, *, policy, …)
  -> mcp__*            -> external MCP executor          (out of scope)
  -> parse arguments
  -> gate              -> ToolPolicy.evaluate -> denial_output on deny
  -> route             -> one of 17 branches, or the browser_* family
  -> envelope          -> {"ok": true, "tool", "result"} + sanitize_result
```

Slice 1 ports everything except the branch bodies.

## Scope

| Part | Status |
| --- | --- |
| `tool_call_name`, `parse_tool_arguments`, `safe_limit`, `is_parallel_safe_tool` | **ported** |
| `SERIAL_TOOL_NAMES` | **ported** |
| success envelope `{"ok": true, "tool", "result"}` + `sanitize_result` | **ported** |
| error envelopes (`AppError` arm, catch-all arm) | **ported** |
| `Unsupported tool:` fallback | **ported** |
| branch inventory + `branch_for` routing | **ported** |
| `generate_chart` (+ `chart_markdown_table`) | **ported** |
| the other 17 branches | **not ported** — see below |
| `execute_tool_calls` (the parallel batch wrapper) | **not ported** |
| `schema_for_tool` / `tool_parameter_schemas` | **not ported** (schema catalog) |

### Why only one branch

Every other branch needs a package that does not exist on the Rust side yet:

| Branch | Blocked on |
| --- | --- |
| `browser_*` | **ported** — see [`BROWSER.md`](BROWSER.md) |
| `python_eval` | **ported** — see [`PYTHON_EVAL.md`](PYTHON_EVAL.md) |
| `search_files` | **ported** — see [`SEARCH_FILES.md`](SEARCH_FILES.md) |
| `fetch_url` | **ported** — see [`FETCH_URL.md`](FETCH_URL.md) |
| `web_search`, `compare_search_results` | the `web_search` callback |
| `suggest_memory`, `recall_memory`, `forget_memory` | `infra.data.memory` |
| `create_reminder`, `list_reminders` | `infra.data.reminders` |
| `list_project_files`, `read_file_chunk` | `infra.data.projects` |
| `data_transform` | its four `transform_*` helpers |
| `create_mindmap` | **ported** — see [`MINDMAP.md`](MINDMAP.md) |
| `create_document` | **ported** — see [`CREATE_DOCUMENT.md`](CREATE_DOCUMENT.md) |
| `create_pptx` | **ported** — see [`CREATE_PPTX.md`](CREATE_PPTX.md) |

`generate_chart` needs none of them: it normalizes a chart type, filters up to 12
points, and renders a markdown table.

[`Branch::blocker`] carries this table in code, and a test asserts that every
branch is either ported or names a blocker — so a gap can never be silent.

## `Unported` has no envelope — the guard rail

```rust
pub enum DispatchOutcome {
    Executed(Value),     // success envelope, sanitized
    Denied(Value),       // denial_output
    Unsupported(Value),  // "Unsupported tool:" error envelope
    Unported { tool: String, branch: Branch },
}
```

`DispatchOutcome::to_output()` returns `None` for `Unported`. That is the point:
a caller that forwards `to_output()` **cannot** report success — or even a tidy
error — for a tool that was never implemented. There is no synthetic result to
misread, and nothing in the module is wired to a route.

## Two orderings that are load-bearing

### 1. The gate runs before the branch

A denial short-circuits with `ToolPolicy::denial_output`; the branch never runs.
The probe makes this observable by recording branch invocations — every denied
case reports `branches: []`.

### 2. Arguments are parsed before the gate

**This one was a real bug in my first draft.** I gated the raw `arguments` value.
The model sends arguments as a JSON *string*, and the guards inspect fields inside
it — so gating the raw string left every argument guard looking at an empty
object, and the SSRF and path checks silently passed.

The oracle parses first (`arguments = parse_tool_arguments(...)`) and there is now
a test that fails if the order is reversed:

```rust
dispatch("fetch_url", &json!("{\"url\": \"http://169.254.169.254/\"}"), …)
  // must be Denied with risk "critical", not routed
```

### A third one: the unknown-tool fallback is a no-policy path

With a policy attached, an unregistered name is denied as `unknown_tool` before
routing, so `Unsupported tool:` is only reachable without one. Reaching it *with*
a policy would mean the gate had been skipped.

This also caught a bug in the probe: the Rust example originally gated every case,
so the `no-policy` case denied where the oracle reached the fallback. The diff
exposed it.

## Method

- Python: `tasks/native-runtime/tool_dispatch_parity_probe.py`
- Rust: `rust/crates/deepseek-policy/examples/tool_dispatch_parity_probe.rs`

Extracted verbatim from `tools.py`: `tool_call_name`, `parse_tool_arguments`,
`safe_limit`, `is_parallel_safe_tool`, `generate_chart`, `chart_markdown_table`,
`execute_tool_call`, `SERIAL_TOOL_NAMES`. The real `ErrorCode` / `AppError` come
from executing the self-contained `core/errors.py`; the real `ToolPolicy` comes
from reusing the tool-policy probe's namespace.

Stubbed, and stated in the probe's docstring:

- **the 18 branch functions** (recorders) — what makes the short-circuit
  observable;
- **`_evaluate_rust_policy` returns `None`**, which is exactly what the real one
  does under the production default `DEEPSEEK_RUST_POLICY=false`;
- **`schema_for_tool` returns `None`** — schema-driven verdicts are covered by the
  tool-policy probe's `validate::*` keys rather than duplicated here.

Result: **49 keys, byte-identical**, normalized MD5
`9d491ef3f97c9f079ad9d7761a815ec4`.

## Behaviours the corpus pins

- `int("7.9")` raises in Python, so `safe_limit` falls back to its default —
  while `int(7.9)` truncates to 7. The two paths are handled separately.
- `data[:12]` is applied **before** the point filter, so the cap counts raw items,
  not usable points. My first test asserted 12 points for a 26-item input; the
  real answer is 7, and the oracle settled it.
- `python_float_str` reproduces `str(float)`: `1.0` not `1`, and a signed,
  zero-padded exponent outside `1e-4..1e16` (`1e+16`, `1e-05`). Rust's `Display`
  does neither, and the values are interpolated into the model-facing markdown.
- `str(title or "Chart")` and `str(label or "").strip()[:80]` — the truncation is
  by code point, not byte.

## Verification

- `cargo test -p deepseek-policy` → 102 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean.

## Explicit non-goals

- Executing any branch other than `generate_chart`.
- `execute_tool_calls` (parallel batching, cancellation).
- Wiring the dispatcher to a route, or to the chat round loop.
- The schema catalog.

## Rollback

Additive and inert: a new module with no production caller. Reverting the commit
restores the previous tree.

---

# Slice 2: the batch layer, `data_transform`, and a shared Python-JSON module

Two of the remaining 18 branches had no external package behind them, so they
could be ported in this slice; the other 15 still cannot (browser engine, RAG,
data layer, media/doc generation, an HTTP client, and a real sandbox for
`python_eval`).

## `data_transform` — a complete, pure branch

`tool_transform.rs` mirrors `data_transform` and its four operations
(`extract_regex`, `json_path`, `csv_summary`, `number_summary`) plus the helpers
(`read_simple_json_path`, `compact_json_value`, `number_summary_payload`, and a
hand-rolled `csv_read` for Python's default CSV dialect).

Two substitutions worth recording:

- Python's JSON-path splitter uses a **lookahead** (`re.split(r"\.(?![^\[]*\])", …)`),
  which the `regex` crate does not support. It is replaced by a plain split on `.`,
  which is equivalent for every path this function can accept — a well-formed part
  is `key[index]` with a digits-only index, so no dot can appear inside brackets.
  Paths where the two disagree are exactly the paths that fail the per-part
  fullmatch and raise "Unsupported JSON path" either way.
- `statistics.fmean` / `statistics.median` are reproduced: mean as `sum/count`,
  median as the middle value (or the mean of the two middles).

## `execute_tool_calls` — the batch layer

`tool_batch.rs` ports the orchestration: selection capped at
`MAX_TOOL_CALLS_PER_RESPONSE`, the serial/parallel batching, cancellation at the
four points the oracle polls it, the None → cancelled / None → "did not run"
assembly, and the `role: "tool"` message including the compact-JSON content
truncated to `MAX_TOOL_RESULT_CHARS`.

The batching plan is exposed as data (`plan_batches` → `Vec<BatchStep>`) so the
serial/parallel split is assertable without execution. Concurrency is **not**
reproduced: the oracle runs a parallel group on a thread pool, but every result is
written back by its original index, so running a group sequentially produces the
same list. What *is* reproduced is the cancellation post-condition — a group
interrupted part-way leaves its remaining slots empty, which the assembly turns
into cancelled envelopes rather than "did not run".

`stable_tool_output_for_model` and `strip_volatile_tool_fields` are ported in full.
The artifact-compaction path (`compact_artifact_tool_output`) for `create_pptx` /
`create_document` / `create_mindmap` is **still deferred**: those branches now
run, but a test pins that their output currently passes through unchanged so
the compaction gap stays visible rather than silent.

## A shared Python-JSON module

`python_json.rs` now owns `dumps_default_separators`, `dumps_compact`, `float_str`
and `value_str` — the rendering rules that `tool_rounds` (gateway), `tool_policy`
and `tool_dispatch` each had a private copy of. `tool_policy::normalized_args_hash`
and `tool_dispatch::python_float_str` now delegate to it, removing two duplicates.
(The gateway's `tool_rounds` copy is noted as a follow-up consolidation; touching
it is out of this slice's crate boundary.)

## One parity rule for engine-specific diagnostics

`Invalid JSON: …` and `Invalid regex: …` embed the *engine's* own error text
(CPython's, or serde_json's/regex's on this side). The prefix is the oracle's own
message and is identical; the suffix is not. The probe masks the suffix on both
sides, the same way the audit `ts` is masked — the divergence is on record in this
doc and in a unit test, not hidden behind a green diff.

## Evidence

The probe now covers 80 keys across the helpers, both ported branches, the batch
layer, and `strip_volatile_tool_fields`:

- Byte-level parity: **identical MD5 `3f088f27bcf1dda772cf3fb18d318cf5`**, 80 keys,
  no differences.
- `cargo test -p deepseek-policy` → 131 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  → clean; `cargo fmt` applied.

## Honest state of the remaining 15 branches

`Branch::blocker()` still names each one's package, and a test asserts none is
silent. They are blocked on real subsystems:

| Branch | Blocker |
| --- | --- |
| `browser_*` | **ported** — see [`BROWSER.md`](BROWSER.md) |
| `python_eval` | **ported** — see [`PYTHON_EVAL.md`](PYTHON_EVAL.md) |
| `search_files` | **ported** — see [`SEARCH_FILES.md`](SEARCH_FILES.md) |
| `fetch_url` | **ported** — see [`FETCH_URL.md`](FETCH_URL.md) |
| `web_search`, `compare_search_results` | the `web_search` callback |
| `suggest_memory`, `recall_memory`, `forget_memory` | `infra.data.memory` |
| `create_reminder`, `list_reminders` | `infra.data.reminders` |
| `list_project_files`, `read_file_chunk` | `infra.data.projects` |
| `create_mindmap` | **ported** — see [`MINDMAP.md`](MINDMAP.md) |
| `create_document` | **ported** — see [`CREATE_DOCUMENT.md`](CREATE_DOCUMENT.md) |
| `create_pptx` | **ported** — see [`CREATE_PPTX.md`](CREATE_PPTX.md) |

Wiring the round loop (and deleting `ToolRoundsUnwired`) stays blocked on these —
wiring it now would replace the oracle's terminating tool loop with a permanently
failing one that still answers `200`.

---

# Slice 3: the search family (`web_search`, `compare_search_results`)

The next-smallest dependency after `data_transform`: neither branch needs a
package. The oracle injects a `web_search_callback` per request (the gateway owns
the actual search provider), so what is left is argument handling, query cleaning,
cross-round de-duplication and the result cap — all pure given the callback.

`tool_search.rs` ports:

- `web_search` and `compare_search_results_branch` — the two branch bodies,
  including their distinct "not enabled for this request" errors;
- `compare_search_results` — up to **two** cleaned queries (whitespace collapsed,
  de-duplicated, 500-character cap), one round each, results de-duplicated across
  rounds and capped at 20;
- `search_result_key` — the de-duplication key.

`ExecutorContext` carries the optional callback, mirroring the keyword arguments
`execute_tool_call` takes. `dispatch` now threads it through, which is the one
signature change in this slice; the test module and the probe example supply a
default context, and the search branches then take their "not enabled" path — a
path the probe compares directly.

## `search_result_key` is a different projection on purpose

It is `urlsplit`/`urlunsplit` based, which is **not** the same projection as
`tool_policy`'s SSRF host extraction: the guard wants a hostname to classify
against the IP tables, while this wants the raw netloc (lowercased, port and
userinfo included) so two results differing only in case or in a fragment collapse
to one key. They are kept separate rather than forced through one parser.

Two measured behaviours, both of which corrected a wrong guess of mine:

- `urlsplit` strips **leading** C0 controls and spaces but never trailing ones, so
  `"  HTTP://X  "` keys to `"http://x  /"`.
- An **empty** URL is not an empty key. `urlsplit("")` normalises to the path `/`,
  so the key is `"/"` — which is why an empty-URL result is kept, not skipped.
  Only a non-object entry is dropped.

That is the sixth time on this project that measuring beat reasoning; the unit
tests now carry the measured values with a note saying so.

## Evidence

- Byte-level parity: **identical MD5 `a6aa9b0ed707966a641940b47fdade55`**, 104 keys,
  no differences.
- `cargo test -p deepseek-policy` → 141 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  → clean; `cargo fmt` applied.

## Branch status after this slice

**Four of eighteen branches are ported**: `generate_chart`, `data_transform`,
`web_search`, `compare_search_results`. The remaining fourteen are still marked
`false` by `Branch::is_ported()` with their blocker named; nothing is wired, and
wiring the round loop stays blocked on them.

---

# Slice 7: the data layer, and slice 8: the gateway wiring

(Slices 4–7 ported the mutation gate, the reminders store, the retrieval scorer,
the memory store, the projects read path, the file cache and the projects branch
wrappers, then wired the seven data branches into `dispatch` via the injected
`WorkspaceContext` — recorded in `tasks/native-runtime/continuation.md`. This
section covers the slice that followed: giving `dispatch()` a production caller.)

## The wiring

`rust/crates/deepseek-gateway/src/chat_tool_loop.rs` is the non-streaming tool
round loop — the core of `call_deepseek`'s loop, in the oracle's order:
`exchange_turn` → `merge_usage_totals` → lenient `tool_calls` normalization →
`decide_round` (Finish / Continue / ForceFinalAnswer) → `execute_tool_calls`
with `dispatch` as the runner → `append_tool_exchange` → next round;
`force_final_answer_without_tools` once the round budget is spent.

`ToolRoundExecutor` is the per-request bundle the runner needs:

- **`WorkspaceBundle`** (root, `FileCache`, `SystemEntropy`, `SystemClock`) —
  the oracle's module globals (`config.ROOT`, the `lru_cache`, `secrets`,
  `utc_now_iso`) as one injectable object. The root comes from
  `DEEPSEEK_INFRA_ROOT`; unset means no workspace, and the data branches then
  answer *"not enabled for this request"* — the same explicit error as
  `web_search` without its callback, never a silent no-op.
- **the policy** — the oracle's `build_tool_policy` main-chat profile:
  `ToolPolicyConfig::default()` (capability `full`, `enforce_schema` /
  `require_confirm` off, `sanitize` on) plus the process's own
  `DEEPSEEK_API_KEY` / `AUTH_TOKEN` as the secret-exfiltration blocklist,
  gated by `TOOL_POLICY_ENABLED` (default on, `_env_bool` semantics). One
  policy object lives across the request's rounds, so its counters accumulate
  like the oracle's single `tool_policy` does.
- the runner extracts name + raw `arguments` per call, locks the shared policy
  (`PoisonError::into_inner` — poisoning is a failure mode the oracle's
  `RLock` does not have), and calls `dispatch` with `schema: None` (the schema
  catalog is not ported; with `enforce_schema` off the absence changes no
  verdict).

The route change that makes the loop *reachable*: `/v1/chat/completions` now
prepares the raw body directly through `prepare_chat_request` instead of
re-encoding through a typed struct first. The typed struct silently dropped every
field it did not enumerate — including `tools`, which meant the model could never
have called anything. Both entry points (`/v1/chat/completions` and
`/gateway/request/prepare`) now share the one validator.

## What runs, honestly

All eighteen branches execute for real. `browser_*` is refused by safety
(private host / confirmation) or served by the static HTML controller rather
than answering `Tool did not run`. Also absent, each with an owner:
the web-search provider (branches answer "not enabled"), `mcp__*` bridging
("Unsupported tool:"), the artifact terminal check, and the loop's surrounding
machinery (semantic cache, memory retrieval, scheduler, traces, budget ledger).

## Verification

- `cargo test -p deepseek-gateway -j 1` → **129 lib + 7 `chat_execution`
  boundary + 6 `chat_stream` + 2 control-boundary tests**, all pass. The
  boundary tests drive the route against a scripted loopback upstream:
  - `chat_route_runs_tool_rounds_through_dispatch` — the follow-up request
    carries the replayed assistant turn (content + `reasoning_content` +
    `tool_calls`) and the `role: "tool"` result; usage merges across rounds;
    `tools` survived preparation;
  - `chat_route_runs_a_data_branch_against_the_workspace` — `create_reminder`
    writes through the mutation fence into `DEEPSEEK_INFRA_ROOT`
    (`.reminders/reminders.json` + `.workspace-generation` both land there);
  - `chat_route_exhausts_the_round_budget_and_forces_a_final_answer` —
    exactly `MAX_TOOL_ROUNDS + 2` upstream turns; the last request carries the
    budget prompt and `tool_choice: "none"` while keeping the `tools` prefix;
  - `chat_route_blocks_a_private_browser_url` — `browser_open_url` of
    `http://127.0.0.1/admin` is `forbidden` (see [`BROWSER.md`](BROWSER.md)).
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → 223 pass.
- `cargo clippy -p deepseek-gateway -p deepseek-policy --all-targets -- -D
  warnings` → only the pre-existing `control_proxy.rs:20`
  `result_large_err` (file byte-identical to HEAD; local rustc 1.97.1 vs the
  declared 1.85). One same-class local-toolchain lint
  (`unnecessary_sort_by`, `python_json.rs`) was fixed mechanically — the two
  sort forms are identical.
- `cargo fmt --all -- --check` clean.

## Known divergences kept on purpose

- An answer with empty content is refused (`NATIVE_CHAT_NO_ANSWER`, 502) where
  the oracle returns `""` with 200 — a divergence this facade has always had;
  now it also covers the budget-exhausted partial turn.
- The workspace root is env-injected; the oracle's is `config.ROOT`.
- `memorySuggestions` / `search` / `diagnostics` are not carried — the OpenAI
  envelope has no channel for them; `on_memory_suggestion` is `None`, so a
  suggestion is still built and returned as the tool result, just not notified.
- The assistant replay always writes `content: ""` where the oracle writes
  `null` when the model's turn carried an explicit `null` content.

## Explicit non-goals

- The **streaming** tool loop (SSE round continuation, `system_note`
  interleaving) — `chat_stream` still refuses tool-call chunks in-band with
  `STREAM_TOOL_ROUNDS_NOT_READY`.
- `schema_for_tool` (the schema catalog).
- The web-search provider, `mcp__*` bridging, artifact terminal handling.

## Rollback

Reverting the commit restores the fail-closed refusal; the deepseek-policy
modules it calls are unchanged by this slice (one mechanical lint fix excepted).
