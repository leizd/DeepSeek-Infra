"""Generate/check terminal A2A SSE fixtures using the Python oracle itself.

Only the task lookup is injected. The production snapshot, chunk filtering,
event projection, and SSE generator functions execute unmodified from the AST.
JSON object key order is deliberately outside this semantic comparison.
"""
from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "rust/crates/deepseek-gateway/tests/fixtures/a2a_stream_oracle.json"
FUNCTIONS = {
    "public_task", "_artifact_update_from_chunk", "_result", "_sse",
    "_chunk_index", "_task_chunks_after", "_stream_task_events",
}


def generate() -> str:
    source = ROOT / "deepseek_infra/infra/agent_runtime/a2a.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    if {node.name for node in definitions} != FUNCTIONS:
        raise RuntimeError("A2A oracle function inventory changed")
    namespace: dict[str, Any] = {
        "Any": Any, "Generator": Generator, "json": json,
        "A2A_HISTORY_LIMIT": 20, "TERMINAL_STATES": {"completed", "failed", "canceled"},
    }
    module = ast.Module(body=[], type_ignores=[])
    module.body.extend(definitions)
    exec(compile(module, str(source), "exec"), namespace)
    cases = []
    for state in ("completed", "failed", "canceled"):
        task: dict[str, Any] = {
            "id": "task_oracle", "contextId": "ctx_oracle", "kind": "task",
            "agentId": "reasoner", "createdAt": "2026-09-19T00:00:00Z",
            "status": {"state": state, "timestamp": "2026-09-19T00:00:01Z"},
            "history": [{"role": "user", "parts": [{"kind": "text", "text": "你好"}]}],
            "artifacts": [], "artifactChunks": [], "_private": "must not leak",
        }
        names = ("progress", "answer") if state == "completed" else ("progress",)
        for index, name in enumerate(names):
            artifact = {
                "artifactId": f"artifact_{index}", "name": name,
                "parts": [{"kind": "text", "text": "你好" if name == "answer" else "A2A worker accepted the task."}],
            }
            task["artifactChunks"].append({
                "taskId": task["id"], "contextId": task["contextId"],
                "artifactId": artifact["artifactId"], "chunkIndex": index,
                "append": True, "final": name == "answer", "createdAt": task["createdAt"], "artifact": artifact,
            })
            if name == "answer":
                task["artifacts"] = [artifact]
        namespace["get_task"] = lambda _task_id: task
        for cursor in (-1, 0, 1, 99):
            frames = namespace["_stream_task_events"]("oracle", task["id"], after_chunk_index=cursor)
            events = [json.loads(frame.decode("utf-8")[6:]) for frame in frames]
            cases.append({"name": f"{state}/{cursor}", "task": task, "cursor": cursor, "events": events})
    return json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    expected = generate()
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(expected, encoding="utf-8")
    if not FIXTURE.exists() or FIXTURE.read_text(encoding="utf-8") != expected:
        raise SystemExit("A2A oracle fixture drift; review and regenerate with --write")
    print(f"A2A oracle: {len(json.loads(expected))} cases verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
