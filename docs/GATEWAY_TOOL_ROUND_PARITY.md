# Gateway tool-round parity (layer 1)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **layer 1 implemented and byte-verified; the non-streaming route now runs
the tool loop (`chat_tool_loop`, see `GATEWAY_TOOL_DISPATCH.md`); streaming stays
fail-closed.**

This document records how the Rust gateway's tool-round *bookkeeping* is proven
equivalent to the Python oracle, what is deliberately **not** implemented yet,
and why the route refused tool-calling turns while the executor did not exist.

## Scope

The oracle's tool loop decomposes into three layers. Only the first is
implemented in Rust.

| Layer | Responsibility | Oracle source | Rust status |
| --- | --- | --- | --- |
| 1. round control | accumulate streamed `tool_calls` deltas, finalize them, decide whether another round is allowed, assemble the assistant + tool-result messages | `deepseek_client.py` | **implemented** (`rust/crates/deepseek-gateway/src/tool_rounds.rs`), **wired into the non-streaming loop** (`chat_tool_loop.rs`) |
| 2. tool execution | the 17 executable branches plus `browser_*` | `tool_runtime/tools.py` | **11 of 18 branches ported and wired** (`deepseek-policy::tool_dispatch`); see `GATEWAY_TOOL_DISPATCH.md` |
| 3. policy and sandbox | `ToolPolicy.evaluate` / `sanitize_result`, Rust-sidecar policy path | `tool_runtime/policy.py` | **policy engine ported** (`deepseek-policy::tool_policy`) and attached by the loop's executor |

Layers 2 and 3 are thousands of lines and depend on the `rag`, `data`,
`browser`, and `media` packages. They are the reported blockers for enabling
tool rounds on a live route.

## Why the route refused tool rounds, and what changed

`/v1/chat/completions` used to answer a turn containing `tool_calls` with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` (both streaming and non-streaming).

Wiring layer 1 alone would have **manufactured a silent behavior change**: with
no executor, the only available tool result is synthetic. The model would receive a
fabricated "tool unavailable" payload, keep generating, and the oracle's
terminating tool loop would become a permanently-failing loop that still answers
`200`. Fail-closed was the only honest option until layers 2–3 landed.

Making the refusal *precise* was the point of that slice: the accumulation,
finalization, and budget rules are pinned by tests, so enabling them later is
a wiring change rather than a rewrite.

**The wiring slice has now landed (2026-09-16).** The non-streaming path runs
the loop through `chat_tool_loop::execute_chat_with_tool_rounds`; the
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` refusal is deleted from the non-streaming
path and remains only as the streaming refusal (`STREAM_TOOL_ROUNDS_NOT_READY`
in `chat_stream.rs`), where continuing a round mid-stream — interleaved with the
SSE emission the oracle interleaves it with — is still its own seam.

## Method

Parity is measured by replaying **identical fixed scripts** through the real
oracle and through the Rust implementation, then diffing canonical JSON.

- Python side: `tasks/native-runtime/tool_round_parity_probe.py`
- Rust side: `rust/crates/deepseek-gateway/examples/tool_round_parity_probe.rs`

The Python probe extracts the oracle functions **verbatim** with
`ast.get_source_segment` and `exec`s them, rather than reimplementing them.
Only two genuine boundaries are stubbed, and each is stated in the probe:

- `execute_tool_calls` — layer 2. The stub returns the synthetic results the
  driver supplies, so the same results reach both sides and the *assembly* code
  under test is the real one.
- `raise_if_cancelled` — cancellation is transport, not assembly.

`append_tool_exchange`, `merge_stream_tool_call_deltas`,
`finalized_stream_tool_calls`, `normalize_tool_calls`, `tool_names`, and
`force_final_answer_without_tools` are all the oracle's own code.

Reproduce:

```sh
python tasks/native-runtime/tool_round_parity_probe.py > python.json
cd rust && cargo run -p deepseek-gateway --example tool_round_parity_probe > ../rust.json
diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)
```

`tr -d '\r'` is required: Python's text-mode stdout emits CRLF on Windows and the
diff otherwise reports every line as different.

## Oracle rules that are load-bearing

These are the behaviors most easily got wrong, and each is covered by a named
probe key and a unit test.

### An index-less delta is placed at the *slot count*

`merge_stream_tool_call_deltas` uses `index = len(accumulator)` when no `index`
is present — the **count of existing slots**, not the highest index plus one.
After slot `5` exists, an index-less call receives slot `2` and therefore sorts
*before* slot `5`.

Observed oracle output for `first` / `index:5 five` / `third`:
`["first", "third", "five"]`.

An early unit test asserted `["first", "five", "third"]`; the probe disproved it
and the test was corrected. This is the clearest example of why the parity check
compares behavior instead of reasoning about it.

### Identity fields overwrite, arguments append

`id`, `type`, and `function.name` are **overwritten** when a delta carries a
non-empty value; `function.arguments` is **appended** (providers stream the
argument JSON in fragments). A later `id` wins, because a provider may send a
placeholder first and the real id later.

### `str(value or fallback)` stringifies truthy non-strings

The oracle writes `str(item.get("id") or f"call_{index + 1}")`. Python's `or`
means a **falsy** id falls back — `None`, `""`, `0`, `false` — while any other
value is stringified. So `id: 123` becomes `"123"`, and `id: 0` becomes
`call_1`. The same rule applies to `type`, where `7` becomes `"7"`.

An early implementation read only string ids, which silently renumbered
`id: 123`. The probe caught it.

### Non-string arguments use Python's **default** JSON separators

`json.dumps(arguments, ensure_ascii=False)` emits `", "` and `": "` — spaced.
`serde_json::to_string` emits compact `{"a":1}`. The Rust implementation
re-emits with Python's spacing.

This is not cosmetic: the string is spliced verbatim into the upstream request
body, so the spacing is part of the prompt prefix and affects DeepSeek's prefix
caching for the whole conversation.

### The round decision tests *finish* before the budget

```text
if not tool_calls: break
if tool_round >= max_tool_rounds: force_final_answer; continue
```

Reversing the two conditions would push a redundant
`TOOL_BUDGET_EXHAUSTED_PROMPT` turn onto an already-final answer.

### `reasoning_content` must be replayed

An assistant message carrying `tool_calls` must echo the previous round's
`reasoning_content` (the oracle also accepts a `reasoning` alias). Under
DeepSeek's thinking mode the upstream rejects a follow-up that omits it with
"The reasoning_content in the thinking mode must be passed back to the API.",
failing the entire tool-calling turn.

### `force_final_answer_without_tools` keeps `tools`

It appends a `user` turn and sets `tool_choice: "none"` rather than removing
`tools`, because `tools` sits in the prompt prefix and removing it cache-misses
the largest request of the turn. When `tools` is absent or empty, `tool_choice`
is removed instead.

## Two `normalize_tool_calls` functions that disagree by design

The oracle has two, and they are **not** interchangeable:

| | location | input | on malformed input |
| --- | --- | --- | --- |
| preparation layer | `rust/.../request_preparation.rs` | a client-supplied `tool_calls` array | **raises** (fails closed) |
| round layer | `tool_rounds::normalize_tool_calls_lenient` | what the model just produced | **silently drops** |

The round layer is fed by the provider, not by a caller, so dropping is the
oracle's chosen behavior there and mirroring it is correctness — not leniency to
be "fixed". Reusing the preparation-layer function for round finalization would
change behavior on exactly the pathological provider output the loop must
survive.

## Known limitation: object key order

This workspace compiles `serde_json` **without** `preserve_order`, so Rust sorts
object keys while Python's `json.dumps` preserves insertion order. For a
non-string `arguments` object whose keys are not already sorted, the serialized
string will therefore differ in key order from the oracle.

Only `deepseek-proof` opts into `preserve_order`; enabling it workspace-wide
would silently reorder every other Rust response, so it was deliberately not
done.

Impact is bounded: it applies only to `arguments` supplied as a JSON object
rather than a string. The streaming path always produces a string (fragments are
concatenated), which is passed through verbatim. Resolving this properly belongs
with argument canonicalization, not with layer 1.

The probe's `normalize::non-ascii-arguments-stay-unescaped` case writes its keys
in sorted order so the case measures the escaping rule instead of tripping on
this.

## Explicit non-goals

- Executing any tool (layer 2).
- Evaluating policy or sanitizing results (layer 3).
- Wiring tool rounds to a live route.
- Changing the frozen wire contracts: `object-set-v1`, Receipt v4, Commit v4,
  FastCDC v3, randomized Age, control-authority-v1, AuthorityCheckpoint v1,
  `dr-readiness-proof-v1`, `evidence-proof-v2`,
  `predictive-planning-proof-v1`, or the signed Federation documents.

## Rollback

The slice is additive and inert. `tool_rounds` is a new module with no
production caller; the route's refusal path is unchanged. Reverting the commit
restores the previous tree with no state to migrate and no compatibility surface
to unwind.

## Test inventory

Unit tests in `tool_rounds.rs` cover: argument append vs identity overwrite,
later-id-wins, index-less slot placement, out-of-order sorting, placeholder ids,
non-array and non-object deltas, empty argument fragments, nameless drops,
top-level name fallback, non-string argument encoding, Python default
separators, nested and non-ASCII arguments, empty-container padding, round
decision ordering, reasoning replay, object `tool_choice` release, string
`tool_choice` preservation, empty reasoning omission, `force_final_answer` with
and without tools, falsy and non-string ids, `tool_names` order and duplicates,
note wording, and the per-response cap.

The probe adds 36 cross-checked keys across 12 input cases.
