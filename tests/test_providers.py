from __future__ import annotations

import json
import types
import unittest
import urllib.error
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.gateway.providers.ollama as ollama_mod
import deepseek_infra.infra.gateway.providers.registry as registry_mod
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.gateway.providers.deepseek import DeepSeekProvider
from deepseek_infra.infra.gateway.providers.ollama import OllamaProvider


class _FakeResp:
    """Context-manager HTTP response: read() for JSON, iteration for NDJSON lines."""

    def __init__(self, *, body: bytes = b"", lines: list[bytes] | None = None) -> None:
        self._body = body
        self._lines = lines or []

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def __iter__(self) -> Any:
        return iter(self._lines)


def _enabled_settings(base: str = "http://127.0.0.1:11434") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        ollama=types.SimpleNamespace(enabled=True, base_url=base, timeout_seconds=120),
        supported_models=("deepseek-v4-pro", "deepseek-v4-flash"),
    )


class DeepSeekProviderTests(unittest.TestCase):
    def test_chat_delegates_to_call_deepseek(self) -> None:
        provider = DeepSeekProvider()
        with patch("deepseek_infra.infra.gateway.providers.deepseek.call_deepseek", return_value={"content": "hi"}) as mocked:
            self.assertEqual(provider.chat({"model": "deepseek-v4-pro"}), {"content": "hi"})
        mocked.assert_called_once()

    def test_stream_delegates_to_stream_deepseek(self) -> None:
        provider = DeepSeekProvider()
        with patch("deepseek_infra.infra.gateway.providers.deepseek.stream_deepseek") as mocked:
            provider.stream_chat({"model": "x"}, lambda event: None)
        mocked.assert_called_once()

    def test_models_and_available(self) -> None:
        provider = DeepSeekProvider()
        self.assertIn("deepseek-v4-pro", provider.models())
        self.assertTrue(provider.available())


class OllamaProviderTests(unittest.TestCase):
    def test_chat_maps_content_and_usage(self) -> None:
        provider = OllamaProvider()
        body = json.dumps(
            {"model": "llama3", "message": {"role": "assistant", "content": "你好"}, "prompt_eval_count": 11, "eval_count": 7}
        ).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_FakeResp(body=body)):
            out = provider.chat({"model": "ollama/llama3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out["content"], "你好")
        self.assertEqual(out["model"], "ollama/llama3")
        self.assertEqual(out["usage"], {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})

    def test_stream_emits_content_then_done(self) -> None:
        provider = OllamaProvider()
        lines = [
            json.dumps({"message": {"content": "He"}, "done": False}).encode("utf-8"),
            json.dumps({"message": {"content": "llo"}, "done": False}).encode("utf-8"),
            json.dumps({"message": {"content": ""}, "done": True}).encode("utf-8"),
        ]
        events: list[dict[str, Any]] = []
        with patch("urllib.request.urlopen", return_value=_FakeResp(lines=lines)):
            provider.stream_chat({"model": "ollama/llama3", "messages": []}, events.append)
        self.assertEqual([e.get("text") for e in events if e["type"] == "content"], ["He", "llo"])
        self.assertEqual(events[-1]["type"], "done")

    def test_chat_unreachable_raises_app_error(self) -> None:
        provider = OllamaProvider()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(AppError):
                provider.chat({"model": "ollama/llama3", "messages": []})

    def test_stream_unreachable_emits_error_event(self) -> None:
        provider = OllamaProvider()
        events: list[dict[str, Any]] = []
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            provider.stream_chat({"model": "ollama/llama3", "messages": []}, events.append)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("Ollama", events[-1]["error"])

    def test_models_disabled_returns_empty(self) -> None:
        provider = OllamaProvider()  # ollama disabled by default
        self.assertEqual(provider.models(), [])
        self.assertFalse(provider.available())

    def test_models_enabled_lists_tags_and_handles(self) -> None:
        provider = OllamaProvider()
        body = json.dumps({"models": [{"name": "llama3:latest"}, {"name": "qwen2"}]}).encode("utf-8")
        with (
            patch.object(ollama_mod, "settings", _enabled_settings()),
            patch("urllib.request.urlopen", return_value=_FakeResp(body=body)),
        ):
            self.assertEqual(provider.models(), ["llama3:latest", "qwen2"])
            self.assertTrue(provider.handles("llama3:latest"))
            self.assertTrue(provider.handles("ollama/anything"))
            self.assertFalse(provider.handles("deepseek-v4-pro"))

    def test_unreachable_models_are_cached_and_not_reprobed(self) -> None:
        provider = OllamaProvider()
        calls = {"n": 0}

        def fake_urlopen(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            raise urllib.error.URLError("refused")

        with (
            patch.object(ollama_mod, "settings", _enabled_settings()),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            self.assertEqual(provider.models(), [])
            self.assertEqual(provider.models(), [])  # within TTL -> served from cache
        self.assertEqual(calls["n"], 1)


class RegistryRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        registry_mod._ollama._tags = []
        registry_mod._ollama._tags_fetched_at = 0.0

    def test_disabled_always_routes_deepseek(self) -> None:
        self.assertEqual(registry_mod.resolve_provider("ollama/llama3").name, "deepseek")
        self.assertEqual(registry_mod.resolve_provider("anything").name, "deepseek")

    def test_catalog_disabled_only_deepseek(self) -> None:
        ids = [entry["id"] for entry in registry_mod.model_catalog()]
        self.assertIn("deepseek-v4-pro", ids)
        self.assertFalse(any(model_id.startswith("ollama/") for model_id in ids))

    def test_enabled_routes_by_prefix_known_and_tag(self) -> None:
        fake = _enabled_settings()
        body = json.dumps({"models": [{"name": "llama3"}]}).encode("utf-8")
        with (
            patch.object(registry_mod, "settings", fake),
            patch.object(ollama_mod, "settings", fake),
            patch("urllib.request.urlopen", return_value=_FakeResp(body=body)),
        ):
            self.assertEqual(registry_mod.resolve_provider("ollama/llama3").name, "ollama")
            self.assertEqual(registry_mod.resolve_provider("deepseek-v4-pro").name, "deepseek")
            self.assertEqual(registry_mod.resolve_provider("llama3").name, "ollama")
            ids = [entry["id"] for entry in registry_mod.model_catalog()]
            self.assertIn("deepseek-v4-pro", ids)
            self.assertIn("ollama/llama3", ids)


if __name__ == "__main__":
    unittest.main()
