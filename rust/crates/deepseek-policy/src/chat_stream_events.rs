//! The `POST /api/chat` NDJSON stream protocol.
//!
//! `deepseek_infra/web/server.py` serves the frontend's chat stream as
//! `application/x-ndjson`, one compact JSON object per line, produced by
//! `encode_stream_event`:
//!
//! ```python
//! json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
//! ```
//!
//! This module is the encoder and the event accumulator. The transport lives in the
//! gateway; keeping the encoding here is what makes byte parity with the oracle a
//! probe rather than a hope.
//!
//! # Why the key order is built by hand
//!
//! Python's `json.dumps` writes keys in **insertion** order, and the frontend reads
//! fields by name — so order alone would not break the UI. It would still break the
//! compatibility contract, which requires byte equality where the specification says
//! so, and `serde_json` is compiled here **without** `preserve_order` (its maps are
//! key-sorted). The same choice was made for the OpenAI SSE frames in
//! `deepseek-gateway::chat_stream`, for the same reason.
//!
//! # The event vocabulary
//!
//! | type | fields (in order) | emitted when |
//! |---|---|---|
//! | `system_note` | `type`, `text` | the oracle wants the user to see a progress line |
//! | `search` | `type`, `search` | search progress, before and during a turn |
//! | `reasoning` | `type`, `text` | one upstream reasoning delta |
//! | `content` | `type`, `text` | one upstream content delta |
//! | `memory_suggestion` | `type`, then the suggestion's own keys | a tool offered to save a memory |
//! | `error` | `type`, `error`, `code` | the turn failed |
//! | `done` | `type`, `id`, `model`, `content`, `reasoning`, `usage`, `search`, `memorySuggestions`, `finishReason`, `diagnostics` | the turn finished |
//!
//! The agent-mode and agent-run events (`agent`, `agent_delta`, `run_status`,
//! `agent_plan`, …) are a different producer and are not in this module.

use serde_json::{Value, json};

/// The media type the oracle sets on the stream.
pub const STREAM_MEDIA_TYPE: &str = "application/x-ndjson; charset=utf-8";

/// One line of the NDJSON stream.
///
/// Variants rather than a bare `Value` so the field order each event needs is
/// expressed once, at construction, instead of being re-derived by every caller.
#[derive(Debug, Clone, PartialEq)]
pub enum ChatEvent {
    /// `{"type": "system_note", "text": ...}`
    SystemNote { text: String },
    /// `{"type": "search", "search": ...}`
    Search { search: Value },
    /// `{"type": "reasoning", "text": ...}`
    Reasoning { text: String },
    /// `{"type": "content", "text": ...}`
    Content { text: String },
    /// `{"type": "memory_suggestion", ...}` — the suggestion's keys are spread into
    /// the event after `type`, which is what `{"type": ..., **suggestion}` does.
    MemorySuggestion { suggestion: Value },
    /// `{"type": "error", "error": ..., "code": ...}`
    Error { error: String, code: String },
    /// The terminal event. Every field is always present, because the oracle always
    /// writes every one of them — including `id: null` and an empty `usage`.
    Done(Box<ChatDone>),
}

/// The terminal event's payload.
///
/// `usage` is a [`RawJson`] rather than a `Value` on purpose: it is the one field the
/// oracle writes in the **provider's** key order, and a `Value` round-trip would sort
/// it. See [`RawJson`].
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ChatDone {
    pub id: Value,
    pub model: String,
    pub content: String,
    pub reasoning: String,
    pub usage: RawJson,
    pub search: Value,
    pub memory_suggestions: Value,
    pub finish_reason: String,
    pub diagnostics: Value,
}

/// The event's `type` string.
pub fn event_type(event: &ChatEvent) -> &'static str {
    match event {
        ChatEvent::SystemNote { .. } => "system_note",
        ChatEvent::Search { .. } => "search",
        ChatEvent::Reasoning { .. } => "reasoning",
        ChatEvent::Content { .. } => "content",
        ChatEvent::MemorySuggestion { .. } => "memory_suggestion",
        ChatEvent::Error { .. } => "error",
        ChatEvent::Done(_) => "done",
    }
}

/// `encode_stream_event`: one compact JSON object plus `b"\n"`.
///
/// `serde_json::to_string` is compact and never escapes non-ASCII, which is what
/// `ensure_ascii=False, separators=(",", ":")` produces.
pub fn encode_stream_event(event: &ChatEvent) -> Vec<u8> {
    let mut out = compact_event_json(event).into_bytes();
    out.push(b'\n');
    out
}

/// The compact JSON body, without the trailing newline.
pub fn compact_event_json(event: &ChatEvent) -> String {
    match event {
        ChatEvent::SystemNote { text } => {
            object(&[("type", json!("system_note")), ("text", json!(text))])
        }
        ChatEvent::Search { search } => {
            object(&[("type", json!("search")), ("search", search.clone())])
        }
        ChatEvent::Reasoning { text } => {
            object(&[("type", json!("reasoning")), ("text", json!(text))])
        }
        ChatEvent::Content { text } => object(&[("type", json!("content")), ("text", json!(text))]),
        ChatEvent::MemorySuggestion { suggestion } => {
            // `{"type": ..., **suggestion}`: `type` first, then the suggestion's own
            // keys in their insertion order. A suggestion that carries its own `type`
            // key would overwrite this one in Python, and it does here too — the
            // spread wins, which is why the field is written from the map when present.
            let mut fields: Vec<(String, Value)> = Vec::new();
            let mut suggestion_fields: Vec<(String, Value)> = suggestion
                .as_object()
                .map(|map| {
                    map.iter()
                        .map(|(key, value)| (key.clone(), value.clone()))
                        .collect()
                })
                .unwrap_or_default();
            // Python's dict preserves the *first* insertion position when a key is
            // overwritten, so `type` stays first even when the suggestion carries one.
            let suggestion_type = suggestion_fields
                .iter()
                .find(|(key, _)| key == "type")
                .map(|(_, value)| value.clone());
            suggestion_fields.retain(|(key, _)| key != "type");
            fields.push((
                "type".to_string(),
                suggestion_type.unwrap_or_else(|| json!("memory_suggestion")),
            ));
            fields.extend(suggestion_fields);
            object_owned(&fields)
        }
        ChatEvent::Error { error, code } => object(&[
            ("type", json!("error")),
            ("error", json!(error)),
            ("code", json!(code)),
        ]),
        ChatEvent::Done(done) => {
            let usage = done.usage.clone();
            object_with_raw(&[
                ("type", Field::Value(json!("done"))),
                ("id", Field::Value(done.id.clone())),
                ("model", Field::Value(json!(done.model))),
                ("content", Field::Value(json!(done.content))),
                ("reasoning", Field::Value(json!(done.reasoning))),
                ("usage", Field::Raw(&usage)),
                ("search", Field::Value(done.search.clone())),
                (
                    "memorySuggestions",
                    Field::Value(done.memory_suggestions.clone()),
                ),
                ("finishReason", Field::Value(json!(done.finish_reason))),
                ("diagnostics", Field::Value(done.diagnostics.clone())),
            ])
        }
    }
}

/// A pre-rendered JSON fragment, so a nested object keeps the key order it was built
/// with.
///
/// `serde_json` is compiled here **without** `preserve_order`, so every `Value` map is
/// key-sorted. The oracle writes `usage` in the provider's insertion order, so the
/// field is carried as its own bytes and inserted verbatim — which is what makes the
/// `done` line byte-identical. The parity probe caught the reordering
/// (`done_full`: `completion_tokens` before `prompt_tokens`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawJson(String);

impl Default for RawJson {
    fn default() -> Self {
        Self("{}".to_string())
    }
}

impl RawJson {
    pub fn from_value(value: &Value) -> Self {
        Self(serde_json::to_string(value).expect("serializing a Value cannot fail"))
    }

    /// Build from ordered pairs, which is how a caller preserves a key order.
    pub fn object(fields: &[(&str, Value)]) -> Self {
        let mut out = String::with_capacity(64);
        out.push('{');
        for (index, (key, value)) in fields.iter().enumerate() {
            if index > 0 {
                out.push(',');
            }
            out.push_str(&serde_json::to_string(key).expect("serializing a &str cannot fail"));
            out.push(':');
            out.push_str(&serde_json::to_string(value).expect("serializing a Value cannot fail"));
        }
        out.push('}');
        Self(out)
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// The fragment as a parsed value, for a caller that needs to read a field back.
    pub fn to_value(&self) -> Value {
        serde_json::from_str(&self.0).unwrap_or(Value::Null)
    }
}

/// One field of a hand-built object: a `Value` to serialize, or bytes to insert.
enum Field<'a> {
    Value(Value),
    Raw(&'a RawJson),
}

/// Build a JSON object from ordered fields, inserting raw fragments verbatim.
fn object_with_raw(fields: &[(&str, Field<'_>)]) -> String {
    let mut out = String::with_capacity(128);
    out.push('{');
    for (index, (key, field)) in fields.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str(&serde_json::to_string(key).expect("serializing a &str cannot fail"));
        out.push(':');
        match field {
            Field::Value(value) => out
                .push_str(&serde_json::to_string(value).expect("serializing a Value cannot fail")),
            Field::Raw(raw) => out.push_str(raw.as_str()),
        }
    }
    out.push('}');
    out
}

/// Build a JSON object from ordered fields.
fn object(fields: &[(&str, Value)]) -> String {
    let owned: Vec<(String, Value)> = fields
        .iter()
        .map(|(key, value)| ((*key).to_string(), value.clone()))
        .collect();
    object_owned(&owned)
}

fn object_owned(fields: &[(String, Value)]) -> String {
    let mut out = String::with_capacity(64);
    out.push('{');
    for (index, (key, value)) in fields.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str(&serde_json::to_string(key).expect("serializing a &str cannot fail"));
        out.push(':');
        out.push_str(&serde_json::to_string(value).expect("serializing a Value cannot fail"));
    }
    out.push('}');
    out
}

/// What the stream has accumulated so far.
///
/// The oracle keeps `content` and `reasoning` as growing strings and emits each delta
/// as it arrives, so the terminal `done` event repeats the whole answer. This mirrors
/// that: the deltas are emitted *and* accumulated, and `done` carries the totals.
#[derive(Debug, Clone, PartialEq)]
pub struct ChatStreamAccumulator {
    content: String,
    reasoning: String,
    id: Value,
    model: String,
    usage: RawJson,
    search: Value,
    memory_suggestions: Vec<Value>,
    finish_reason: String,
}

impl Default for ChatStreamAccumulator {
    fn default() -> Self {
        Self {
            content: String::new(),
            reasoning: String::new(),
            id: Value::Null,
            model: String::new(),
            usage: RawJson::default(),
            search: Value::Null,
            memory_suggestions: Vec::new(),
            finish_reason: String::new(),
        }
    }
}

impl ChatStreamAccumulator {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn content(&self) -> &str {
        &self.content
    }

    pub fn reasoning(&self) -> &str {
        &self.reasoning
    }

    pub fn finish_reason(&self) -> &str {
        &self.finish_reason
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    /// The accumulated usage, as a value.
    ///
    /// Returned as a `Value` rather than the [`RawJson`] the terminal event carries,
    /// because a caller that reads a counter out of it (the diagnostics block) needs a
    /// map, and the byte order that matters is the one written into the event.
    pub fn usage(&self) -> Value {
        self.usage.to_value()
    }

    /// `response_id = chunk.get("id") or response_id` — a falsy id does not replace
    /// the one already seen, which is why this is a truthiness check and not a
    /// presence check.
    pub fn observe_id(&mut self, id: Option<&Value>) {
        if let Some(value) = id.filter(|value| crate::core_utils::python_truthy(value)) {
            self.id = value.clone();
        }
    }

    /// `response_model = chunk.get("model") or response_model`.
    pub fn observe_model(&mut self, model: Option<&Value>) {
        if let Some(value) = model.filter(|value| crate::core_utils::python_truthy(value)) {
            self.model = crate::python_json::value_str(value);
        }
    }

    /// `if choices[0].get("finish_reason"): round_finish = str(...)`.
    pub fn observe_finish_reason(&mut self, reason: Option<&Value>) {
        if let Some(value) = reason.filter(|value| crate::core_utils::python_truthy(value)) {
            self.finish_reason = crate::python_json::value_str(value);
        }
    }

    /// `if isinstance(chunk.get("usage"), dict): round_usage = chunk["usage"]`, then
    /// `usage = merge_usage_totals(usage, round_usage)`.
    pub fn observe_usage(&mut self, usage: Option<&Value>) {
        if let Some(value) = usage.filter(|value| value.is_object()) {
            self.usage = RawJson::from_value(&merge_usage_totals(&self.usage.to_value(), value));
        }
    }

    pub fn observe_search(&mut self, search: Value) {
        self.search = search;
    }

    pub fn push_memory_suggestion(&mut self, suggestion: Value) {
        self.memory_suggestions.push(suggestion);
    }

    /// Append a reasoning delta and return the event to emit for it.
    pub fn reasoning_delta(&mut self, text: &str) -> ChatEvent {
        self.reasoning.push_str(text);
        ChatEvent::Reasoning {
            text: text.to_string(),
        }
    }

    /// Append a content delta and return the event to emit for it.
    pub fn content_delta(&mut self, text: &str) -> ChatEvent {
        self.content.push_str(text);
        ChatEvent::Content {
            text: text.to_string(),
        }
    }

    /// The terminal event, carrying everything accumulated.
    pub fn done(&self, diagnostics: Value) -> ChatEvent {
        ChatEvent::Done(Box::new(ChatDone {
            id: self.id.clone(),
            model: self.model.clone(),
            content: self.content.clone(),
            reasoning: self.reasoning.clone(),
            usage: self.usage.clone(),
            search: self.search.clone(),
            memory_suggestions: Value::Array(self.memory_suggestions.clone()),
            finish_reason: self.finish_reason.clone(),
            diagnostics,
        }))
    }
}

/// The accumulator `merge_stream_tool_call_deltas` fills: index → call.
///
/// A `BTreeMap` because `finalized_stream_tool_calls` reads the entries in **sorted
/// index order** (`for index in sorted(accumulator)`), and the upstream may deliver the
/// indices out of order.
///
/// The key is a **signed** integer: the oracle does `int(index_value)` with no
/// non-negativity check, so a negative index is a legal (if unusual) key and sorts
/// first. The parity probe measured exactly that — a `>= 0` filter here produced
/// `call_1`/`call_2` where the oracle produced `call_-3`/`call_1`.
pub type StreamToolCalls = std::collections::BTreeMap<i64, Value>;

/// The placeholder a new index starts from.
fn empty_stream_tool_call(index: i64) -> Value {
    json!({
        "id": format!("call_{}", index + 1),
        "type": "function",
        "function": {"name": "", "arguments": ""},
    })
}

/// `merge_stream_tool_call_deltas`: fold one chunk's `tool_calls` deltas in.
///
/// The upstream streams a tool call in pieces — the id and name once, the arguments
/// character by character — so this appends rather than replaces. Two details are the
/// oracle's and are easy to get wrong:
///
/// - **A missing or unparseable `index` is `len(accumulator)`**, not an error and not
///   zero: the call is appended after what has been seen.
/// - **An empty `id`, `type`, `name` or `arguments` is falsy and therefore ignored**,
///   so a later chunk cannot blank a field an earlier one set, and an empty arguments
///   fragment does not count as progress.
pub fn merge_stream_tool_call_deltas(accumulator: &mut StreamToolCalls, deltas: Option<&Value>) {
    let Some(Value::Array(items)) = deltas else {
        return;
    };
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let index = match object.get("index") {
            None | Some(Value::Null) => accumulator.len() as i64,
            Some(value) => crate::python_json::value_str(value)
                .trim()
                .parse::<i64>()
                .unwrap_or(accumulator.len() as i64),
        };
        let entry = accumulator
            .entry(index)
            .or_insert_with(|| empty_stream_tool_call(index));
        if let Some(id) = object
            .get("id")
            .filter(|value| crate::core_utils::python_truthy(value))
        {
            entry["id"] = json!(crate::python_json::value_str(id));
        }
        if let Some(kind) = object
            .get("type")
            .filter(|value| crate::core_utils::python_truthy(value))
        {
            entry["type"] = json!(crate::python_json::value_str(kind));
        }
        let Some(function) = object.get("function").and_then(Value::as_object) else {
            continue;
        };
        if let Some(name) = function
            .get("name")
            .filter(|value| crate::core_utils::python_truthy(value))
        {
            entry["function"]["name"] = json!(crate::python_json::value_str(name));
        }
        if let Some(arguments) = function
            .get("arguments")
            .filter(|value| crate::core_utils::python_truthy(value))
        {
            let appended = format!(
                "{}{}",
                entry["function"]["arguments"].as_str().unwrap_or_default(),
                crate::python_json::value_str(arguments)
            );
            entry["function"]["arguments"] = json!(appended);
        }
    }
}

/// `finalized_stream_tool_calls`: the accumulated calls in index order, normalized.
///
/// `normalize_tool_calls` drops an entry with no name, which is how a half-received
/// call disappears rather than being sent back with an empty name. The ids and argument
/// JSON are preserved as the upstream sent them, because the prompt cache can only
/// reuse the prefix when the assistant `tool_calls` sent back match the model's own
/// output.
pub fn finalized_stream_tool_calls(accumulator: &StreamToolCalls) -> Vec<Value> {
    let calls: Vec<Value> = accumulator.values().cloned().collect();
    crate::request_messages::normalize_tool_calls(Some(&Value::Array(calls)), false, false)
}

/// `USAGE_SUM_FIELDS`: the five token counters, each with its camelCase alias.
pub const USAGE_SUM_FIELDS: [(&str, &str); 5] = [
    ("prompt_tokens", "promptTokens"),
    ("completion_tokens", "completionTokens"),
    ("total_tokens", "totalTokens"),
    ("prompt_cache_hit_tokens", "promptCacheHitTokens"),
    ("prompt_cache_miss_tokens", "promptCacheMissTokens"),
];

/// `usage_int`: the first present, non-empty, parseable name, floored at zero.
///
/// `max(0, int(raw))` — a negative counter becomes zero, a non-numeric string is
/// skipped in favour of the alias, and a value that parses nowhere is zero. Python's
/// `int()` truncates toward zero, which is why `int(2.7)` is `2`.
pub fn usage_int(usage: &Value, canonical: &str, alias: &str) -> i64 {
    let Some(map) = usage.as_object() else {
        return 0;
    };
    for name in [canonical, alias] {
        let Some(raw) = map.get(name) else {
            continue;
        };
        if raw.is_null() {
            continue;
        }
        let parsed = match raw {
            Value::String(text) if text.is_empty() => continue,
            Value::String(text) => text.trim().parse::<f64>().ok(),
            Value::Number(number) => number.as_f64(),
            Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
            _ => None,
        };
        if let Some(number) = parsed {
            return (number.trunc() as i64).max(0);
        }
    }
    0
}

/// `merge_usage_totals`: sum the five token counters across rounds.
///
/// Only those five fields are summed. Everything else in the round's usage — a cache
/// flag, a cost, an unknown vendor field — is **dropped**, because the oracle starts
/// from `dict(total)` and never copies the round's other keys. A round that is not a
/// non-empty mapping leaves the total untouched.
pub fn merge_usage_totals(total: &Value, usage: &Value) -> Value {
    let empty = serde_json::Map::new();
    let total = total.as_object().unwrap_or(&empty);
    let Some(usage_map) = usage.as_object().filter(|map| !map.is_empty()) else {
        return Value::Object(total.clone());
    };
    let mut result = total.clone();
    for (canonical, alias) in USAGE_SUM_FIELDS {
        let value = usage_int(&Value::Object(usage_map.clone()), canonical, alias);
        if value != 0 {
            let existing = usage_int(&Value::Object(result.clone()), canonical, alias);
            result.insert(canonical.to_string(), json!(existing + value));
        }
    }
    Value::Object(result)
}

impl ChatEvent {
    /// Unwrap a `Done` event. Test-only convenience for asserting on the payload.
    #[doc(hidden)]
    pub fn into_done(self) -> ChatDone {
        match self {
            ChatEvent::Done(done) => *done,
            other => panic!("not a done event: {other:?}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_event_encodes_to_the_oracles_bytes() {
        assert_eq!(
            String::from_utf8(encode_stream_event(&ChatEvent::SystemNote {
                text: "hi\n\n".to_string()
            }))
            .unwrap(),
            "{\"type\":\"system_note\",\"text\":\"hi\\n\\n\"}\n"
        );
        assert_eq!(
            String::from_utf8(encode_stream_event(&ChatEvent::Content {
                text: "你好".to_string()
            }))
            .unwrap(),
            // `ensure_ascii=False`: non-ASCII is not escaped.
            "{\"type\":\"content\",\"text\":\"你好\"}\n"
        );
        // The event envelope's own field order is the oracle's (`type` first) and is
        // asserted as bytes. The `search` *value* is the caller's, and its key order is
        // not the encoder's to guarantee: `serde_json` is built with `preserve_order`
        // only when the packages that enable it are in the graph, so the same call
        // sorts these keys under `-p deepseek-policy` and keeps the caller's order under
        // `--workspace`. Asserting either order as bytes would make this test depend on
        // build scope — measured on one machine, both ways, before it was rewritten.
        let line = String::from_utf8(encode_stream_event(&ChatEvent::Search {
            search: json!({"status": "done", "results": 2}),
        }))
        .unwrap();
        assert!(
            line.starts_with("{\"type\":\"search\",\"search\":{") && line.ends_with("}\n"),
            "the envelope order is the encoder's: {line}"
        );
        let parsed: Value =
            serde_json::from_str(line.trim_end()).expect("one JSON object per line");
        assert_eq!(
            parsed,
            json!({"type": "search", "search": {"status": "done", "results": 2}})
        );
        assert_eq!(
            String::from_utf8(encode_stream_event(&ChatEvent::Error {
                error: "boom".to_string(),
                code: "internal".to_string()
            }))
            .unwrap(),
            "{\"type\":\"error\",\"error\":\"boom\",\"code\":\"internal\"}\n"
        );
    }

    #[test]
    fn the_done_event_always_carries_every_field() {
        let accumulator = ChatStreamAccumulator::new();
        let encoded = String::from_utf8(encode_stream_event(&accumulator.done(json!({})))).unwrap();
        assert_eq!(
            encoded,
            "{\"type\":\"done\",\"id\":null,\"model\":\"\",\"content\":\"\",\"reasoning\":\"\",\
             \"usage\":{},\"search\":null,\"memorySuggestions\":[],\"finishReason\":\"\",\
             \"diagnostics\":{}}\n"
        );
    }

    #[test]
    fn the_accumulator_grows_the_totals_and_the_done_event_repeats_them() {
        let mut accumulator = ChatStreamAccumulator::new();
        assert_eq!(
            accumulator.reasoning_delta("think "),
            ChatEvent::Reasoning {
                text: "think ".to_string()
            }
        );
        accumulator.reasoning_delta("more");
        accumulator.content_delta("hello ");
        accumulator.content_delta("world");
        accumulator.observe_id(Some(&json!("resp-1")));
        accumulator.observe_model(Some(&json!("deepseek-v4-flash")));
        accumulator.observe_finish_reason(Some(&json!("stop")));
        accumulator.observe_usage(Some(&json!({"prompt_tokens": 10, "completion_tokens": 4})));

        let ChatEvent::Done(done) = accumulator.done(json!({"a": 1})) else {
            panic!("done is the terminal event");
        };
        assert_eq!(done.content, "hello world");
        assert_eq!(done.reasoning, "think more");
        assert_eq!(done.id, json!("resp-1"));
        assert_eq!(done.model, "deepseek-v4-flash");
        assert_eq!(done.finish_reason, "stop");
        assert_eq!(done.usage.to_value()["prompt_tokens"], 10);
        assert_eq!(done.diagnostics, json!({"a": 1}));
        // The deltas were emitted as they arrived *and* accumulated.
        assert_eq!(accumulator.content(), "hello world");
    }

    #[test]
    fn a_falsy_id_or_model_does_not_replace_what_was_seen() {
        let mut accumulator = ChatStreamAccumulator::new();
        accumulator.observe_id(Some(&json!("resp-1")));
        accumulator.observe_model(Some(&json!("m1")));
        // `chunk.get("id") or response_id`: an empty string and a null are falsy.
        accumulator.observe_id(Some(&json!("")));
        accumulator.observe_id(Some(&Value::Null));
        accumulator.observe_id(None);
        accumulator.observe_model(Some(&json!("")));
        assert_eq!(
            accumulator.done(json!({})).clone().into_done().id,
            json!("resp-1")
        );
        assert_eq!(accumulator.model(), "m1");
    }

    #[test]
    fn a_memory_suggestion_spreads_its_own_keys_after_the_type() {
        // `{"type": "memory_suggestion", **suggestion}`.
        let event = ChatEvent::MemorySuggestion {
            suggestion: json!({"content": "记住我喜欢喝咖啡", "type": "instruction"}),
        };
        assert_eq!(
            compact_event_json(&event),
            // The suggestion's own `type` wins, and keeps the first position, which is
            // what Python's dict does when a key is overwritten.
            "{\"type\":\"instruction\",\"content\":\"记住我喜欢喝咖啡\"}"
        );
        let event = ChatEvent::MemorySuggestion {
            suggestion: json!({"content": "x"}),
        };
        assert_eq!(
            compact_event_json(&event),
            "{\"type\":\"memory_suggestion\",\"content\":\"x\"}"
        );
    }

    #[test]
    fn stream_tool_call_deltas_accumulate_in_index_order() {
        let mut calls = StreamToolCalls::new();
        // The upstream streams the id and name first, then the arguments in pieces.
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{
                "index": 0,
                "id": "call_abc",
                "type": "function",
                "function": {"name": "create_document", "arguments": "{\"title\":"}
            }])),
        );
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{"index": 0, "function": {"arguments": "\"x\"}"}}])),
        );
        // A second call at a *higher* index, delivered before a lower one, still
        // finalizes in sorted order.
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([
                {"index": 2, "id": "call_2", "function": {"name": "b", "arguments": "{}"}},
                {"index": 1, "id": "call_1", "function": {"name": "a", "arguments": "{}"}},
            ])),
        );
        let finalized = finalized_stream_tool_calls(&calls);
        assert_eq!(finalized.len(), 3);
        assert_eq!(finalized[0]["id"], "call_abc");
        assert_eq!(finalized[0]["function"]["name"], "create_document");
        assert_eq!(finalized[0]["function"]["arguments"], "{\"title\":\"x\"}");
        assert_eq!(finalized[1]["function"]["name"], "a");
        assert_eq!(finalized[2]["function"]["name"], "b");
    }

    #[test]
    fn a_missing_or_unparseable_index_appends_at_the_end() {
        let mut calls = StreamToolCalls::new();
        // No index at all: `len(accumulator)` is 0.
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{"function": {"name": "first", "arguments": "{}"}}])),
        );
        // A string index parses; an unparseable one appends after what is there.
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{"index": "1", "function": {"name": "second", "arguments": "{}"}}])),
        );
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(
                &json!([{"index": "not a number", "function": {"name": "third", "arguments": "{}"}}]),
            ),
        );
        let finalized = finalized_stream_tool_calls(&calls);
        assert_eq!(
            finalized
                .iter()
                .map(|call| call["function"]["name"].as_str().unwrap_or_default())
                .collect::<Vec<_>>(),
            vec!["first", "second", "third"]
        );
        // The placeholder id is `call_{index+1}` when the upstream never sent one.
        assert_eq!(finalized[0]["id"], "call_1");
    }

    #[test]
    fn an_empty_fragment_never_blanks_a_field() {
        let mut calls = StreamToolCalls::new();
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{
                "index": 0,
                "id": "call_keep",
                "type": "function",
                "function": {"name": "keep", "arguments": "{}"}
            }])),
        );
        // Every field empty or falsy: nothing changes.
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{
                "index": 0,
                "id": "",
                "type": "",
                "function": {"name": "", "arguments": ""}
            }])),
        );
        let finalized = finalized_stream_tool_calls(&calls);
        assert_eq!(finalized[0]["id"], "call_keep");
        assert_eq!(finalized[0]["function"]["name"], "keep");
        assert_eq!(finalized[0]["function"]["arguments"], "{}");
    }

    #[test]
    fn a_call_with_no_name_is_dropped_and_a_non_list_is_ignored() {
        let mut calls = StreamToolCalls::new();
        // A half-received call: an index and arguments but no name. The accumulator
        // keeps the entry — the oracle's `setdefault` created it — and the *finalizer*
        // is what drops it, via `normalize_tool_calls`.
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([{"index": 0, "function": {"arguments": "{}"}}])),
        );
        assert_eq!(calls.len(), 1);
        assert!(finalized_stream_tool_calls(&calls).is_empty());

        // A non-list, a list of non-objects, and `None` add nothing.
        let mut ignored = StreamToolCalls::new();
        merge_stream_tool_call_deltas(&mut ignored, Some(&json!("not a list")));
        merge_stream_tool_call_deltas(&mut ignored, Some(&json!(["not an object", 5])));
        merge_stream_tool_call_deltas(&mut ignored, None);
        assert!(ignored.is_empty());
    }

    #[test]
    fn a_negative_index_is_a_legal_key_and_sorts_first() {
        // `int(index_value)` has no non-negativity check, so `-4` is stored as `-4`
        // and its placeholder id is `call_-3`. The parity probe measured this.
        let mut calls = StreamToolCalls::new();
        merge_stream_tool_call_deltas(
            &mut calls,
            Some(&json!([
                {"index": null, "function": {"name": "n", "arguments": "{}"}},
                {"index": -4, "function": {"name": "neg", "arguments": "{}"}},
            ])),
        );
        let finalized = finalized_stream_tool_calls(&calls);
        assert_eq!(finalized[0]["id"], "call_-3");
        assert_eq!(finalized[0]["function"]["name"], "neg");
        assert_eq!(finalized[1]["id"], "call_1");
        assert_eq!(finalized[1]["function"]["name"], "n");
    }

    #[test]
    fn usage_totals_sum_only_the_five_token_counters() {
        let total = merge_usage_totals(
            &json!({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}),
            &json!({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        );
        assert_eq!(total["prompt_tokens"], 13);
        assert_eq!(total["completion_tokens"], 6);
        assert_eq!(total["total_tokens"], 19);
        // The camelCase alias is read when the snake_case name is absent, and the
        // result is written under the canonical name.
        let total = merge_usage_totals(&json!({}), &json!({"promptTokens": 7}));
        assert_eq!(total["prompt_tokens"], 7);
        assert!(total.get("promptTokens").is_none());
        // A field the oracle does not sum is dropped from the round, not merged in.
        let total = merge_usage_totals(&json!({"cache": "miss"}), &json!({"cache": "hit"}));
        assert_eq!(total["cache"], "miss");
        // A round that is empty or not a mapping leaves the total untouched.
        assert_eq!(
            merge_usage_totals(&json!({"a": 1}), &json!({})),
            json!({"a": 1})
        );
        assert_eq!(
            merge_usage_totals(&json!({"a": 1}), &json!("scalar")),
            json!({"a": 1})
        );
        // A non-numeric string falls through to the alias, then to zero; a negative
        // counter is floored at zero.
        assert_eq!(
            merge_usage_totals(
                &json!({}),
                &json!({"prompt_tokens": "x", "promptTokens": 4})
            ),
            json!({"prompt_tokens": 4})
        );
        assert_eq!(
            merge_usage_totals(&json!({"prompt_tokens": 5}), &json!({"prompt_tokens": -3})),
            json!({"prompt_tokens": 5})
        );
        // `int(2.7)` is `2`, and an empty string is skipped.
        assert_eq!(
            merge_usage_totals(&json!({}), &json!({"prompt_tokens": 2.7})),
            json!({"prompt_tokens": 2})
        );
        assert_eq!(
            merge_usage_totals(&json!({}), &json!({"prompt_tokens": ""})),
            json!({})
        );
    }
}
