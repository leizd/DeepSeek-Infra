//! Retrieval-scorer parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/core_utils_parity_probe.py`
//! through `deepseek_policy::core_utils` and prints canonical JSON.
//!
//! Token lists are compared **sorted**. The oracle's own order varies between runs,
//! because `sorted(..., key=len, reverse=True)` is stable over a `set` whose
//! iteration order depends on `PYTHONHASHSEED`; see the module docs of
//! `core_utils`. Inputs where more than 80 tokens survive report only the count,
//! since the surviving subset itself differs run to run.
//!
//! Usage::
//!
//!     python tasks/native-runtime/core_utils_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example core_utils_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::core_utils::{latest_user_query, query_tokens, score_chunk, utc_now_iso};
use serde_json::{Map, Value, json};

fn token_cases() -> Vec<(&'static str, &'static str)> {
    vec![
        ("ascii-simple", "Rust   OWNERSHIP"),
        ("single-chars", "a bb ccc"),
        ("mixed-lengths", "aa bbbb c ddddd"),
        ("punctuation", "hello, world! (test)"),
        ("digits-underscore", "v1_2 item-3 x+y"),
        ("uppercase", "ALPHA Beta gamma"),
        ("cjk-short", "中文"),
        ("cjk-three", "中文测"),
        ("cjk-long", "中文测试用例"),
        ("cjk-mixed", "中文 abc 测试"),
        ("empty", ""),
        ("whitespace", "   "),
        ("newlines", "a\nbb\tcc"),
    ]
}

fn capped_cases() -> Vec<(&'static str, String)> {
    let many_short: Vec<String> = (0..200).map(|index| format!("t{index:03}")).collect();
    vec![
        ("many-short", many_short.join(" ")),
        ("many-cjk", "中".repeat(60)),
    ]
}

fn score_cases() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        ("counts-and-length", "ab ab", "ab"),
        ("ten-char-weight", "abcdefghij", "abcdefghij"),
        ("longer-than-ten", "abcdefghijk", "abcdefghijk"),
        ("case-insensitive", "RUST rust", "rust"),
        ("no-match", "nothing here", "zzz"),
        ("heading-bonus", "rust\n# Title", "rust"),
        ("heading-no-space", "rust\n#Title", "rust"),
        ("heading-too-deep", "rust\n####### deep", "rust"),
        ("multiple-tokens", "aa aa bb", "aa bb"),
        ("cjk", "中文测试", "中文"),
    ]
}

fn utc_cases() -> Vec<i64> {
    vec![0, 1, 1_760_000_000, 1_780_272_000]
}

fn query_cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "last-user",
            json!({"messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "  second  "},
            ]}),
        ),
        (
            "blank-skipped",
            json!({"messages": [
                {"role": "user", "content": "earlier"},
                {"role": "user", "content": "   "},
            ]}),
        ),
        (
            "non-string-content",
            json!({"messages": [
                {"role": "user", "content": "text"},
                {"role": "user", "content": ["parts"]},
                "not-an-object",
            ]}),
        ),
        (
            "no-user",
            json!({"messages": [{"role": "assistant", "content": "a"}]}),
        ),
        ("empty-messages", json!({"messages": []})),
        ("no-messages-key", json!({})),
        ("messages-not-list", json!({"messages": "no"})),
        ("non-dict-message", json!({"messages": ["x", 7, null]})),
    ]
}

fn sorted_tokens(query: &str) -> Vec<String> {
    let mut tokens = query_tokens(query);
    tokens.sort();
    tokens
}

fn main() {
    let mut out = Map::new();

    for (label, query) in token_cases() {
        let tokens = query_tokens(query);
        out.insert(
            format!("tokens::{label}"),
            json!({"count": tokens.len(), "sorted": sorted_tokens(query)}),
        );
    }

    for (label, query) in capped_cases() {
        out.insert(
            format!("capped::{label}"),
            json!({"count": query_tokens(&query).len()}),
        );
    }

    for (label, text, token) in score_cases() {
        let tokens = query_tokens(token);
        out.insert(
            format!("score::{label}"),
            json!({
                "tokens": sorted_tokens(token),
                "count": tokens.len(),
                "score": score_chunk(text, &tokens),
            }),
        );
    }

    for epoch in utc_cases() {
        out.insert(format!("utc::{epoch}"), json!(utc_now_iso(epoch)));
    }

    for (label, payload) in query_cases() {
        out.insert(
            format!("query::{label}"),
            json!(latest_user_query(&payload)),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
