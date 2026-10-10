from __future__ import annotations

import unittest

from deepseek_infra.infra.gateway.chat_payload import count_payload_attachments, expanded_message_content


class ChatPayloadTests(unittest.TestCase):
    def test_expanded_message_without_attachments_returns_content(self) -> None:
        self.assertEqual(expanded_message_content({"role": "user", "content": "hello"}), "hello")

    def test_expanded_message_with_legacy_text_attachment_includes_both_parts(self) -> None:
        message = {
            "role": "user",
            "content": "Summarize this",
            "attachments": [{"name": "note.txt", "kind": "text", "text": "lorem ipsum"}],
        }

        output = expanded_message_content(message)

        self.assertIn("Summarize this", output)
        self.assertIn("note.txt", output)
        self.assertIn("lorem ipsum", output)

    def test_count_payload_attachments_ignores_malformed_input(self) -> None:
        self.assertEqual(count_payload_attachments(None), 0)
        self.assertEqual(count_payload_attachments("not a list"), 0)
        self.assertEqual(count_payload_attachments([{"attachments": [{"a": 1}, "bad", {"b": 2}]}, "bad"]), 2)


if __name__ == "__main__":
    unittest.main()


