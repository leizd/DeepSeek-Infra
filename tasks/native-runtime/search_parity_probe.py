"""Tavily search parity probe (layers 1+2), Python side.

Covers the non-transport half of `infra/tool_runtime/search.py`: query planning,
response normalization, ranking, the round projection, the cache, and the two
prompt-context formatters. `search_tavily` / `search_tavily_with_retry` are
excluded — they are the HTTP slice, and only their retry *policy* is compared
here. The three payload predicates (`search_mode` & co.) live in
`gateway/deepseek_client.py`, not in the search module, and are extracted
verbatim from that file — importing it would pull the whole gateway.

Usage::

    python tasks/native-runtime/search_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example search_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DEEPSEEK_CLIENT = REPO / "deepseek_infra" / "infra" / "gateway" / "deepseek_client.py"
GATEWAY_PREDICATES = ("search_mode", "forced_search_mode", "search_tool_enabled")


def extract_gateway_predicates() -> dict:
    """The oracle's own payload predicates, pulled verbatim from deepseek_client.py."""
    source = DEEPSEEK_CLIENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in GATEWAY_PREDICATES:
            found[node.name] = ast.get_source_segment(source, node) or ""
    missing = set(GATEWAY_PREDICATES) - set(found)
    if missing:
        raise SystemExit(f"could not extract {sorted(missing)} from {DEEPSEEK_CLIENT}")
    namespace: dict = {}
    exec(
        compile("\n\n".join(found[name] for name in GATEWAY_PREDICATES), str(DEEPSEEK_CLIENT), "exec"),
        namespace,
    )
    return namespace

MODES = [
    {},
    {'searchMode': 'on'},
    {'searchMode': 'OFF'},
    {'searchMode': ' force '},
    {'searchMode': ''},
    {'searchMode': None},
    {'searchMode': 'auto'},
    # `or` reads the raw value's truthiness: a falsy 0 / False lands on the default.
    {'searchMode': 0},
    {'searchMode': True},
    {'searchMode': 'true'},
    {'searchMode': '1'},
]
ENABLED = [
    {},
    {'searchEnabled': True},
    {'searchEnabled': True, 'searchMode': 'off'},
    {'searchEnabled': True, 'searchMode': 'auto'},
    {'searchEnabled': 1},
    {'searchEnabled': 'true'},
    {'searchEnabled': False, 'searchMode': 'force'},
    # a numeric 0 is falsy, so the mode is 'auto' (enabled); the string '0' is the off mode.
    {'searchEnabled': True, 'searchMode': 0},
    {'searchEnabled': True, 'searchMode': '0'},
]
CONTEXTS = [{'query': 'q', 'answer': ' a ', 'results': []}, {'query': None, 'answer': '', 'results': [{'title': '', 'url': 'u', 'raw_content': '', 'content': 'c'}]}, {'results': [{'citation_id': 'W9', 'title': 'T', 'url': 'u', 'raw_content': 'rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr'}]}, {'results': [{'url': 'u1'}, {'url': 'u2'}]}, {},
    # every `or` chain here reads the raw value, truthiness first: a falsy 0 takes
    # the fallback, the next arm, or nothing at all. (A non-dict entry would make
    # the oracle raise AttributeError, so it is not part of the compared corpus.)
    {'query': 7, 'answer': 0,
     'results': [{'title': 0, 'citation_id': 0, 'raw_content': 0, 'content': ' c ', 'url': ''}]},
]
FAILURES = [
    {'rounds': [{'error': 'e1'}, {'error': ''}, {'error': 'e2'}, {'error': 'e3'}, {'error': 'e4'}]},
    {'rounds': []},
    {},
    {'rounds': ['not-a-dict', {'error': 'only'}]},
    {'rounds': [{'error': True}, {'error': 0}, {'error': 'e'}]},
]

QUERY_CASES = [
    "  DeepSeek   V3   release  ",
    "最新消息",
    "如何配置 python 的 logging？",
    "x" * 600,
    "",
    "   ",
]

SHOULD_SEARCH_CASES = [
    ("今天天气如何", {}),
    ("今天天气如何", {"searchMode": "off"}),
    ("今天天气如何", {"searchMode": "ON"}),
    ("今天天气如何", {"searchMode": "force"}),
    ("随便聊聊", {}),
    ("看下 docs.python.org", {}),
    ("https://example.com/a", {}),
    ("搜索一下这个", {}),
    ("", {}),
    # a falsy numeric mode falls through to text matching instead of reading as off
    ("最新消息", {"searchMode": 0}),
    ("随便聊聊", {"searchMode": 0}),
]

INTENT_CASES = ["最新新闻", "显卡价格", "python 报错", "政策法规", "A 和 B 的区别", "随便聊聊"]

NETLOC_CASES = [
    ("https://WWW.Example.COM/Path/?q=1#frag",),
    ("http://a.b/c/",),
    ("https://x.dev",),
    ("not a url",),
    ("https://user:pw@Host.COM:8443/x",),
]

RAW_RESULT = [
    {"title": "  Official Docs  ", "url": "https://docs.example.com/a", "content": "c" * 1300,
     "raw_content": "r" * 3600, "score": 0.9, "favicon": "f"},
    {"title": "", "url": "", "content": "dropped"},
    {"url": "https://other.org", "score": 0.5},
    "not-a-dict",
]

TAVILY_RESPONSE = {
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
        {"url": "https://d.com", "title": "Official docs", "content": "d2", "score": 0.7},
    ],
    "response_time": 1.25,
    "request_id": "req-1",
}

ROUNDS = [
    {"round": 1, "status": "done", "query": "q1", "answer": "a1", "response_time": 1.0,
     "results": [
         {"title": "A", "url": "https://a.com", "content": "alpha", "score": 0.8},
         {"title": "B", "url": "https://b.com", "content": "beta", "score": 0.5},
     ]},
    {"round": 2, "status": "error", "query": "q2", "answer": "", "error": "boom",
     "results": [
         {"title": "A again", "url": "https://a.com/", "content": "alpha2", "score": 0.9},
         {"title": "C", "url": "https://c.com", "content": "gamma", "score": 0.6},
     ]},
]

CACHE_CASES = ["DeepSeek", "  deep   seek  ", "DEEPSEEK", ""]


def main() -> int:
    from deepseek_infra.infra.tool_runtime import search as s

    out: dict = {}

    for index, query in enumerate(QUERY_CASES):
        out[f"normalize::{index}"] = s.normalize_search_query_text(query)
        out[f"retry-query::{index}"] = s.simplified_retry_query(query)
        out[f"intent-for::{index}"] = s.search_intent(query)
        out[f"reason::{index}"] = s.search_reason_for_query(query)
        out[f"queries::{index}"] = s.search_queries_for(query)
        out[f"options::{index}"] = s.tavily_options_for_query(query)
        out[f"domains::{index}"] = s.search_domain_filters(query)
        out[f"cache-key::{index}"] = s.search_cache_key(query)

    for index, (query, payload) in enumerate(SHOULD_SEARCH_CASES):
        out[f"should-search::{index}"] = s.should_search_for_query(query, payload)

    for index, query in enumerate(INTENT_CASES):
        out[f"intent::{index}"] = s.search_intent(query)

    for index, (url,) in enumerate(NETLOC_CASES):
        out[f"normalize-url::{index}"] = s.normalize_search_url(url)
        out[f"domain::{index}"] = s.domain_from_url(url)

    out["normalize-response::raw"] = s.normalize_search_response("fallback", {"results": RAW_RESULT})
    out["normalize-response::empty"] = s.normalize_search_response("q", {})
    out["normalize-response::tavily"] = s.normalize_search_response("q", TAVILY_RESPONSE)

    for index, result in enumerate(TAVILY_RESPONSE["results"]):
        out[f"score::{index}"] = s.search_result_score(result, "python docs")

    ranked = s.rerank_search_results(TAVILY_RESPONSE["results"], "python docs", limit=45)
    out["rerank::urls"] = [item.get("url") for item in ranked]

    aggregated = s.aggregate_search_rounds("q", ROUNDS)
    out["aggregate::status"] = aggregated["status"]
    out["aggregate::answer"] = aggregated["answer"]
    out["aggregate::reason"] = aggregated["reason"]
    out["aggregate::result-urls"] = [item.get("url") for item in aggregated["results"]]
    out["aggregate::rounds"] = aggregated["rounds"]

    out["compact::basic"] = s.compact_search_tool_result(aggregated, intent="technical", citation_offset=3)
    out["compact::empty"] = s.compact_search_tool_result({}, intent="", citation_offset=0)

    out["round-status::plain"] = s.search_round_status("q", 2, "searching")
    out["round-status::error"] = s.search_round_status("q", 0, "error", "boom")
    out["rounds-in-order"] = s.rounds_in_order({2: {"round": 2}, 1: {"round": 1}})
    out["round-from-cache"] = s.search_round_from_cache("fallback", {"query": "cached", "results": [{"url": "u"}, "x"]}, 5)
    out["round-from-cache::empty"] = s.search_round_from_cache("fallback", {}, 1)

    # --- the HTTP layer's non-transport half ---------------------------------------
    #
    # `search_tavily` itself is not called: it needs a network. What is compared is the
    # request it would send, the error mapping, and the retry policy with the transport
    # stubbed out — all of which is where the logic lives.
    for index, query in enumerate(["deepseek", "最新消息", "x" * 600, "python 报错", "政策法规"]):
        body = {
            "query": query[:500],
            **s.tavily_options_for_query(query),
            **s.search_domain_filters(query),
        }
        out[f"body::{index}"] = json.dumps(body)

    for index, raw in enumerate([
        '{"error": {"message": "quota exhausted"}}',
        '{"error": {"type": "rate_limited"}}',
        '{"error": {"message": "", "type": "x"}}',
        "plain text",
        "",
        "y" * 700,
    ]):
        out[f"format-error::{index}"] = s.format_upstream_error(raw)

    # The retry policy, driven through a stubbed `search_tavily`.
    real_search_tavily = s.search_tavily
    try:
        def drive(outcomes):
            calls: list[tuple[str, str]] = []

            def fake(query: str, *, tavily_api_key: str = "") -> dict:
                calls.append((query, tavily_api_key))
                code, status = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
                if code:
                    # `code.value`, not `code`: an f-string renders the enum *member*.
                    raise s.AppError(f"boom {code.value}", code=code, status=status)
                return s.normalize_search_response(
                    query,
                    {"query": query, "answer": "", "results": [{"url": "https://a.com", "title": "t"}]},
                )

            s.search_tavily = fake
            try:
                result = s.search_tavily_with_retry("最新消息 价格", tavily_api_key="k")
                return {"calls": [call[0] for call in calls], "result": result}
            except s.AppError as exc:
                return {"calls": [call[0] for call in calls], "raised": str(exc), "code": exc.code.value}

        timeout = (s.ErrorCode.UPSTREAM_TIMEOUT, 502)
        failure = (s.ErrorCode.UPSTREAM_FAILURE, 502)
        missing = (s.ErrorCode.MISSING_API_KEY, 503)
        out["retry::ok-first"] = drive([(None, 200)])
        out["retry::ok-after-timeout"] = drive([timeout, (None, 200)])
        out["retry::ok-after-503"] = drive([failure, (None, 200)])
        out["retry::both-fail"] = drive([timeout, timeout])
        out["retry::no-retry-on-missing-key"] = drive([missing, (None, 200)])
    finally:
        s.search_tavily = real_search_tavily

    # --- step 1: the pure predicates and the two prompt formatters -------------------
    gateway = extract_gateway_predicates()
    for index, payload in enumerate(MODES):
        out[f"search-mode::{index}"] = gateway["search_mode"](payload)
        out[f"forced::{index}"] = gateway["forced_search_mode"](payload)
    for index, payload in enumerate(ENABLED):
        out[f"tool-enabled::{index}"] = gateway["search_tool_enabled"](payload)
    for index, data in enumerate(CONTEXTS):
        out[f"context::{index}"] = s.format_search_context(data)
    for index, data in enumerate(FAILURES):
        out[f"failure-context::{index}"] = s.format_search_failure_context(data)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
