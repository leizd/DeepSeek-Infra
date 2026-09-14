//! Tool-round control: accumulation, finalization, and the multi-round budget.
//!
//! Scope: **layer 1 only** — the round *bookkeeping* the oracle performs around
//! a tool-calling turn. It does not execute tools. The three layers are:
//!
//! 1. *round control* (this module) — accumulate streamed `tool_calls` deltas,
//!    finalize them, decide whether another round is allowed, and assemble the
//!    assistant + tool-result messages that seed that round;
//! 2. *tool execution* — the 17 executable branches in
//!    `deepseek_infra/infra/tool_runtime/tools.py`, plus `browser_*`;
//! 3. *policy and sandbox* — `ToolPolicy.evaluate` / `sanitize_result` and the
//!    Rust-sidecar policy path.
//!
//! Layers 2 and 3 are **not implemented here**, and that has a hard consequence:
//! a `tool_calls` turn cannot be continued by this gateway. Wiring layer 1 to a
//! live route before layers 2–3 exist would manufacture a silent behavior
//! change — the model would receive a synthetic "tool unavailable" result and
//! keep generating, turning the oracle's tool loop into a permanently-failing
//! loop while still answering `200`. So the route keeps refusing tool rounds
//! (`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`) and this module exists to make that
//! refusal *precise* and to be ready for the later layers.
//!
//! The distinction matters because the oracle has **two** different
//! `normalize_tool_calls` functions and they disagree by design:
//!
//! - the *preparation* layer (`request_preparation.rs`) validates a
//!   client-supplied `tool_calls` array and **raises** on anything malformed;
//! - the *round* layer (`deepseek_client.normalize_tool_calls`, reached through
//!   `finalized_stream_tool_calls`) normalizes what the model just produced and
//!   **silently drops** entries it cannot represent.
//!
//! They are not interchangeable. The round layer is fed by the provider, not by
//! a caller, so dropping is the oracle's chosen behavior there and mirroring it
//! is correctness — not leniency to be "fixed".

use serde_json::{Map, Value, json};

/// Mirrors `tool_runtime.tools.MAX_TOOL_ROUNDS`.
///
/// The oracle's loop is `for tool_round in range(max_tool_rounds + 2)`, and it
/// takes a final un-tooled turn once `tool_round >= max_tool_rounds`, so the
/// effective number of *tool-calling* rounds is `max_tool_rounds` and the extra
/// `+2` only covers the forced final answer.
pub const MAX_TOOL_ROUNDS: usize = 3;

/// Mirrors `tool_runtime.tools.MAX_TOOL_CALLS_PER_RESPONSE`.
///
/// The oracle truncates with `tool_calls[:MAX_TOOL_CALLS_PER_RESPONSE]` before
/// executing, so calls beyond this are never run and never appear in the
/// follow-up request.
pub const MAX_TOOL_CALLS_PER_RESPONSE: usize = 6;

/// Mirrors `deepseek_client.TOOL_BUDGET_EXHAUSTED_PROMPT`.
///
/// Pushed as a `user` turn when the round budget runs out. The wording is a
/// parity surface: it is part of the upstream body and therefore affects both
/// the model's behavior and prompt-cache prefix stability.
pub const TOOL_BUDGET_EXHAUSTED_PROMPT: &str = "本轮可用的本地工具调用次数已经用完。请不要再调用任何工具，\
直接基于已经获得的信息和对话上下文给出最终回答；如信息不足，请明确说明。";

/// Accumulates streamed `tool_calls` deltas into whole calls.
///
/// Mirrors `merge_stream_tool_call_deltas`. Three behaviors are load-bearing and
/// easy to get wrong:
///
/// - a delta **without** an `index` appends at `len(accumulator)`, not at a
///   fixed slot — an unindexed call therefore lands after every indexed one;
/// - `id` / `type` / `function.name` are *overwritten* when present, while
///   `function.arguments` is *appended* (providers stream the argument JSON in
///   fragments);
/// - a call slot is created on first sight with the placeholder id
///   `call_{index + 1}` and empty name/arguments, and a later delta fills it in.
#[derive(Debug, Clone, Default)]
pub struct ToolCallAccumulator {
    slots: Vec<(usize, Value)>,
}

impl ToolCallAccumulator {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_empty(&self) -> bool {
        self.slots.is_empty()
    }

    /// Merge one `delta.tool_calls` array.
    ///
    /// A non-array `deltas` is ignored, matching the oracle's `isinstance` guard.
    pub fn merge(&mut self, deltas: &Value) {
        let Some(items) = deltas.as_array() else {
            return;
        };
        for item in items {
            let Some(object) = item.as_object() else {
                continue;
            };
            let index = match object.get("index") {
                None => self.slots.len(),
                Some(value) => value
                    .as_i64()
                    .and_then(|value| usize::try_from(value).ok())
                    .unwrap_or(self.slots.len()),
            };
            let slot = self.slot_mut(index);
            let Some(slot_object) = slot.as_object_mut() else {
                continue;
            };
            if let Some(id) = object.get("id").and_then(Value::as_str) {
                if !id.is_empty() {
                    slot_object.insert("id".to_string(), Value::String(id.to_string()));
                }
            }
            if let Some(kind) = object.get("type").and_then(Value::as_str) {
                if !kind.is_empty() {
                    slot_object.insert("type".to_string(), Value::String(kind.to_string()));
                }
            }
            let Some(incoming) = object.get("function").and_then(Value::as_object) else {
                continue;
            };
            let Some(function) = slot_object
                .get_mut("function")
                .and_then(Value::as_object_mut)
            else {
                continue;
            };
            if let Some(name) = incoming.get("name").and_then(Value::as_str) {
                if !name.is_empty() {
                    function.insert("name".to_string(), Value::String(name.to_string()));
                }
            }
            if let Some(fragment) = incoming.get("arguments").and_then(Value::as_str) {
                if !fragment.is_empty() {
                    let existing = function
                        .get("arguments")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let mut joined = String::with_capacity(existing.len() + fragment.len());
                    joined.push_str(existing);
                    joined.push_str(fragment);
                    function.insert("arguments".to_string(), Value::String(joined));
                }
            }
        }
    }

    /// The slot for `index`, created with the oracle's placeholder shape.
    fn slot_mut(&mut self, index: usize) -> &mut Value {
        if let Some(position) = self.slots.iter().position(|(slot, _)| *slot == index) {
            return &mut self.slots[position].1;
        }
        let placeholder = json!({
            "id": format!("call_{}", index + 1),
            "type": "function",
            "function": {"name": "", "arguments": ""},
        });
        self.slots.push((index, placeholder));
        let position = self.slots.len() - 1;
        &mut self.slots[position].1
    }

    /// Emit the finalized calls, in ascending index order.
    ///
    /// Mirrors `finalized_stream_tool_calls`: slots are sorted by index before
    /// normalization, so the model's tool-call order is preserved regardless of
    /// the order the deltas arrived in.
    ///
    /// The arguments are passed through *unmodified* here — no canonicalization —
    /// because the oracle deliberately preserves the provider's argument JSON
    /// byte-for-byte so the next request's prefix matches the model's own output
    /// and DeepSeek's prompt cache can reuse it.
    pub fn finalize(&self) -> Vec<Value> {
        let mut ordered: Vec<&(usize, Value)> = self.slots.iter().collect();
        ordered.sort_by_key(|(index, _)| *index);
        let raw: Vec<Value> = ordered.iter().map(|(_, value)| (*value).clone()).collect();
        normalize_tool_calls_lenient(&raw)
    }
}

/// Lenient normalization, mirroring `deepseek_client.normalize_tool_calls`
/// with `stable_ids=False, canonical_arguments=False`.
///
/// Drops non-object entries and entries without a usable function name. The
/// name is read from `function.name` first, falling back to a top-level `name`.
/// `arguments` is passed through when it is already a string (the streaming
/// path always produces a string) and JSON-encoded otherwise.
///
/// Note this is **not** the same contract as `request_preparation`'s
/// `normalize_tool_calls`, which fails closed on the same inputs. See the module
/// docs for why the two must differ.
pub fn normalize_tool_calls_lenient(value: &[Value]) -> Vec<Value> {
    let mut tool_calls = Vec::with_capacity(value.len());
    for (index, item) in value.iter().enumerate() {
        let Some(object) = item.as_object() else {
            continue;
        };
        let function = object.get("function").and_then(Value::as_object);
        let name = function
            .and_then(|function| function.get("name"))
            .and_then(Value::as_str)
            .or_else(|| object.get("name").and_then(Value::as_str))
            .unwrap_or("")
            .trim()
            .to_string();
        if name.is_empty() {
            continue;
        }
        let arguments = function
            .and_then(|function| function.get("arguments"))
            .cloned()
            .unwrap_or_else(|| Value::String(String::new()));
        // A `str` argument is kept verbatim; anything else is JSON-encoded to
        // match `json.dumps(arguments, ensure_ascii=False)`. Python's default
        // separators are `", "` / `": "` — *spaced* — so `serde_json`'s compact
        // output is not byte-equivalent and must be re-emitted with the spacing.
        //
        // This is not cosmetic: the string lands verbatim in the upstream request
        // body, so any difference changes the prompt prefix and breaks DeepSeek's
        // prefix caching for the whole conversation.
        let normalized_arguments = match arguments {
            Value::String(text) => Value::String(text),
            other => Value::String(json_dumps_default_separators(&other)),
        };
        // The oracle is `str(item.get("id") or f"call_{index + 1}")`. The `or`
        // means a *falsy* id falls back — `None`, `""`, `0`, `false` — while any
        // other value is stringified, so an integer id `123` becomes `"123"`.
        // Reading only string ids here would silently renumber such calls.
        let id = stringify_or_fallback(object.get("id"), &format!("call_{}", index + 1));
        let kind = stringify_or_fallback(object.get("type"), "function");
        let mut normalized = Map::new();
        normalized.insert("id".to_string(), Value::String(id));
        normalized.insert("type".to_string(), Value::String(kind));
        normalized.insert(
            "function".to_string(),
            json!({"name": name, "arguments": normalized_arguments}),
        );
        tool_calls.push(Value::Object(normalized));
    }
    tool_calls
}

/// Serialize a JSON value the way Python's `json.dumps(value,
/// ensure_ascii=False)` does, with its **default separators**.
///
/// `serde_json::to_string` emits compact JSON (`{"a":1}`); Python emits
/// `{"a": 1}` — a space after `:` and after each `,`. The oracle's output is the
/// Python form, and since these strings are spliced into the upstream request
/// body, the difference changes the prompt prefix.
///
/// `ensure_ascii=False` is already Rust's behavior: non-ASCII characters are
/// emitted as UTF-8 rather than `\uXXXX` escapes.
fn json_dumps_default_separators(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let rendered: Vec<String> = items.iter().map(json_dumps_default_separators).collect();
            format!("[{}]", rendered.join(", "))
        }
        Value::Object(fields) => {
            let rendered: Vec<String> = fields
                .iter()
                .map(|(key, value)| {
                    format!(
                        "{}: {}",
                        Value::String(key.clone()),
                        json_dumps_default_separators(value)
                    )
                })
                .collect();
            format!("{{{}}}", rendered.join(", "))
        }
        Value::String(text) => Value::String(text.clone()).to_string(),
        other => other.to_string(),
    }
}

/// Mirror Python's `str(value or fallback)` for a JSON value.
///
/// Python truthiness maps onto JSON as: `null`, `""`, `0`, `0.0`, `false`, `[]`
/// and `{}` are falsy; everything else is truthy. A truthy value is stringified
/// the way `str()` would, which for the id/type fields the oracle feeds means
/// strings pass through and numbers/booleans become their literal text.
fn stringify_or_fallback(value: Option<&Value>, fallback: &str) -> String {
    match value {
        None | Some(Value::Null) => fallback.to_string(),
        Some(Value::String(text)) => {
            if text.is_empty() {
                fallback.to_string()
            } else {
                text.clone()
            }
        }
        Some(Value::Bool(flag)) => {
            if *flag {
                "True".to_string()
            } else {
                fallback.to_string()
            }
        }
        Some(Value::Number(number)) => {
            // `0` and `0.0` are falsy in Python; everything else stringifies.
            if number.as_f64() == Some(0.0) {
                fallback.to_string()
            } else {
                number.to_string()
            }
        }
        // `[]` and `{}` are falsy in Python; non-empty containers stringify to
        // their repr, which no provider sends for `id`/`type`.
        Some(Value::Array(items)) => {
            if items.is_empty() {
                fallback.to_string()
            } else {
                serde_json::to_string(&Value::Array(items.clone()))
                    .unwrap_or_else(|_| fallback.to_string())
            }
        }
        Some(Value::Object(fields)) => {
            if fields.is_empty() {
                fallback.to_string()
            } else {
                serde_json::to_string(&Value::Object(fields.clone()))
                    .unwrap_or_else(|_| fallback.to_string())
            }
        }
    }
}

/// Result of the round-budget decision, mirroring the oracle's branch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoundDecision {
    /// The turn produced no tool calls, so the answer is final.
    Finish,
    /// Run another tool round, then ask upstream again.
    Continue,
    /// The round budget is spent: take one final turn with tools disabled.
    ForceFinalAnswer,
}

/// Decide what to do after a completed round.
///
/// Mirrors the oracle's ordering exactly:
///
/// ```text
/// if not tool_calls: break
/// if tool_round >= max_tool_rounds:
///     emit system_note; force_final_answer_without_tools(body); continue
/// ```
///
/// The check order matters: a round that produced *no* calls finishes even when
/// the budget is exhausted, because the oracle breaks before reaching the budget
/// test. Reversing the two conditions would push a redundant
/// `TOOL_BUDGET_EXHAUSTED_PROMPT` turn onto an already-final answer.
pub fn decide_round(
    tool_call_count: usize,
    tool_round: usize,
    max_tool_rounds: usize,
) -> RoundDecision {
    if tool_call_count == 0 {
        return RoundDecision::Finish;
    }
    if tool_round >= max_tool_rounds {
        return RoundDecision::ForceFinalAnswer;
    }
    RoundDecision::Continue
}

/// Append the assistant turn that requested tools, plus the tool results.
///
/// Mirrors the message-assembly half of `append_tool_exchange`; the tool
/// *execution* half (`execute_tool_calls`) is layer 2 and is supplied by the
/// caller as `tool_results`.
///
/// `reasoning_content` is replayed when present, and that is not cosmetic:
/// under DeepSeek's thinking mode the upstream rejects a follow-up request whose
/// assistant `tool_calls` message omits `reasoning_content` with
/// "The reasoning_content in the thinking mode must be passed back to the API."
/// Dropping it fails the entire tool-calling turn.
///
/// `tool_choice` is reset to `"auto"` when it was an object (a forced
/// `tool_choice` pins one function and would prevent the model from choosing the
/// next one). A string `tool_choice` is left alone.
pub fn append_tool_exchange(
    body: &Value,
    assistant_content: &str,
    assistant_reasoning: &str,
    tool_calls: &[Value],
    tool_results: &[Value],
) -> Value {
    let mut messages = body
        .get("messages")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut assistant = Map::new();
    assistant.insert("role".to_string(), Value::String("assistant".to_string()));
    assistant.insert(
        "content".to_string(),
        Value::String(assistant_content.to_string()),
    );
    assistant.insert("tool_calls".to_string(), Value::Array(tool_calls.to_vec()));
    if !assistant_reasoning.is_empty() {
        assistant.insert(
            "reasoning_content".to_string(),
            Value::String(assistant_reasoning.to_string()),
        );
    }
    messages.push(Value::Object(assistant));
    messages.extend(tool_results.iter().cloned());

    let mut next = body.as_object().cloned().unwrap_or_default();
    next.insert("messages".to_string(), Value::Array(messages));
    if next.get("tool_choice").is_some_and(Value::is_object) {
        next.insert("tool_choice".to_string(), Value::String("auto".to_string()));
    }
    Value::Object(next)
}

/// Force a final answer by disabling tools, mirroring
/// `force_final_answer_without_tools`.
///
/// The `tools` array is **kept** and `tool_choice` set to `"none"` instead of
/// dropping `tools`. That is deliberate in the oracle: `tools` sits in the
/// prompt prefix, and removing it cache-misses the largest request of the turn.
pub fn force_final_answer_without_tools(body: &Value) -> Value {
    let mut messages = body
        .get("messages")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    messages.push(json!({"role": "user", "content": TOOL_BUDGET_EXHAUSTED_PROMPT}));
    let mut next = body.as_object().cloned().unwrap_or_default();
    next.insert("messages".to_string(), Value::Array(messages));
    let has_tools = next
        .get("tools")
        .and_then(Value::as_array)
        .is_some_and(|tools| !tools.is_empty());
    if has_tools {
        next.insert("tool_choice".to_string(), Value::String("none".to_string()));
    } else {
        next.remove("tool_choice");
    }
    Value::Object(next)
}

/// `system_note` text for the round-budget message.
///
/// The oracle **inlines** this literal in `stream_deepseek`'s loop rather than
/// naming it, so this constant is a Rust-side convenience name. The probe's
/// `note::round-budget` key lifts the literal structurally out of the
/// `emit_checked({...})` call and compares it here, so the two spellings are
/// proven equal rather than assumed.
pub const TOOL_BUDGET_NOTE: &str = "工具调用次数已达上限，改为直接整理最终回答。\n\n";

/// `system_note` text announcing the tools about to run, mirroring the oracle's
/// inlined literal. The names are joined with ", " and an empty list falls back
/// to `"tool"`.
pub fn tool_call_note(names: &[String]) -> String {
    let joined = if names.is_empty() {
        "tool".to_string()
    } else {
        names.join(", ")
    };
    format!("正在调用本地工具：{joined}\n\n")
}

/// Extract the tool names from finalized calls, in order.
///
/// Mirrors `tool_names`: reads `function.name` and skips blanks, preserving
/// duplicates (the oracle does not deduplicate; `diagnostics_with_tools` sorts
/// and dedupes separately, downstream).
pub fn tool_names(tool_calls: &[Value]) -> Vec<String> {
    tool_calls
        .iter()
        .filter_map(|call| {
            call.get("function")
                .and_then(Value::as_object)
                .and_then(|function| function.get("name"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .map(str::to_string)
        })
        .collect()
}

/// Truncate to the per-response call cap, mirroring `tool_calls[:MAX]`.
pub fn select_tool_calls(tool_calls: &[Value]) -> &[Value] {
    let limit = tool_calls.len().min(MAX_TOOL_CALLS_PER_RESPONSE);
    &tool_calls[..limit]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arguments_are_appended_while_identity_fields_are_overwritten() {
        let mut accumulator = ToolCallAccumulator::new();
        accumulator.merge(&json!([{
            "index": 0,
            "id": "call_a",
            "function": {"name": "search_files", "arguments": "{\"qu"}
        }]));
        accumulator.merge(&json!([{
            "index": 0,
            "function": {"arguments": "ery\":\"x\"}"}
        }]));
        let calls = accumulator.finalize();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0]["id"], "call_a");
        assert_eq!(calls[0]["function"]["name"], "search_files");
        assert_eq!(calls[0]["function"]["arguments"], "{\"query\":\"x\"}");
    }

    /// A later `id` wins — the provider may send a placeholder id first and the
    /// real one in a subsequent delta.
    #[test]
    fn a_later_id_overwrites_an_earlier_one() {
        let mut accumulator = ToolCallAccumulator::new();
        accumulator.merge(&json!([{"index": 0, "id": "call_1", "function": {"name": "a"}}]));
        accumulator.merge(&json!([{"index": 0, "id": "call_real"}]));
        assert_eq!(accumulator.finalize()[0]["id"], "call_real");
    }

    /// A delta with no `index` is placed at `len(accumulator)` — the *count* of
    /// existing slots, not the highest index plus one.
    ///
    /// That distinction is observable: after slot `5` exists, an index-less call
    /// gets slot `2` (two slots present), so it sorts **before** slot `5`. This
    /// expectation was initially wrong and was corrected against the real oracle
    /// (`merge_stream_tool_call_deltas` + `finalized_stream_tool_calls`), which
    /// yields `["first", "third", "five"]`.
    #[test]
    fn an_indexless_delta_lands_at_the_slot_count_not_the_end() {
        let mut accumulator = ToolCallAccumulator::new();
        accumulator.merge(&json!([{"function": {"name": "first", "arguments": "{}"}}]));
        accumulator.merge(&json!([{"index": 5, "function": {"name": "five", "arguments": "{}"}}]));
        accumulator.merge(&json!([{"function": {"name": "third", "arguments": "{}"}}]));
        let names = tool_names(&accumulator.finalize());
        assert_eq!(names, vec!["first", "third", "five"]);
    }

    /// Slot creation must use the oracle's placeholder id, because a provider
    /// that never sends an id would otherwise yield an empty one.
    #[test]
    fn missing_ids_fall_back_to_the_positional_placeholder() {
        let mut accumulator = ToolCallAccumulator::new();
        accumulator.merge(&json!([{"index": 2, "function": {"name": "x", "arguments": "{}"}}]));
        let calls = accumulator.finalize();
        assert_eq!(calls[0]["id"], "call_3");
        assert_eq!(calls[0]["type"], "function");
    }

    #[test]
    fn non_array_and_non_object_deltas_are_ignored() {
        let mut accumulator = ToolCallAccumulator::new();
        accumulator.merge(&json!("nope"));
        accumulator.merge(&json!([1, "two", null]));
        assert!(accumulator.is_empty());
    }

    /// Non-string arguments are JSON-encoded with Python's **default separators**,
    /// not `serde_json`'s compact form.
    ///
    /// Found by the parity probe: `serde_json::to_string` yields `{"a":1}` while
    /// the oracle yields `{"a": 1}`. The string is spliced into the upstream
    /// request body, so the spacing is part of the prompt prefix.
    #[test]
    fn object_arguments_use_python_default_separators() {
        let calls = normalize_tool_calls_lenient(&[json!({
            "function": {"name": "x", "arguments": {"a": 1}}
        })]);
        assert_eq!(calls[0]["function"]["arguments"], "{\"a\": 1}");
    }

    /// The separator rule is recursive, and non-ASCII text stays raw UTF-8
    /// (`ensure_ascii=False`), which is also Rust's default.
    ///
    /// The keys are written in sorted order because this workspace compiles
    /// `serde_json` without `preserve_order`, so Rust re-sorts object keys while
    /// Python keeps insertion order. Writing them sorted isolates the escaping
    /// rule; the ordering limitation itself is documented in
    /// `docs/GATEWAY_TOOL_ROUND_PARITY.md`.
    #[test]
    fn nested_and_non_ascii_arguments_match_python_dumps() {
        let nested = normalize_tool_calls_lenient(&[json!({
            "function": {"name": "n", "arguments": {"a": 1, "b": [1, 2, {"c": "x"}]}}
        })]);
        assert_eq!(
            nested[0]["function"]["arguments"],
            "{\"a\": 1, \"b\": [1, 2, {\"c\": \"x\"}]}"
        );

        let unicode = normalize_tool_calls_lenient(&[json!({
            "function": {"name": "n", "arguments": {"n": null, "t": true, "名": "值"}}
        })]);
        assert_eq!(
            unicode[0]["function"]["arguments"],
            "{\"n\": null, \"t\": true, \"名\": \"值\"}"
        );
    }

    /// Empty containers get no inner padding: `{}` and `[]`, like Python.
    #[test]
    fn empty_containers_have_no_inner_padding() {
        let calls = normalize_tool_calls_lenient(&[json!({
            "function": {"name": "n", "arguments": {"e": {}, "l": []}}
        })]);
        assert_eq!(calls[0]["function"]["arguments"], "{\"e\": {}, \"l\": []}");
    }

    /// The oracle writes `str(item.get("id") or f"call_{index + 1}")`, so a
    /// truthy non-string id is stringified rather than discarded.
    ///
    /// This case was found by the parity probe: the first implementation read
    /// only string ids, which silently renumbered `id: 123` to `call_1`. The
    /// oracle's real output for the same input is `"123"` / `"7"`.
    #[test]
    fn a_non_string_id_and_type_are_stringified_not_discarded() {
        let calls = normalize_tool_calls_lenient(&[json!({
            "id": 123,
            "type": 7,
            "function": {"name": "n", "arguments": "{}"}
        })]);
        assert_eq!(calls[0]["id"], "123");
        assert_eq!(calls[0]["type"], "7");
    }

    /// `0` and `""` are falsy in Python, so `or` falls back for both.
    #[test]
    fn falsy_ids_fall_back_to_the_positional_placeholder() {
        let zero = normalize_tool_calls_lenient(&[json!({
            "id": 0,
            "type": "function",
            "function": {"name": "n", "arguments": "{}"}
        })]);
        assert_eq!(zero[0]["id"], "call_1");

        let empty = normalize_tool_calls_lenient(&[json!({
            "id": "",
            "type": "function",
            "function": {"name": "n", "arguments": "{}"}
        })]);
        assert_eq!(empty[0]["id"], "call_1");

        let falsey = normalize_tool_calls_lenient(&[json!({
            "id": false,
            "type": "function",
            "function": {"name": "n", "arguments": "{}"}
        })]);
        assert_eq!(falsey[0]["id"], "call_1");
    }

    #[test]
    fn finalize_sorts_by_index_not_arrival_order() {
        let mut accumulator = ToolCallAccumulator::new();
        accumulator
            .merge(&json!([{"index": 1, "function": {"name": "second", "arguments": "{}"}}]));
        accumulator.merge(&json!([{"index": 0, "function": {"name": "first", "arguments": "{}"}}]));
        assert_eq!(tool_names(&accumulator.finalize()), vec!["first", "second"]);
    }

    /// A nameless call is dropped, not raised on. This is the round layer's
    /// contract and differs from the preparation layer on purpose.
    #[test]
    fn nameless_calls_are_dropped_not_rejected() {
        let calls = normalize_tool_calls_lenient(&[
            json!({"id": "a", "function": {"name": "  ", "arguments": "{}"}}),
            json!({"id": "b", "function": {"name": "keep", "arguments": "{}"}}),
            json!("not an object"),
        ]);
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0]["id"], "b");
    }

    /// The top-level `name` is a fallback when `function.name` is absent.
    #[test]
    fn top_level_name_is_a_fallback() {
        let calls = normalize_tool_calls_lenient(&[json!({"name": "flat", "arguments": "{}"})]);
        assert_eq!(calls[0]["function"]["name"], "flat");
    }

    /// Non-string arguments are JSON-encoded — and **not** compactly.
    ///
    /// This test originally asserted `{"a":1}` and was corrected after the
    /// parity probe showed the oracle emitting `{"a": 1}`, which is Python's
    /// default `json.dumps` separator. Kept as a named regression guard; the
    /// deeper separation/nesting cases live in
    /// `nested_and_non_ascii_arguments_match_python_dumps`.
    #[test]
    fn non_string_arguments_are_json_encoded_with_python_separators() {
        let calls = normalize_tool_calls_lenient(&[
            json!({"function": {"name": "x", "arguments": {"a": 1}}}),
        ]);
        assert_eq!(calls[0]["function"]["arguments"], r#"{"a": 1}"#);
    }

    #[test]
    fn round_decision_finishes_before_the_budget_is_checked() {
        // No calls -> final, even with the budget exhausted. The oracle breaks
        // before reaching the budget test.
        assert_eq!(decide_round(0, 99, MAX_TOOL_ROUNDS), RoundDecision::Finish);
        assert_eq!(decide_round(1, 0, MAX_TOOL_ROUNDS), RoundDecision::Continue);
        assert_eq!(decide_round(1, 2, MAX_TOOL_ROUNDS), RoundDecision::Continue);
        assert_eq!(
            decide_round(1, 3, MAX_TOOL_ROUNDS),
            RoundDecision::ForceFinalAnswer
        );
    }

    #[test]
    fn append_tool_exchange_replays_reasoning_and_resets_object_tool_choice() {
        let body = json!({
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": {"type": "function", "function": {"name": "pinned"}},
        });
        let calls = vec![
            json!({"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}),
        ];
        let results = vec![json!({"role": "tool", "tool_call_id": "c1", "content": "ok"})];
        let next = append_tool_exchange(&body, "thinking out loud", "why", &calls, &results);
        let messages = next["messages"].as_array().unwrap();
        assert_eq!(messages.len(), 3);
        assert_eq!(messages[1]["role"], "assistant");
        assert_eq!(messages[1]["content"], "thinking out loud");
        assert_eq!(messages[1]["reasoning_content"], "why");
        assert_eq!(messages[1]["tool_calls"][0]["id"], "c1");
        assert_eq!(messages[2]["role"], "tool");
        // An object tool_choice pinned one function; it must be released.
        assert_eq!(next["tool_choice"], "auto");
    }

    /// A string `tool_choice` (`"auto"` / `"none"`) is left alone — only an
    /// object pins a specific function.
    #[test]
    fn a_string_tool_choice_is_preserved() {
        let body = json!({"messages": [], "tool_choice": "auto"});
        let next = append_tool_exchange(&body, "", "", &[], &[]);
        assert_eq!(next["tool_choice"], "auto");
    }

    /// Absent or empty reasoning must not add the field, or the upstream sees an
    /// empty `reasoning_content` it did not produce.
    #[test]
    fn empty_reasoning_is_not_emitted() {
        let body = json!({"messages": []});
        let next = append_tool_exchange(&body, "answer", "", &[], &[]);
        let assistant = &next["messages"][0];
        assert!(assistant.get("reasoning_content").is_none());
        assert_eq!(assistant["content"], "answer");
    }

    #[test]
    fn force_final_answer_keeps_tools_and_disables_choice() {
        let body = json!({
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": "auto",
        });
        let next = force_final_answer_without_tools(&body);
        assert_eq!(next["tool_choice"], "none");
        assert!(next.get("tools").is_some(), "tools must be kept for cache");
        let messages = next["messages"].as_array().unwrap();
        assert_eq!(messages[1]["role"], "user");
        assert_eq!(messages[1]["content"], TOOL_BUDGET_EXHAUSTED_PROMPT);
    }

    #[test]
    fn force_final_answer_without_tools_drops_tool_choice() {
        let body = json!({"messages": [], "tool_choice": "auto"});
        let next = force_final_answer_without_tools(&body);
        assert!(next.get("tool_choice").is_none());
    }

    /// An empty `tools` array counts as "no tools", matching the oracle's
    /// truthiness test on the list.
    #[test]
    fn an_empty_tools_array_counts_as_no_tools() {
        let body = json!({"messages": [], "tools": [], "tool_choice": "auto"});
        let next = force_final_answer_without_tools(&body);
        assert!(next.get("tool_choice").is_none());
    }

    #[test]
    fn tool_names_preserve_order_and_duplicates() {
        let calls = vec![
            json!({"function": {"name": "b"}}),
            json!({"function": {"name": "a"}}),
            json!({"function": {"name": "b"}}),
            json!({"function": {"name": "  "}}),
            json!({"id": "no-function"}),
        ];
        assert_eq!(tool_names(&calls), vec!["b", "a", "b"]);
    }

    #[test]
    fn tool_call_note_matches_the_oracle_wording() {
        assert_eq!(
            tool_call_note(&["a".to_string(), "b".to_string()]),
            "正在调用本地工具：a, b\n\n"
        );
        assert_eq!(tool_call_note(&[]), "正在调用本地工具：tool\n\n");
    }

    #[test]
    fn select_tool_calls_caps_at_the_per_response_limit() {
        let calls: Vec<Value> = (0..9)
            .map(|index| json!({"function": {"name": format!("t{index}")}}))
            .collect();
        assert_eq!(select_tool_calls(&calls).len(), MAX_TOOL_CALLS_PER_RESPONSE);
        let few: Vec<Value> = calls[..2].to_vec();
        assert_eq!(select_tool_calls(&few).len(), 2);
    }
}
