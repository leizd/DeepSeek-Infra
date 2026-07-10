from __future__ import annotations

import io
import json
import os
import tempfile
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import deepseek_infra.infra.tool_runtime.search as search
from deepseek_infra.core.errors import AppError, ErrorCode
from urllib.error import HTTPError, URLError


TEMP_DIR = "C:/Users/12393/AppData/Local/Temp/opencode"


def test_normalize_search_url_handles_valueerror() -> None:
    with patch.object(search, "urlsplit", side_effect=ValueError("bad url")):
        assert search.normalize_search_url("BAD URL ") == "bad url"


def test_should_search_for_query_empty_text() -> None:
    assert search.should_search_for_query("   ", {}) is False
    assert search.should_search_for_query("", {"searchMode": "auto"}) is False


def test_should_search_for_query_explicit_search_terms() -> None:
    assert search.should_search_for_query("查一下 政策", {"searchMode": "auto"}) is True
    assert search.should_search_for_query("search python docs", {"searchMode": "auto"}) is True
    assert search.should_search_for_query("look up official docs", {"searchMode": "auto"}) is True


def test_search_intent_variants() -> None:
    assert search.search_intent("iPhone 15 price buy") == "shopping"
    assert search.search_intent("law regulation policy") == "official"
    assert search.search_intent("compare A vs B differences") == "compare"


def test_search_reason_for_query_branches() -> None:
    assert search.search_reason_for_query("latest news today") == "检测到时效性问题"
    assert search.search_reason_for_query("official docs source") == "需要外部来源验证"
    assert search.search_reason_for_query("price review ranking") == "需要查询当前市场信息"
    assert search.search_reason_for_query("random query") == "自动判断需要联网补充资料"


def test_search_multiple_progress_callbacks() -> None:
    progress: list[dict[str, object]] = []

    def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
        return {
            "query": query,
            "answer": "",
            "results": [{"title": query, "url": f"https://example.com/{query}", "content": "ok"}],
        }

    with (
        patch.object(search, "load_search_cache", return_value=None),
        patch.object(search, "save_search_cache"),
        patch.object(search, "search_tavily", side_effect=fake_search_tavily),
    ):
        search.search_multiple("latest news", progress_callback=progress.append, tavily_api_key="k")

    statuses = [p["status"] for p in progress]
    assert "searching" in statuses
    assert "done" in statuses


def test_search_multiple_cache_hit_progress_callback() -> None:
    cached = {"status": "done", "query": "docs", "results": [], "rounds": []}
    progress: list[dict[str, object]] = []
    with patch.object(search, "load_search_cache", return_value=cached), patch.object(search, "search_tavily") as mocked:
        result = search.search_multiple("docs", progress_callback=progress.append, tavily_api_key="k")
    mocked.assert_not_called()
    assert result["cached"] is True
    assert progress[0]["cached"] is True


def test_search_single_round_empty_query() -> None:
    result = search.search_single_round("   ", round_index=1, tavily_api_key="k")
    assert result["ok"] is False
    assert result["error"] == "Empty query"
    assert result["results"] == []


def test_compact_search_tool_result_skips_non_dict_items() -> None:
    round_data = {
        "query": "q",
        "round": 1,
        "results": [
            "not a dict",
            {"title": "Valid", "url": "https://example.com", "content": "ok"},
        ],
    }
    result = search.compact_search_tool_result(round_data)
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Valid"


def test_simplified_retry_query_single_part_branch() -> None:
    assert search.simplified_retry_query("python") == "python"


def test_search_queries_for_empty_query() -> None:
    assert search.search_queries_for("") == []
    assert search.search_queries_for("   ") == []


def test_search_queries_for_deduplicates_and_limits() -> None:
    with patch.object(search, "SEARCH_ROUND_LIMIT", 1):
        queries = search.search_queries_for("latest news")
        assert len(queries) == 1
        assert queries[0] == "latest news"

    def same_normalized(query: str) -> str:
        return "same"

    with patch.object(search, "normalize_search_query_text", side_effect=same_normalized):
        queries = search.search_queries_for("latest news")
        assert len(queries) == 1


def test_tavily_options_for_fresh_query() -> None:
    options = search.tavily_options_for_query("latest news today")
    assert options["search_depth"] == "advanced"
    assert options["include_answer"] == "advanced"


def test_search_domain_filters() -> None:
    assert search.search_domain_filters("government policy law") == {
        "include_domains": ["gov.cn", "mfa.gov.cn", "ica.gov.sg", "mom.gov.sg", "gov.sg"]
    }
    assert "docs.python.org" in search.search_domain_filters("official docs python")["include_domains"]
    assert search.search_domain_filters("generic query") == {}


def test_search_tavily_missing_api_key() -> None:
    with patch.object(search, "TAVILY_API_KEY", ""):
        with pytest.raises(AppError) as exc:
            search.search_tavily("query")
    assert exc.value.code == ErrorCode.MISSING_API_KEY


def test_search_tavily_http_error() -> None:
    fp = io.BytesIO(b'{"error": "bad request"}')
    error = HTTPError("https://api.tavily.com", 400, "Bad Request", Message(), fp)
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(AppError) as exc:
            search.search_tavily("query", tavily_api_key="k")
    assert exc.value.code == ErrorCode.UPSTREAM_FAILURE
    assert "bad request" in str(exc.value).lower()


def test_search_tavily_url_error_timeout() -> None:
    with patch("urllib.request.urlopen", side_effect=URLError("timed out")):
        with pytest.raises(AppError) as exc:
            search.search_tavily("query", tavily_api_key="k")
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT


def test_search_tavily_url_error_other() -> None:
    with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
        with pytest.raises(AppError) as exc:
            search.search_tavily("query", tavily_api_key="k")
    assert exc.value.code == ErrorCode.UPSTREAM_FAILURE


def test_normalize_search_response_skips_invalid_items() -> None:
    data = {
        "results": [
            "not a dict",
            {"title": "No URL", "url": ""},
            {"title": "Valid", "url": "https://example.com", "content": "ok"},
        ]
    }
    result = search.normalize_search_response("q", data)
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Valid"


def test_aggregate_search_rounds_normalizes_invalid_and_retried() -> None:
    rounds = [
        {
            "round": 1,
            "query": "q",
            "answer": "answer 1",
            "results": [
                "not a dict",
                {"url": ""},
                {"url": "https://a.com", "content": "ok"},
            ],
        },
        {
            "round": 2,
            "query": "q",
            "status": "searching",
            "results": [],
        },
        {
            "round": 3,
            "query": "q",
            "retried": True,
            "retryQuery": "rq",
            "retryError": "retry failed",
            "results": [],
        },
    ]
    result = search.aggregate_search_rounds("q", rounds)
    assert result["status"] == "searching"
    assert result["answer"] == "answer 1"
    assert result["rounds"][2]["retried"] is True
    assert result["rounds"][2]["retryError"] == "retry failed"


def test_domain_from_url_handles_valueerror() -> None:
    with patch.object(search, "urlsplit", side_effect=ValueError("bad url")):
        assert search.domain_from_url("bad url") == ""


def test_search_result_score_edge_cases() -> None:
    bad_score = {"title": "t", "content": "c", "url": "https://example.com", "score": "nope"}
    assert search.search_result_score(bad_score, "query") >= 0

    trusted = {"title": "t", "content": "c", "url": "https://docs.python.org"}
    assert search.search_result_score(trusted, "query") > search.search_result_score(bad_score, "query")

    empty_content = {"title": "t", "content": "", "url": "https://example.com"}
    base = {"title": "t", "content": "c", "url": "https://example.com"}
    assert search.search_result_score(empty_content, "query") < search.search_result_score(base, "query")


def test_rerank_search_results_domain_limit() -> None:
    results = [
        {"url": "https://example.com/1", "content": "ok"},
        {"url": "https://example.com/2", "content": "ok"},
        {"url": "https://example.com/3", "content": "ok"},
    ]
    ranked = search.rerank_search_results(results, "query", limit=10)
    assert len(ranked) == 2


def test_rerank_search_results_limit_break() -> None:
    results = [
        {"url": "https://a.com/1", "content": "ok"},
        {"url": "https://b.com/1", "content": "ok"},
        {"url": "https://c.com/1", "content": "ok"},
    ]
    ranked = search.rerank_search_results(results, "query", limit=2)
    assert len(ranked) == 2


def test_format_search_context_includes_answer() -> None:
    context = search.format_search_context({"query": "q", "answer": "summary", "results": []})
    assert "Tavily 摘要" in context
    assert "summary" in context


def test_format_search_failure_context() -> None:
    context = search.format_search_failure_context({
        "rounds": [
            {"error": "error 1"},
            {"error": "error 2"},
        ]
    })
    assert "搜索错误" in context
    assert "error 1" in context


def test_search_cache_key() -> None:
    key = search.search_cache_key("Query One")
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


def test_search_cache_save_and_load() -> None:
    with tempfile.TemporaryDirectory(dir=TEMP_DIR) as tmp:
        cache_dir = Path(tmp)
        with patch.object(search, "SEARCH_CACHE_DIR", cache_dir):
            search.save_search_cache("query", {"status": "done", "results": []})
            loaded = search.load_search_cache("query")
            assert loaded is not None
            assert loaded["status"] == "done"


def test_load_search_cache_expired_and_malformed() -> None:
    with tempfile.TemporaryDirectory(dir=TEMP_DIR) as tmp:
        cache_dir = Path(tmp)
        with patch.object(search, "SEARCH_CACHE_DIR", cache_dir):
            key = search.search_cache_key("old")
            path = cache_dir / f"{key}.json"
            path.write_text(json.dumps({"status": "done"}))
            expired = 0
            os.utime(path, (expired, expired))
            assert search.load_search_cache("old") is None

            bad_key = search.search_cache_key("bad")
            bad_path = cache_dir / f"{bad_key}.json"
            bad_path.write_text("not json")
            assert search.load_search_cache("bad") is None

            missing = search.load_search_cache("missing")
            assert missing is None


def test_cleanup_search_cache_no_dir() -> None:
    with tempfile.TemporaryDirectory(dir=TEMP_DIR) as tmp:
        cache_dir = Path(tmp) / "nonexistent"
        with patch.object(search, "SEARCH_CACHE_DIR", cache_dir):
            search.cleanup_search_cache()


def test_cleanup_search_cache_handles_oserror_on_stat() -> None:
    mock_path = MagicMock()
    mock_path.stat.side_effect = OSError("stat failed")
    mock_dir = MagicMock()
    mock_dir.exists.return_value = True
    mock_dir.glob.return_value = [mock_path]
    with patch.object(search, "SEARCH_CACHE_DIR", mock_dir):
        search.cleanup_search_cache()


def test_cleanup_search_cache_handles_oserror_on_unlink() -> None:
    mock_path = MagicMock()
    mock_path.stat.return_value.st_mtime = 0
    mock_path.unlink.side_effect = OSError("unlink failed")
    mock_dir = MagicMock()
    mock_dir.exists.return_value = True
    mock_dir.glob.return_value = [mock_path]
    with patch.object(search, "SEARCH_CACHE_DIR", mock_dir):
        search.cleanup_search_cache()


def test_save_search_cache_creates_file() -> None:
    with tempfile.TemporaryDirectory(dir=TEMP_DIR) as tmp:
        cache_dir = Path(tmp)
        with patch.object(search, "SEARCH_CACHE_DIR", cache_dir):
            search.save_search_cache("save-me", {"status": "done"})
            key = search.search_cache_key("save-me")
            assert (cache_dir / f"{key}.json").exists()


def test_search_for_client_none_input() -> None:
    assert search.search_for_client(None) is None


def test_diagnostics_with_search() -> None:
    diag = search.diagnostics_with_search({"existing": "value"}, None)
    assert diag == {"existing": "value"}

    diag = search.diagnostics_with_search(
        {"existing": "value"},
        {"rounds": [1, 2, 3], "results": [1, 2]},
    )
    assert diag["searchRoundCount"] == 3
    assert diag["searchResultCount"] == 2
