//! Request-shaping helpers: what the assembled body carries besides its messages.
//!
//! These are the pure leaves of `build_deepseek_request`'s dependency closure — the pieces
//! that need no I/O and no unported subsystem, ported so the assembly itself is one step
//! closer to existing. Mirrors `deepseek_client.py`'s tool selection, artifact forcing,
//! effort normalisation and image detection, plus `chat_payload.py`'s attachment count.
//!
//! `empty_memory_state` and `memory_scope_from_payload` live in [`crate::memory`], next to
//! the scope normaliser they build on.

use serde_json::Value;

use crate::core_utils::{latest_user_query, python_truthy};
use crate::dynamic_context::presentation_intent_requested;
use crate::python_json::value_str;
use crate::search::search_tool_enabled;
use crate::tool_catalog::agent_tool_definitions;

use std::sync::OnceLock;

use regex::Regex;

/// The turn-level nudge toward parallel tool calls and local tools.
///
/// Written with `concat!` and explicit escapes rather than as a multi-line raw string: a
/// raw string would take its line endings from the source file, and this repository is
/// checked out with CRLF on Windows.
pub const TOOL_PARALLEL_SYSTEM_HINT: &str = concat!(
    "当需要多个独立信息时（如查询多个不同 URL、多个不同文件），请在同一回复中并行发起多个工具调用，而不是一轮一个。",
    "当某个本地工具能直接产出用户想要的成果时，必须调用该工具，不要用文本或 Markdown 自行模拟其结果——",
    "用户要求制作 PPT / 幻灯片 / 演示文稿时调用 create_pptx 生成可下载文件，不要输出 Marp / Markdown 幻灯片大纲来代替；需要图表时调用 generate_chart。"
);

/// The effort levels `normalize_reasoning_effort` accepts, verbatim.
const REASONING_EFFORTS: [&str; 5] = ["minimal", "low", "medium", "high", "max"];

/// Tools dropped from the request when search is off for this turn.
const SEARCH_ONLY_TOOLS: [&str; 2] = ["web_search", "compare_search_results"];

/// Mirrors `normalize_reasoning_effort`: an unknown or absent effort becomes `high` rather
/// than being passed through, so the upstream never sees a level it does not know.
pub fn normalize_reasoning_effort(value: Option<&Value>) -> &'static str {
    let effort = text_or_empty(value);
    let effort = effort.trim();
    if REASONING_EFFORTS.contains(&effort) {
        return REASONING_EFFORTS
            .iter()
            .find(|candidate| **candidate == effort)
            .expect("just matched");
    }
    "high"
}

/// Mirrors `_has_image_content`: a message counts as multimodal only when its `content` is
/// a *list* carrying an `image_url` part. A string content that merely mentions an image
/// data URL does not — which is what keeps the vision model off for text-only turns.
pub fn has_image_content(api_messages: &[Value]) -> bool {
    api_messages
        .iter()
        .any(|message| match message.get("content") {
            Some(Value::Array(parts)) => parts
                .iter()
                .any(|part| part.get("type") == Some(&Value::String("image_url".to_string()))),
            _ => false,
        })
}

/// The function-name of a tool definition, or `""` when it has none.
fn function_name(tool: &Value) -> String {
    text_or_empty(tool.get("function").and_then(|found| found.get("name")))
}

/// Mirrors `tools_for_payload`.
///
/// Two filters compose, and their order matters for what the caller sees: an explicit
/// `allowedTools` list wins first, then the search tools are dropped when search is off
/// for the turn. An `allowedTools` list that names `web_search` therefore still loses it.
pub fn tools_for_payload(payload: &Value) -> Vec<Value> {
    let mut tools = agent_tool_definitions(None);
    if let Some(Value::Array(allowed)) = payload.get("allowedTools") {
        let allowed_names: Vec<String> = allowed.iter().map(value_str).collect();
        tools.retain(|tool| allowed_names.contains(&function_name(tool)));
    }
    if search_tool_enabled(payload) {
        return tools;
    }
    tools.retain(|tool| !SEARCH_ONLY_TOOLS.contains(&function_name(tool).as_str()));
    tools
}

/// Mirrors `should_force_create_pptx`, which is `presentation_intent_requested` under
/// another name. Both exist in the oracle; only one is needed here, and callers reading
/// either name should find the same predicate.
pub fn should_force_create_pptx(payload: &Value) -> bool {
    presentation_intent_requested(payload)
}

/// Mirrors `has_create_pptx_tool`.
pub fn has_create_pptx_tool(tools: &[Value]) -> bool {
    tools
        .iter()
        .any(|tool| function_name(tool) == "create_pptx")
}

/// Mirrors `mindmap_intent_requested`: the same two-gate shape as the slides check — name
/// a mind map *and* ask for one to be made.
pub fn mindmap_intent_requested(payload: &Value) -> bool {
    let query = latest_user_query(payload);
    if query.is_empty() || !mindmap_keywords_regex().is_match(&query) {
        return false;
    }
    mindmap_create_regex().is_match(&query)
}

/// Mirrors `forced_artifact_tool_name`.
///
/// The `allowedTools` fallback is the subtle part: when the payload carries no list the
/// allowed set *is* the available set, so forcing is governed by availability alone, and
/// when it does carry one a tool has to appear in both.
pub fn forced_artifact_tool_name(payload: &Value, tools: &[Value]) -> &'static str {
    if payload.get("toolsEnabled") == Some(&Value::Bool(false)) {
        return "";
    }
    let available: Vec<String> = tools.iter().map(function_name).collect();
    let allowed: Option<Vec<String>> = match payload.get("allowedTools") {
        Some(Value::Array(items)) => Some(items.iter().map(value_str).collect()),
        _ => None,
    };
    let permitted = |name: &str| match &allowed {
        Some(names) => names.iter().any(|candidate| candidate == name),
        None => true,
    };
    let listed = |name: &str| available.iter().any(|candidate| candidate == name);

    if presentation_intent_requested(payload) && listed("create_pptx") && permitted("create_pptx") {
        return "create_pptx";
    }
    if mindmap_intent_requested(payload) && listed("create_mindmap") && permitted("create_mindmap")
    {
        return "create_mindmap";
    }
    ""
}

/// Mirrors `count_payload_attachments` (`chat_payload.py`): every dict entry under any
/// message's `attachments` list, across the whole history — not just the latest turn.
pub fn count_payload_attachments(messages: Option<&Value>) -> usize {
    let Some(Value::Array(messages)) = messages else {
        return 0;
    };
    messages
        .iter()
        .map(|message| match message.get("attachments") {
            Some(Value::Array(attachments)) => {
                attachments.iter().filter(|item| item.is_object()).count()
            }
            _ => 0,
        })
        .sum()
}

/// `str(value or "")` without the strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

fn mindmap_keywords_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"(?i)思维导图|腦圖|脑图|mind\s*map|mindmap").expect("static pattern")
    })
}

fn mindmap_create_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(
            r"(?i)画|畫|做|生成|创建|建立|绘制|梳理|整理|导出|create|make|draw|generate|build|map",
        )
        .expect("static pattern")
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tool(name: &str) -> Value {
        json!({"type": "function", "function": {"name": name}})
    }

    fn names(tools: &[Value]) -> Vec<String> {
        tools.iter().map(function_name).collect()
    }

    #[test]
    fn the_parallel_hint_is_one_line_so_a_caller_can_join_it() {
        assert!(!TOOL_PARALLEL_SYSTEM_HINT.contains('\n'));
        assert!(TOOL_PARALLEL_SYSTEM_HINT.contains("create_pptx"));
    }

    #[test]
    fn an_unknown_effort_becomes_high_rather_than_being_passed_through() {
        for level in REASONING_EFFORTS {
            assert_eq!(normalize_reasoning_effort(Some(&json!(level))), level);
        }
        // Case matters: `MEDIUM` is not in the set, so it falls back like anything else.
        assert_eq!(normalize_reasoning_effort(Some(&json!("MEDIUM"))), "high");
        assert_eq!(normalize_reasoning_effort(Some(&json!("  high  "))), "high");
        for junk in [json!(5), json!(true), json!(""), json!([])] {
            assert_eq!(normalize_reasoning_effort(Some(&junk)), "high");
        }
        assert_eq!(normalize_reasoning_effort(None), "high");
    }

    #[test]
    fn only_a_content_list_carrying_an_image_part_counts_as_vision() {
        let image = json!([{"type": "image_url", "image_url": {"url": "u"}}]);
        assert!(has_image_content(&[json!({"content": image})]));
        // A string that happens to contain the data URL is still a text turn.
        assert!(!has_image_content(&[
            json!({"content": "data:image/png;base64,AAAA"})
        ]));
        assert!(!has_image_content(&[
            json!({"content": [{"type": "text", "text": "a"}]})
        ]));
        assert!(!has_image_content(&[]));
    }

    #[test]
    fn the_search_tools_are_dropped_when_search_is_off_for_the_turn() {
        let off = tools_for_payload(&json!({}));
        assert!(!names(&off).contains(&"web_search".to_string()));
        assert!(!names(&off).contains(&"compare_search_results".to_string()));

        let on = tools_for_payload(&json!({"searchEnabled": true, "searchMode": "on"}));
        assert!(names(&on).contains(&"web_search".to_string()));
        assert!(on.len() > off.len());
    }

    #[test]
    fn an_allowed_tools_list_is_applied_before_the_search_filter() {
        // Both filters compose, and the search filter runs last: naming web_search in the
        // allow-list does not keep it when search is off.
        let payload = json!({"allowedTools": ["create_pptx", "web_search"]});
        assert_eq!(names(&tools_for_payload(&payload)), vec!["create_pptx"]);

        let payload = json!({"allowedTools": ["create_pptx", "web_search"], "searchEnabled": true, "searchMode": "on"});
        assert_eq!(
            names(&tools_for_payload(&payload)),
            vec!["web_search", "create_pptx"]
        );
    }

    #[test]
    fn a_non_list_allowed_tools_is_ignored_rather_than_treated_as_empty() {
        let payload = json!({"allowedTools": "create_pptx"});
        assert!(tools_for_payload(&payload).len() > 1);
    }

    #[test]
    fn forcing_an_artifact_needs_availability_and_permission() {
        let deck = json!({"messages": [{"role": "user", "content": "帮我做一份 PPT"}]});
        let catalog = vec![tool("create_pptx"), tool("create_mindmap")];
        assert_eq!(forced_artifact_tool_name(&deck, &catalog), "create_pptx");

        // `toolsEnabled: false` wins over everything else.
        let mut disabled = deck.clone();
        disabled["toolsEnabled"] = json!(false);
        assert_eq!(forced_artifact_tool_name(&disabled, &catalog), "");

        // Allowed, but the tool is not in the list handed to the upstream.
        assert_eq!(
            forced_artifact_tool_name(&deck, &[tool("create_mindmap")]),
            ""
        );

        // Available, but the payload's allow-list excludes it.
        let mut restricted = deck.clone();
        restricted["allowedTools"] = json!(["web_search"]);
        assert_eq!(forced_artifact_tool_name(&restricted, &catalog), "");

        // Without an allow-list, availability alone decides.
        let mut allowed = deck.clone();
        allowed["allowedTools"] = json!(["create_pptx"]);
        assert_eq!(forced_artifact_tool_name(&allowed, &catalog), "create_pptx");
    }

    #[test]
    fn a_mindmap_question_without_a_verb_still_requests_one_because_the_verb_list_has_map() {
        // The oracle's create-verb alternation contains `map`, and `mindmap` contains it,
        // so the keyword gate alone is enough for this spelling. Kept as-is: the two sides
        // agree, and "fixing" it here would diverge from the oracle rather than from a bug.
        let payload = json!({"messages": [{"role": "user", "content": "什么是 mindmap？"}]});
        assert!(mindmap_intent_requested(&payload));

        let plain = json!({"messages": [{"role": "user", "content": "列出文件"}]});
        assert!(!mindmap_intent_requested(&plain));
    }

    #[test]
    fn the_create_pptx_alias_is_the_same_predicate() {
        let deck = json!({"messages": [{"role": "user", "content": "帮我做一份 PPT"}]});
        assert_eq!(
            should_force_create_pptx(&deck),
            presentation_intent_requested(&deck)
        );
        assert!(has_create_pptx_tool(&[tool("create_pptx")]));
        assert!(!has_create_pptx_tool(&[tool("create_mindmap")]));
    }

    #[test]
    fn attachments_are_counted_across_the_whole_history_and_only_when_they_are_objects() {
        let messages = json!([
            {"attachments": [{"a": 1}, {"b": 2}]},
            {"attachments": "x"},
            {"attachments": [1, "y", {"z": 3}]},
            {"role": "user"},
        ]);
        assert_eq!(count_payload_attachments(Some(&messages)), 3);
        assert_eq!(count_payload_attachments(Some(&json!({}))), 0);
        assert_eq!(count_payload_attachments(None), 0);
    }
}
