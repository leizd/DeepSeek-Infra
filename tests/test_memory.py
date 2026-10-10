from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

import pytest

import deepseek_infra.infra.data.memory as memory
from deepseek_infra.core.errors import AppError


@pytest.mark.usefixtures("tmp_settings")
class MemoryTests(unittest.TestCase):
    def test_upsert_memory_creates_and_updates_existing_item(self) -> None:
        first = memory.upsert_memory("Prefers concise answers", category="preference")
        second = memory.upsert_memory("Prefers concise answers", category="fact")
        loaded = memory.load_memories()

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["category"], "fact")

    def test_sensitive_memory_is_rejected(self) -> None:
        with self.assertRaises(AppError):
            memory.upsert_memory("api key: secret")

    def test_sensitive_memory_detector_blocks_common_secret_terms(self) -> None:
        for text in ["api key: sk-test", "password: hunter2", "secret token abc"]:
            with self.subTest(text=text):
                self.assertTrue(memory.is_sensitive_memory(text))

        self.assertFalse(memory.is_sensitive_memory("Prefers concise answers"))

    def test_infer_memory_category_handles_supported_keyword_families(self) -> None:
        self.assertEqual(memory.infer_memory_category("The app uses a local Python backend"), "project")
        self.assertEqual(memory.infer_memory_category("todo: refactor the request builder"), "todo")
        self.assertEqual(memory.infer_memory_category("Earth radius is about 6371 km"), "fact")

    def test_delete_memories_by_query_removes_matching_items(self) -> None:
        memory.upsert_memory("Project uses a local Python backend", category="project")
        memory.upsert_memory("Prefers short replies", category="preference")

        self.assertEqual(memory.delete_memories_by_query("local Python backend"), 1)
        self.assertEqual([item["content"] for item in memory.load_memories()], ["Prefers short replies"])

    def test_delete_memories_by_query_does_not_delete_by_fuzzy_token_score(self) -> None:
        memory.upsert_memory("Project alpha uses Python service with a local backend", category="project")
        memory.upsert_memory("Prefers short replies", category="preference")

        self.assertEqual(memory.delete_memories_by_query("Python backend"), 0)
        self.assertEqual(len(memory.load_memories()), 2)

    def test_memory_scope_limits_retrieval_to_current_context(self) -> None:
        memory.upsert_memory("Global prefers concise answers", category="preference")
        memory.upsert_memory("Project A uses SQLite", category="project", scope="project:alpha")
        memory.upsert_memory("Project B uses Postgres", category="project", scope="project:beta")

        alpha = memory.retrieve_memories("Project uses", scopes=["global", "project:alpha"])
        beta = memory.retrieve_memories("Project uses", scopes=["global", "project:beta"])

        self.assertIn("Project A uses SQLite", {item["content"] for item in alpha})
        self.assertNotIn("Project B uses Postgres", {item["content"] for item in alpha})
        self.assertIn("Project B uses Postgres", {item["content"] for item in beta})

    def test_memory_scope_from_payload_prefers_project_over_seek(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi", "projectId": "proj1", "seekId": "seek1"}]}

        self.assertEqual(memory.memory_scope_from_payload(payload), "project:proj1")
        self.assertEqual(memory.memory_scope_candidates(payload), ["global", "project:proj1"])

    def test_memory_conflict_detection_can_replace_old_preference(self) -> None:
        old = memory.upsert_memory("我喜欢 Vue", category="preference", scope="project:web")

        conflicts = memory.detect_memory_conflicts("我换用 React 了", category="preference", scope="project:web")
        saved = memory.upsert_memory("我换用 React 了", category="preference", scope="project:web", replace_ids=[old["id"]])

        self.assertEqual(conflicts[0]["id"], old["id"])
        self.assertEqual(saved["scope"], "project:web")
        self.assertEqual([item["content"] for item in memory.load_memories()], ["我换用 React 了"])

    def test_build_memory_suggestion_includes_scope_and_conflicts(self) -> None:
        memory.upsert_memory("我喜欢 Vue", category="preference", scope="project:web")

        suggestion = memory.build_memory_suggestion("我换用 React 了", category="preference", scope="project:web")

        self.assertEqual(suggestion["category"], "preference")
        self.assertEqual(suggestion["scope"], "project:web")
        self.assertEqual(len(suggestion["conflicts"]), 1)

    def test_concurrent_upserts_preserve_distinct_items(self) -> None:
        threads = [
            threading.Thread(target=memory.upsert_memory, args=(f"Project note {index}",), kwargs={"category": "project"})
            for index in range(12)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        contents = {item["content"] for item in memory.load_memories()}
        self.assertEqual(contents, {f"Project note {index}" for index in range(12)})

    def test_cross_process_upserts_preserve_distinct_items(self) -> None:
        script = (
            "from pathlib import Path; "
            "import sys; "
            "import deepseek_infra.infra.data.memory as memory; "
            "memory.MEMORY_DIR = Path(sys.argv[1]); "
            "memory.MEMORY_FILE = memory.MEMORY_DIR / 'memories.json'; "
            "memory.upsert_memory(sys.argv[2], category='project')"
        )
        # Module monkeypatches do not cross process boundaries. Configure the root
        # before the child imports memory/local_rag so index writes stay isolated too.
        environment = {**os.environ, "DEEPSEEK_INFRA_ROOT": str(memory.MEMORY_DIR.parent)}
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(memory.MEMORY_DIR), f"Cross process note {index}"],
                cwd=Path.cwd(), env=environment,
            )
            for index in range(2)
        ]

        for process in processes:
            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                self.fail("cross-process memory writer timed out")
            self.assertEqual(return_code, 0)

        contents = {item["content"] for item in memory.load_memories()}
        self.assertEqual(contents, {"Cross process note 0", "Cross process note 1"})

    def test_explicit_memory_command_ignores_plain_text_without_command(self) -> None:
        self.assertEqual(memory.apply_explicit_memory_command("Please keep this in the current chat only"), "")
        self.assertEqual(memory.load_memories(), [])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


