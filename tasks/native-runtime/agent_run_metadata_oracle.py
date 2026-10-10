"""Capture original pure Agent metadata semantics without importing runtime stores."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import random
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "go/internal/agent/testdata/agent_run_metadata_v1.json"
REFERENCES = {
    "multi_agent.py": ({"AGENT_PROFILES", "MAX_AGENTS"}, {"_clean_depends_on", "safe_agent_plan", "default_agent_plan"}),
    "agent_state.py": ({"TERMINAL_NODE_STATES", "NON_NODE_PHASES"}, {"_usage_int", "_new_node", "_ensure_node", "_plan_dependencies", "reduce_node_states", "_record_latency"}),
    "agent_runs.py": ({"RUN_STATUSES"}, {"apply_event_snapshot"}),
}


def reference() -> tuple[dict[str, Any], list[dict[str, str]]]:
    namespace: dict[str, Any] = {"Any": Any}
    sources = []
    for name, (constants, functions) in REFERENCES.items():
        path = ROOT / "deepseek_infra/infra/agent_runtime" / name
        raw = path.read_bytes()
        nodes: list[ast.stmt] = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in ast.parse(raw.decode("utf-8")).body:
            names = {target.id for target in node.targets if isinstance(target, ast.Name)} if isinstance(node, ast.Assign) else (
                {node.target.id} if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) else set())
            if names & constants or isinstance(node, ast.FunctionDef) and node.name in functions:
                nodes.append(node)
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, path.as_posix(), "exec"), namespace)
        sources.append({"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
    return namespace, sources


def metadata(raw: dict[str, Any], ns: dict[str, Any]) -> dict[str, Any]:
    # Transport normalization is explicit: this corpus checks Go's reducer of
    # typed metadata, not a future Rust public-JSON parser or artifact custody.
    result = {key: raw[key] for key in ("type", "phase", "status", "scope", "plan") if key in raw}
    if not isinstance(result.get("plan"), list):
        result.pop("plan", None)
    output = raw.get("output") or {}
    if raw["type"] == "agent_output":
        result["phase"] = str(raw.get("phase") or output.get("id") or "")
        result["failed"] = bool(output.get("failed"))
        duration = output.get("duration_ms")
        usage = output.get("usage") or {}
        result["promptUsage"] = ns["_usage_int"](usage, "prompt_tokens", "promptTokens")
        result["completionUsage"] = ns["_usage_int"](usage, "completion_tokens", "completionTokens")
    else:
        duration = raw.get("durationMs")
    if isinstance(duration, int) and not isinstance(duration, bool):
        result["durationMs"] = duration
    return result


def e(kind: str, **values: Any) -> dict[str, Any]:
    return {"type": kind, **values}


def capture() -> bytes:
    ns, sources = reference()
    plan = [{"id": "coder", "task": "code"}, {"id": "critic", "task": "review", "depends_on": ["coder"]}]
    cases = {
        "plan editing removes untouched nodes": [e("agent_plan", plan=plan), e("agent_plan", plan=[plan[0]])],
        "plan editing retains nodes with execution history": [e("agent_plan", plan=plan), e("agent", phase="critic", status="running"), e("agent_plan", plan=[plan[0]])],
        "null plan and empty plan differ": [e("agent_plan", plan=[]), e("agent_plan", plan=None)],
        "missing plan clears the projected plan": [e("agent_plan", plan=plan), e("agent_plan")],
        "long public tasks are retained": [e("agent_plan", plan=[{"id": "coder", "task": "题😀" * 600}])],
        "unknown statuses do not replace status": [e("run_status", status="running"), e("run_status", status="future")],
        "cancelled view stays sticky after resume": [e("agent_plan", plan=plan), e("run_status", status="cancelled"), e("run_status", status="running"), e("agent", phase="coder", status="running")],
        "succeeded nodes survive cancellation": [e("agent_plan", plan=plan), e("agent_output", phase="coder", output={}), e("run_status", status="cancelled")],
        "usage aliases and zero updates": [e("agent_output", output={"id": "coder", "usage": {"prompt_tokens": "bad", "promptTokens": "12", "completion_tokens": 8}}), e("agent_output", phase="coder", output={"usage": {"prompt_tokens": -1, "completion_tokens": 0}})],
        "latency validation and retries": [e("agent", phase="coder", status="running"), e("agent", phase="coder", status="done", durationMs=0), e("agent_output", phase="coder", output={"failed": True, "duration_ms": -1}), e("agent_reset", phase="coder"), e("agent", phase="coder", status="running"), e("agent", phase="coder", status="error", durationMs=True)],
        "leader and synthesizer are not DAG nodes": [e("agent", phase="leader", status="running"), e("agent_output", phase="synthesizer", output={}), e("agent_reset", phase=""), e("agent_delta", phase="unknown", text="streamed")],
        "unknown execution phase is retained": [e("agent", phase="extension", status="running"), e("agent_output", phase="extension", output={"failed": "yes", "duration_ms": 2})],
        "final reset cursor and terminal states": [e("content", text="old"), e("final_reset", scope="other"), e("final_reset", scope="final_answer"), e("done"), e("error", error="failed"), e("run_status", status="orphaned")],
        "plan normalization": [e("agent_plan", plan=[{"id": "unknown", "task": "discard"}, {"id": "\u001ccoder\u001f", "task": "  ", "depends_on": ["coder", " critic ", "researcher", "researcher", "missing"]}, {"id": "coder", "task": "duplicate"}, {"id": "critic", "task": " review ", "depends_on": [" coder "]}])],
    }
    rng = random.Random(20261009)
    samples = [e("agent_plan", plan=plan), e("agent_plan", plan=[plan[0]]), e("agent_plan", plan=[]),
               e("agent_plan", plan=None), e("agent", phase="coder", status="running"),
               e("agent", phase="coder", status="done", durationMs=5), e("agent", phase="critic", status="error", durationMs=0),
               e("agent_output", phase="coder", output={"failed": True, "usage": {"prompt_tokens": 13, "completionTokens": 7}}),
               e("agent_output", phase="critic", output={}), e("agent_reset", phase="coder"),
               e("run_status", status="cancelled"), e("run_status", status="running"), e("agent_delta", phase="critic")]
    for index in range(48):
        cases[f"deterministic event history {index}"] = [rng.choice(samples) for _ in range(20)]
    captured = []
    for name, events in cases.items():
        run: dict[str, Any] = {"status": "created", "plan": [], "finalAnswer": ""}
        final_after = -1
        history = []
        checkpoints = []
        for index, event in enumerate(events):
            history.append(event)
            if event["type"] in {"run_status", "agent_plan", "final_reset", "done", "error", "content"}:
                ns["apply_event_snapshot"](run, event)
            if event["type"] == "final_reset" and event.get("scope") == "final_answer":
                final_after = index
            nodes = ns["reduce_node_states"](run["plan"], history)
            for node in nodes.values():
                node["promptUsage"] = node.pop("promptTokens")
                node["completionUsage"] = node.pop("completionTokens")
            checkpoints.append({"status": run["status"], "plan": run["plan"], "nodes": nodes, "finalAfter": final_after})
        captured.append({"name": name, "events": [metadata(item, ns) for item in events], "checkpoints": checkpoints})
    return (json.dumps({"schemaVersion": 1, "scope": "Go typed metadata projection only", "referenceSources": sources,
                        "cases": captured}, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    raw = capture()
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(raw)
    elif not OUTPUT.exists() or OUTPUT.read_bytes() != raw:
        raise SystemExit("Agent metadata reference fixture drift")
    value = json.loads(raw)
    print(json.dumps({"cases": len(value["cases"]), "checkpoints": sum(len(item["events"]) for item in value["cases"]),
                      "sha256": hashlib.sha256(raw).hexdigest()}))


if __name__ == "__main__":
    main()
