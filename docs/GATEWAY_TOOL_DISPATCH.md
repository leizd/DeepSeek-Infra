# Tool dispatch parity (executor seam, slice 1)

Status: **seam ported and byte-verified; one of 18 branches implemented; nothing
wired.**

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
| `browser_*` | `infra.browser.actions` |
| `python_eval` | a real sandbox — the oracle shells out to a Python interpreter, which the migrated runtime must not do |
| `search_files` | `infra.rag` |
| `fetch_url` | an HTTP client + the DNS-time SSRF guard |
| `web_search`, `compare_search_results` | the `web_search` callback |
| `suggest_memory`, `recall_memory`, `forget_memory` | `infra.data.memory` |
| `create_reminder`, `list_reminders` | `infra.data.reminders` |
| `list_project_files`, `read_file_chunk` | `infra.data.projects` |
| `data_transform` | its four `transform_*` helpers |
| `create_mindmap`, `create_pptx`, `create_document` | `infra.tool_runtime` media modules |

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
