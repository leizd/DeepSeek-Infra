//! The part of `gateway/edge_inference.py` that the model router consumes.
//!
//! The oracle module is 529 lines and mostly about *edge routing* — providers, quantisation,
//! local/cloud decisions — none of which is ported here. What is here is the surface
//! `model_router` reaches for: three query-shape patterns and two payload readers. Naming it
//! after its module rather than hiding it inside the router keeps that boundary visible: the
//! edge-routing half is its own slice, and when it is ported these items stay where the
//! oracle puts them.
//!
//! The patterns are exposed as **text** as well as compiled, because they contain CJK
//! literals that would otherwise be transcribed blind: the parity probe compares the pattern
//! strings themselves against the oracle's, so a wrong character cannot pass as a behaviour
//! difference.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

/// Mirrors `COMPLEX_QUERY_RE`'s pattern, without the `IGNORECASE` flag.
pub const COMPLEX_QUERY_PATTERN: &str = r"```|\b(code|debug|bug|traceback|exception|leetcode|algorithm|proof|integral|derivative|matrix|equation|sql|regex|api|fastapi|flask)\b|代码|编程|调试|报错|算法|证明|数学|积分|微分|方程";

/// Mirrors `ARTIFACT_QUERY_RE`'s pattern.
pub const ARTIFACT_QUERY_PATTERN: &str =
    r"\b(ppt|powerpoint|presentation|mindmap|mind map|docx|pdf)\b";

/// Mirrors `SIMPLE_TASK_RE`'s pattern.
pub const SIMPLE_TASK_PATTERN: &str = r"\b(hi|hello|chat|summarize|summary|rewrite|polish|translate|explain|outline)\b|你好|闲聊|总结|概括|提炼|改写|润色|翻译|解释";

/// Mirrors `chat_messages_from_payload`: the payload's messages, dicts only.
pub fn chat_messages_from_payload(payload: &Value) -> Vec<Value> {
    match payload.get("messages") {
        Some(Value::Array(messages)) => messages
            .iter()
            .filter(|message| message.is_object())
            .cloned()
            .collect(),
        _ => Vec::new(),
    }
}

/// Mirrors `has_image_attachment`.
///
/// This is the **attachment** test, not the content-parts test: it looks for a `data:image/`
/// `imageData` field on any message's attachments, across the whole history. The router uses
/// it to decide capability, while `request_shaping::has_image_content` looks at the assembled
/// message parts instead — two different questions that happen to share a name in the oracle.
pub fn has_image_attachment(payload: &Value) -> bool {
    for message in chat_messages_from_payload(payload) {
        let Some(Value::Array(attachments)) = message.get("attachments") else {
            continue;
        };
        for attachment in attachments {
            if !attachment.is_object() {
                continue;
            }
            if text_or_empty(attachment.get("imageData")).starts_with("data:image/") {
                return true;
            }
        }
    }
    false
}

pub fn complex_query_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| compiled(COMPLEX_QUERY_PATTERN))
}

pub fn artifact_query_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| compiled(ARTIFACT_QUERY_PATTERN))
}

pub fn simple_task_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| compiled(SIMPLE_TASK_PATTERN))
}

/// The oracle compiles these with `re.IGNORECASE`; the inline flag is the Rust spelling of
/// the same thing.
fn compiled(pattern: &str) -> Regex {
    Regex::new(&format!("(?i){pattern}")).expect("static pattern must compile")
}

/// `str(value or "")` without the strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if crate::core_utils::python_truthy(found) => {
            crate::python_json::value_str(found)
        }
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn only_an_attachment_with_image_data_counts() {
        let with = json!({"messages": [{"role": "user", "attachments": [
            {"imageData": "data:image/png;base64,AA"},
        ]}]});
        assert!(has_image_attachment(&with));

        // A `http` url is not an inline image, an empty string is not either, and a content
        // part is a different question entirely (see request_shaping::has_image_content).
        let url =
            json!({"messages": [{"role": "user", "attachments": [{"imageData": "http://x"}]}]});
        assert!(!has_image_attachment(&url));
        let parts = json!({"messages": [{"role": "user", "content": [{"type": "image_url"}]}]});
        assert!(!has_image_attachment(&parts));
        assert!(!has_image_attachment(&json!({})));
    }

    #[test]
    fn payload_messages_keep_only_the_objects() {
        let payload = json!({"messages": ["x", {"role": "user"}, 5]});
        assert_eq!(
            chat_messages_from_payload(&payload),
            vec![json!({"role": "user"})]
        );
        assert!(chat_messages_from_payload(&json!({"messages": "not-a-list"})).is_empty());
    }

    #[test]
    fn the_patterns_are_exposed_as_text_so_the_probe_can_compare_them() {
        assert!(COMPLEX_QUERY_PATTERN.contains("代码"));
        assert!(SIMPLE_TASK_PATTERN.contains("你好"));
        assert!(complex_query_regex().is_match("帮我写代码"));
        assert!(artifact_query_regex().is_match("做一份 PPT"));
        assert!(!simple_task_regex().is_match("随便聊聊"));
    }
}
