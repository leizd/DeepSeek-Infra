//! Tavily search parity probe (layers 1+2), Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/search_parity_probe.py` through
//! `deepseek_policy::search`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/search_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example search_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::search::{
    Transport, TransportOutcome, aggregate_search_rounds, compact_search_tool_result,
    domain_from_url, forced_search_mode, format_search_context, format_search_failure_context,
    format_upstream_error, normalize_search_query_text, normalize_search_response,
    normalize_search_url, rerank_search_results, rounds_in_order, search_cache_key,
    search_domain_filters, search_intent, search_mode, search_queries_for, search_reason_for_query,
    search_result_score, search_round_from_cache, search_round_status, search_tavily_with_retry,
    search_tool_enabled, should_search_for_query, simplified_retry_query, tavily_options_for_query,
    tavily_request_body_json,
};
use serde_json::{Map, Value, json};

fn query_cases() -> Vec<String> {
    vec![
        "  DeepSeek   V3   release  ".to_string(),
        "最新消息".to_string(),
        "如何配置 python 的 logging？".to_string(),
        "x".repeat(600),
        String::new(),
        "   ".to_string(),
    ]
}

fn should_search_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("今天天气如何", json!({})),
        ("今天天气如何", json!({"searchMode": "off"})),
        ("今天天气如何", json!({"searchMode": "ON"})),
        ("今天天气如何", json!({"searchMode": "force"})),
        ("随便聊聊", json!({})),
        ("看下 docs.python.org", json!({})),
        ("https://example.com/a", json!({})),
        ("搜索一下这个", json!({})),
        ("", json!({})),
        // a falsy numeric mode falls through to text matching instead of reading as off
        ("最新消息", json!({"searchMode": 0})),
        ("随便聊聊", json!({"searchMode": 0})),
    ]
}

fn intent_cases() -> Vec<&'static str> {
    vec![
        "最新新闻",
        "显卡价格",
        "python 报错",
        "政策法规",
        "A 和 B 的区别",
        "随便聊聊",
    ]
}

fn netloc_cases() -> Vec<&'static str> {
    vec![
        "https://WWW.Example.COM/Path/?q=1#frag",
        "http://a.b/c/",
        "https://x.dev",
        "not a url",
        "https://user:pw@Host.COM:8443/x",
    ]
}

fn raw_result() -> Value {
    json!([
        {"title": "  Official Docs  ", "url": "https://docs.example.com/a", "content": "c".repeat(1300),
         "raw_content": "r".repeat(3600), "score": 0.9, "favicon": "f"},
        {"title": "", "url": "", "content": "dropped"},
        {"url": "https://other.org", "score": 0.5},
        "not-a-dict"
    ])
}

fn tavily_response() -> Value {
    json!({
        "query": "deepseek",
        "answer": "  an answer  ",
        "results": [
            {"title": "A", "url": "https://a.com", "content": "alpha", "score": 0.8},
            {"title": "B", "url": "https://b.com", "content": "", "score": 0.1},
            {"url": "https://docs.python.org/x", "title": "Docs", "content": "d", "score": 0.2},
            {"title": "dup", "url": "https://a.com/", "content": "dup", "score": 0.3},
            {"url": "https://c.com", "content": "c", "score": 0.4},
            {"url": "https://c.com/2", "content": "c2", "score": 0.45},
            {"url": "https://c.com/3", "content": "c3", "score": 0.44},
            {"url": "https://d.com", "title": "Official docs", "content": "d2", "score": 0.7}
        ],
        "response_time": 1.25,
        "request_id": "req-1"
    })
}

fn rounds() -> Vec<Value> {
    vec![
        json!({"round": 1, "status": "done", "query": "q1", "answer": "a1", "response_time": 1.0,
        "results": [
            {"title": "A", "url": "https://a.com", "content": "alpha", "score": 0.8},
            {"title": "B", "url": "https://b.com", "content": "beta", "score": 0.5}
        ]}),
        json!({"round": 2, "status": "error", "query": "q2", "answer": "", "error": "boom",
        "results": [
            {"title": "A again", "url": "https://a.com/", "content": "alpha2", "score": 0.9},
            {"title": "C", "url": "https://c.com", "content": "gamma", "score": 0.6}
        ]}),
    ]
}

fn main() {
    let mut out = Map::new();

    for (index, query) in query_cases().iter().enumerate() {
        out.insert(
            format!("normalize::{index}"),
            json!(normalize_search_query_text(query)),
        );
        out.insert(
            format!("retry-query::{index}"),
            json!(simplified_retry_query(query)),
        );
        out.insert(format!("intent-for::{index}"), json!(search_intent(query)));
        out.insert(
            format!("reason::{index}"),
            json!(search_reason_for_query(query)),
        );
        out.insert(
            format!("queries::{index}"),
            json!(search_queries_for(query)),
        );
        out.insert(format!("options::{index}"), tavily_options_for_query(query));
        out.insert(format!("domains::{index}"), search_domain_filters(query));
        out.insert(
            format!("cache-key::{index}"),
            json!(search_cache_key(query)),
        );
    }

    for (index, (query, payload)) in should_search_cases().iter().enumerate() {
        out.insert(
            format!("should-search::{index}"),
            json!(should_search_for_query(query, payload)),
        );
    }

    for (index, query) in intent_cases().iter().enumerate() {
        out.insert(format!("intent::{index}"), json!(search_intent(query)));
    }

    for (index, url) in netloc_cases().iter().enumerate() {
        out.insert(
            format!("normalize-url::{index}"),
            json!(normalize_search_url(url)),
        );
        out.insert(format!("domain::{index}"), json!(domain_from_url(url)));
    }

    out.insert(
        "normalize-response::raw".to_string(),
        normalize_search_response("fallback", &json!({"results": raw_result()})),
    );
    out.insert(
        "normalize-response::empty".to_string(),
        normalize_search_response("q", &json!({})),
    );
    out.insert(
        "normalize-response::tavily".to_string(),
        normalize_search_response("q", &tavily_response()),
    );

    let results = tavily_response()["results"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    for (index, result) in results.iter().enumerate() {
        out.insert(
            format!("score::{index}"),
            json!(search_result_score(result, "python docs")),
        );
    }

    let ranked = rerank_search_results(&results, "python docs", 45);
    out.insert(
        "rerank::urls".to_string(),
        json!(
            ranked
                .iter()
                .map(|item| item.get("url").cloned().unwrap_or(Value::Null))
                .collect::<Vec<Value>>()
        ),
    );

    let aggregated = aggregate_search_rounds("q", &rounds(), None);
    out.insert(
        "aggregate::status".to_string(),
        aggregated["status"].clone(),
    );
    out.insert(
        "aggregate::answer".to_string(),
        aggregated["answer"].clone(),
    );
    out.insert(
        "aggregate::reason".to_string(),
        aggregated["reason"].clone(),
    );
    out.insert(
        "aggregate::result-urls".to_string(),
        json!(
            aggregated["results"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .iter()
                .map(|item| item.get("url").cloned().unwrap_or(Value::Null))
                .collect::<Vec<Value>>()
        ),
    );
    out.insert(
        "aggregate::rounds".to_string(),
        aggregated["rounds"].clone(),
    );

    out.insert(
        "compact::basic".to_string(),
        compact_search_tool_result(&aggregated, "technical", 3),
    );
    out.insert(
        "compact::empty".to_string(),
        compact_search_tool_result(&json!({}), "", 0),
    );

    out.insert(
        "round-status::plain".to_string(),
        search_round_status("q", 2, "searching", ""),
    );
    out.insert(
        "round-status::error".to_string(),
        search_round_status("q", 0, "error", "boom"),
    );
    let mut by_index = std::collections::BTreeMap::new();
    by_index.insert(2i64, json!({"round": 2}));
    by_index.insert(1i64, json!({"round": 1}));
    out.insert(
        "rounds-in-order".to_string(),
        json!(rounds_in_order(&by_index)),
    );
    out.insert(
        "round-from-cache".to_string(),
        search_round_from_cache(
            "fallback",
            &json!({"query": "cached", "results": [{"url": "u"}, "x"]}),
            5,
        ),
    );
    out.insert(
        "round-from-cache::empty".to_string(),
        search_round_from_cache("fallback", &json!({}), 1),
    );

    // --- the HTTP layer's non-transport half ---------------------------------------
    //
    // `search_tavily` is driven through a stub transport: what is compared is the request
    // that would be sent, the error mapping, and the retry policy — which is where the
    // logic lives. No network is touched.
    for (index, query) in [
        "deepseek",
        "最新消息",
        &"x".repeat(600),
        "python 报错",
        "政策法规",
    ]
    .iter()
    .enumerate()
    {
        out.insert(
            format!("body::{index}"),
            json!(tavily_request_body_json(query)),
        );
    }

    for (index, raw) in [
        r#"{"error": {"message": "quota exhausted"}}"#,
        r#"{"error": {"type": "rate_limited"}}"#,
        r#"{"error": {"message": "", "type": "x"}}"#,
        "plain text",
        "",
        &"y".repeat(700),
    ]
    .iter()
    .enumerate()
    {
        out.insert(
            format!("format-error::{index}"),
            json!(format_upstream_error(raw)),
        );
    }

    // Each arm of the retry policy, with the transport stubbed. `drive` records the query
    // each attempt used, so the probe can see the simplified retry query.
    fn drive(outcomes: Vec<(Option<&'static str>, u16)>) -> Value {
        // `AppError.code` is `&'static str`; the codes here are literals.
        let calls = std::rc::Rc::new(std::cell::RefCell::new(Vec::<String>::new()));
        let observed = std::rc::Rc::clone(&calls);
        let transport = move |_url: &str, body: &[u8], _headers: &[(&str, &str)]| {
            let parsed: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
            let query = parsed
                .get("query")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let mut recorded = calls.borrow_mut();
            recorded.push(query.clone());
            let position = recorded.len() - 1;
            drop(recorded);
            let (code, status) = outcomes[position.min(outcomes.len() - 1)];
            match code {
                // `Rejected` injects the exact `AppError` a Python stub raises, so the
                // arm under test is the retry policy rather than the error mapping.
                Some(code) => TransportOutcome::Rejected(deepseek_policy::app_error::AppError {
                    message: format!("boom {code}"),
                    code,
                    status,
                }),
                // The same payload the Python fake feeds through
                // `normalize_search_response`, keyed on the attempt's own query.
                None => TransportOutcome::Response {
                    status,
                    body: json!({
                        "query": query,
                        "answer": "",
                        "results": [{"url": "https://a.com", "title": "t"}],
                    })
                    .to_string()
                    .into_bytes(),
                },
            }
        };
        match search_tavily_with_retry("最新消息 价格", "k", &transport as &Transport) {
            Ok(result) => json!({"calls": *observed.borrow(), "result": result}),
            Err(error) => {
                json!({"calls": *observed.borrow(), "raised": error.message, "code": error.code})
            }
        }
    }

    out.insert("retry::ok-first".to_string(), drive(vec![(None, 200)]));
    out.insert(
        "retry::ok-after-timeout".to_string(),
        drive(vec![(Some("upstream_timeout"), 502), (None, 200)]),
    );
    out.insert(
        "retry::ok-after-503".to_string(),
        drive(vec![(Some("upstream_failure"), 503), (None, 200)]),
    );
    out.insert(
        "retry::both-fail".to_string(),
        drive(vec![
            (Some("upstream_timeout"), 502),
            (Some("upstream_timeout"), 502),
        ]),
    );
    out.insert(
        "retry::no-retry-on-missing-key".to_string(),
        drive(vec![(Some("missing_api_key"), 503), (None, 200)]),
    );

    // --- step 1: the pure predicates and the two prompt formatters -------------------
    const MODES: [&str; 11] = [
        r#"{}"#,
        r#"{"searchMode": "on"}"#,
        r#"{"searchMode": "OFF"}"#,
        r#"{"searchMode": " force "}"#,
        r#"{"searchMode": ""}"#,
        r#"{"searchMode": null}"#,
        r#"{"searchMode": "auto"}"#,
        // `or` reads the raw value's truthiness: a falsy 0 / false lands on the default.
        r#"{"searchMode": 0}"#,
        r#"{"searchMode": true}"#,
        r#"{"searchMode": "true"}"#,
        r#"{"searchMode": "1"}"#,
    ];
    const ENABLED: [&str; 9] = [
        r#"{}"#,
        r#"{"searchEnabled": true}"#,
        r#"{"searchEnabled": true, "searchMode": "off"}"#,
        r#"{"searchEnabled": true, "searchMode": "auto"}"#,
        r#"{"searchEnabled": 1}"#,
        r#"{"searchEnabled": "true"}"#,
        r#"{"searchEnabled": false, "searchMode": "force"}"#,
        // a numeric 0 is falsy, so the mode is "auto" (enabled); the string "0" is the off mode.
        r#"{"searchEnabled": true, "searchMode": 0}"#,
        r#"{"searchEnabled": true, "searchMode": "0"}"#,
    ];
    const FAILURES: [&str; 5] = [
        r#"{"rounds": [{"error": "e1"}, {"error": ""}, {"error": "e2"}, {"error": "e3"}, {"error": "e4"}]}"#,
        r#"{"rounds": []}"#,
        r#"{}"#,
        r#"{"rounds": ["not-a-dict", {"error": "only"}]}"#,
        r#"{"rounds": [{"error": true}, {"error": 0}, {"error": "e"}]}"#,
    ];

    for (index, raw) in MODES.iter().enumerate() {
        let payload: Value = serde_json::from_str(raw).unwrap();
        out.insert(
            format!("search-mode::{index}"),
            json!(search_mode(&payload)),
        );
        out.insert(
            format!("forced::{index}"),
            json!(forced_search_mode(&payload)),
        );
    }
    for (index, raw) in ENABLED.iter().enumerate() {
        let payload: Value = serde_json::from_str(raw).unwrap();
        out.insert(
            format!("tool-enabled::{index}"),
            json!(search_tool_enabled(&payload)),
        );
    }
    let contexts = [
        json!({"query": "q", "answer": " a ", "results": []}),
        json!({"query": null, "answer": "",
               "results": [{"title": "", "url": "u", "raw_content": "", "content": "c"}]}),
        json!({"results": [{"citation_id": "W9", "title": "T", "url": "u",
                            "raw_content": "r".repeat(3600)}]}),
        json!({"results": [{"url": "u1"}, {"url": "u2"}]}),
        json!({}),
        // every `or` chain here reads the raw value, truthiness first: a falsy 0 takes
        // the fallback, the next arm, or nothing at all. (A non-dict entry would make
        // the oracle raise AttributeError, so it is not part of the compared corpus.)
        json!({"query": 7, "answer": 0,
               "results": [{"title": 0, "citation_id": 0, "raw_content": 0,
                            "content": " c ", "url": ""}]}),
    ];
    for (index, data) in contexts.iter().enumerate() {
        out.insert(
            format!("context::{index}"),
            json!(format_search_context(data)),
        );
    }
    for (index, raw) in FAILURES.iter().enumerate() {
        let data: Value = serde_json::from_str(raw).unwrap();
        out.insert(
            format!("failure-context::{index}"),
            json!(format_search_failure_context(&data)),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
