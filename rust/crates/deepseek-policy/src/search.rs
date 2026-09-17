//! Tavily search: query planning, response normalization, ranking, the cache, and
//! the prompt-context formatters.
//!
//! Mirrors `infra/tool_runtime/search.py` — everything except the concrete HTTP
//! client, which arrives injected as a [`Transport`] — plus the three payload
//! predicates the oracle defines in `gateway/deepseek_client.py`
//! ([`search_mode`], [`forced_search_mode`], [`search_tool_enabled`]). They are
//! ported into this module rather than the gateway crate because their consumers
//! here are the tool catalog and `tools_for_payload`; the parity probe extracts
//! them from the oracle's own file, so the placement is noted rather than hidden.
//!
//! [`format_search_context`] / [`format_search_failure_context`] build the
//! **prompt context** that `search_if_needed` injects as `searchContext` at
//! request-assembly time. The Rust request-assembly layer does not exist yet, so
//! nothing in this workspace calls them: they are the offline, byte-verified first
//! step of the search-prefetch pipeline recorded in
//! `tasks/native-runtime/continuation.md`, whose remaining links
//! (`search_if_needed`, `search_multiple`, the taint firewall, the consumer) are
//! later slices.
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

// --- the transport ---------------------------------------------------------------

/// `TAVILY_URL`.
pub const TAVILY_URL: &str = "https://api.tavily.com/search";
/// `TAVILY_TIMEOUT_SECONDS`.
pub const TAVILY_TIMEOUT_SECONDS: u64 = 45;

/// What a transport attempt produced.
///
/// The two arms mirror the oracle's two `except` clauses, because the *mapping* to an
/// `AppError` is part of `search_tavily` rather than of the HTTP client. Keeping them
/// separate is what lets the mapping be verified offline.
pub enum TransportOutcome {
    /// A response arrived — any status, with its body.
    Response { status: u16, body: Vec<u8> },
    /// The request never completed. `reason` is the transport's own message.
    Failure { reason: String, timed_out: bool },
    /// The transport itself already classified the attempt as an `AppError`.
    ///
    /// This is the seam the parity probe uses: the Python probe drives the retry policy
    /// by raising an `AppError` from a stubbed `search_tavily`, so the Rust side has to be
    /// able to inject one at the same point. Without it the probe would be comparing
    /// "mapping **and** policy" against Python's "policy alone".
    Rejected(AppError),
}

/// Perform one POST. Injected so `search_tavily` stays testable without a network.
pub type Transport = dyn Fn(&str, &[u8], &[(&str, &str)]) -> TransportOutcome;

/// Mirrors `format_upstream_error`.
///
/// An `error.message` (or `error.type`) wins; otherwise the **first 500 characters** of
/// the raw text, or `DeepSeek API error` when that is empty.
pub fn format_upstream_error(raw: &str) -> String {
    if let Ok(parsed) = serde_json::from_str::<Value>(raw) {
        if let Some(error) = parsed.get("error").and_then(Value::as_object) {
            // `error.get("message") or error.get("type")` — the `or` tests the **values**'
            // truthiness, so an empty-string `message` falls through to `type`. Checking
            // only for the key's presence (or for the rendered text being empty) misses
            // that: the probe caught `{"message": "", "type": "x"}` returning the whole raw
            // text instead of `x`.
            let message = error
                .get("message")
                .filter(|value| python_truthy(value))
                .or_else(|| error.get("type").filter(|value| python_truthy(value)));
            if let Some(message) = message {
                return crate::python_json::value_str(message);
            }
        }
    }
    let truncated: String = raw.chars().take(500).collect();
    if truncated.is_empty() {
        "DeepSeek API error".to_string()
    } else {
        truncated
    }
}

/// Mirrors the request body `search_tavily` assembles.
///
/// The key order is the oracle's dict-merge order — `query` first, then the options in
/// their insertion order, then the domain filters — because the body is serialized and
/// sent. `search_depth`, `include_answer` and `include_raw_content` are **updated in
/// place** by the intent rules, so they keep their original positions rather than moving
/// to the end.
///
/// Serialized with default separators and `ensure_ascii=True`, matching
/// `json.dumps(request_body)`.
pub fn tavily_request_body_json(query: &str) -> String {
    let options = tavily_options_for_query(query);
    let filters = search_domain_filters(query);
    let order = [
        "query",
        "topic",
        "search_depth",
        "max_results",
        "include_answer",
        "include_raw_content",
        "include_images",
        "include_favicon",
    ];

    let mut merged = Map::new();
    // `query[:500]` — truncated by characters, as Python slices strings.
    merged.insert(
        "query".to_string(),
        json!(query.chars().take(500).collect::<String>()),
    );
    if let Some(fields) = options.as_object() {
        for (key, value) in fields {
            merged.insert(key.clone(), value.clone());
        }
    }
    if let Some(fields) = filters.as_object() {
        for (key, value) in fields {
            merged.insert(key.clone(), value.clone());
        }
    }

    let mut parts: Vec<String> = Vec::new();
    let mut emitted: Vec<&str> = Vec::new();
    for key in order {
        if let Some(value) = merged.get(key) {
            parts.push(format!(
                "{}: {}",
                crate::python_json::escaped_string(&format!("\"{key}\"")),
                crate::python_json::dumps_default_separators_ascii(value)
            ));
            emitted.push(key);
        }
    }
    // Anything the options or filters added that is not in the declared order goes last,
    // in sorted order — so a schema drift shows up instead of being dropped.
    for (key, value) in &merged {
        if emitted.contains(&key.as_str()) {
            continue;
        }
        parts.push(format!(
            "{}: {}",
            crate::python_json::dumps_default_separators_ascii(&json!(key)),
            crate::python_json::dumps_default_separators_ascii(value)
        ));
    }
    format!("{{{}}}", parts.join(", "))
}

/// Mirrors `search_tavily`.
///
/// A missing key is a 503 `missing_api_key`; a non-2xx response is an
/// `upstream_failure` whose status is `min(status, 502)`; and a transport failure is
/// `upstream_timeout` when the reason mentions a timeout, otherwise `upstream_failure`,
/// both at 502. The successful body is passed through [`normalize_search_response`].
pub fn search_tavily(
    query: &str,
    tavily_api_key: &str,
    transport: &Transport,
) -> Result<Value, AppError> {
    let api_key = tavily_api_key.trim();
    if api_key.is_empty() {
        return Err(AppError {
            message: "Tavily search is not configured. Set TAVILY_API_KEY or provide tavilyApiKey in the request."
                .to_string(),
            code: codes::MISSING_API_KEY,
            status: 503,
        });
    }

    let body = tavily_request_body_json(query);
    let headers = [
        ("Authorization", format!("Bearer {api_key}")),
        ("Content-Type", "application/json".to_string()),
        ("Accept", "application/json".to_string()),
    ];
    let header_refs: Vec<(&str, &str)> = headers
        .iter()
        .map(|(name, value)| (*name, value.as_str()))
        .collect();

    match transport(TAVILY_URL, body.as_bytes(), &header_refs) {
        TransportOutcome::Response { status, body } => {
            if !(200..300).contains(&status) {
                let detail = String::from_utf8_lossy(&body);
                return Err(AppError {
                    message: format!("Tavily search failed: {}", format_upstream_error(&detail)),
                    code: codes::UPSTREAM_FAILURE,
                    status: status.min(502),
                });
            }
            let parsed: Value = serde_json::from_slice(&body).map_err(|_| AppError {
                message: "Tavily search failed: malformed response".to_string(),
                code: codes::UPSTREAM_FAILURE,
                status: 502,
            })?;
            Ok(normalize_search_response(query, &parsed))
        }
        TransportOutcome::Rejected(error) => Err(error),
        TransportOutcome::Failure { reason, timed_out } => Err(AppError {
            message: format!("Cannot reach Tavily API: {reason}"),
            code: if timed_out {
                codes::UPSTREAM_TIMEOUT
            } else {
                codes::UPSTREAM_FAILURE
            },
            status: 502,
        }),
    }
}

/// Mirrors `search_tavily_with_retry`.
///
/// One retry, on a simplified query, and only when [`should_retry_tavily_error`] says so
/// and the simplification actually differs from the original. Note the two different
/// failure shapes: if the retry **also** fails this returns an error *round status* as a
/// value (so the caller records it as a round), whereas a retry that is not worth
/// attempting re-raises the original error.
pub fn search_tavily_with_retry(
    query: &str,
    tavily_api_key: &str,
    transport: &Transport,
) -> Result<Value, AppError> {
    let first = match search_tavily(query, tavily_api_key, transport) {
        Ok(value) => return Ok(value),
        Err(error) => error,
    };

    let retry_query = simplified_retry_query(query);
    if retry_query.is_empty()
        || retry_query.to_lowercase() == normalize_search_query_text(query).to_lowercase()
        || !should_retry_tavily_error(&first)
    {
        return Err(first);
    }

    match search_tavily(&retry_query, tavily_api_key, transport) {
        Ok(mut retried) => {
            retried["query"] = json!(normalize_search_query_text(query));
            retried["retried"] = json!(true);
            retried["retryQuery"] = json!(retry_query);
            retried["originalError"] = json!(first.message);
            Ok(retried)
        }
        Err(retry_error) => {
            let mut status = search_round_status(
                query,
                0,
                "error",
                &format!("{}; retry failed: {}", first.message, retry_error.message),
            );
            status["retried"] = json!(true);
            status["retryQuery"] = json!(retry_query);
            status["retryError"] = json!(retry_error.message);
            Ok(status)
        }
    }
}

/// Mirrors `search_multiple`: the module's only concurrency — a bounded pool over
/// the planned queries, with rounds folded in **completion order**.
///
/// # The parallel shape, reproduced rather than approximated
///
/// - a **cache hit** short-circuits: `{**cached, "cached": True}` goes to the
///   progress callback and is returned with no search at all;
/// - every round is recorded as `"searching"` and announced to the progress
///   callback **in submission order**, with those snapshots' aggregate status
///   forced to `"searching"`;
/// - completions are processed in the order they **finish** — `as_completed`, not
///   submission order — each updating its round and firing the callback with the
///   status left for the aggregate to infer;
/// - a completed round has `round` and `status` re-stamped by this caller
///   (`str(round_data.get("status") or "done")`);
/// - both of the oracle's failure arms produce the **same** round: an `AppError`
///   and any other exception each become `search_round_status(..., "error",
///   str(exc))`, differing only in logging, which is not part of the contract.
///   Here the second arm is a panicking transport, caught per task so the pool
///   survives the way a Python future swallows its exception;
/// - the cache is written only when the aggregate has results, and a **failed
///   write is an error** — the oracle's `save_search_cache` raises out of
///   `search_multiple`, so this returns `Err` rather than silently returning
///   results the oracle would have lost. The message is this port's own (the
///   oracle's is whatever `OSError` produced) and the code is the documented
///   mapping, as with the stores' `TypeError`.
///
/// The worker bound is the oracle's `max(1, min(len(queries), SEARCH_ROUND_LIMIT))`.
/// It never queues today — `search_queries_for` caps at [`SEARCH_ROUND_LIMIT`], so
/// every query has its own worker — but the bound is kept (work is chunked as
/// `index % workers`) so raising the limit cannot silently over-parallelise. The
/// workers also start after the submission announcements rather than during them;
/// nothing they do is observable until the `as_completed` loop runs, which in the
/// oracle also only starts once every round is submitted.
///
/// The transport is a generic parameter bounded `Fn + Send + Sync + 'static`: it
/// crosses the worker threads, and the `Transport` alias already requires
/// 'static objects (every transport this crate has passed is one — owned
/// clients and owned probe tables alike). The rounds map and the progress
/// callback stay on the calling thread, as in the oracle.
pub fn search_multiple<F>(
    query: &str,
    tavily_api_key: &str,
    transport: &F,
    cache_root: &Path,
    now_epoch: i64,
    progress: &mut dyn FnMut(&Value),
) -> Result<Value, AppError>
where
    F: Fn(&str, &[u8], &[(&str, &str)]) -> TransportOutcome + Send + Sync + 'static,
{
    if let Some(cached) = load_search_cache(cache_root, query, now_epoch) {
        let mut hit = cached;
        if let Some(object) = hit.as_object_mut() {
            object.insert("cached".to_string(), json!(true));
        }
        progress(&hit);
        return Ok(hit);
    }

    let queries = search_queries_for(query);
    let mut rounds_by_index: BTreeMap<i64, Value> = BTreeMap::new();

    // Submission: every round enters as "searching" and is announced in order.
    for (position, search_query) in queries.iter().enumerate() {
        let round_index = (position + 1) as i64;
        rounds_by_index.insert(
            round_index,
            search_round_status(search_query, round_index, "searching", ""),
        );
        progress(&aggregate_search_rounds(
            query,
            &rounds_in_order(&rounds_by_index),
            Some("searching"),
        ));
    }

    let worker_count = queries.len().min(SEARCH_ROUND_LIMIT);
    std::thread::scope(|scope| {
        let (sender, receiver) = std::sync::mpsc::channel::<(usize, Result<Value, String>)>();
        for worker in 0..worker_count {
            let sender = sender.clone();
            let queries = &queries;
            scope.spawn(move || {
                for index in (worker..queries.len()).step_by(worker_count) {
                    // `catch_unwind` is the `except Exception` arm: a panicking
                    // transport must not take the pool — and the other rounds — down.
                    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        search_tavily_with_retry(
                            &queries[index],
                            tavily_api_key,
                            transport as &Transport,
                        )
                    }));
                    let payload = match outcome {
                        Ok(Ok(value)) => Ok(value),
                        Ok(Err(error)) => Err(error.message),
                        Err(panic) => Err(panic_message(&panic)),
                    };
                    if sender.send((index, payload)).is_err() {
                        break;
                    }
                }
            });
        }
        drop(sender);

        // `as_completed`: each round is folded in as it finishes, in finish order.
        for _ in 0..queries.len() {
            let (position, payload) = receiver
                .recv()
                .expect("every submitted round reports exactly once");
            let round_index = (position + 1) as i64;
            let round_data = match payload {
                Ok(mut data) => {
                    if let Some(object) = data.as_object_mut() {
                        let status = python_str(object.get("status").unwrap_or(&Value::Null));
                        object.insert("round".to_string(), json!(round_index));
                        object.insert("status".to_string(), json!(status.if_empty("done")));
                    }
                    data
                }
                Err(message) => {
                    search_round_status(&queries[position], round_index, "error", &message)
                }
            };
            rounds_by_index.insert(round_index, round_data);
            progress(&aggregate_search_rounds(
                query,
                &rounds_in_order(&rounds_by_index),
                None,
            ));
        }
    });

    let result = aggregate_search_rounds(query, &rounds_in_order(&rounds_by_index), None);
    if python_truthy(result.get("results").unwrap_or(&Value::Null)) {
        save_search_cache(cache_root, query, &result, now_epoch).map_err(|error| AppError {
            message: format!("Cannot write search cache: {error}"),
            code: codes::INTERNAL,
            status: 500,
        })?;
    }
    Ok(result)
}

/// Mirrors `search_mode` (`gateway/deepseek_client.py`): the payload's mode,
/// trimmed and lowercased, defaulting to `auto` — via an **`or`** on the raw
/// value, so every falsy spelling (missing, `null`, `""`, `false`, `0`, `0.0`)
/// becomes `auto`. The sibling `should_search_for_query` defaults its mode to
/// `""` instead; the two `or` chains are genuinely different and must not be
/// unified.
pub fn search_mode(payload: &Value) -> String {
    let raw = payload.get("searchMode").unwrap_or(&Value::Null);
    let text = if python_truthy(raw) {
        crate::python_json::value_str(raw)
    } else {
        "auto".to_string()
    };
    text.trim().to_lowercase()
}

/// Mirrors `forced_search_mode`: the user explicitly demanded a search.
pub fn forced_search_mode(payload: &Value) -> bool {
    matches!(search_mode(payload).as_str(), "on" | "force" | "true" | "1")
}

/// Mirrors `search_tool_enabled`.
///
/// Note `payload.get("searchEnabled") is not True`: an **identity** check, so a JSON
/// `1` or `"true"` does not enable the tool — only the boolean `true` does. Reading it
/// for truthiness would enable search for values the oracle refuses.
pub fn search_tool_enabled(payload: &Value) -> bool {
    if payload.get("searchEnabled") != Some(&Value::Bool(true)) {
        return false;
    }
    !matches!(search_mode(payload).as_str(), "off" | "false" | "0")
}

// --- prompt context --------------------------------------------------------------

/// Mirrors `format_search_context`: the per-turn web-search block that joins the prompt.
///
/// Two details are easy to "clean up" into a divergence:
///
/// - the query line is `search_data.get("query", "")` inside an f-string. A **missing**
///   key renders as empty, but a present `null` renders as Python's `str(None)` —
///   `"None"`. Collapsing both to empty would change the prompt bytes.
/// - `raw_content or content` is a truthiness `or`, so an empty `raw_content` falls
///   through to `content`.
///
/// The block is emitted with `\n` joins and no trailing newline, and the answer and
/// result sections are separated by **blank lines**, which is what makes the guard text
/// and the sources legible as separate sections to the model.
///
/// # Measured divergence, kept on purpose
///
/// The oracle reads `result.get("title")` off **every** entry, so a non-dict entry —
/// or a non-array `results` — raises `AttributeError` / `TypeError` and the request
/// fails. This port renders non-dict entries through the fallbacks and treats a
/// non-array `results` as empty. Both shapes are unreachable from the wired
/// pipeline (`normalize_search_response` / `aggregate_search_rounds` guarantee dict
/// entries in a list) and reachable only from a hand-corrupted cache file, where
/// the oracle's own behaviour is an uncontrolled 500. Pinned by a unit test so the
/// tolerance is a recorded decision rather than an accident.
pub fn format_search_context(search_data: &Value) -> String {
    let mut lines: Vec<String> = vec![
        "When citing these web sources, use the exact [^Wn] markers shown below.".to_string(),
        "你可以使用以下联网搜索结果回答用户问题。".to_string(),
        format!(
            "搜索问题: {}",
            python_str_verbatim(search_data.get("query"))
        ),
        "要求:".to_string(),
        "1. 只在搜索结果支持时给出时效性结论。".to_string(),
        "2. 引用来源时在论断后追加对应的 [^Wn] 标记，不要写 [来源]/[Source] 或 Markdown 链接。"
            .to_string(),
        "3. 具体日期、价格、版本号、政策、新闻结论后必须给出来源链接。".to_string(),
        "4. 不要引用未出现在搜索来源里的网页。".to_string(),
        "5. 如果结果不足或互相矛盾，请明确说明不确定。".to_string(),
        "6. 优先使用官方、原始、权威来源。".to_string(),
        "7. 如已有结果足以回答，不要继续搜索；只有缺少关键事实时最多再补充 1 次 web_search。"
            .to_string(),
    ];

    let answer = python_str(search_data.get("answer").unwrap_or(&Value::Null))
        .trim()
        .to_string();
    if !answer.is_empty() {
        lines.push(String::new());
        lines.push(format!("Tavily 摘要: {answer}"));
    }

    let results = search_data
        .get("results")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if !results.is_empty() {
        lines.push(String::new());
        lines.push("搜索来源:".to_string());
    }

    for (position, result) in results.iter().take(SEARCH_CONTEXT_RESULT_LIMIT).enumerate() {
        let index = position as i64 + 1;
        let title = python_str(result.get("title").unwrap_or(&Value::Null));
        let title = if title.is_empty() {
            format!("来源 {index}")
        } else {
            title
        };
        let citation_id = python_str(result.get("citation_id").unwrap_or(&Value::Null));
        let citation_id = if citation_id.is_empty() {
            format!("W{index}")
        } else {
            citation_id
        };
        let url = python_str(result.get("url").unwrap_or(&Value::Null));
        // `raw_content or content`, then `.strip()`.
        let raw = python_str(result.get("raw_content").unwrap_or(&Value::Null));
        let content = if raw.is_empty() {
            python_str(result.get("content").unwrap_or(&Value::Null))
        } else {
            raw
        };
        let content = content.trim();

        lines.push(String::new());
        lines.push(format!("[^{citation_id}] {title}"));
        lines.push(format!("URL: {url}"));
        if !content.is_empty() {
            lines.push(format!(
                "内容摘录: {}",
                truncate_chars(content, SEARCH_RAW_CONTENT_CHARS)
            ));
        }
    }
    lines.join("\n")
}

/// Mirrors `format_search_failure_context`.
///
/// Only the **first three** errors are listed, and an empty list still renders a bullet
/// (`- 未知错误`) rather than an empty section.
pub fn format_search_failure_context(search_data: &Value) -> String {
    let errors: Vec<String> = search_data
        .get("rounds")
        .and_then(Value::as_array)
        .map(|rounds| {
            rounds
                .iter()
                .filter(|round| round.is_object())
                .filter_map(|round| {
                    let error = python_str(round.get("error").unwrap_or(&Value::Null));
                    if error.is_empty() { None } else { Some(error) }
                })
                .take(3)
                .collect()
        })
        .unwrap_or_default();

    let listed = if errors.is_empty() {
        "- 未知错误".to_string()
    } else {
        errors
            .iter()
            .map(|error| format!("- {error}"))
            .collect::<Vec<String>>()
            .join("\n")
    };

    [
        "本轮尝试联网搜索，但搜索没有得到可用来源。",
        "回答时不要声称已经查到最新资料。",
        "如果问题依赖实时信息，请明确说明无法确认最新状态。",
        "",
        "搜索错误:",
        &listed,
    ]
    .join("\n")
}

// --- helpers ---------------------------------------------------------------------

/// `str(value)` for a value that may be absent, where the two cases differ.
///
/// `search_data.get("query", "")` inside an f-string renders a **missing** key as
/// empty but a present `null` as `None`. `python_str` collapses both to empty,
/// which is right for the `x or fallback` idiom and wrong here. Container values
/// render through [`crate::python_json::value_str`], whose JSON quoting is this
/// crate's standing approximation of Python's `repr` — unreachable from the wired
/// pipeline, where `query` is always a string.
fn python_str_verbatim(value: Option<&Value>) -> String {
    match value {
        None => String::new(),
        Some(value) => crate::python_json::value_str(value),
    }
}

/// `str(value or "")`, the oracle's stringification.
///
/// The `or` reads the **raw** value's truthiness, so a falsy spelling — `null`,
/// `""`, `false`, `0`, an empty array — renders as the empty string. This helper
/// once matched on the rendered text instead, which let a numeric `0` through as
/// `"0"`: that difference flips `should_search_for_query` for
/// `{"searchMode": 0}` (the oracle falls through to text matching, `"0"` is the
/// off mode) and turns `{"answer": 0}` into a `Tavily 摘要` line.
fn python_str(value: &Value) -> String {
    if python_truthy(value) {
        crate::python_json::value_str(value)
    } else {
        String::new()
    }
}

/// `str(exc)` for a caught panic — the message of the `except Exception` arm.
///
/// A `panic!` with a string payload is this port's `raise RuntimeError(...)`,
/// which is what the probe drives; any other payload keeps a fixed description,
/// because there is no faithful rendering of an arbitrary one.
///
/// The parameter is the `Box` itself, not `&(dyn Any + Send)`: coercing a trait
/// object into another trait object re-vtables it as an `Any` implementor of its
/// own, whose `type_id` is the *object type's* — and every downcast then misses.
/// Downcasting through the box keeps the original vtable. Measured, not
/// inferred: the direct downcast worked while the coerced one did not.
fn panic_message(panic: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(text) = panic.downcast_ref::<&'static str>() {
        (*text).to_string()
    } else if let Some(text) = panic.downcast_ref::<String>() {
        text.clone()
    } else {
        "search round panicked".to_string()
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_falsy_mode_spelling_is_auto_not_the_rendered_text() {
        // `str(payload.get("searchMode") or "auto")` reads the raw value's
        // truthiness: `0` is falsy, so the oracle renders "auto", which neither
        // forces nor disables. Reading the rendered text instead gives "0" — an
        // off-mode spelling — and flips `search_tool_enabled`.
        for mode in [json!(0), json!(0.0), json!(false), json!(""), json!(null)] {
            let payload = json!({"searchEnabled": true, "searchMode": mode});
            assert_eq!(search_mode(&payload), "auto");
            assert!(search_tool_enabled(&payload));
            assert!(!forced_search_mode(&payload));
        }
        let payload = json!({"searchEnabled": true, "searchMode": "0"});
        assert_eq!(search_mode(&payload), "0");
        assert!(!search_tool_enabled(&payload));
    }

    #[test]
    fn a_falsy_mode_falls_through_to_text_matching() {
        // `should_search_for_query` defaults its mode to "" (not "auto"), so a
        // numeric 0 must fall through to the query-text patterns rather than be
        // read as the off mode.
        assert!(should_search_for_query(
            "最新消息",
            &json!({"searchMode": 0})
        ));
        assert!(!should_search_for_query(
            "随便聊聊",
            &json!({"searchMode": 0})
        ));
    }

    #[test]
    fn the_query_line_renders_missing_and_null_differently() {
        assert!(format_search_context(&json!({})).contains("搜索问题: \n"));
        assert!(format_search_context(&json!({"query": null})).contains("搜索问题: None\n"));
        assert!(format_search_context(&json!({"query": 7})).contains("搜索问题: 7\n"));
    }

    #[test]
    fn falsy_fields_take_the_fallbacks_of_their_or_chains() {
        let text = format_search_context(&json!({
            "answer": 0,
            "results": [{"title": 0, "citation_id": 0, "raw_content": 0, "content": " c ", "url": ""}],
        }));
        assert!(
            !text.contains("Tavily 摘要"),
            "a falsy answer renders no summary line"
        );
        assert!(text.contains("[^W1] 来源 1\nURL: \n"));
        // the excerpt is the final line: the block carries no trailing newline.
        assert!(text.ends_with("内容摘录: c"));
    }

    #[test]
    fn a_non_dict_result_entry_renders_where_the_oracle_raises() {
        // The oracle calls `result.get("title")` on every entry, so a non-dict
        // entry raises AttributeError and the request fails. The port renders it
        // through the fallbacks instead — see the divergence note on
        // `format_search_context`. Pinned so the tolerance stays a decision.
        let text = format_search_context(&json!({"results": ["not-a-dict", {"url": "u"}]}));
        assert!(text.contains("[^W1] 来源 1"));
        assert!(text.contains("[^W2] 来源 2\nURL: u"));
    }

    #[test]
    fn failure_errors_render_through_str_and_keep_the_first_three() {
        let text = format_search_failure_context(&json!({
            "rounds": [{"error": true}, {"error": 0}, {"error": "e"}, {"error": "f"}, {"error": "g"}]
        }));
        assert!(
            text.contains("- True\n- e\n- f"),
            "a bool renders as Python's True; a falsy 0 is skipped"
        );
        assert!(
            !text.contains("g"),
            "only the first three errors are listed"
        );
        assert!(format_search_failure_context(&json!({})).ends_with("- 未知错误"));
    }

    fn temp_root(label: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("search-multiple-{label}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn an_empty_query_aggregates_without_searching() {
        let root = temp_root("empty");
        let boom = |_url: &str, _body: &[u8], _headers: &[(&str, &str)]| -> TransportOutcome {
            panic!("the empty query must not search");
        };
        let mut seen = Vec::new();
        let result = search_multiple("", "k", &boom, &root, 1_700_000_000, &mut |payload| {
            seen.push(payload.clone())
        })
        .expect("no results means no cache write");
        assert_eq!(result["status"], json!("done"));
        assert_eq!(result["results"], json!([]));
        assert_eq!(result["rounds"], json!([]));
        assert!(seen.is_empty(), "no submission or completion is announced");
        let saved = search_cache_dir(&root).join(format!("{}.json", search_cache_key("")));
        assert!(!saved.exists(), "nothing is cached");
    }

    #[test]
    fn a_cache_hit_returns_without_searching() {
        let root = temp_root("hit");
        let saved = json!({
            "status": "done", "query": "q", "reason": "r", "answer": "a",
            "results": [{"title": "T", "url": "https://x.com/a"}],
            "rounds": [], "response_time": null, "cached": false,
        });
        save_search_cache(&root, "q", &saved, 1_700_000_000).unwrap();
        let boom = |_url: &str, _body: &[u8], _headers: &[(&str, &str)]| -> TransportOutcome {
            panic!("a cache hit must not search");
        };
        let mut seen = Vec::new();
        let result = search_multiple("q", "k", &boom, &root, 1_700_000_000, &mut |payload| {
            seen.push(payload.clone())
        })
        .expect("no results means no cache write");
        assert_eq!(seen.len(), 1, "the hit is announced exactly once");
        let mut expected = saved;
        expected["cached"] = json!(true);
        assert_eq!(result, expected);
        assert_eq!(seen[0], expected);
    }

    #[test]
    fn completions_fold_in_in_finish_order_not_submission_order() {
        let root = temp_root("order");
        let query = "最新消息"; // fresh intent -> three variants
        let queries = search_queries_for(query);
        assert_eq!(queries.len(), 3);
        // the first query sleeps longest, so the completions arrive backwards
        let delays: std::collections::HashMap<String, std::time::Duration> = queries
            .iter()
            .enumerate()
            .map(|(index, text)| {
                (
                    text.clone(),
                    std::time::Duration::from_millis(120 * (2 - index) as u64),
                )
            })
            .collect();
        let transport =
            move |_url: &str, body: &[u8], _headers: &[(&str, &str)]| -> TransportOutcome {
                let parsed: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
                let query = parsed
                    .get("query")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                std::thread::sleep(delays.get(&query).copied().unwrap_or_default());
                TransportOutcome::Response {
                    status: 200,
                    body: json!({
                        "query": query,
                        "answer": "",
                        "results": [{"title": "T", "url": "https://finish-order.com/a",
                                     "content": "c", "score": 0.5}],
                    })
                    .to_string()
                    .into_bytes(),
                }
            };
        let mut seen = Vec::new();
        let result = search_multiple(
            query,
            "k",
            &transport,
            &root,
            1_700_000_000,
            &mut |payload| seen.push(payload.clone()),
        )
        .expect("results exist, so the cache is written");
        // three submission announcements, then one per completion
        assert_eq!(seen.len(), 6);
        fn statuses(snapshot: &Value) -> Vec<&str> {
            snapshot["rounds"]
                .as_array()
                .unwrap()
                .iter()
                .map(|round| round["status"].as_str().unwrap())
                .collect()
        }
        // `searching` flips to `done` from the END first, because the first query
        // sleeps longest — submission order alone would flip from the front
        assert_eq!(statuses(&seen[3]), vec!["searching", "searching", "done"]);
        assert_eq!(statuses(&seen[4]), vec!["searching", "done", "done"]);
        assert_eq!(statuses(&seen[5]), vec!["done", "done", "done"]);
        assert_eq!(result["rounds"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn a_panicking_transport_becomes_an_error_round_like_an_exception() {
        let root = temp_root("panic");
        let query = "政策法规"; // official intent -> three variants
        let queries = search_queries_for(query);
        assert_eq!(queries.len(), 3);
        let failing_query = queries[2].clone();
        let transport =
            move |_url: &str, body: &[u8], _headers: &[(&str, &str)]| -> TransportOutcome {
                let parsed: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
                let query = parsed
                    .get("query")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if query == failing_query {
                    // the `except Exception` arm, driven as a panic
                    panic!("worker exploded");
                }
                TransportOutcome::Response {
                    status: 200,
                    body: json!({
                        "query": query,
                        "answer": "",
                        "results": [{"title": "T", "url": "https://panic-round.com/a",
                                     "content": "c", "score": 0.5}],
                    })
                    .to_string()
                    .into_bytes(),
                }
            };
        let mut seen = Vec::new();
        let result = search_multiple(
            query,
            "k",
            &transport,
            &root,
            1_700_000_000,
            &mut |payload| seen.push(payload.clone()),
        )
        .expect("results exist, so the cache is written");
        let rounds = result["rounds"].as_array().unwrap();
        assert_eq!(rounds[0]["status"], json!("done"));
        assert_eq!(rounds[1]["status"], json!("done"));
        assert_eq!(rounds[2]["status"], json!("error"));
        assert_eq!(rounds[2]["error"], json!("worker exploded"));
        // not every round errored, so the aggregate stays `done`
        assert_eq!(result["status"], json!("done"));
        assert_eq!(seen.len(), 6);
    }

    #[test]
    fn a_failed_cache_write_surfaces_rather_than_being_swallowed() {
        // The oracle's `save_search_cache` raises out of `search_multiple`; a port
        // that swallowed it would answer with results the oracle would have lost.
        let file =
            std::env::temp_dir().join(format!("search-multiple-blocked-{}", std::process::id()));
        let _ = std::fs::remove_file(&file);
        std::fs::write(&file, b"not a directory").unwrap();
        let transport = |_url: &str, _body: &[u8], _headers: &[(&str, &str)]| -> TransportOutcome {
            TransportOutcome::Response {
                status: 200,
                body: json!({
                    "query": "q",
                    "answer": "",
                    "results": [{"title": "T", "url": "https://blocked-write.com/a",
                                 "content": "c", "score": 0.5}],
                })
                .to_string()
                .into_bytes(),
            }
        };
        let error = search_multiple("q", "k", &transport, &file, 1_700_000_000, &mut |_| {})
            .expect_err("the cache write fails, so the call does");
        assert_eq!(error.code, codes::INTERNAL);
        let _ = std::fs::remove_file(&file);
    }
}
