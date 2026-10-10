from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.gateway import title_generator
from deepseek_infra.infra.gateway.title_generator import _sanitize_title, generate_title_payload


class TitleGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        title_generator._TITLE_RATE_LIMITS.clear()

    def test_sanitize_title_strips_quotes_prefixes_and_punctuation(self) -> None:
        self.assertEqual(_sanitize_title('"标题：DeepSeek API 调用。"'), "DeepSeek API 调用")
        self.assertEqual(_sanitize_title("标题: 中文测试。"), "中文测试")
        self.assertEqual(_sanitize_title("Multiline\ntitle"), "Multiline title")
        self.assertEqual(len(_sanitize_title("一" * 50)), 24)

    def test_generate_title_returns_empty_for_blank_user_message(self) -> None:
        self.assertEqual(generate_title_payload({"apiKey": "fake", "userMessage": "   "}), {"title": ""})

    def test_title_rate_limit_uses_429(self) -> None:
        with patch.object(title_generator, "TITLE_RATE_LIMIT_COUNT", 2), patch.object(title_generator, "TITLE_RATE_LIMIT_WINDOW_SECONDS", 60):
            title_generator.check_title_rate_limit("key")
            title_generator.check_title_rate_limit("key")
            with self.assertRaises(AppError) as cm:
                title_generator.check_title_rate_limit("key")

        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.code, ErrorCode.RATE_LIMITED)

    def test_generate_title_uses_readable_prompt_and_sanitizes_response(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"choices": [{"message": {"content": "标题：自动标题生成。"}}]}).encode("utf-8")

        with patch.object(title_generator.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            result = generate_title_payload(
                {
                    "apiKey": "fake",
                    "userMessage": "帮我总结一下网络协议",
                    "assistantMessage": "这段回答介绍了 OSI 和 TCP/IP。",
                }
            )

        self.assertEqual(result, {"title": "自动标题生成"})
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertIn("你是一个对话标题生成器", body["messages"][0]["content"])
        self.assertIn("用户首轮提问", body["messages"][1]["content"])
        self.assertIn("助手首轮回复摘要", body["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
