//! The shared retrieval scorer, mirroring the relevant parts of
//! `deepseek_infra/core/utils.py`.
//!
//! Used by both the memory branches and the RAG `search_files` branch, so one port
//! serves two branches.
//!
//! # A measured divergence: the oracle is non-deterministic here
//!
//! `query_tokens` ends with `sorted(tokens, key=len, reverse=True)[:80]` over a
//! **`set`**. Python's `sorted` is stable, so tokens of equal length keep the set's
//! iteration order — and a `set` of `str` iterates in an order that depends on
//! `PYTHONHASHSEED`. When more than 80 tokens survive, *which* 80 are kept therefore
//! changes from run to run.
//!
//! That is not an internal detail: it changes the score. Measured with a query of
//! 60 three-character tokens followed by 40 two-character tokens, where the text
//! repeats one of the short tokens ten times:
//!
//! ```text
//! PYTHONHASHSEED=1 -> score 360
//! PYTHONHASHSEED=5 -> score 390
//! ```
//!
//! [This port is deterministic][`query_tokens`] — length descending, then
//! lexicographic — so it returns *one* of the values the oracle can return rather
//! than a different one each run. Reproducing CPython's set order is not possible
//! by construction, so "preserving the existing behaviour" literally cannot mean
//! preserving this. The change narrows a nondeterministic result to a fixed one; it
//! does not weaken anything.
//!
//! The parity probe therefore compares token lists **order-insensitively** and
//! records the divergence rather than hiding it behind a flaky green tick. The
//! deterministic ordering is pinned by unit tests instead.

use regex::Regex;
use serde_json::Value;

pub(crate) fn encode_lower_hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;

    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(encoded, "{byte:02x}").expect("writing to a String cannot fail");
    }
    encoded
}

/// `query_tokens`: the capped token list.
///
/// Deterministic: longest first, ties broken lexicographically. See the module
/// docs for why this cannot match the oracle's tie order.
pub fn query_tokens(query: &str) -> Vec<String> {
    let whitespace = compiled(r"\s+");
    let normalized = whitespace
        .replace_all(&query.to_lowercase(), " ")
        .to_string();

    let word = compiled(r"[a-z0-9_+-]{2,}|[\u{4e00}-\u{9fff}]{2,}");
    let cjk_run = compiled(r"[\u{4e00}-\u{9fff}]{3,}");

    // A set, as the oracle uses — the order is imposed below, not by insertion.
    let mut tokens: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for found in word.find_iter(&normalized) {
        tokens.insert(found.as_str().to_string());
    }
    // Every two-character window of any CJK run of three or more.
    for found in cjk_run.find_iter(&normalized) {
        let run: Vec<char> = found.as_str().chars().collect();
        for index in 0..run.len().saturating_sub(1) {
            tokens.insert(run[index..index + 2].iter().collect());
        }
    }

    let mut ordered: Vec<String> = tokens.into_iter().collect();
    // Length descending, then lexicographic — the lexicographic pass is what makes
    // ties deterministic, and it costs nothing because the input is already sorted
    // lexicographically and only the length key is reversed.
    ordered.sort_by(|left, right| {
        right
            .chars()
            .count()
            .cmp(&left.chars().count())
            .then_with(|| left.cmp(right))
    });
    ordered.truncate(80);
    ordered
}

/// `score_chunk`: the sum of `count × max(2, min(len, 10))` over the tokens, plus a
/// markdown-heading bonus.
///
/// Order-independent, so the oracle's token-order instability reaches it only
/// through *which* tokens survive the cap.
pub fn score_chunk(text: &str, tokens: &[String]) -> i64 {
    if tokens.is_empty() {
        return 0;
    }
    let lowered = text.to_lowercase();
    let mut score = 0i64;
    for token in tokens {
        let count = lowered.matches(token.as_str()).count();
        if count > 0 {
            let weight = 2.max(token.chars().count().min(10));
            score += count as i64 * weight as i64;
        }
    }
    if heading_regex().is_match(text) {
        score += 2;
    }
    score
}

/// Mirrors `utc_now_iso`: `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
///
/// `timespec="seconds"` is why there is no fractional part. The clock is a
/// parameter so callers can pin it.
pub fn utc_now_iso(epoch_seconds: i64) -> String {
    let days = epoch_seconds.div_euclid(86_400);
    let remainder = epoch_seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
        remainder / 3600,
        (remainder % 3600) / 60,
        remainder % 60
    )
}

/// Python truthiness for a JSON value.
///
/// Needed wherever the oracle writes `if x:` or `x or fallback` — a shape that
/// appears in the stores, the file cache and the streaming tool-call accumulator.
/// Getting it wrong by "obvious" reasoning goes both ways: `0`, `false`, `""`, `[]`
/// and `{}` are **falsy** (so an `or` chain replaces them), while a non-zero number
/// and `true` are truthy (so an `or` chain keeps them, and `str()` renders `"5"` /
/// `"True"`).
pub fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|float| float != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// The wall clock, injected.
///
/// The oracle's `utc_now_iso()` reads the clock itself and takes no argument. This
/// port supplies it instead, so a store write is reproducible in tests and in the
/// parity probe while production keeps using the real clock.
pub trait Clock {
    /// The current instant as `utc_now_iso` would render it.
    fn now_iso(&self) -> String;
}

/// Python's bare `int()`, or `None` where it would raise.
///
/// One implementation for the three places that need it: the file-cache read path
/// (which turns a failure into the documented 500), the streaming tool-call merge
/// (which falls back to a running count), and anything else that meets a
/// `lineStart`-style field.
///
/// The coercions are the whole point and are easy to get wrong by "doing the
/// obvious thing":
///
/// - a bool is an int (`True` -> 1, `False` -> 0), because `bool` subclasses `int`;
/// - a float **truncates toward zero**, so `-3.7` -> `-3`, not `-4`;
/// - a string is trimmed, may carry a sign, and may use single underscores between
///   digits (`"1_0"` -> 10), but `"3.7"` and `"True"` **raise**;
/// - `None`, a list and a dict all raise.
pub fn python_int_opt(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::Number(number) => match number.as_i64() {
            Some(int) => Some(int),
            // `int(2.7)` truncates toward zero; `int(nan)`/`int(inf)` raise.
            None => number
                .as_f64()
                .filter(|float| float.is_finite())
                .map(|float| float.trunc() as i64),
        },
        Value::String(text) => {
            let trimmed = text.trim();
            let (sign, digits) = match trimmed.strip_prefix('-') {
                Some(rest) => (-1i64, rest),
                None => (1i64, trimmed.strip_prefix('+').unwrap_or(trimmed)),
            };
            // Python allows `1_0` but not a leading, trailing or doubled underscore.
            let normalised = digits.replace('_', "");
            let shape_ok = !normalised.is_empty()
                && normalised.bytes().all(|byte| byte.is_ascii_digit())
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__");
            if !shape_ok {
                return None;
            }
            normalised.parse::<i64>().ok().map(|parsed| sign * parsed)
        }
        Value::Null | Value::Array(_) | Value::Object(_) => None,
    }
}

/// The production clock.
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_iso(&self) -> String {
        let seconds = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_secs() as i64)
            .unwrap_or(0);
        utc_now_iso(seconds)
    }
}

/// A clock frozen at a fixed instant, for tests and the parity probe.
#[derive(Debug, Clone, Copy)]
pub struct FixedClock {
    pub epoch_seconds: i64,
}

impl Clock for FixedClock {
    fn now_iso(&self) -> String {
        utc_now_iso(self.epoch_seconds)
    }
}

/// Mirrors `latest_user_query`: the most recent user message whose content is a
/// non-blank string, trimmed.
pub fn latest_user_query(payload: &Value) -> String {
    let Some(Value::Array(messages)) = payload.get("messages") else {
        return String::new();
    };
    for message in messages.iter().rev() {
        let Some(object) = message.as_object() else {
            continue;
        };
        if object.get("role").and_then(Value::as_str) != Some("user") {
            continue;
        }
        if let Some(Value::String(content)) = object.get("content") {
            let trimmed = content.trim();
            if !trimmed.is_empty() {
                return trimmed.to_string();
            }
        }
    }
    String::new()
}

fn compiled(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static pattern must compile")
}

fn heading_regex() -> &'static Regex {
    static HEADING: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    HEADING.get_or_init(|| Regex::new(r"(?m)^#{1,6}\s+").expect("static heading pattern"))
}

/// Days since 1970-01-01 to a civil date (Howard Hinnant's algorithm).
fn civil_from_days(days: i64) -> (i32, u32, u32) {
    let days = days + 719_468;
    let era = if days >= 0 { days } else { days - 146_096 } / 146_097;
    let day_of_era = days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    (
        (year + if month <= 2 { 1 } else { 0 }) as i32,
        month as u32,
        day as u32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn query_tokens_normalises_case_and_whitespace() {
        // Longest first, so "ownership" precedes "rust".
        assert_eq!(query_tokens("Rust   OWNERSHIP"), vec!["ownership", "rust"]);
        // Single characters are dropped; the ASCII class needs two.
        assert_eq!(query_tokens("a bb ccc"), vec!["ccc", "bb"]);
        // Padded whitespace collapses rather than producing empty tokens.
        assert_eq!(query_tokens("  a  b  "), Vec::<String>::new());
    }

    #[test]
    fn query_tokens_is_ordered_longest_first_then_lexicographically() {
        // Distinct lengths, so the order is fully determined by either rule.
        assert_eq!(query_tokens("aa bbbb c"), vec!["bbbb", "aa"]);
        // Ties are broken lexicographically, which is the deliberate divergence
        // from the oracle's hash-seed-dependent set order.
        assert_eq!(
            query_tokens("zz aa mm"),
            vec!["aa", "mm", "zz"],
            "equal-length tokens must come out in a fixed order"
        );
    }

    #[test]
    fn query_tokens_adds_cjk_bigrams() {
        // A three-character run contributes the run itself plus both bigrams.
        let tokens = query_tokens("中文测试");
        assert!(tokens.contains(&"中文测试".to_string()));
        assert!(tokens.contains(&"中文".to_string()));
        assert!(tokens.contains(&"文测".to_string()));
        assert!(tokens.contains(&"测试".to_string()));
        // A two-character run is matched whole and has no extra bigrams.
        let short = query_tokens("中文");
        assert_eq!(short, vec!["中文"]);
    }

    #[test]
    fn query_tokens_caps_at_eighty() {
        let query: Vec<String> = (0..200).map(|index| format!("t{index:03}")).collect();
        let tokens = query_tokens(&query.join(" "));
        assert_eq!(tokens.len(), 80);
        // The cap is applied after ordering, so the longest survive.
        assert!(tokens.iter().all(|token| token.chars().count() == 4));
    }

    #[test]
    fn score_chunk_weights_by_length_and_count() {
        let tokens = vec!["ab".to_string()];
        // `max(2, min(len, 10))` -> 2 for a two-character token.
        assert_eq!(score_chunk("ab ab", &tokens), 4);
        // A ten-character token weighs 10, and anything longer is still 10.
        assert_eq!(score_chunk("x", &["abcdefghij".to_string()]), 0);
        assert_eq!(score_chunk("abcdefghij", &["abcdefghij".to_string()]), 10);
        assert_eq!(score_chunk("abcdefghijk", &["abcdefghijk".to_string()]), 10);
        // No tokens means no score, even with a heading.
        assert_eq!(score_chunk("# heading", &[]), 0);
    }

    #[test]
    fn score_chunk_matches_at_any_case_and_adds_the_heading_bonus() {
        let tokens = vec!["rust".to_string()];
        assert_eq!(score_chunk("RUST rust", &tokens), 8);
        // A markdown heading adds two.
        assert_eq!(score_chunk("rust\n# Title", &tokens), 6);
        // The heading pattern requires a space after the hashes.
        assert_eq!(score_chunk("rust\n#Title", &tokens), 4);
        // Too many hashes is not a heading.
        assert_eq!(score_chunk("rust\n####### deep", &tokens), 4);
    }

    /// Pinned against CPython's own `int()`: a bool is an int, a float truncates
    /// toward zero, and a string may carry a sign and single underscores between
    /// digits — but `"3.7"`, `"1__0"`, `nan` and `inf` all raise.
    #[test]
    fn python_int_opt_matches_pythons_int() {
        assert_eq!(python_int_opt(Some(&json!(5))), Some(5));
        assert_eq!(python_int_opt(Some(&json!(-3))), Some(-3));
        assert_eq!(python_int_opt(Some(&json!(true))), Some(1));
        assert_eq!(python_int_opt(Some(&json!(false))), Some(0));
        // Truncation is toward zero, so a negative float is not floored.
        assert_eq!(python_int_opt(Some(&json!(3.7))), Some(3));
        assert_eq!(python_int_opt(Some(&json!(-3.7))), Some(-3));
        assert_eq!(python_int_opt(Some(&json!("12"))), Some(12));
        assert_eq!(python_int_opt(Some(&json!("  8  "))), Some(8));
        assert_eq!(python_int_opt(Some(&json!("-4"))), Some(-4));
        assert_eq!(python_int_opt(Some(&json!("+9"))), Some(9));
        assert_eq!(python_int_opt(Some(&json!("1_0"))), Some(10));
        // Everything `int()` rejects.
        for bad in [
            json!("3.7"),
            json!("abc"),
            json!(""),
            json!("1__0"),
            json!("_1"),
            json!("1_"),
            Value::Null,
            json!([1]),
            json!({"a": 1}),
        ] {
            assert_eq!(python_int_opt(Some(&bad)), None, "{bad}");
        }
        assert_eq!(python_int_opt(None), None);
    }

    #[test]
    fn utc_now_iso_renders_whole_seconds() {
        assert_eq!(utc_now_iso(0), "1970-01-01T00:00:00+00:00");
        assert_eq!(utc_now_iso(1_760_000_000), "2025-10-09T08:53:20+00:00");
        // `timespec="seconds"` means no fractional part.
        assert!(!utc_now_iso(1).contains('.'));
    }

    #[test]
    fn latest_user_query_takes_the_last_non_blank_user_message() {
        let payload = json!({"messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "  second  "},
        ]});
        assert_eq!(latest_user_query(&payload), "second");

        // A blank user message is skipped, falling back to the earlier one.
        let blank = json!({"messages": [
            {"role": "user", "content": "earlier"},
            {"role": "user", "content": "   "},
        ]});
        assert_eq!(latest_user_query(&blank), "earlier");

        // Non-string content is skipped, and non-dict entries are tolerated.
        let odd = json!({"messages": [
            {"role": "user", "content": "text"},
            {"role": "user", "content": ["parts"]},
            "not-an-object",
        ]});
        assert_eq!(latest_user_query(&odd), "text");

        assert_eq!(latest_user_query(&json!({"messages": []})), "");
        assert_eq!(latest_user_query(&json!({})), "");
        assert_eq!(latest_user_query(&json!({"messages": "no"})), "");
    }

    /// The measured instability, pinned as a *documented* property of the port: the
    /// same query must give the same answer every time, unlike the oracle.
    #[test]
    fn query_tokens_is_stable_across_calls_where_the_oracle_is_not() {
        let query: Vec<String> = (0..40).map(|index| format!("y{index:02}")).collect();
        let joined = query.join(" ");
        let first = query_tokens(&joined);
        for _ in 0..5 {
            assert_eq!(query_tokens(&joined), first);
        }
    }
}
