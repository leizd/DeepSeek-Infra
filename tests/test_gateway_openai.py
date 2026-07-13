from __future__ import annotations

import http.client
import json
import threading
import unittest
from typing import Any, Callable
from unittest.mock import patch

import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.gateway import openai_api
from deepseek_infra.web.server import FastAPIServer


class OpenAIPayloadTests(unittest.TestCase):
    def test_to_internal_payload_normalizes_and_defaults(self) -> None:
        payload = openai_api.openai_to_internal_payload(
            {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "stream": True, "temperature": 0.3},
            local_base_url="http://127.0.0.1:8000",
        )
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["thinkingEnabled"])
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["messages"][0]["content"], "hi")
        self.assertEqual(payload["localBaseUrl"], "http://127.0.0.1:8000")

    def test_to_internal_payload_requires_messages(self) -> None:
        with self.assertRaises(AppError):
            openai_api.openai_to_internal_payload({"messages": []})
        with self.assertRaises(AppError):
            openai_api.openai_to_internal_payload({"model": "deepseek-v4-pro"})

    def test_models_list_returns_catalog(self) -> None:
        data = openai_api.openai_models_list()
        self.assertEqual(data["object"], "list")
        ids = {model["id"] for model in data["data"]}
        self.assertIn("deepseek-v4-pro", ids)
        self.assertIn("deepseek-v4-flash", ids)
        self.assertTrue(all(model["object"] == "model" for model in data["data"]))

    def test_completion_response_maps_content_and_usage(self) -> None:
        out = openai_api.openai_completion_response(
            {
                "id": "abc",
                "model": "deepseek-v4-pro",
                "content": "Hello",
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                "diagnostics": {"gatewayRequestPreparation": {"runtime": "rust", "fallback": False}},
            },
            "deepseek-v4-pro",
        )
        self.assertEqual(out["object"], "chat.completion")
        self.assertEqual(out["model"], "deepseek-v4-pro")
        self.assertEqual(out["choices"][0]["message"], {"role": "assistant", "content": "Hello"})
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")
        self.assertEqual(out["usage"], {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        self.assertEqual(out["diagnostics"]["gatewayRequestPreparation"]["runtime"], "rust")


class OpenAIStreamTests(unittest.TestCase):
    def _collect(self) -> str:
        chunks = list(openai_api.openai_chat_stream({"model": "deepseek-v4-pro", "messages": []}, "deepseek-v4-pro"))
        return b"".join(chunks).decode("utf-8")

    def test_stream_emits_chunks_and_done(self) -> None:
        def fake(payload: dict[str, Any], emit: Callable[[dict[str, Any]], None], **kwargs: Any) -> None:
            emit({"type": "content", "text": "Hello"})
            emit({"type": "content", "text": " world"})
            emit({"type": "done"})

        with patch("deepseek_infra.infra.gateway.providers.deepseek.stream_deepseek", fake):
            text = self._collect()
        self.assertIn('"role":"assistant"', text)
        self.assertIn('"content":"Hello"', text)
        self.assertIn('"content":" world"', text)
        self.assertIn('"finish_reason":"stop"', text)
        self.assertIn('"object":"chat.completion.chunk"', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    def test_stream_surfaces_upstream_error(self) -> None:
        def fake(payload: dict[str, Any], emit: Callable[[dict[str, Any]], None], **kwargs: Any) -> None:
            emit({"type": "error", "error": "内容安全提示：DeepSeek 判定本轮内容存在风险"})

        with patch("deepseek_infra.infra.gateway.providers.deepseek.stream_deepseek", fake):
            text = self._collect()
        self.assertIn('"error"', text)
        self.assertIn("内容安全提示", text)
        self.assertIn("data: [DONE]", text)


class OpenAIRouteTests(unittest.TestCase):
    def make_server(self) -> tuple[FastAPIServer, threading.Thread]:
        server, _ = server_module.create_server(0, host="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def request(
        self, server: FastAPIServer, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
        finally:
            connection.close()

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {server_module.settings.auth.token}"}

    def test_v1_models_requires_auth_then_lists(self) -> None:
        server, thread = self.make_server()
        try:
            status, _ = self.request(server, "GET", "/v1/models")
            self.assertEqual(status, 401)

            status, payload = self.request(server, "GET", "/v1/models", headers=self.auth())
            self.assertEqual(status, 200)
            self.assertEqual(payload["object"], "list")
            self.assertIn("deepseek-v4-pro", {model["id"] for model in payload["data"]})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_v1_chat_completions_non_stream(self) -> None:
        server, thread = self.make_server()
        canned = {
            "id": "abc",
            "model": "deepseek-v4-pro",
            "content": "Hi there",
            "reasoning": "",
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }
        try:
            with patch("deepseek_infra.infra.gateway.providers.deepseek.call_deepseek", return_value=canned):
                status, payload = self.request(
                    server,
                    "POST",
                    "/v1/chat/completions",
                    headers={"Content-Type": "application/json", **self.auth()},
                    body=json.dumps({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
                )
            self.assertEqual(status, 200)
            self.assertEqual(payload["object"], "chat.completion")
            self.assertEqual(payload["choices"][0]["message"]["content"], "Hi there")
            self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
            self.assertEqual(payload["usage"]["total_tokens"], 6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
