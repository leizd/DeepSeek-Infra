//! Tavily search: query planning, response normalization, ranking, and the cache.
//!
//! Mirrors the non-transport half of `infra/tool_runtime/search.py` — everything the
//! `web_search` tool branch needs **except the HTTP call itself**, which is the next
//! slice. That boundary is a dependency closure rather than a taste call:
//!
//! - [`format_search_context`] / [`format_search_failure_context`] are **not** here.
//!   They build the *prompt context* at request-assembly time; the tool branch returns
//!   a compiled tool result and never touches them.
//! - [`search_tavily`] / [`search_tavily_with_retry`] are not here either. Their
//!   retry *policy* is ([`should_retry_tavily_error`], [`simplified_retry_query`]);
//!   only the request itself is missing.
//!
//! # What is reproduced rather than tidied
//!
//! - `search_cache_key` hashes the whitespace-collapsed **lowercased** query, while
//!   `load_search_cache` / `save_search_cache` pass the raw query through it — so two
//!   queries differing only in case hit the same cache entry, which is the point.
//! - `save_search_cache` calls `cleanup_search_cache` first, so a write also prunes.
//! - The temp file is `path.with_suffix(".tmp")`, which **replaces** `".json"` rather
//!   than appending to it — `abc.json` becomes `abc.tmp`, matching the reminders store.
//! - `search_result_score` multiplies the provider score by 20 and then adds token
//!   hits, domain hints and an official-docs bonus, and **subtracts** for an empty
//!   snippet. The weights are not obviously proportional; they are transcribed.
//! - `rerank_search_results` caps a domain at **two** results, which is a diversity
//!   rule rather than a score rule, and it runs after the sort.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::app_error::{AppError, codes};
use crate::core_utils::{python_int_opt, python_truthy, query_tokens};

/// `SEARCH_RESULT_LIMIT`.
pub const SEARCH_RESULT_LIMIT: usize = 15;
/// `SEARCH_ROUND_LIMIT`.
pub const SEARCH_ROUND_LIMIT: usize = 3;
/// `SEARCH_TOTAL_RESULT_LIMIT`.
pub const SEARCH_TOTAL_RESULT_LIMIT: usize = 45;
/// `SEARCH_CONTENT_CHARS`.
pub const SEARCH_CONTENT_CHARS: usize = 1200;
/// `SEARCH_RAW_CONTENT_CHARS`.
pub const SEARCH_RAW_CONTENT_CHARS: usize = 3500;
/// `SEARCH_CONTEXT_RESULT_LIMIT`.
pub const SEARCH_CONTEXT_RESULT_LIMIT: usize = 24;
/// `SEARCH_CACHE_MAX_AGE_SECONDS`.
pub const SEARCH_CACHE_MAX_AGE_SECONDS: i64 = 1800;

/// `TRUSTED_DOMAIN_HINTS`.
pub const TRUSTED_DOMAIN_HINTS: [&str; 9] = [
    ".gov",
    ".edu",
    "wikipedia.org",
    "github.com",
    "docs.",
    "developer.",
    "support.",
    "learn.microsoft.com",
    "developer.mozilla.org",
];

/// `<root>/.search-cache`.
pub fn search_cache_dir(root: &Path) -> PathBuf {
    root.join(".search-cache")
}

// --- query planning --------------------------------------------------------------

/// Mirrors `normalize_search_query_text`: whitespace collapsed, trimmed, capped at 500.
pub fn normalize_search_query_text(query: &str) -> String {
    whitespace()
        .replace_all(query, " ")
        .trim()
        .chars()
        .take(500)
        .collect()
}

/// Mirrors `simplified_retry_query`.
///
/// Strips every non-word, non-CJK character, then keeps either the first eight words
/// or — when there is at most one — a 120-character prefix. The result is normalised
/// again.
pub fn simplified_retry_query(query: &str) -> String {
    let cleaned = normalize_search_query_text(query);
    let simplified = non_word().replace_all(&cleaned, " ").to_string();
    let parts: Vec<&str> = simplified.split_whitespace().collect();
    let simplified = if parts.len() > 1 {
        parts[..parts.len().min(8)].join(" ")
    } else {
        simplified.trim().chars().take(120).collect()
    };
    normalize_search_query_text(&simplified)
}

/// Mirrors `should_search_for_query`: an explicit mode wins, otherwise the query text
/// is matched against the freshness, lookup and bare-domain patterns.
pub fn should_search_for_query(query: &str, payload: &Value) -> bool {
    let mode = payload
        .get("searchMode")
        .map(python_str)
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    if matches!(mode.as_str(), "off" | "false" | "0") {
        return false;
    }
    if matches!(mode.as_str(), "on" | "force" | "true" | "1") {
        return true;
    }
    let text = query.trim();
    if text.is_empty() {
        return false;
    }
    if fresh_query_pattern().is_match(text) || lookup_query_pattern().is_match(text) {
        return true;
    }
    bare_domain_pattern().is_match(text)
}

/// Mirrors `search_intent`, including the check order.
pub fn search_intent(query: &str) -> String {
    let text = query.to_lowercase();
    for (pattern, intent) in [
        (
            r"(今天|今日|最新|新闻|现在|近期|刚刚|实时|today|latest|news|current|recent|now)",
            "fresh",
        ),
        (
            r"(价格|多少钱|报价|购买|推荐|评测|排行|price|buy|review|best|deal)",
            "shopping",
        ),
        (
            r"(文档|api|sdk|报错|错误|版本|安装|配置|docs|documentation|error|exception|version|install)",
            "technical",
        ),
        (
            r"(政策|法规|法律|标准|条例|policy|law|regulation|standard|official|官网|官方)",
            "official",
        ),
        (r"(对比|区别|比较|优缺点|compare|difference|vs)", "compare"),
    ] {
        if cached(pattern).is_match(&text) {
            return intent.to_string();
        }
    }
    "general".to_string()
}

/// Mirrors `search_reason_for_query`.
pub fn search_reason_for_query(query: &str) -> String {
    for (pattern, reason) in [
        (
            r"(最新|现在|近期|新闻|实时|today|latest|current|news)",
            "检测到时效性问题",
        ),
        (
            r"(官网|文档|来源|引用|official|docs|source)",
            "需要外部来源验证",
        ),
        (
            r"(价格|报价|评测|排名|price|review|ranking)",
            "需要查询当前市场信息",
        ),
    ] {
        if case_insensitive(pattern).is_match(query) {
            return reason.to_string();
        }
    }
    "自动判断需要联网补充资料".to_string()
}

/// Mirrors `search_queries_for`.
///
/// The first candidate is the query itself; the intent's suffixes follow, capped by
/// `SEARCH_ROUND_LIMIT`. De-duplication is on the **lowercased** candidate.
pub fn search_queries_for(query: &str) -> Vec<String> {
    let normalized = normalize_search_query_text(query);
    if normalized.is_empty() {
        return Vec::new();
    }
    let limit = SEARCH_ROUND_LIMIT.max(1);
    let suffixes: [&str; 2] = match search_intent(&normalized).as_str() {
        "fresh" => ["最新进展", "官方回应"],
        "shopping" => ["评测 对比", "价格 购买"],
        "technical" => ["官方文档", "常见问题 解决方案"],
        "official" => ["官方来源", "政策 解读"],
        "compare" => ["对比 分析", "评论 观点"],
        _ => ["背景 信息", "评论 观点"],
    };

    let mut queries: Vec<String> = Vec::new();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let push = |candidate: String, seen: &mut BTreeSet<String>, queries: &mut Vec<String>| {
        let cleaned = normalize_search_query_text(&candidate);
        let key = cleaned.to_lowercase();
        if cleaned.is_empty() || !seen.insert(key) {
            return;
        }
        queries.push(cleaned);
    };
    push(normalized.clone(), &mut seen, &mut queries);
    for suffix in suffixes {
        if queries.len() >= limit {
            break;
        }
        push(format!("{normalized} {suffix}"), &mut seen, &mut queries);
    }
    queries.truncate(limit);
    queries
}

/// Mirrors `tavily_options_for_query`.
pub fn tavily_options_for_query(query: &str) -> Value {
    let intent = search_intent(query);
    let mut options = Map::new();
    options.insert("topic".into(), json!("general"));
    options.insert("search_depth".into(), json!("basic"));
    options.insert("max_results".into(), json!(SEARCH_RESULT_LIMIT));
    options.insert("include_answer".into(), json!("basic"));
    options.insert("include_raw_content".into(), json!(false));
    options.insert("include_images".into(), json!(false));
    options.insert("include_favicon".into(), json!(true));
    if matches!(
        intent.as_str(),
        "technical" | "official" | "compare" | "fresh"
    ) {
        options.insert("search_depth".into(), json!("advanced"));
    }
    if matches!(intent.as_str(), "technical" | "official") {
        options.insert("include_raw_content".into(), json!(true));
    }
    if intent == "fresh" {
        options.insert("include_answer".into(), json!("advanced"));
    }
    Value::Object(options)
}

/// Mirrors `search_domain_filters`.
pub fn search_domain_filters(query: &str) -> Value {
    let text = query.to_lowercase();
    if cached(r"(政策|法规|签证|税|法律|government|law|regulation)").is_match(&text) {
        return json!({"include_domains": [
            "gov.cn", "mfa.gov.cn", "ica.gov.sg", "mom.gov.sg", "gov.sg"
        ]});
    }
    if cached(r"(官方文档|官网文档|official docs|official documentation)").is_match(&text) {
        return json!({"include_domains": [
            "docs.python.org",
            "developer.mozilla.org",
            "react.dev",
            "nodejs.org",
            "docs.tavily.com",
            "api-docs.deepseek.com",
            "github.com"
        ]});
    }
    json!({})
}

// --- response normalization and ranking -------------------------------------------

/// Mirrors `normalize_search_url`: scheme and host lowercased, trailing slash dropped
/// from the path (but never to empty), and the **fragment dropped**.
pub fn normalize_search_url(url: &str) -> String {
    let trimmed = url.trim();
    let Some((scheme, rest)) = trimmed.split_once("://") else {
        return trimmed.to_lowercase();
    };
    let lower_scheme = scheme.to_lowercase();
    let (authority, path_and_more) = match rest.find(['/', '?', '#']) {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    let lower_authority = authority.to_lowercase();
    let (path, query) = match path_and_more.find(['?', '#']) {
        Some(index) => {
            if path_and_more.as_bytes()[index] == b'?' {
                let (path, tail) = path_and_more.split_at(index);
                let query = tail[1..].split('#').next().unwrap_or("");
                (path, query)
            } else {
                (&path_and_more[..index], "")
            }
        }
        None => (path_and_more, ""),
    };
    // A trailing slash is dropped, and an empty path becomes `/`.
    let path = path.trim_end_matches('/');
    let path = if path.is_empty() { "/" } else { path };
    if query.is_empty() {
        format!("{lower_scheme}://{lower_authority}{path}")
    } else {
        format!("{lower_scheme}://{lower_authority}{path}?{query}")
    }
}

/// Mirrors `domain_from_url`: `urlsplit(url).netloc.lower()` with a leading `www.`
/// removed.
///
/// `netloc` is the **whole authority** — userinfo and port included. Extracting just the
/// host reads as the obvious cleanup and is a divergence: the oracle returns
/// `user:pw@host.com:8443` for that URL, and the probe caught it.
pub fn domain_from_url(url: &str) -> String {
    let trimmed = url.trim();
    let Some((_, rest)) = trimmed.split_once("://") else {
        return String::new();
    };
    let authority = match rest.find(['/', '?', '#']) {
        Some(index) => &rest[..index],
        None => rest,
    };
    let authority = authority.to_lowercase();
    authority
        .strip_prefix("www.")
        .unwrap_or(&authority)
        .to_string()
}

/// Mirrors `normalize_search_response`.
///
/// A result without a URL is dropped; the title defaults to `Untitled`; content and
/// raw content are truncated to `SEARCH_CONTENT_CHARS` / `SEARCH_RAW_CONTENT_CHARS`;
/// and the whole list is capped at `SEARCH_RESULT_LIMIT`.
pub fn normalize_search_response(query: &str, data: &Value) -> Value {
    let mut results: Vec<Value> = Vec::new();
    if let Some(items) = data.get("results").and_then(Value::as_array) {
        for item in items {
            let Some(object) = item.as_object() else {
                continue;
            };
            let title = python_str(object.get("title").unwrap_or(&Value::Null));
            let title = if title.trim().is_empty() {
                "Untitled".to_string()
            } else {
                title.trim().to_string()
            };
            let url = python_str(object.get("url").unwrap_or(&Value::Null));
            let url = url.trim().to_string();
            if url.is_empty() {
                continue;
            }
            let content = truncate_chars(
                python_str(object.get("content").unwrap_or(&Value::Null)).trim(),
                SEARCH_CONTENT_CHARS,
            );
            let raw = truncate_chars(
                python_str(object.get("raw_content").unwrap_or(&Value::Null)).trim(),
                SEARCH_RAW_CONTENT_CHARS,
            );
            results.push(json!({
                "title": truncate_chars(&title, 180),
                "url": url,
                "content": content,
                "raw_content": raw,
                "score": object.get("score").cloned().unwrap_or(Value::Null),
                "favicon": object.get("favicon").cloned().unwrap_or(Value::Null),
            }));
        }
    }
    results.truncate(SEARCH_RESULT_LIMIT);
    json!({
        "query": python_str(data.get("query").unwrap_or(&Value::Null)).if_empty(query),
        "answer": python_str(data.get("answer").unwrap_or(&Value::Null)).trim(),
        "results": results,
        "response_time": data.get("response_time").cloned().unwrap_or(Value::Null),
        "request_id": data.get("request_id").cloned().unwrap_or(Value::Null),
    })
}

/// Mirrors `search_result_score`.
///
/// The weights are transcribed, not derived: the provider score is scaled by 20,
/// a title token adds 8 and a body token 3, a trusted domain adds 10, the
/// official-docs pattern adds 6, and an empty snippet **subtracts** 8.
pub fn search_result_score(result: &Value, query: &str) -> f64 {
    let title = python_str(result.get("title").unwrap_or(&Value::Null));
    let content = python_str(result.get("content").unwrap_or(&Value::Null));
    let url = python_str(result.get("url").unwrap_or(&Value::Null));
    let domain = domain_from_url(&url);
    // `float(result.get("score") or 0) * 20`, with a bad value falling back to 0.
    let score = python_float_opt(result.get("score")).unwrap_or(0.0) * 20.0;

    let combined = format!("{title}\n{content}").to_lowercase();
    let title_lower = title.to_lowercase();
    let mut score = score;
    for token in query_tokens(query) {
        if title_lower.contains(&token) {
            score += 8.0;
        }
        if combined.contains(&token) {
            score += 3.0;
        }
    }
    if TRUSTED_DOMAIN_HINTS
        .iter()
        .any(|hint| domain.contains(hint))
    {
        score += 10.0;
    }
    if case_insensitive(r"(official|docs|documentation|developer|官方|文档)")
        .is_match(&format!("{title} {url}"))
    {
        score += 6.0;
    }
    if content.trim().is_empty() {
        score -= 8.0;
    }
    score
}

/// Mirrors `rerank_search_results`: sort by score descending, then keep at most two
/// results per domain while filling up to `limit`.
///
/// The sort is Python's `sorted`, which is stable, so equal scores keep their input
/// order.
pub fn rerank_search_results(results: &[Value], query: &str, limit: usize) -> Vec<Value> {
    let mut ranked: Vec<Value> = results.to_vec();
    ranked.sort_by(|left, right| {
        let left = search_result_score(left, query);
        let right = search_result_score(right, query);
        right
            .partial_cmp(&left)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut selected: Vec<Value> = Vec::new();
    let mut domain_counts: BTreeMap<String, usize> = BTreeMap::new();
    for result in ranked {
        let domain = domain_from_url(&python_str(result.get("url").unwrap_or(&Value::Null)));
        let count = domain_counts.get(&domain).copied().unwrap_or(0);
        if !domain.is_empty() && count >= 2 {
            continue;
        }
        selected.push(result);
        if !domain.is_empty() {
            domain_counts.insert(domain, count + 1);
        }
        if selected.len() >= limit {
            break;
        }
    }
    selected
}

/// Mirrors `aggregate_search_rounds`.
///
/// Citation ids are assigned in round-then-result order and a URL is **kept once**
/// across rounds, keyed on its normalized form. The aggregate status is inferred when
/// the caller passes none.
pub fn aggregate_search_rounds(query: &str, rounds: &[Value], status: Option<&str>) -> Value {
    let mut results: Vec<Value> = Vec::new();
    let mut seen_urls: BTreeSet<String> = BTreeSet::new();
    let mut answers: Vec<String> = Vec::new();
    let mut normalized_rounds: Vec<Value> = Vec::new();
    let mut citation_counter = 0i64;

    for (index, round_data) in rounds.iter().enumerate() {
        let index = (index + 1) as i64;
        let mut round_results: Vec<Value> = Vec::new();
        if let Some(items) = round_data.get("results").and_then(Value::as_array) {
            for result in items {
                let Some(object) = result.as_object() else {
                    continue;
                };
                let url = python_str(object.get("url").unwrap_or(&Value::Null))
                    .trim()
                    .to_string();
                if url.is_empty() {
                    continue;
                }
                citation_counter += 1;
                let citation_id = python_str(object.get("citation_id").unwrap_or(&Value::Null));
                let citation_id = if citation_id.is_empty() {
                    format!("W{citation_counter}")
                } else {
                    citation_id
                };
                let cite = python_str(object.get("cite").unwrap_or(&Value::Null));
                let cite = if cite.is_empty() {
                    format!("[^{citation_id}]")
                } else {
                    cite
                };
                let client_result = json!({
                    "cite": cite,
                    "citation_id": citation_id,
                    "title": python_str(object.get("title").unwrap_or(&Value::Null)).trim(),
                    "url": url,
                    "content": python_str(object.get("content").unwrap_or(&Value::Null)).trim(),
                    "raw_content": python_str(object.get("raw_content").unwrap_or(&Value::Null)).trim(),
                    "score": object.get("score").cloned().unwrap_or(Value::Null),
                    "favicon": object.get("favicon").cloned().unwrap_or(Value::Null),
                });
                round_results.push(client_result.clone());
                let key = normalize_search_url(&url);
                if !key.is_empty() && seen_urls.insert(key) {
                    let round_number = python_int_opt(round_data.get("round")).unwrap_or(index);
                    let mut published = client_result;
                    published["round"] = json!(round_number);
                    results.push(published);
                }
            }
        }

        let answer = python_str(round_data.get("answer").unwrap_or(&Value::Null))
            .trim()
            .to_string();
        if !answer.is_empty() {
            answers.push(answer.clone());
        }

        let mut normalized_round = Map::new();
        normalized_round.insert(
            "round".into(),
            json!(python_int_opt(round_data.get("round")).unwrap_or(index)),
        );
        normalized_round.insert(
            "status".into(),
            json!(python_str(round_data.get("status").unwrap_or(&Value::Null)).if_empty("done")),
        );
        normalized_round.insert(
            "query".into(),
            json!(python_str(round_data.get("query").unwrap_or(&Value::Null))),
        );
        normalized_round.insert("answer".into(), json!(answer));
        normalized_round.insert("results".into(), Value::Array(round_results));
        normalized_round.insert(
            "response_time".into(),
            round_data
                .get("response_time")
                .cloned()
                .unwrap_or(Value::Null),
        );
        if python_truthy(round_data.get("error").unwrap_or(&Value::Null)) {
            normalized_round.insert(
                "error".into(),
                json!(python_str(round_data.get("error").unwrap_or(&Value::Null))),
            );
        }
        if python_truthy(round_data.get("retried").unwrap_or(&Value::Null)) {
            normalized_round.insert("retried".into(), json!(true));
            normalized_round.insert(
                "retryQuery".into(),
                json!(python_str(
                    round_data.get("retryQuery").unwrap_or(&Value::Null)
                )),
            );
        }
        if python_truthy(round_data.get("retryError").unwrap_or(&Value::Null)) {
            normalized_round.insert(
                "retryError".into(),
                json!(python_str(
                    round_data.get("retryError").unwrap_or(&Value::Null)
                )),
            );
        }
        normalized_rounds.push(Value::Object(normalized_round));
    }

    let status = status.map(str::to_string).unwrap_or_else(|| {
        if normalized_rounds
            .iter()
            .any(|round| round.get("status").and_then(Value::as_str) == Some("searching"))
        {
            "searching".to_string()
        } else if !normalized_rounds.is_empty()
            && normalized_rounds
                .iter()
                .all(|round| round.get("status").and_then(Value::as_str) == Some("error"))
        {
            "error".to_string()
        } else {
            "done".to_string()
        }
    });

    // `"\n\n".join(dict.fromkeys(answers))` — de-duplicated, order-preserving.
    let mut unique_answers: Vec<String> = Vec::new();
    for answer in answers {
        if !unique_answers.contains(&answer) {
            unique_answers.push(answer);
        }
    }

    json!({
        "status": status,
        "query": query,
        "reason": search_reason_for_query(query),
        "answer": unique_answers.join("\n\n"),
        "results": rerank_search_results(&results, query, SEARCH_TOTAL_RESULT_LIMIT),
        "rounds": normalized_rounds,
        "response_time": Value::Null,
        "cached": false,
    })
}

/// Mirrors `compact_search_tool_result`: the projection the model actually sees.
pub fn compact_search_tool_result(round_data: &Value, intent: &str, citation_offset: i64) -> Value {
    let mut results_for_model: Vec<Value> = Vec::new();
    let items = round_data
        .get("results")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for (position, item) in items.iter().take(SEARCH_RESULT_LIMIT).enumerate() {
        let Some(object) = item.as_object() else {
            continue;
        };
        let index = position as i64 + 1;
        let content = python_str(object.get("content").unwrap_or(&Value::Null));
        let raw = python_str(object.get("raw_content").unwrap_or(&Value::Null));
        let snippet = if content.is_empty() { raw } else { content };
        let citation_id = python_str(object.get("citation_id").unwrap_or(&Value::Null));
        let citation_id = if citation_id.is_empty() {
            format!("W{}", citation_offset + index)
        } else {
            citation_id
        };
        results_for_model.push(json!({
            "cite": format!("[^{citation_id}]"),
            "citation_id": citation_id,
            "title": truncate_chars(&python_str(object.get("title").unwrap_or(&Value::Null)), 180),
            "url": python_str(object.get("url").unwrap_or(&Value::Null)),
            "snippet": truncate_chars(&snippet, 600),
        }));
    }
    // `json!` does not accept a block expression as a value, so the two fallbacks are
    // computed first.
    let retry_query = round_data.get("retryQuery").cloned().unwrap_or(Value::Null);
    let retry_query = if python_truthy(&retry_query) {
        retry_query
    } else {
        json!("")
    };
    let retry_error = round_data.get("retryError").cloned().unwrap_or(Value::Null);
    let retry_error = if python_truthy(&retry_error) {
        retry_error
    } else {
        json!("")
    };
    json!({
        "query": python_str(round_data.get("query").unwrap_or(&Value::Null)),
        "round": python_int_opt(round_data.get("round")).unwrap_or(0),
        "intent": if intent.trim().is_empty() { "general" } else { intent },
        "answer": truncate_chars(&python_str(round_data.get("answer").unwrap_or(&Value::Null)), 600),
        "results": results_for_model,
        "status": python_str(round_data.get("status").unwrap_or(&Value::Null)).if_empty("done"),
        "error": round_data.get("error").cloned().unwrap_or(Value::Null),
        "retried": python_truthy(round_data.get("retried").unwrap_or(&Value::Null)),
        "retryQuery": retry_query,
        "retryError": retry_error,
        "cached": python_truthy(round_data.get("cached").unwrap_or(&Value::Null)),
    })
}

/// Mirrors `search_round_status`.
pub fn search_round_status(query: &str, round_index: i64, status: &str, error: &str) -> Value {
    let mut result = Map::new();
    result.insert("round".into(), json!(round_index));
    result.insert("status".into(), json!(status));
    result.insert("query".into(), json!(query));
    result.insert("answer".into(), json!(""));
    result.insert("results".into(), json!([]));
    result.insert("response_time".into(), Value::Null);
    if !error.is_empty() {
        result.insert("error".into(), json!(error));
    }
    Value::Object(result)
}

/// Mirrors `rounds_in_order`: ascending by round index.
pub fn rounds_in_order(rounds_by_index: &BTreeMap<i64, Value>) -> Vec<Value> {
    rounds_by_index.values().cloned().collect()
}

/// Mirrors `search_round_from_cache`.
pub fn search_round_from_cache(query: &str, cached: &Value, round_index: i64) -> Value {
    let stored_query = python_str(cached.get("query").unwrap_or(&Value::Null));
    let results: Vec<Value> = cached
        .get("results")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter(|item| item.is_object())
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    json!({
        "query": if stored_query.is_empty() { query.to_string() } else { stored_query },
        "round": round_index,
        "status": python_str(cached.get("status").unwrap_or(&Value::Null)).if_empty("done"),
        "answer": python_str(cached.get("answer").unwrap_or(&Value::Null)),
        "results": results,
        "response_time": cached.get("response_time").cloned().unwrap_or(Value::Null),
        "cached": true,
    })
}

/// Mirrors `should_retry_tavily_error`: a missing key or a bad payload is never retried;
/// a timeout — or an upstream status in the retryable set — is.
pub fn should_retry_tavily_error(error: &AppError) -> bool {
    if error.code == codes::MISSING_API_KEY || error.code == codes::INVALID_PAYLOAD {
        return false;
    }
    error.code == codes::UPSTREAM_TIMEOUT
        || matches!(error.status, 408 | 429 | 500 | 502 | 503 | 504)
}

// --- the cache -------------------------------------------------------------------

/// Mirrors `search_cache_key`: `sha256(whitespace-collapsed-lowercase)[:32]`.
///
/// Lowercasing here is what makes the cache case-insensitive, since the callers pass
/// the raw query through.
pub fn search_cache_key(query: &str) -> String {
    let normalized = whitespace()
        .replace_all(&query.to_lowercase(), " ")
        .trim()
        .to_string();
    let digest = Sha256::digest(normalized.as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    hex[..32].to_string()
}

/// Mirrors `load_search_cache`. A **stale** entry is `None` rather than an error, and
/// anything unreadable or malformed is `None` too.
pub fn load_search_cache(root: &Path, query: &str, now_epoch: i64) -> Option<Value> {
    let path = search_cache_dir(root).join(format!("{}.json", search_cache_key(query)));
    let metadata = std::fs::metadata(&path).ok()?;
    let modified = metadata.modified().ok()?;
    let mtime = modified
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    if now_epoch - mtime > SEARCH_CACHE_MAX_AGE_SECONDS {
        return None;
    }
    let raw = std::fs::read_to_string(&path).ok()?;
    let value: Value = serde_json::from_str(&raw).ok()?;
    if value.is_object() { Some(value) } else { None }
}

/// Mirrors `cleanup_search_cache`: unlink every `*.json` older than the max age.
pub fn cleanup_search_cache(root: &Path, now_epoch: i64) {
    let directory = search_cache_dir(root);
    if !directory.exists() {
        return;
    }
    let Ok(entries) = std::fs::read_dir(&directory) else {
        return;
    };
    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
            continue;
        }
        let Ok(metadata) = std::fs::metadata(&path) else {
            continue;
        };
        let Ok(modified) = metadata.modified() else {
            continue;
        };
        let mtime = modified
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_secs() as i64)
            .unwrap_or(0);
        if now_epoch - mtime <= SEARCH_CACHE_MAX_AGE_SECONDS {
            continue;
        }
        let _ = std::fs::remove_file(&path);
    }
}

/// Mirrors `save_search_cache`.
///
/// Note the two behaviours worth keeping: it **prunes first**, so a write is also a
/// cleanup, and the temp file is `with_suffix(".tmp")` — which **replaces** `.json`
/// rather than appending, so `abc.json` is written through `abc.tmp`.
pub fn save_search_cache(
    root: &Path,
    query: &str,
    data: &Value,
    now_epoch: i64,
) -> std::io::Result<()> {
    let directory = search_cache_dir(root);
    std::fs::create_dir_all(&directory)?;
    cleanup_search_cache(root, now_epoch);
    let path = directory.join(format!("{}.json", search_cache_key(query)));
    let temp_path = path.with_extension("tmp");
    std::fs::write(
        &temp_path,
        serde_json::to_string(data).unwrap_or_else(|_| "null".into()),
    )?;
    std::fs::rename(&temp_path, &path)
}

// --- helpers ---------------------------------------------------------------------

/// `str(value or "")`, the oracle's stringification.
fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) if !text.is_empty() => text.clone(),
        Value::Number(number) => number.to_string(),
        Value::Bool(true) => "True".to_string(),
        _ => String::new(),
    }
}

/// `float(value or 0)`, or `None` where Python raises.
fn python_float_opt(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let number = match value {
        Value::Bool(flag) => f64::from(u8::from(*flag)),
        Value::Number(number) => number.as_f64()?,
        Value::String(text) => text.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    number.is_finite().then_some(number)
}

fn truncate_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

trait IfEmpty {
    fn if_empty(self, fallback: &str) -> String;
}

impl IfEmpty for String {
    fn if_empty(self, fallback: &str) -> String {
        if self.is_empty() {
            fallback.to_string()
        } else {
            self
        }
    }
}

fn cached(pattern: &str) -> &'static Regex {
    let mut cache = pattern_cache()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if let Some(found) = cache.get(pattern) {
        return found;
    }
    let leaked: &'static Regex = Box::leak(Box::new(
        Regex::new(pattern).expect("static pattern must compile"),
    ));
    cache.insert(pattern.to_string(), leaked);
    leaked
}

fn pattern_cache() -> &'static std::sync::Mutex<std::collections::HashMap<String, &'static Regex>> {
    static CACHE: OnceLock<std::sync::Mutex<std::collections::HashMap<String, &'static Regex>>> =
        OnceLock::new();
    CACHE.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

fn case_insensitive(pattern: &str) -> &'static Regex {
    cached(&format!("(?i){pattern}"))
}

fn whitespace() -> &'static Regex {
    cached(r"\s+")
}

fn non_word() -> &'static Regex {
    cached(r"[^\w\s\u4e00-\u9fff]+")
}

fn fresh_query_pattern() -> &'static Regex {
    case_insensitive(
        r"(今天|今日|昨天|明天|本周|本月|最新|现在|近期|刚刚|实时|新闻|价格|票房|天气|汇率|股价|today|latest|current|recent|now|news|price|weather|schedule|release|version)",
    )
}

fn lookup_query_pattern() -> &'static Regex {
    case_insensitive(
        r"(查一下|搜索|联网|网上|来源|引用|网址|官网|文档|政策|法规|标准|榜单|排名|评测|search|browse|look up|source|citation|official docs)",
    )
}

fn bare_domain_pattern() -> &'static Regex {
    case_insensitive(r"https?://|www\.|[a-z0-9-]+\.(com|org|net|io|dev|cn|edu|gov)")
}
