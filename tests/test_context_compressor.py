from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import deepseek_infra.infra.rag.context_compressor as compressor
from deepseek_infra.core.errors import AppError, ErrorCode


class ContextCompressorTests(unittest.TestCase):
    def test_serialize_messages_filters_roles_and_keeps_attachments(self) -> None:
        serialized = compressor.serialize_messages_for_context_summary(
            [
                {"role": "system", "content": "hidden"},
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "", "attachments": [{"name": "notes.txt", "text": "attached text"}]},
            ]
        )

        self.assertNotIn("hidden", serialized)
        self.assertIn("question", serialized)
        self.assertIn("answer", serialized)
        self.assertIn("attached text", serialized)

    def test_compress_context_payload_requires_api_key(self) -> None:
        with self.assertRaises(AppError) as cm:
            compressor.compress_context_payload({"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(cm.exception.code, ErrorCode.MISSING_API_KEY)

    def test_compress_context_payload_returns_summary_from_upstream(self) -> None:
        response = {
            "choices": [{"message": {"content": "summary"}}],
            "usage": {"prompt_tokens": 1},
        }

        with patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))):
            result = compressor.compress_context_payload(
                {
                    "apiKey": "test",
                    "messages": [{"role": "user", "content": "new context"}],
                    "previousSummary": "old context",
                    "contextPins": ["pin"],
                }
            )

        self.assertEqual(result["summary"], "summary")
        self.assertEqual(result["compressedMessageCount"], 1)
        self.assertEqual(result["usage"], {"prompt_tokens": 1})

    def test_compress_context_payload_uses_configured_timeout(self) -> None:
        response = {
            "choices": [{"message": {"content": "summary"}}],
            "usage": {},
        }

        with (
            patch.object(compressor, "DEEPSEEK_TIMEOUT_SECONDS", 11),
            patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))) as mocked,
        ):
            compressor.compress_context_payload({"apiKey": "test", "messages": [{"role": "user", "content": "new context"}]})

        self.assertEqual(mocked.call_args.kwargs["timeout"], 11)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


if __name__ == "__main__":
    unittest.main()
