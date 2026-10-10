from __future__ import annotations

import unittest

from deepseek_infra.infra.gateway.deepseek_client import build_deepseek_request
from deepseek_infra.infra.rag.files import chunk_text, select_file_chunk_indices
from deepseek_infra.infra.tool_runtime.search import aggregate_search_rounds, search_for_client
from deepseek_infra.core.utils import clean_filename, normalize_model_name, query_tokens


class CoreFunctionTests(unittest.TestCase):
    def test_normalize_model_aliases(self) -> None:
        self.assertEqual(normalize_model_name("fast"), "deepseek-v4-flash")
        self.assertEqual(normalize_model_name("deepseek_v4_pro"), "deepseek-v4-pro")

    def test_build_deepseek_request_adds_system_and_diagnostics(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test-key",
                "model": "fast",
                "systemPrompt": "System",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 3,
                "thinkingEnabled": False,
            },
            stream=False,
            memory_state={"enabled": True, "notice": "", "context": "Memory", "hitCount": 1},
        )
        self.assertEqual(prepared.body["model"], "deepseek-v4-flash")
        self.assertFalse(prepared.body["stream"])
        self.assertEqual(prepared.body["temperature"], 2)
        self.assertEqual(prepared.body["messages"][0]["role"], "system")
        self.assertNotIn("Memory", prepared.body["messages"][0]["content"])
        self.assertIn("Memory", prepared.body["messages"][-1]["content"])
        self.assertEqual(prepared.diagnostics["memoryHitCount"], 1)
        self.assertEqual(prepared.diagnostics["requestMessageCount"], 1)

    def test_chunk_selection_prefers_matching_chunks(self) -> None:
        text = ("alpha\n" * 3000) + ("needle target\n" * 20) + ("omega\n" * 3000)
        chunks = chunk_text(text)
        selected = select_file_chunk_indices(chunks, "needle target", char_budget=7000)
        self.assertTrue(selected)
        selected_text = "\n".join(chunks[index]["text"] for index in selected)
        self.assertIn("needle", selected_text)

    def test_search_aggregation_deduplicates_urls(self) -> None:
        data = aggregate_search_rounds(
            "docs",
            [
                {"round": 1, "query": "docs", "results": [{"title": "A", "url": "https://example.com/a/", "content": "docs"}]},
                {"round": 2, "query": "docs", "results": [{"title": "A2", "url": "https://example.com/a", "content": "docs"}]},
            ],
        )
        client_data = search_for_client(data)
        self.assertIsNotNone(client_data)
        assert client_data is not None
        self.assertEqual(len(client_data["results"]), 1)

    def test_utils_clean_filename_and_tokens(self) -> None:
        self.assertEqual(clean_filename(r"C:\tmp\report.md"), "report.md")
        self.assertIn("hello", query_tokens("hello world"))


if __name__ == "__main__":
    unittest.main()


