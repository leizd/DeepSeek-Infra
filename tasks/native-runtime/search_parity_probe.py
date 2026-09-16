"""Tavily search parity probe (layers 1+2), Python side.

Covers the non-transport half of `infra/tool_runtime/search.py`: query planning,
response normalization, ranking, the round projection, and the cache.
`search_tavily` / `search_tavily_with_retry` are excluded — they are the HTTP slice,
and only their retry *policy* is compared here.

Usage::

    python tasks/native-runtime/search_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example search_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

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

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
