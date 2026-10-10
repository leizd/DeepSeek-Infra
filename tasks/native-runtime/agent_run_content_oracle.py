"""Capture original Agent content replay; never import or open runtime stores."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import random
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "rust/crates/deepseek-policy/testdata/agent_run_content_v1.json"
REFERENCES = {
    "multi_agent.py": (
        {"AGENT_PROFILES", "MAX_AGENTS", "_SECTION_ALIASES", "_ATX_HEADER_RE", "_BOLD_HEADER_RE", "_LABEL_HEADER_RE"},
        {"_clean_depends_on", "safe_agent_plan", "default_agent_plan", "_section_key_for_title", "_header_section_key", "parse_structured_agent_output"},
    ),
    "agent_runs.py": (
        {"RUN_STATUSES", "SENSITIVE_PAYLOAD_KEYS"},
        {"sanitize_payload", "apply_event_snapshot", "update_agent_output_snapshot", "task_for_agent"},
    ),
}


def reference() -> tuple[dict[str, Any], list[dict[str, str]]]:
    namespace: dict[str, Any] = {"Any": Any, "re": re}
    sources = []
    for name, (constants, functions) in REFERENCES.items():
        path = ROOT / "deepseek_infra/infra/agent_runtime" / name
        raw = path.read_bytes()
        nodes: list[ast.stmt] = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in ast.parse(raw.decode("utf-8")).body:
            names = {target.id for target in node.targets if isinstance(target, ast.Name)} if isinstance(node, ast.Assign) else set()
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            if names & constants or isinstance(node, ast.FunctionDef) and node.name in functions:
                nodes.append(node)
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, str(path), "exec"), namespace)
        sources.append({"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
    return namespace, sources


def corpus() -> dict[str, Any]:
    namespace, sources = reference()
    source = json.loads((ROOT / "go/internal/agent/testdata/agent_run_metadata_v1.json").read_text(encoding="utf-8"))
    histories = {case["name"]: case["events"] for case in source["cases"]}
    histories.update({
        "structured streamed output and reasoning": [
            {"type": "agent_plan", "plan": [{"id": "coder", "task": "preserve this task"}]},
            {"type": "agent", "phase": "coder", "name": "Custom", "status": "running", "text": "started", "durationMs": 0},
            {"type": "agent_delta", "phase": "coder", "text": "# Summary\nworking"},
            {"type": "agent_delta", "phase": "coder", "text": " result\r\n**Evidence**\nsource\nRisks:\nunknown"},
            {"type": "agent_reasoning", "phase": "coder", "text": "first"},
            {"type": "agent_reasoning", "phase": "coder", "text": " second"},
            {"type": "agent_note", "phase": "coder", "text": "note"},
            {"type": "agent", "phase": "coder", "name": "", "status": "done", "text": "", "durationMs": None},
        ],
        "replacement reset and output phase fallback": [
            {"type": "agent_delta", "phase": "coder", "text": "partial"},
            {"type": "agent_output", "output": {"id": "coder", "content": "replacement", "apiKey": "fixture-only", "extension": [1, True]}},
            {"type": "agent_reset", "phase": "coder"},
            {"type": "agent_output", "phase": "critic", "output": None},
            {"type": "agent_delta", "phase": "coder", "text": {"extension": [False, None]}},
        ],
        "final answer reset and diagnostics precedence": [
            {"type": "content", "text": "old"}, {"type": "final_reset", "scope": "other"},
            {"type": "content", "text": True}, {"type": "final_reset", "scope": "final_answer"},
            {"type": "content", "text": "new"}, {"type": "done", "diagnostics": {"usage": 4}},
            {"type": "error", "error": "unknown effect"}, {"type": "done", "diagnostics": []},
        ],
        "bounded notes and leader exclusion": [
            *[{"type": "agent_note", "phase": "extension", "text": str(index)} for index in range(25)],
            {"type": "agent_delta", "phase": "leader", "text": "not a worker output"},
            {"type": "agent_reasoning", "phase": "", "text": "empty phase"},
        ],
        "recursive credential removal": [
            {"type": "content", "text": "preserved", "apiKey": "fixture-only", "nested": [{"tavilyApiKey": "fixture-search", "data": 2}]},
            {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "nested": {"apiKey": "fixture-nested", "content": "kept"}}},
        ],
    })
    rng = random.Random(202610091)
    samples = [event for name, events in histories.items() if name in {
        "structured streamed output and reasoning", "replacement reset and output phase fallback", "final answer reset and diagnostics precedence"
    } for event in events]
    for index in range(24):
        histories[f"deterministic content history {index}"] = [rng.choice(samples) for _ in range(18)]
    captured = []
    for name, events in histories.items():
        run: dict[str, Any] = {"status": "created", "plan": [], "agentOutputs": {}, "finalAnswer": "", "diagnostics": {}}
        checkpoints = []
        for event in events:
            clean = namespace["sanitize_payload"](event)
            namespace["apply_event_snapshot"](run, clean)
            checkpoints.append(json.loads(json.dumps({"event": clean, "plan": run["plan"],
                "expected": {key: run[key] for key in ["agentOutputs", "finalAnswer", "diagnostics"]}}, ensure_ascii=False)))
        captured.append({"name": name, "checkpoints": checkpoints})
    return {"schemaVersion": 1, "scope": "Rust presentation of immutable bodies; Go status/plan/DAG are external authoritative inputs",
        "referenceSources": sources, "cases": captured}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    value = corpus()
    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(raw)
    unchanged = OUTPUT.exists() and OUTPUT.read_bytes() == raw
    print(json.dumps({"status": "PASS" if unchanged else "FAIL", "cases": len(value["cases"]),
        "checkpoints": sum(len(case["checkpoints"]) for case in value["cases"]), "sha256": hashlib.sha256(raw).hexdigest()}))
    return 0 if unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
