//! Title generation, mirroring `deepseek_infra/infra/gateway/title_generator.py`.
//!
//! `POST /api/title` is what names a conversation after its first exchange. The
//! oracle reaches the upstream with a small non-streaming call and then sanitises
//! whatever came back; the sanitisation is where the observable behaviour lives, and
//! it is pure, so it lives here with a parity probe.
//!
//! # What is here and what is not
//!
//! - **Here:** the prompt, the request body the upstream receives, the truncation
//!   limits, the sanitiser, and the rate limiter. All of it is decided by the oracle's
//!   own constants and none of it needs a socket.
//! - **Not here:** the HTTP call. `deepseek-policy` has no transport dependency on
//!   purpose; the gateway's `title_route` performs the request and reports upstream
//!   failures with [`format_upstream_error`].
//!
//! # The rate limiter is a port, not a guard
//!
//! `check_title_rate_limit` counts *accepted* calls per API-key digest inside a
//! 60-second window and raises on the 13th. It is per-process in the oracle too
//! (module-level dict, `threading.RLock`), so a multi-worker deployment has one
//! window per worker on both sides. Reproducing that rather than "improving" it keeps
//! the two behaviours equal — and the difference is recorded in the matrix instead of
//! being silently fixed on one side.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::core_utils::normalize_model_name;
use crate::python_json::value_str;

/// `TITLE_MAX_CHARS`: the sanitiser's hard cap, in characters.
pub const TITLE_MAX_CHARS: usize = 24;
/// `TITLE_RATE_LIMIT_COUNT`.
pub const TITLE_RATE_LIMIT_COUNT: usize = 12;
/// `TITLE_RATE_LIMIT_WINDOW_SECONDS`.
pub const TITLE_RATE_LIMIT_WINDOW_SECONDS: u64 = 60;
/// `TITLE_SYSTEM_PROMPT`.
pub const TITLE_SYSTEM_PROMPT: &str = concat!(
    "你是一个对话标题生成器。根据用户首轮提问和助手首轮回复摘要生成短标题。\n",
    "要求：只返回标题纯文本；中文不超过14个汉字，英文不超过6个单词；",
    "不要加引号、句号、标签或 emoji；抓住具体话题，避免空话；",
    "闲聊或问候返回“闲聊”；优先使用用户主要语言。"
);
/// `TITLE_PREFIXES`.
pub const TITLE_PREFIXES: [&str; 4] = ["标题：", "标题:", "Title:", "title:"];
/// `TITLE_STRIP_CHARS`, as the set the oracle passes to `str.strip`.
pub const TITLE_STRIP_CHARS: [char; 15] = [
    '「', '」', '『', '』', '《', '》', '"', '\'', '“', '”', '‘', '’', '`', ' ', '\t',
];
/// `TITLE_STRIP_CHARS` also carries `\n` and `\r`.
const TITLE_STRIP_NEWLINES: [char; 2] = ['\n', '\r'];
/// `TITLE_TRAILING_PUNCTUATION`.
pub const TITLE_TRAILING_PUNCTUATION: [char; 12] = [
    '。', '.', '，', ',', '！', '!', '？', '?', '；', ';', '：', ':',
];
/// The oracle's `CONTEXT_COMPRESS_MODEL` default.
pub const CONTEXT_COMPRESS_MODEL: &str = "deepseek-v4-flash";
/// The oracle's `SUPPORTED_MODELS` default.
pub const SUPPORTED_MODELS: [&str; 2] = ["deepseek-v4-pro", "deepseek-v4-flash"];
/// The oracle's `MODEL_ALIASES` default (`core/config.py`).
pub const MODEL_ALIASES: [(&str, &str); 9] = [
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    ("deepseekv4pro", "deepseek-v4-pro"),
    ("v4pro", "deepseek-v4-pro"),
    ("expert", "deepseek-v4-pro"),
    ("deepseek-v4-flash", "deepseek-v4-flash"),
    ("deepseekv4flash", "deepseek-v4-flash"),
    ("v4flash", "deepseek-v4-flash"),
    ("flash", "deepseek-v4-flash"),
    ("fast", "deepseek-v4-flash"),
];

/// The oracle's `_truncate`: strip, then append `...` only when the text is longer.
pub fn truncate(value: &str, limit: usize) -> String {
    let text = value.trim();
    if text.chars().count() <= limit {
        return text.to_string();
    }
    let head: String = text.chars().take(limit).collect();
    format!("{head}...")
}

/// The oracle's `_sanitize_title`.
///
/// The order matters and is the oracle's: strip the wrapping characters, then drop one
/// leading label, then collapse whitespace, then peel trailing punctuation, then cut to
/// [`TITLE_MAX_CHARS`]. Peeling after collapsing is why `"标题： 你好 。"` loses the
/// label *and* the full stop, and cutting last is why a 30-character title that ends in
/// a full stop returns 24 characters rather than 23.
pub fn sanitize_title(value: &str) -> String {
    let stripped = value.trim();
    let stripped = strip_chars(stripped);
    let stripped = stripped.trim();
    let mut title = stripped.to_string();
    for prefix in TITLE_PREFIXES {
        if let Some(rest) = title.strip_prefix(prefix) {
            title = rest.trim().to_string();
            break;
        }
    }
    title = title.replace(['\n', '\r'], " ");
    title = title.split_whitespace().collect::<Vec<_>>().join(" ");
    while title
        .chars()
        .last()
        .is_some_and(|last| TITLE_TRAILING_PUNCTUATION.contains(&last))
    {
        title.pop();
        title = title.trim().to_string();
    }
    title.chars().take(TITLE_MAX_CHARS).collect()
}

/// Python's `str.strip(chars)`: both ends, repeatedly, over the given set.
fn strip_chars(value: &str) -> String {
    let is_strippable = |character: char| {
        TITLE_STRIP_CHARS.contains(&character) || TITLE_STRIP_NEWLINES.contains(&character)
    };
    value.trim_matches(is_strippable).to_string()
}

/// The oracle's `generate_title_payload` body, minus the transport.
///
/// Returns `None` when the oracle returns `{"title": ""}` without calling upstream:
/// an empty (or whitespace-only) `userMessage`. The caller must not make a request in
/// that case, which is the observable difference between the two branches.
pub fn title_request_body(payload: &Value) -> Option<Value> {
    // `str(payload.get("userMessage") or "")`: the `or ""` runs *before* `str`, so a
    // missing key and an explicit null both become the empty string — not Python's
    // `str(None)`, which would be the four characters `"None"` and would look like a
    // user message.
    let user_text = truncate(
        &crate::core_utils::text_or_empty(payload.get("userMessage")),
        1200,
    );
    if user_text.trim().is_empty() {
        return None;
    }
    let assistant_text = truncate(
        &crate::core_utils::text_or_empty(payload.get("assistantMessage")),
        600,
    );
    let aliases: Vec<(String, String)> = MODEL_ALIASES
        .iter()
        .map(|(from, to)| ((*from).to_string(), (*to).to_string()))
        .collect();
    let requested = payload
        .get("titleModel")
        .filter(|value| !matches!(value, Value::Null) && crate::core_utils::python_truthy(value));
    let mut model = normalize_model_name(requested, &aliases);
    if !SUPPORTED_MODELS.contains(&model.as_str()) {
        model = CONTEXT_COMPRESS_MODEL.to_string();
    }
    Some(json!({
        "model": model,
        "stream": false,
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": TITLE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": format!(
                    "用户首轮提问:\n{user_text}\n\n助手首轮回复摘要:\n{}\n\n请直接给出标题。",
                    if assistant_text.is_empty() { "（暂无）" } else { &assistant_text }
                ),
            },
        ],
    }))
}

/// The oracle's read of the upstream answer: `choices[0].message.content`, or empty.
///
/// The `or ""` runs before `str`, so a null content is the empty string rather than
/// Python's `str(None)` — measured by the parity probe, which caught exactly that
/// (`null_content: oracle='' native='None'`).
pub fn title_from_response(response: &Value) -> String {
    let content = response
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(Value::as_object)
        .and_then(|choice| choice.get("message"))
        .and_then(Value::as_object)
        .and_then(|message| message.get("content"))
        .map(|value| crate::core_utils::text_or_empty(Some(value)))
        .unwrap_or_default();
    sanitize_title(&content)
}

/// `format_upstream_error` from `core/utils.py`.
///
/// A JSON body with `error.message` (or `error.type`) becomes that message; anything
/// else is the raw body truncated to 500 characters, or the oracle's own fallback.
pub fn format_upstream_error(raw: &str) -> String {
    let parsed = serde_json::from_str::<Value>(raw);
    if let Ok(value) = parsed {
        if let Some(message) = value
            .get("error")
            .and_then(Value::as_object)
            .and_then(|error| error.get("message").or_else(|| error.get("type")))
            .filter(|message| crate::core_utils::python_truthy(message))
        {
            return value_str(message);
        }
    }
    let head: String = raw.chars().take(500).collect();
    if head.is_empty() {
        "DeepSeek API error".to_string()
    } else {
        head
    }
}

/// Why a title call was refused. One variant, because the oracle raises one error —
/// `AppError("Title generation is temporarily rate limited.", RATE_LIMITED, 429)` —
/// and a richer type here would be a shape the route has to flatten again.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TitleRateLimited;

impl std::fmt::Display for TitleRateLimited {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Title generation is temporarily rate limited.")
    }
}

impl std::error::Error for TitleRateLimited {}

/// The per-key window the oracle keeps in `_TITLE_RATE_LIMITS`.
///
/// `now` is injected so a test can drive the window without sleeping, exactly as the
/// oracle's `time.monotonic()` makes it testable by patching.
pub struct TitleRateLimiter {
    window: Duration,
    count: usize,
    recent: Mutex<HashMap<String, Vec<Instant>>>,
}

impl Default for TitleRateLimiter {
    fn default() -> Self {
        Self::new(
            Duration::from_secs(TITLE_RATE_LIMIT_WINDOW_SECONDS),
            TITLE_RATE_LIMIT_COUNT,
        )
    }
}

impl TitleRateLimiter {
    pub fn new(window: Duration, count: usize) -> Self {
        Self {
            window,
            count,
            recent: Mutex::new(HashMap::new()),
        }
    }

    /// The oracle's `check_title_rate_limit`: `Ok(())` when the call is admitted and
    /// the instant is recorded, `Err(TitleRateLimited)` when the window already holds
    /// the limit.
    ///
    /// The key is the SHA-256 digest of the API key, truncated to 16 hex characters —
    /// the same digest the oracle uses, so the *key* never reaches a map in plaintext
    /// here either.
    pub fn check(&self, api_key: &str, now: Instant) -> Result<(), TitleRateLimited> {
        let key = crate::core_utils::sha256_hex_prefix(api_key, 16);
        let mut recent = self
            .recent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let entry = recent.entry(key).or_default();
        entry.retain(|instant| now.duration_since(*instant) < self.window);
        if entry.len() >= self.count {
            return Err(TitleRateLimited);
        }
        entry.push(now);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncate_appends_an_ellipsis_only_past_the_limit() {
        assert_eq!(truncate("  hello  ", 10), "hello");
        assert_eq!(truncate("abcdef", 3), "abc...");
        assert_eq!(truncate("abc", 3), "abc");
        // Characters, not bytes: a CJK title of 4 characters is under a 4-char limit.
        assert_eq!(truncate("中文标题", 4), "中文标题");
        assert_eq!(truncate("中文标题", 3), "中文标...");
    }

    #[test]
    fn the_sanitiser_follows_the_oracles_order() {
        // Wrapping quotes and a label both go.
        assert_eq!(sanitize_title("「标题： 你好世界」"), "你好世界");
        assert_eq!(sanitize_title("Title:  Hello World  "), "Hello World");
        // Newlines collapse, then trailing punctuation peels, then the cap applies.
        assert_eq!(sanitize_title("first\nsecond。"), "first second");
        assert_eq!(sanitize_title("topic..."), "topic");
        assert_eq!(sanitize_title("a。，！"), "a");
        // The cut is last, so 30 characters ending in a stop returns exactly 24.
        let long = format!("{}。", "字".repeat(30));
        assert_eq!(sanitize_title(&long).chars().count(), TITLE_MAX_CHARS);
        assert_eq!(sanitize_title(""), "");
    }

    #[test]
    fn a_blank_user_message_asks_for_no_request() {
        assert!(title_request_body(&json!({"userMessage": "   "})).is_none());
        assert!(title_request_body(&json!({})).is_none());
        assert!(title_request_body(&json!({"userMessage": "hi"})).is_some());
    }

    #[test]
    fn the_request_body_is_the_oracles() {
        let body = title_request_body(&json!({
            "userMessage": "解释一下 FastCDC",
            "assistantMessage": "FastCDC 是一种内容定义分块算法。",
            "titleModel": "flash",
        }))
        .expect("a body");
        assert_eq!(body["model"], "deepseek-v4-flash");
        assert_eq!(body["stream"], false);
        assert_eq!(body["thinking"], json!({"type": "disabled"}));
        assert_eq!(body["temperature"], 0.3);
        assert_eq!(body["max_tokens"], 60);
        assert_eq!(body["messages"][0]["content"], TITLE_SYSTEM_PROMPT);
        assert!(
            body["messages"][1]["content"]
                .as_str()
                .unwrap_or_default()
                .contains("解释一下 FastCDC")
        );
    }

    #[test]
    fn an_unsupported_model_falls_back_to_the_compress_model() {
        let body = title_request_body(&json!({"userMessage": "hi", "titleModel": "gpt-9"}))
            .expect("a body");
        assert_eq!(body["model"], CONTEXT_COMPRESS_MODEL);
        // A missing titleModel is the same fallback, not an empty string.
        let body = title_request_body(&json!({"userMessage": "hi"})).expect("a body");
        assert_eq!(body["model"], CONTEXT_COMPRESS_MODEL);
        // The alias table is consulted, so `expert` resolves rather than falling back.
        let body = title_request_body(&json!({"userMessage": "hi", "titleModel": "expert"}))
            .expect("a body");
        assert_eq!(body["model"], "deepseek-v4-pro");
    }

    #[test]
    fn the_response_reader_takes_the_first_choice_content() {
        assert_eq!(
            title_from_response(&json!({"choices": [{"message": {"content": "「标题」"}}]})),
            "标题"
        );
        assert_eq!(title_from_response(&json!({"choices": []})), "");
        assert_eq!(title_from_response(&json!({})), "");
        assert_eq!(
            title_from_response(&json!({"choices": [{"message": {"content": ""}}]})),
            ""
        );
    }

    #[test]
    fn upstream_errors_prefer_the_providers_own_message() {
        assert_eq!(
            format_upstream_error(r#"{"error": {"message": "Invalid API key"}}"#),
            "Invalid API key"
        );
        assert_eq!(
            format_upstream_error(r#"{"error": {"type": "rate_limit"}}"#),
            "rate_limit"
        );
        assert_eq!(format_upstream_error("not json"), "not json");
        assert_eq!(format_upstream_error(""), "DeepSeek API error");
        let long = "x".repeat(600);
        assert_eq!(format_upstream_error(&long).chars().count(), 500);
    }

    #[test]
    fn the_rate_limiter_admits_twelve_and_refuses_the_thirteenth() {
        let limiter = TitleRateLimiter::new(Duration::from_secs(60), 12);
        let start = Instant::now();
        for index in 0..12 {
            assert!(
                limiter.check("key-a", start).is_ok(),
                "call {index} must be admitted"
            );
        }
        assert!(limiter.check("key-a", start).is_err());
        // A different key has its own window.
        assert!(limiter.check("key-b", start).is_ok());
        // Past the window the oldest entries expire, so the 13th succeeds.
        assert!(
            limiter
                .check("key-a", start + Duration::from_secs(61))
                .is_ok()
        );
    }
}
