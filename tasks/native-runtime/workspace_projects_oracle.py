"""Freeze project read projections from the current Python implementation.

Python is an offline oracle only. All reads use an isolated temporary workspace;
the Rust fixture tests consume these files without importing or launching Python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "rust/crates/deepseek-policy/tests/fixtures/workspace_projects_oracle.json"
NOW = 1_700_000_000_000


def cases() -> list[dict[str, Any]]:
    project = {"id": "proj-read", "name": "  Example  ", "description": " Detail\r\nline ",
               "createdAt": NOW, "updatedAt": NOW + 1000,
               "documents": [{"fileId": "a" * 32, "projectId": "proj-read", "name": "paper.txt", "charCount": 123, "chunkCount": 2}],
               "conversations": [{"id": "conv-old", "title": " Old\nchat ", "createdAtMs": NOW,
                                  "messages": [None, {"id": True, "role": "user", "content": " hello ",
                                                       "reasoning": " why ", "sourceRef": {"x!": [True, None]}}]},
                                 {"conversationId": "conv-new", "createdAtMs": NOW + 1000, "tags": ["A", "a", "B"]}]}
    files: dict[str, Any] = {
        ".projects/proj-read/project.json": project,
        ".projects/proj-read/saved-items.json": {"items": [
            {"id": "save-old", "type": "chat_snippet", "title": " A\nB ", "createdAtMs": NOW, "content": " text "},
            None, {"savedId": "save-new", "type": "chat_snippet", "createdAtMs": NOW + 2000, "tags": ["A", "a"]}]},
        ".projects/proj-read/artifacts.json": {"artifacts": [
            {"id": "art-one", "path": ".generated/result.md", "createdAtMs": NOW,
             "versions": [{"version": "2", "path": ".generated/second.md", "createdAtMs": NOW + 1000}]}]},
        ".memory/memories.json": [{"id": "mem1", "scope": "project:proj-read", "content": "local", "pinned": True},
                                  {"id": "mem2", "scope": "global", "content": "global"}],
    }
    base = {"name": "full_children", "files": files}
    result = [base, {"name": "missing_project", "files": {}},
              {"name": "empty_project", "files": {".projects/proj-read/project.json": {"id": "proj-read"}}}]
    for name, relative, value in [
        ("corrupt_saved", "saved-items.json", "{broken"),
        ("invalid_saved_type", "saved-items.json", {"items": [{"id": "s", "type": "unknown"}]}),
        ("invalid_saved_project", "saved-items.json", {"items": [{"id": "s", "type": "chat_snippet", "projectId": "../outside"}]}),
        ("invalid_artifact_path", "artifacts.json", {"artifacts": [{"id": "a", "path": "../outside.txt"}]}),
        ("invalid_artifact_numbers", "artifacts.json", {"artifacts": [{"id": "a", "path": "result.txt", "version": [],
                                                                     "createdAtMs": "broken", "updatedAtMs": NOW}]}),
        ("wrong_child_root", "saved-items.json", []),
    ]:
        result.append({"name": name, "files": {**files, f".projects/proj-read/{relative}": value}})
    for name, conversations in [
        ("conversation_coercion", [{"id": [True, "id"], "title": False, "createdAtMs": "bad", "updatedAtMs": -1,
                                   "messages": [{"id": {"x": True}, "role": True, "content": [False, " text "], "createdAt": True}]}]),
        ("message_limit_before_filter", [{"id": "conv-limit", "createdAtMs": NOW,
                                          "messages": [None] * 399 + [{"content": "last"}, {"content": "excluded"}]}]),
        ("conversation_limit_and_sort", [{"id": f"c-{i}", "createdAtMs": NOW + i} for i in range(201)]),
        ("conversation_stable_sort", [{"id": "first", "createdAtMs": NOW}, {"id": "second", "createdAtMs": NOW}]),
    ]:
        result.append({"name": name, "files": {**files, ".projects/proj-read/project.json": {**project, "conversations": conversations}}})
    result.append({"name": "multiple_projects", "files": {**files, ".projects/proj-next/project.json": {
        "id": "ignored-id", "name": "Newer", "createdAt": NOW, "updatedAt": NOW + 5000}}})
    return result


def generate() -> str:
    from deepseek_infra.core import config
    from deepseek_infra.core.errors import AppError
    from deepseek_infra.infra.data import memory, projects as legacy
    from deepseek_infra.infra.workspace import artifacts, projects, saved_items

    output = []
    for case in cases():
        with tempfile.TemporaryDirectory(prefix="native-project-oracle-") as directory:
            root = Path(directory)
            for relative, value in case["files"].items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding="utf-8")

            def snapshot() -> dict[str, str]:
                return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in root.rglob("*") if path.is_file()}

            before = snapshot()
            with (patch.multiple(config, ROOT=root, PROJECTS_DIR=root / ".projects", GENERATED_DIR=root / ".generated"),
                  patch.object(legacy, "PROJECTS_DIR", root / ".projects"),
                  patch.object(memory, "MEMORY_FILE", root / ".memory/memories.json"),
                  patch.object(projects.time, "time", return_value=NOW / 1000)):
                expected: dict[str, Any] = {}
                for key, call in [("legacy_list", legacy.list_projects), ("list", projects.list_projects),
                                  ("get", lambda: projects.get_project("proj-read")),
                                  ("conversations", lambda: projects.list_project_conversations("proj-read")),
                                  ("saved", lambda: saved_items.list_saved_items("proj-read")),
                                  ("saved_filtered", lambda: saved_items.list_saved_items("proj-read", item_type="chat_snippet", tags=["a"])),
                                  ("artifacts", lambda: artifacts.list_artifacts("proj-read"))]:
                    try:
                        expected[key] = {"ok": call()}
                    except AppError as exc:
                        expected[key] = {"error": {"code": exc.code, "message": str(exc), "status": exc.status}}
            assert snapshot() == before, "read oracle mutated the workspace"
            if case["name"] == "full_children":
                assert expected["get"]["ok"]["stats"] == {
                    "files": 1, "savedItems": 2, "artifacts": 1, "conversations": 2, "memories": 1}
            output.append({**case, "now_ms": NOW, "expected": expected})
    return json.dumps(output, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    expected = generate()
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(expected, encoding="utf-8")
    if not FIXTURE.exists() or FIXTURE.read_text(encoding="utf-8") != expected:
        raise SystemExit("Workspace projects oracle drift; review and regenerate with --write")
    print(f"Workspace projects oracle: {len(json.loads(expected))} isolated storage cases verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
