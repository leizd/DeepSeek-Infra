from __future__ import annotations

import importlib.util
from pathlib import Path

from deepseek_infra.infra.evaluation import harness


def _load_suite_runner():
    path = Path("evals/runners/run_offline_eval_suite.py").resolve()
    spec = importlib.util.spec_from_file_location("run_offline_eval_suite_agent_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_eval_suite_can_include_agent_report_only_block() -> None:
    runner = _load_suite_runner()
    rag = harness.EvalReport(suite="rag", cases=1, metrics={"ragRecallAtK": 1.0, "ragMrr": 1.0, "citationAccuracy": 1.0})
    tool_policy = harness.EvalReport(
        suite="tool-policy",
        cases=1,
        metrics={"toolPolicyPassRate": 1.0, "injectionDefensePassRate": 1.0},
    )
    injection = harness.EvalReport(
        suite="injection-adversarial",
        cases=1,
        metrics={"blockRate": 1.0, "falsePositiveRate": 0.0, "bypassRate": 0.0},
        benchmarks={"softGate": {"passed": True, "thresholds": {}}},
    )
    agent = {
        "status": "WARNING",
        "warnings": ["agentSuccessRate below recommended threshold"],
        "agent": {
            "cases": 2,
            "toolCallAccuracy": 1.0,
            "toolCallF1": 1.0,
            "agentSuccessRate": 0.5,
            "promptRegressionPassRate": 0.5,
            "avgLatencyMs": 1000.0,
            "p95LatencyMs": 1500.0,
            "avgTokens": 500.0,
            "avgCostUsd": 0.001,
        },
        "baselineCompare": {"status": "WARNING", "checks": []},
    }

    report = runner.build_suite_report(rag, tool_policy, injection, agent, version="2.2.8", sha="abc", generated_at="2026-06-27T00:00:00Z")

    assert report["status"] == "PASS"
    assert report["agent"]["reportOnly"] is True
    assert report["agent"]["status"] == "WARNING"
    markdown = runner.render_markdown(report)
    assert "| Agent | Agent Success Rate | 0.5000 | WARNING |" in markdown
    assert "- Agent replay: 2 cases" in markdown
