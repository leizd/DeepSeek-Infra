from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

import deepseek_infra.infra.tool_runtime.search as search
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime.search import aggregate_search_rounds, format_search_context, search_for_client, search_queries_for, should_search_for_query


class FakeTavilyResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeTavilyResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class SearchTests(unittest.TestCase):
    def test_should_search_for_query_respects_modes(self) -> None:
        self.assertFalse(should_search_for_query("latest Python release", {"searchMode": "off"}))
        self.assertTrue(should_search_for_query("stable fact", {"searchMode": "on"}))
        self.assertTrue(should_search_for_query("latest Python release", {"searchMode": "auto"}))
        self.assertFalse(should_search_for_query("explain recursion", {"searchMode": "auto"}))

    def test_should_search_for_query_recognizes_mode_aliases_and_urls(self) -> None:
        for mode in ["off", "false", "0"]:
            with self.subTest(mode=mode):
                self.assertFalse(should_search_for_query("today's weather", {"searchMode": mode}))

        for mode in ["on", "force", "true", "1"]:
            with self.subTest(mode=mode):
                self.assertTrue(should_search_for_query("1+1?", {"searchMode": mode}))

        self.assertTrue(should_search_for_query("check https://example.com/docs", {"searchMode": "auto"}))

    def test_search_queries_for_deduplicates_and_limits_rounds(self) -> None:
        queries = search_queries_for("latest DeepSeek docs")
        self.assertEqual(queries[0], "latest DeepSeek docs")
        self.assertGreaterEqual(len(queries), 2)
        self.assertLessEqual(len(queries), search.SEARCH_ROUND_LIMIT)
        self.assertEqual(len(queries), len(set(queries)))

    def test_search_tavily_accepts_request_scoped_api_key(self) -> None:
        response = FakeTavilyResponse(
            {
                "query": "联网搜索",
                "answer": "",
                "results": [{"title": "Result", "url": "https://example.com", "content": "ok"}],
            }
        )
        with patch.object(search, "TAVILY_API_KEY", ""), patch("urllib.request.urlopen", return_value=response) as mocked:
            result = search.search_tavily("联网搜索", tavily_api_key="tvly-client")

        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tvly-client")
        self.assertEqual(result["results"][0]["url"], "https://example.com")

    def test_search_tavily_uses_configured_timeout(self) -> None:
        response = FakeTavilyResponse(
            {
                "query": "docs",
                "answer": "",
                "results": [{"title": "Result", "url": "https://example.com", "content": "ok"}],
            }
        )
        with patch.object(search, "TAVILY_TIMEOUT_SECONDS", 7), patch("urllib.request.urlopen", return_value=response) as mocked:
            search.search_tavily("docs", tavily_api_key="tvly-client")

        self.assertEqual(mocked.call_args.kwargs["timeout"], 7)

    def test_search_multiple_prefetches_complementary_queries(self) -> None:
        started: list[str] = []

        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            started.append(query)
            return {
                "query": query,
                "answer": "",
                "results": [{"title": query, "url": f"https://example.com/{len(started)}", "content": "ok"}],
            }

        expected_queries = search_queries_for("latest DeepSeek docs")
        with (
            patch.object(search, "load_search_cache", return_value=None),
            patch.object(search, "save_search_cache"),
            patch.object(search, "search_tavily", side_effect=fake_search_tavily),
        ):
            result = search.search_multiple("latest DeepSeek docs", tavily_api_key="tvly-client")

        self.assertCountEqual(started, expected_queries)
        self.assertEqual([round_data["round"] for round_data in result["rounds"]], list(range(1, len(expected_queries) + 1)))
        self.assertEqual(result["status"], "done")

    def test_search_multiple_reports_each_round_failure(self) -> None:
        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            raise AppError("round failed", code=ErrorCode.UPSTREAM_FAILURE, status=502)

        expected_count = len(search_queries_for("python api docs"))
        with (
            patch.object(search, "load_search_cache", return_value=None),
            patch.object(search, "save_search_cache"),
            patch.object(search, "search_tavily", side_effect=fake_search_tavily),
        ):
            result = search.search_multiple("python api docs", tavily_api_key="tvly-client")

        statuses = [round_data["status"] for round_data in result["rounds"]]
        self.assertEqual(statuses, ["error"] * expected_count)
        self.assertEqual([round_data["round"] for round_data in result["rounds"]], list(range(1, expected_count + 1)))

    def test_search_single_round_emits_progress_and_returns_compact_result(self) -> None:
        progress: list[dict[str, object]] = []

        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            return {
                "query": query,
                "answer": "a" * 700,
                "results": [{"title": "T" * 200, "url": "https://example.com", "content": "s" * 700}],
            }

        with patch.object(search, "search_tavily", side_effect=fake_search_tavily):
            result = search.search_single_round(" docs ", intent="technical", round_index=2, tavily_api_key="tvly-client", progress_callback=progress.append)

        self.assertEqual([item["status"] for item in progress], ["searching", "done"])
        self.assertEqual(result["round"], 2)
        self.assertEqual(result["intent"], "technical")
        self.assertEqual(len(result["answer"]), 600)
        self.assertEqual(len(result["results"][0]["snippet"]), 600)
        self.assertEqual(result["results"][0]["cite"], "[^W1]")
        self.assertEqual(result["results"][0]["citation_id"], "W1")

    def test_search_single_round_retries_transient_error_with_simplified_query(self) -> None:
        progress: list[dict[str, object]] = []
        calls: list[str] = []

        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            calls.append(query)
            if len(calls) == 1:
                raise AppError("Cannot reach Tavily API: Remote end closed connection without response", code=ErrorCode.UPSTREAM_FAILURE, status=502)
            return {
                "query": query,
                "answer": "",
                "results": [{"title": "Retry result", "url": "https://example.com/retry", "content": "ok"}],
            }

        with patch.object(search, "search_tavily", side_effect=fake_search_tavily):
            result = search.search_single_round(
                "alpha, beta! gamma delta epsilon zeta eta theta iota kappa",
                intent="general",
                round_index=3,
                tavily_api_key="tvly-client",
                progress_callback=progress.append,
            )

        self.assertEqual(calls, ["alpha, beta! gamma delta epsilon zeta eta theta iota kappa", "alpha beta gamma delta epsilon zeta eta theta"])
        self.assertEqual([item["status"] for item in progress], ["searching", "done"])
        self.assertTrue(progress[-1]["retried"])
        self.assertEqual(progress[-1]["retryQuery"], "alpha beta gamma delta epsilon zeta eta theta")
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["retried"])
        self.assertEqual(result["retryQuery"], "alpha beta gamma delta epsilon zeta eta theta")

    def test_search_single_round_reports_retry_failure_without_hiding_original_error(self) -> None:
        calls: list[str] = []

        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            calls.append(query)
            raise AppError(f"upstream failed for {query}", code=ErrorCode.UPSTREAM_FAILURE, status=502)

        with patch.object(search, "search_tavily", side_effect=fake_search_tavily):
            result = search.search_single_round("alpha, beta! gamma delta epsilon zeta eta theta iota", intent="fresh", round_index=1)

        self.assertEqual(calls, ["alpha, beta! gamma delta epsilon zeta eta theta iota", "alpha beta gamma delta epsilon zeta eta theta"])
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["retried"])
        self.assertIn("retry failed", str(result["error"]))
        self.assertIn("upstream failed for alpha beta", result["retryError"])

    def test_search_single_round_uses_cache_when_enabled(self) -> None:
        cached = {
            "status": "done",
            "query": "docs",
            "answer": "cached answer",
            "results": [{"title": "Cached", "url": "https://example.com", "content": "cached snippet"}],
        }
        progress: list[dict[str, object]] = []

        with (
            patch.object(search, "load_search_cache", return_value=cached),
            patch.object(search, "search_tavily") as search_tavily,
        ):
            result = search.search_single_round("docs", intent="technical", round_index=3, progress_callback=progress.append, use_cache=True)

        search_tavily.assert_not_called()
        self.assertEqual(result["round"], 3)
        self.assertTrue(result["cached"])
        self.assertEqual(result["answer"], "cached answer")
        self.assertEqual(progress, [{**cached, "round": 3, "response_time": None, "cached": True}])

    def test_search_single_round_saves_successful_results_when_cache_enabled(self) -> None:
        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            return {
                "query": query,
                "answer": "",
                "results": [{"title": "Result", "url": "https://example.com", "content": "ok"}],
            }

        with (
            patch.object(search, "load_search_cache", return_value=None),
            patch.object(search, "save_search_cache") as save_cache,
            patch.object(search, "search_tavily", side_effect=fake_search_tavily),
        ):
            result = search.search_single_round("docs", intent="technical", round_index=1, use_cache=True)

        self.assertEqual(result["status"], "done")
        save_cache.assert_called_once()
        self.assertEqual(save_cache.call_args.args[0], "docs")
        self.assertEqual(save_cache.call_args.args[1]["results"][0]["url"], "https://example.com")

    def test_search_retry_skips_non_transient_missing_key(self) -> None:
        calls: list[str] = []

        def fake_search_tavily(query: str, *, tavily_api_key: str = "") -> dict[str, object]:
            calls.append(query)
            raise AppError("missing key", code=ErrorCode.MISSING_API_KEY, status=503)

        with patch.object(search, "search_tavily", side_effect=fake_search_tavily):
            result = search.search_single_round("alpha, beta! gamma delta", intent="general", round_index=1)

        self.assertEqual(calls, ["alpha, beta! gamma delta"])
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["retried"])

    def test_search_multiple_cache_hit_skips_worker_search(self) -> None:
        cached = {"status": "done", "query": "docs", "results": [], "rounds": []}
        with patch.object(search, "load_search_cache", return_value=cached), patch.object(search, "search_tavily") as mocked:
            result = search.search_multiple("docs", tavily_api_key="tvly-client")

        mocked.assert_not_called()
        self.assertTrue(result["cached"])

    def test_aggregate_search_rounds_deduplicates_normalized_urls(self) -> None:
        result = aggregate_search_rounds(
            "docs",
            [
                {"round": 1, "query": "docs", "results": [{"title": "Docs", "url": "https://example.com/a/", "content": "docs"}]},
                {"round": 2, "query": "docs", "results": [{"title": "Docs again", "url": "https://example.com/a", "content": "docs"}]},
            ],
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["rounds"][0]["results"][0]["citation_id"], "W1")
        self.assertEqual(result["rounds"][1]["results"][0]["citation_id"], "W2")

    def test_search_for_client_strips_raw_content(self) -> None:
        client = search_for_client(
            {
                "status": "done",
                "query": "q",
                "reason": "r",
                "answer": "",
                "cached": False,
                "results": [{"title": "T", "url": "https://example.com", "content": "short", "raw_content": "secret"}],
                "rounds": [],
            }
        )
        self.assertIsNotNone(client)
        assert client is not None
        self.assertNotIn("raw_content", client["results"][0])
        self.assertIn("citation_id", client["results"][0])

    def test_format_search_context_omits_today_date(self) -> None:
        context = format_search_context({"query": "docs", "results": [{"title": "Docs", "url": "https://example.com", "content": "short"}]})

        self.assertNotIn(date.today().isoformat(), context)
        self.assertIn("[^W1]", context)
        self.assertIn("web_search", context)

    def test_cleanup_search_cache_removes_expired_entries(self) -> None:
        cache_dir = Path.cwd() / f".test-search-cache-{uuid.uuid4().hex}"
        cache_dir.mkdir()
        self.addCleanup(lambda: shutil.rmtree(cache_dir, ignore_errors=True))
        fresh = (cache_dir / ("f" * 32)).with_suffix(".json")
        old = (cache_dir / ("0" * 32)).with_suffix(".json")
        fresh.write_text("{}", encoding="utf-8")
        old.write_text("{}", encoding="utf-8")
        expired = int(os.path.getmtime(old)) - search.SEARCH_CACHE_MAX_AGE_SECONDS - 10
        os.utime(old, (expired, expired))

        with patch.object(search, "SEARCH_CACHE_DIR", cache_dir):
            search.cleanup_search_cache()

        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()


