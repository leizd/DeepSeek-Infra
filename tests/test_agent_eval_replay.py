from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_agent_runner():
    path = Path("evals/runners/run_agent_eval.py").resolve()
    spec = importlib.util.spec_from_file_location("run_agent_eval_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_eval_replay_scores_normalized_predictions_and_writes_reports(tmp_path: Path) -> None:
    runner = _load_agent_runner()
    golden = [
        {"id": "agent_a", "task": "search", "expected_tools": ["web_search"], "expected_keywords": ["source"]},
        {"id": "agent_b", "task": "code", "expected_tools": ["search_files", "python_eval"], "expected_keywords": ["bug"]},
    ]
    predictions = [
        {
            "id": "agent_a",
            "tools": ["web_search"],
            "final": "answer with source",
            "status": "succeeded",
            "latencyMs": 1000,
            "usage": {"inputTokens": 100, "outputTokens": 50, "estimatedCostUsd": 0.001},
            "runId": "volatile",
        },
        {
            "id": "agent_b",
            "toolCalls": [{"name": "search_files"}, {"name": "python_eval"}],
            "answer": "bug verified",
            "status": "completed",
            "latencyMs": 2000,
            "usage": {"prompt_tokens": 200, "completion_tokens": 100},
            "trace": {"spanId": "volatile", "agentCount": 2},
        },
    ]

    report = runner.evaluate(golden, predictions)
    payload = runner.build_agent_report(report, baseline=None, generated_at="2026-06-27T00:00:00Z")
    json_path, md_path = runner.write_reports(tmp_path, payload)

    assert payload["status"] == "PASS"
    assert payload["agent"]["toolCallAccuracy"] == 1.0
    assert payload["agent"]["agentSuccessRate"] == 1.0
    assert json.loads(json_path.read_text(encoding="utf-8"))["schemaVersion"] == "agent-eval-report.v1"
    assert "Agent Eval Report" in md_path.read_text(encoding="utf-8")


def test_agent_eval_replay_missing_prediction_is_warning() -> None:
    runner = _load_agent_runner()
    report = runner.evaluate(
        [{"id": "agent_a", "task": "search", "expected_tools": ["web_search"], "expected_keywords": ["source"]}],
        [],
    )
    payload = runner.build_agent_report(report, baseline=None, generated_at="2026-06-27T00:00:00Z")

    assert payload["status"] == "WARNING"
    assert "missing predictions: agent_a" in payload["warnings"]


def test_agent_eval_baseline_compare_is_report_only_warning() -> None:
    runner = _load_agent_runner()
    current = {"agent": {"toolCallAccuracy": 0.9, "toolCallF1": 1.0, "agentSuccessRate": 1.0, "promptRegressionPassRate": 1.0}}
    baseline = {"agent": {"toolCallAccuracy": 1.0, "toolCallF1": 1.0, "agentSuccessRate": 1.0, "promptRegressionPassRate": 1.0}}

    compare = runner.compare_agent_baseline(current, baseline)

    assert compare["status"] == "WARNING"
    assert [check for check in compare["checks"] if check["metric"] == "toolCallAccuracy"][0]["status"] == "WARNING"
