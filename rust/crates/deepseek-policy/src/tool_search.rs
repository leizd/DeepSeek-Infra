//! The search-family branches: `web_search` and `compare_search_results`,
//! mirroring `deepseek_infra/infra/tool_runtime/tools.py`.
//!
//! These are the two branches with the **smallest** dependency surface of the
//! remaining fifteen: neither needs a package. The oracle injects a
//! `web_search_callback` per request (the gateway owns the actual search
//! provider), so the logic here is the argument handling, the query cleaning, the
//! cross-round de-duplication, and the result cap — all pure given the callback.
//!
//! `search_result_key` is the de-duplication key. It is `urlsplit`/`urlunsplit`
//! based, which is a *different projection* from
//! [`crate::tool_policy`]'s SSRF host extraction: the guard wants a hostname to
//! classify, this wants the raw netloc (lowercased, port and all) so two results
//! that differ only in case or in a fragment collapse to one. They are kept
//! separate on purpose rather than forced through one parser.

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::tool_dispatch::ToolFailure;

/// The per-request `web_search_callback` the oracle injects.
///
/// Returns the search round (a JSON object with a `results` array) or an error
/// that becomes the tool's error envelope — mirroring the oracle, where a raising
/// callback is caught by `execute_tool_call`'s `AppError` arm.
pub type WebSearchCallback = dyn Fn(&str, &str) -> Result<Value, ToolFailure>;

/// Per-request dependencies the branches need.
///
/// Mirrors the keyword arguments `execute_tool_call` takes. A `None` callback is
/// not "no search" — it is the oracle's explicit "not enabled for this request"
/// error, which is why the branches below check it rather than silently skipping.
#[derive(Default)]
pub struct ExecutorContext<'a> {
    pub web_search: Option<&'a WebSearchCallback>,
    /// The workspace the data branches run against. `None` means the request
    /// carries no workspace, which the data branches report as "not enabled for
    /// this request" rather than silently doing nothing.
    pub workspace: Option<&'a crate::tool_dispatch::WorkspaceContext<'a>>,
    /// The DNS + HTTP + cache context `fetch_url` runs against. `None` is the
    /// oracle's explicit "not enabled for this request" path, used when the
    /// request has no workspace root to hang the cache off.
    pub fetch: Option<&'a crate::fetch_url::FetchContext<'a>>,
    /// The browser engine the `browser_*` family drives. `None` means this
    /// deployment has no engine, which is the documented static-controller
    /// fallback rather than a failure: the safety gate and the session registry
    /// run either way, and only the controller changes.
    pub browser_engine: Option<&'a dyn crate::browser_engine::BrowserEngine>,
}

impl<'a> ExecutorContext<'a> {
    pub fn with_web_search(callback: &'a WebSearchCallback) -> Self {
        Self {
            web_search: Some(callback),
            workspace: None,
            fetch: None,
            browser_engine: None,
        }
    }

    pub fn with_workspace(workspace: &'a crate::tool_dispatch::WorkspaceContext<'a>) -> Self {
        Self {
            web_search: None,
            workspace: Some(workspace),
            fetch: None,
            browser_engine: None,
        }
    }

    /// Attach the browser engine, keeping every other part of the context.
    pub fn with_browser_engine(
        mut self,
        engine: Option<&'a dyn crate::browser_engine::BrowserEngine>,
    ) -> Self {
        self.browser_engine = engine;
        self
    }
}

/// Mirrors the `web_search` branch.
pub fn web_search(
    arguments: &Map<String, Value>,
    context: &ExecutorContext<'_>,
) -> Result<Value, ToolFailure> {
    let Some(callback) = context.web_search else {
        return Err(ToolFailure::app(
            "web_search",
            "web_search is not enabled for this request",
        ));
    };
    let query = python_str_or(arguments.get("query"), "");
    let intent = python_str_or(arguments.get("intent"), "general");
    callback(&query, &intent)
}

/// Mirrors the `compare_search_results` branch.
pub fn compare_search_results_branch(
    arguments: &Map<String, Value>,
    context: &ExecutorContext<'_>,
) -> Result<Value, ToolFailure> {
    let Some(callback) = context.web_search else {
        return Err(ToolFailure::app(
            "compare_search_results",
            "compare_search_results is not enabled for this request",
        ));
    };
    let intent = python_str_or(arguments.get("intent"), "general");
    // `arguments.get("queries")` is passed through raw, not stringified.
    let queries = arguments.get("queries").cloned().unwrap_or(Value::Null);
    compare_search_results(Some(&queries), &intent, callback)
}

/// Mirrors `compare_search_results`.
///
/// Cleans up to **two** queries (whitespace collapsed, de-duplicated, capped at
/// 500 characters each), runs one round per query, then de-duplicates the results
/// across rounds by [`search_result_key`] and keeps at most 20.
pub fn compare_search_results(
    queries: Option<&Value>,
    intent: &str,
    callback: &WebSearchCallback,
) -> Result<Value, ToolFailure> {
    let Some(Value::Array(items)) = queries else {
        return Err(ToolFailure::app(
            "compare_search_results",
            "queries must be a list",
        ));
    };

    let whitespace = compiled(r"\s+");
    let mut cleaned_queries: Vec<String> = Vec::new();
    for query in items {
        let cleaned = whitespace
            .replace_all(&python_str_or(Some(query), ""), " ")
            .trim()
            .to_string();
        if !cleaned.is_empty() && !cleaned_queries.contains(&cleaned) {
            cleaned_queries.push(cleaned.chars().take(500).collect());
        }
        if cleaned_queries.len() >= 2 {
            break;
        }
    }
    if cleaned_queries.is_empty() {
        return Err(ToolFailure::app(
            "compare_search_results",
            "At least one query is required",
        ));
    }

    let effective_intent = if intent.is_empty() { "general" } else { intent };
    let mut rounds: Vec<Value> = Vec::new();
    let mut results: Vec<Value> = Vec::new();
    let mut seen_urls: Vec<String> = Vec::new();
    for query in &cleaned_queries {
        let round_result = callback(query, effective_intent)?;
        if let Some(items) = round_result.get("results").and_then(Value::as_array) {
            for item in items {
                let Some(object) = item.as_object() else {
                    continue;
                };
                let url = python_str_or(object.get("url"), "");
                let key = search_result_key(&url);
                if key.is_empty() || seen_urls.contains(&key) {
                    continue;
                }
                seen_urls.push(key);
                results.push(item.clone());
            }
        }
        rounds.push(round_result);
    }

    results.truncate(20);
    Ok(json!({
        "queries": cleaned_queries,
        "intent": effective_intent,
        "rounds": rounds,
        "results": results,
    }))
}

/// Mirrors `search_result_key` — the cross-round de-duplication key.
///
/// Python catches `ValueError` from `urlsplit` (malformed IPv6 brackets) and falls
/// back to the stripped, lowercased raw string; this does the same.
pub fn search_result_key(url: &str) -> String {
    let Some(parts) = urlsplit(url) else {
        return url.trim().to_lowercase();
    };
    let path = parts.path.trim_end_matches('/');
    let path = if path.is_empty() { "/" } else { path };
    urlunsplit(
        &parts.scheme.to_lowercase(),
        &parts.netloc.to_lowercase(),
        path,
        &parts.query,
    )
}

/// The projection of a URL that a de-duplication key needs.
///
/// Deliberately **not** [`crate::tool_policy::evaluate_url_safety`]'s projection:
/// that one yields a hostname to classify against the IP tables, while this keeps
/// the raw netloc so `Example.COM:443` and `example.com:443` collapse together.
struct UrlParts {
    scheme: String,
    netloc: String,
    path: String,
    query: String,
}

/// `urlsplit` for a de-duplication key.
///
/// `None` stands for the `ValueError` the oracle catches. Only the shapes a
/// search result can hold are modelled — scheme, netloc, path, query; the
/// fragment is dropped because `urlunsplit` is called without one.
fn urlsplit(raw: &str) -> Option<UrlParts> {
    // Python strips ASCII tab/newline/CR before parsing, then leading C0 controls
    // and spaces.
    let stripped: String = raw
        .chars()
        .filter(|c| !matches!(c, '\t' | '\n' | '\r'))
        .collect();
    let stripped = stripped.trim_start_matches(|c: char| c <= ' ');

    let mut scheme = String::new();
    let mut rest = stripped;
    if let Some(colon) = stripped.find(':') {
        let prefix = &stripped[..colon];
        let valid = !prefix.is_empty()
            && prefix
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic())
            && prefix
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
        if valid {
            scheme = prefix.to_string();
            rest = &stripped[colon + 1..];
        }
    }

    let mut netloc = String::new();
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        let (authority, remainder) = after.split_at(end);
        // A stray `[`/`]` is what makes `urlsplit` raise.
        if unbalanced_brackets(authority) {
            return None;
        }
        netloc = authority.to_string();
        rest = remainder;
    }

    let (before_fragment, _fragment) = match rest.find('#') {
        Some(index) => rest.split_at(index),
        None => (rest, ""),
    };
    let (path, query) = match before_fragment.find('?') {
        Some(index) => {
            let (path, query) = before_fragment.split_at(index);
            (path, &query[1..])
        }
        None => (before_fragment, ""),
    };

    Some(UrlParts {
        scheme,
        netloc,
        path: path.to_string(),
        query: query.to_string(),
    })
}

fn unbalanced_brackets(authority: &str) -> bool {
    let opens = authority.matches('[').count();
    let closes = authority.matches(']').count();
    if opens == 0 && closes == 0 {
        return false;
    }
    // `urlsplit` only accepts a single well-formed `[...]` host.
    opens != closes || opens > 1 || authority.find(']') < authority.find('[')
}

/// `urlunsplit((scheme, netloc, path, query, ""))` for the shapes above.
fn urlunsplit(scheme: &str, netloc: &str, path: &str, query: &str) -> String {
    let mut out = String::new();
    if !netloc.is_empty() {
        out.push_str("//");
        out.push_str(netloc);
        let path = if path.starts_with("//") {
            format!("/{}", path.trim_start_matches('/'))
        } else {
            path.to_string()
        };
        out.push_str(&path);
    } else if !path.is_empty() {
        out.push_str(path);
    }
    if !scheme.is_empty() {
        out = format!("{scheme}:{out}");
    }
    if !query.is_empty() {
        out.push('?');
        out.push_str(query);
    }
    out
}

/// `str(value or fallback)`, shared with the dispatcher so the two branch modules
/// do not drift on how an argument is stringified.
use crate::tool_dispatch::python_str_or;

fn compiled(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static pattern must compile")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_dispatch::INVALID_PAYLOAD;

    fn round(urls: &[&str]) -> Value {
        json!({"results": urls.iter().map(|url| json!({"url": url})).collect::<Vec<_>>()})
    }

    #[test]
    fn search_result_key_normalises_case_path_and_fragment() {
        assert_eq!(
            search_result_key("https://Example.COM/a/b/"),
            "https://example.com/a/b"
        );
        assert_eq!(
            search_result_key("https://example.com"),
            "https://example.com/"
        );
        assert_eq!(
            search_result_key("https://example.com/"),
            "https://example.com/"
        );
        assert_eq!(
            search_result_key("https://example.com/a?q=1#frag"),
            "https://example.com/a?q=1"
        );
        // The port is kept — it is part of the netloc.
        assert_eq!(
            search_result_key("https://example.com:8443/x"),
            "https://example.com:8443/x"
        );
    }

    /// Both expectations here were wrong on my first pass and the probe settled
    /// them: `urlsplit` strips **leading** C0 controls and spaces but never
    /// trailing ones, and a blank string still yields the path key `"/"` rather
    /// than an empty key.
    #[test]
    fn search_result_key_matches_urlsplit_on_the_awkward_inputs() {
        // Unbalanced brackets make `urlsplit` raise, so the raw form is used.
        assert_eq!(search_result_key("http://[::1/"), "http://[::1/");
        // Leading whitespace goes, trailing whitespace stays.
        assert_eq!(search_result_key("  HTTP://X  "), "http://x  /");
        // A blank URL is not an empty key — it normalises to the root path.
        assert_eq!(search_result_key("   "), "/");
        assert_eq!(search_result_key(""), "/");
    }

    #[test]
    fn web_search_requires_the_callback() {
        let context = ExecutorContext::default();
        let failure = web_search(&Map::new(), &context).unwrap_err();
        assert_eq!(failure.error, "web_search is not enabled for this request");
        assert_eq!(failure.code, INVALID_PAYLOAD);
        assert_eq!(failure.tool, "web_search");
    }

    #[test]
    fn web_search_forwards_the_query_and_defaults_the_intent() {
        let callback = |query: &str, intent: &str| {
            Ok(json!({"query": query, "intent": intent, "results": []}))
        };
        let context = ExecutorContext::with_web_search(&callback);
        let mut arguments = Map::new();
        arguments.insert("query".to_string(), json!("rust ownership"));
        let result = web_search(&arguments, &context).unwrap();
        assert_eq!(result["query"], "rust ownership");
        assert_eq!(result["intent"], "general");
    }

    #[test]
    fn compare_cleans_and_caps_queries_at_two() {
        let callback = |query: &str, _intent: &str| Ok(json!({"results": [{"url": query}]}));
        let result = compare_search_results(
            Some(&json!(["  a   b  ", "c", "d", "e"])),
            "general",
            &callback,
        )
        .unwrap();
        assert_eq!(result["queries"], json!(["a b", "c"]));
        assert_eq!(result["rounds"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn compare_deduplicates_queries_and_rejects_empty_input() {
        let callback = |_query: &str, _intent: &str| Ok(json!({"results": []}));
        // A duplicate collapses, leaving one query.
        let result = compare_search_results(Some(&json!(["x", "x", "y"])), "", &callback).unwrap();
        assert_eq!(result["queries"], json!(["x", "y"]));
        assert_eq!(result["intent"], "general");

        // Non-list input and an all-blank list are both errors.
        let not_a_list = compare_search_results(Some(&json!("x")), "", &callback).unwrap_err();
        assert_eq!(not_a_list.error, "queries must be a list");
        let blank = compare_search_results(Some(&json!(["", "   "])), "", &callback).unwrap_err();
        assert_eq!(blank.error, "At least one query is required");
        assert!(compare_search_results(None, "", &callback).is_err());
    }

    #[test]
    fn compare_deduplicates_results_across_rounds_and_caps_at_twenty() {
        let callback = |query: &str, _intent: &str| {
            if query == "a" {
                Ok(round(&[
                    "https://example.com/1/",
                    "https://EXAMPLE.com/1/#f",
                ]))
            } else {
                Ok(json!({"results": (0..30)
                    .map(|index| json!({"url": format!("https://other.com/{index}")}))
                    .collect::<Vec<Value>>()}))
            }
        };
        let result =
            compare_search_results(Some(&json!(["a", "b"])), "general", &callback).unwrap();
        // The two `a` results collapse to one; the second round adds 20+, capped.
        assert_eq!(result["results"].as_array().unwrap().len(), 20);
        assert_eq!(result["results"][0]["url"], "https://example.com/1/");
    }

    /// Only a non-object is dropped. An **empty** URL is not: `urlsplit("")`
    /// normalises to the path `/`, so its key is `"/"` — non-empty, and kept.
    /// My first version expected one surviving result; the probe showed two.
    #[test]
    fn compare_skips_only_non_objects() {
        let callback = |_query: &str, _intent: &str| {
            Ok(json!({"results": ["not-an-object", {"url": ""}, {"url": "https://ok.com/"}]}))
        };
        let result = compare_search_results(Some(&json!(["a"])), "", &callback).unwrap();
        let results = result["results"].as_array().unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(results[0]["url"], "");
        assert_eq!(results[1]["url"], "https://ok.com/");
    }

    #[test]
    fn a_failing_callback_propagates_as_the_tool_error() {
        let callback = |_query: &str, _intent: &str| {
            Err(ToolFailure::app("web_search", "upstream unavailable"))
        };
        let failure = compare_search_results(Some(&json!(["a"])), "", &callback).unwrap_err();
        assert_eq!(failure.error, "upstream unavailable");
    }

    #[test]
    fn compare_requires_the_callback_with_its_own_message() {
        let context = ExecutorContext::default();
        let failure = compare_search_results_branch(&Map::new(), &context).unwrap_err();
        assert_eq!(
            failure.error,
            "compare_search_results is not enabled for this request"
        );
    }
}
