#!/usr/bin/env python3
"""Agent replay eval: score normalized recorded predictions against golden tasks.

Multi-agent runs need the cloud model, so this runner stays offline and
deterministic: it joins a recorded predictions file with golden tasks by ``id``,
normalizes volatile fields away, then scores Tool Call Accuracy, Agent Success
Rate, Prompt Regression, latency and token/USD cost with the pure evaluation
harness.

Metric regressions are report-only in v2.2.8. The runner exits 1 for structural
problems such as invalid JSONL, malformed prediction rows, or missing golden ids.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.evaluation import agent_recording, harness  # noqa: E402

SCHEMA_VERSION = "agent-eval-report.v1"
TOOL_ACCURACY_WARNING_THRESHOLD = 0.80
AGENT_SUCCESS_WARNING_THRESHOLD = 0.80
PROMPT_REGRESSION_WARNING_THRESHOLD = 0.80


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def evaluate(golden: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> harness.EvalReport:
    agent_recording.validate_golden_tasks(golden)
    normalized_predictions = agent_recording.normalize_predictions(predictions)
    by_id = harness.index_by_id(normalized_predictions)
    rows: list[dict[str, Any]] = []
    for task in golden:
        task_id = str(task.get("id") or "")
        prediction = by_id.get(task_id, {})
        expected_tools = [str(t) for t in (task.get("expected_tools") or [])]
        actual_tools = [str(t) for t in (prediction.get("tools") or [])]
        expected_keywords = [str(kw) for kw in (task.get("expected_keywords") or [])]
        rows.append(
            {
                "id": task_id,
                "task": str(task.get("task") or ""),
                "tool": harness.tool_call_score(expected_tools, actual_tools),
                "success": harness.agent_success(prediction, expected_keywords),
                "latencyMs": float(prediction.get("latencyMs") or 0.0),
                "usage": prediction.get("usage") or {},
                "model": str(prediction.get("model") or ""),
                "status": str(prediction.get("status") or "missing"),
                "trace": prediction.get("trace") or {},
                "hasPrediction": bool(prediction),
            }
        )
    return harness.build_agent_report(rows, suite="agent")


def agent_warnings(report: harness.EvalReport) -> list[str]:
    metrics = report.metrics
    warnings: list[str] = []
    if float(metrics.get("toolCallAccuracy") or 0.0) < TOOL_ACCURACY_WARNING_THRESHOLD:
        warnings.append("toolCallAccuracy below recommended threshold")
    if float(metrics.get("agentSuccessRate") or 0.0) < AGENT_SUCCESS_WARNING_THRESHOLD:
        warnings.append("agentSuccessRate below recommended threshold")
    if float(metrics.get("promptRegressionPassRate") or 0.0) < PROMPT_REGRESSION_WARNING_THRESHOLD:
        warnings.append("promptRegressionPassRate below recommended threshold")
    missing = [str(row.get("id")) for row in report.details if not row.get("hasPrediction")]
    if missing:
        warnings.append(f"missing predictions: {', '.join(missing)}")
    return warnings


def agent_metrics(report: harness.EvalReport) -> dict[str, Any]:
    return {
        "cases": report.cases,
        "toolCallAccuracy": float(report.metrics.get("toolCallAccuracy") or 0.0),
        "toolCallF1": float(report.metrics.get("toolCallF1") or 0.0),
        "agentSuccessRate": float(report.metrics.get("agentSuccessRate") or 0.0),
        "promptRegressionPassRate": float(report.metrics.get("promptRegressionPassRate") or 0.0),
        "avgLatencyMs": float(report.metrics.get("avgLatencyMs") or 0.0),
        "p95LatencyMs": float(report.metrics.get("p95LatencyMs") or 0.0),
        "avgTokens": float(report.metrics.get("avgTokens") or 0.0),
        "avgCostUsd": float(report.metrics.get("avgCostUsd") or 0.0),
    }


def compare_agent_baseline(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    if not baseline:
        return {"status": "SKIPPED", "checks": []}
    current_metrics = current.get("agent", {})
    baseline_metrics = baseline.get("agent", {})
    checks: list[dict[str, Any]] = []
    for metric in (
        "toolCallAccuracy",
        "toolCallF1",
        "agentSuccessRate",
        "promptRegressionPassRate",
        "avgLatencyMs",
        "p95LatencyMs",
        "avgTokens",
        "avgCostUsd",
    ):
        old = float(baseline_metrics.get(metric) or 0.0)
        new = float(current_metrics.get(metric) or 0.0)
        higher_is_worse = metric in {"avgLatencyMs", "p95LatencyMs", "avgTokens", "avgCostUsd"}
        regressed = new > old if higher_is_worse else new < old
        checks.append({"metric": metric, "baseline": round(old, 4), "current": round(new, 4), "status": "WARNING" if regressed else "PASS"})
    return {"status": "WARNING" if any(check["status"] == "WARNING" for check in checks) else "PASS", "checks": checks}


def load_optional_baseline(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def build_agent_report(
    report: harness.EvalReport,
    *,
    version: str = APP_VERSION,
    generated_at: str | None = None,
    warnings: list[str] | None = None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    warning_list = warnings if warnings is not None else agent_warnings(report)
    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "generatedAt": generated_at or utc_now(),
        "status": "WARNING" if warning_list else "PASS",
        "warnings": warning_list,
        "agent": agent_metrics(report),
        "benchmarks": report.benchmarks,
        "details": report.details,
    }
    payload["baselineCompare"] = compare_agent_baseline(payload, baseline)
    if payload["baselineCompare"]["status"] == "WARNING" and payload["status"] == "PASS":
        payload["status"] = "WARNING"
    return payload


def render_markdown(report: dict[str, Any]) -> str:
    agent = report["agent"]
    lines = [
        "# Agent Eval Report",
        "",
        f"- Version: {report['version']}",
        f"- Generated: {report['generatedAt']}",
        f"- Status: {report['status']}",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Tool Call Accuracy | {agent['toolCallAccuracy']:.4f} |",
        f"| Tool Call F1 | {agent['toolCallF1']:.4f} |",
        f"| Agent Success Rate | {agent['agentSuccessRate']:.4f} |",
        f"| Prompt Regression Pass | {agent['promptRegressionPassRate']:.4f} |",
        f"| Avg Latency | {agent['avgLatencyMs']:.2f} ms |",
        f"| P95 Latency | {agent['p95LatencyMs']:.2f} ms |",
        f"| Avg Tokens | {agent['avgTokens']:.1f} |",
        f"| Avg Cost | ${agent['avgCostUsd']:.6f} |",
        "",
    ]
    if report.get("warnings"):
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
        lines.append("")
    baseline = report.get("baselineCompare") or {}
    if baseline.get("checks"):
        lines.extend(["## Baseline Compare", "", f"Status: {baseline['status']}", ""])
        lines.extend(f"- {check['metric']}: {check['baseline']} -> {check['current']} [{check['status']}]" for check in baseline["checks"])
        lines.append("")
    return "\n".join(lines)


def write_reports(report_dir: str | Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "agent-latest.json"
    md_path = directory / "agent-latest.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent replay eval with stable recording normalization")
    parser.add_argument("--golden", default=str(REPO_ROOT / "evals" / "golden" / "agent_tasks.jsonl"))
    parser.add_argument("--predictions", default=str(REPO_ROOT / "evals" / "golden" / "agent_predictions.v2.2.8.sample.jsonl"))
    parser.add_argument("--baseline", default=str(REPO_ROOT / "evals" / "baselines" / "agent-v2.2.8.json"))
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("--no-report", action="store_true", help="Skip writing agent-latest JSON/Markdown reports.")
    parser.add_argument("--report-only", action="store_true", help="Keep metric regressions as warnings; structural errors still fail.")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable report instead of Markdown.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        golden = harness.load_jsonl(args.golden)
        predictions = harness.load_jsonl(args.predictions)
        eval_report = evaluate(golden, predictions)
        baseline = None if args.no_baseline else load_optional_baseline(args.baseline)
        payload = build_agent_report(eval_report, baseline=baseline)
    except (OSError, ValueError, agent_recording.AgentRecordingError) as exc:
        print(f"Agent eval failed: {exc}", file=sys.stderr)
        return 1

    if not args.no_report:
        json_path, md_path = write_reports(args.report_dir, payload)
        print(f"\nAgent reports written to {json_path} and {md_path}", file=sys.stderr)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
