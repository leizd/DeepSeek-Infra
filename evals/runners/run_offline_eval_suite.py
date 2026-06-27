#!/usr/bin/env python3
"""Run the stable offline eval gates and write one comparable report."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNERS_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(RUNNERS_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNERS_DIR))

import run_agent_eval  # noqa: E402
import run_injection_adversarial  # noqa: E402
import run_rag_eval  # noqa: E402
import run_tool_eval  # noqa: E402
from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.evaluation import harness  # noqa: E402
from deepseek_infra.infra.rag import local_rag  # noqa: E402

SCHEMA_VERSION = "offline-eval-suite.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_sha(root: Path = REPO_ROOT) -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def git_dirty(root: Path = REPO_ROOT) -> bool:
    result = subprocess.run(["git", "status", "--short"], cwd=root, check=False, capture_output=True, text=True)
    return bool(result.stdout.strip()) if result.returncode == 0 else False


def run_rag_report(golden_path: Path, docs_root: Path, k: int) -> harness.EvalReport:
    golden = harness.load_jsonl(golden_path)
    index_dir = Path(tempfile.mkdtemp(prefix="rag-eval-suite-"))
    old_enabled = local_rag.LOCAL_RAG_ENABLED
    old_dir = local_rag.LOCAL_RAG_DIR
    old_db = local_rag.LOCAL_RAG_DB
    rag_logger = logging.getLogger(local_rag.__name__)
    old_level = rag_logger.level
    old_disable = logging.root.manager.disable
    local_rag.LOCAL_RAG_ENABLED = True
    local_rag.LOCAL_RAG_DIR = index_dir
    local_rag.LOCAL_RAG_DB = index_dir / "rag.sqlite3"
    rag_logger.setLevel(logging.ERROR)
    logging.disable(logging.WARNING)
    try:
        return run_rag_eval.evaluate(golden, k=max(1, int(k)), docs_root=docs_root)
    finally:
        local_rag.LOCAL_RAG_ENABLED = old_enabled
        local_rag.LOCAL_RAG_DIR = old_dir
        local_rag.LOCAL_RAG_DB = old_db
        rag_logger.setLevel(old_level)
        logging.disable(old_disable)
        shutil.rmtree(index_dir, ignore_errors=True)


def run_agent_report(args: argparse.Namespace) -> dict[str, Any] | None:
    if not bool(args.include_agent):
        return None
    eval_report = run_agent_eval.evaluate(harness.load_jsonl(args.agent_golden), harness.load_jsonl(args.agent_predictions))
    baseline = None if bool(args.no_agent_baseline) else run_agent_eval.load_optional_baseline(args.agent_baseline)
    return run_agent_eval.build_agent_report(eval_report, baseline=baseline)


def run_all(args: argparse.Namespace) -> tuple[harness.EvalReport, harness.EvalReport, harness.EvalReport, dict[str, Any] | None]:
    rag = run_rag_report(Path(args.rag_golden), Path(args.docs_root), int(args.k))
    tool_policy = run_tool_eval.evaluate(harness.load_jsonl(args.tool_golden))
    injection = run_injection_adversarial.evaluate(harness.load_jsonl(args.injection_golden))
    agent = run_agent_report(args)
    return rag, tool_policy, injection, agent


def _round_metric(value: Any) -> float:
    return round(float(value or 0.0), 4)


def _latency_metrics(report: harness.EvalReport) -> dict[str, float]:
    return {
        "avgMs": _round_metric(report.metrics.get("avgLatencyMs")),
        "p95Ms": _round_metric(report.metrics.get("p95LatencyMs")),
    }


def build_suite_report(
    rag: harness.EvalReport,
    tool_policy: harness.EvalReport,
    injection: harness.EvalReport,
    agent: dict[str, Any] | None = None,
    *,
    version: str = APP_VERSION,
    sha: str = "unknown",
    dirty: bool = False,
    generated_at: str | None = None,
    paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    gate = injection.benchmarks.get("softGate", {})
    gate_passed = bool(gate.get("passed"))
    policy_passed = _round_metric(tool_policy.metrics.get("toolPolicyPassRate")) >= 1.0
    defense_passed = _round_metric(tool_policy.metrics.get("injectionDefensePassRate")) >= 1.0
    hard_fail = not (policy_passed and defense_passed)
    status = "FAIL" if hard_fail else ("PASS" if gate_passed else "WARNING")

    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "gitSha": sha,
        "gitDirty": dirty,
        "generatedAt": generated_at or utc_now(),
        "status": status,
        "paths": paths or {},
        "rag": {
            "status": "PASS" if rag.cases > 0 else "FAIL",
            "cases": rag.cases,
            "k": rag.k,
            "recallAt5": _round_metric(rag.metrics.get("ragRecallAtK")),
            "mrr": _round_metric(rag.metrics.get("ragMrr")),
            "citationAccuracy": _round_metric(rag.metrics.get("citationAccuracy")),
            "keywordCoverage": _round_metric(rag.metrics.get("keywordCoverage")),
            "latency": _latency_metrics(rag),
        },
        "toolPolicy": {
            "status": "PASS" if policy_passed and defense_passed else "FAIL",
            "cases": tool_policy.cases,
            "passRate": _round_metric(tool_policy.metrics.get("toolPolicyPassRate")),
            "injectionDefensePassRate": _round_metric(tool_policy.metrics.get("injectionDefensePassRate")),
            "latency": _latency_metrics(tool_policy),
        },
        "injection": {
            "status": "PASS" if gate_passed else "WARNING",
            "cases": injection.cases,
            "blockRate": _round_metric(injection.metrics.get("blockRate")),
            "falsePositiveRate": _round_metric(injection.metrics.get("falsePositiveRate")),
            "bypassRate": _round_metric(injection.metrics.get("bypassRate")),
            "softGate": "PASS" if gate_passed else "WARNING",
            "thresholds": gate.get("thresholds", {}),
            "latency": _latency_metrics(injection),
        },
    }
    if agent is not None:
        agent_metrics = agent.get("agent", {})
        payload["agent"] = {
            "status": str(agent.get("status") or "WARNING"),
            "reportOnly": True,
            "cases": int(agent_metrics.get("cases") or 0),
            "toolCallAccuracy": _round_metric(agent_metrics.get("toolCallAccuracy")),
            "toolCallF1": _round_metric(agent_metrics.get("toolCallF1")),
            "agentSuccessRate": _round_metric(agent_metrics.get("agentSuccessRate")),
            "promptRegressionPassRate": _round_metric(agent_metrics.get("promptRegressionPassRate")),
            "avgLatencyMs": _round_metric(agent_metrics.get("avgLatencyMs")),
            "p95LatencyMs": _round_metric(agent_metrics.get("p95LatencyMs")),
            "avgTokens": _round_metric(agent_metrics.get("avgTokens")),
            "avgCostUsd": _round_metric(agent_metrics.get("avgCostUsd")),
            "warnings": agent.get("warnings", []),
            "baselineCompare": agent.get("baselineCompare", {}),
        }
    return payload


def write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def render_markdown(report: dict[str, Any]) -> str:
    rag = report["rag"]
    tool_policy = report["toolPolicy"]
    injection = report["injection"]
    agent = report.get("agent")
    lines = [
        "# Offline Eval Report",
        "",
        f"- Version: {report['version']}",
        f"- Git SHA: {report['gitSha']}{' (dirty)' if report.get('gitDirty') else ''}",
        f"- Generated: {report['generatedAt']}",
        f"- Overall: {report['status']}",
        "",
        "| Suite | Metric | Value | Status |",
        "| --- | --- | ---: | --- |",
        f"| RAG | Recall@5 | {rag['recallAt5']:.4f} | {rag['status']} |",
        f"| RAG | Citation Accuracy | {rag['citationAccuracy']:.4f} | {rag['status']} |",
        f"| RAG | MRR | {rag['mrr']:.4f} | {rag['status']} |",
        f"| Tool Policy | Pass Rate | {tool_policy['passRate']:.4f} | {tool_policy['status']} |",
        f"| Tool Policy | Injection Defense Pass Rate | {tool_policy['injectionDefensePassRate']:.4f} | {tool_policy['status']} |",
        f"| Injection | Block Rate | {injection['blockRate']:.4f} | {injection['softGate']} |",
        f"| Injection | False Positive Rate | {injection['falsePositiveRate']:.4f} | {injection['softGate']} |",
        f"| Injection | Bypass Rate | {injection['bypassRate']:.4f} | {injection['softGate']} |",
    ]
    if isinstance(agent, dict):
        lines.extend(
            [
                f"| Agent | Tool Call Accuracy | {float(agent['toolCallAccuracy']):.4f} | {agent['status']} |",
                f"| Agent | Agent Success Rate | {float(agent['agentSuccessRate']):.4f} | {agent['status']} |",
                f"| Agent | Prompt Regression Pass | {float(agent['promptRegressionPassRate']):.4f} | {agent['status']} |",
            ]
        )
    lines.extend(
        [
            "",
            "## Dataset Sizes",
            "",
            f"- RAG: {rag['cases']} cases",
            f"- Tool Policy: {tool_policy['cases']} cases",
            f"- Injection adversarial: {injection['cases']} cases",
        ]
    )
    if isinstance(agent, dict):
        lines.append(f"- Agent replay: {agent['cases']} cases")
    lines.extend(
        [
            "",
            "## Regression Compare",
            "",
            "```bash",
            "python evals/runners/compare_eval_baseline.py --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_markdown(path: str | Path, report: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report), encoding="utf-8")
    return target


def default_paths(args: argparse.Namespace) -> dict[str, str]:
    def repo_relative(path: str) -> str:
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return resolved.as_posix()

    return {
        "ragGolden": repo_relative(args.rag_golden),
        "toolPolicyGolden": repo_relative(args.tool_golden),
        "injectionGolden": repo_relative(args.injection_golden),
        "agentGolden": repo_relative(args.agent_golden),
        "agentPredictions": repo_relative(args.agent_predictions),
        "docsRoot": repo_relative(args.docs_root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run all stable offline evals and write a unified report")
    parser.add_argument("--rag-golden", default=str(REPO_ROOT / "evals" / "golden" / "rag_questions.jsonl"))
    parser.add_argument("--tool-golden", default=str(REPO_ROOT / "evals" / "golden" / "tool_policy_cases.jsonl"))
    parser.add_argument("--injection-golden", default=str(REPO_ROOT / "evals" / "golden" / "injection_adversarial.jsonl"))
    parser.add_argument("--docs-root", default=str(REPO_ROOT))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / "latest.json"))
    parser.add_argument("--markdown", default=str(REPO_ROOT / "evals" / "reports" / "latest.md"))
    parser.add_argument("--include-agent", action="store_true", help="Include Agent replay eval as report-only metrics.")
    parser.add_argument("--agent-golden", default=str(REPO_ROOT / "evals" / "golden" / "agent_tasks.jsonl"))
    parser.add_argument("--agent-predictions", default=str(REPO_ROOT / "evals" / "golden" / "agent_predictions.v2.2.8.sample.jsonl"))
    parser.add_argument("--agent-baseline", default=str(REPO_ROOT / "evals" / "baselines" / "agent-v2.2.8.json"))
    parser.add_argument("--no-agent-baseline", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the JSON report instead of the Markdown summary")
    parser.add_argument("--no-write", action="store_true", help="Do not write report files")
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None and os.environ.get("PYTHONHASHSEED") != "0":
        child_env = {**os.environ, "PYTHONHASHSEED": "0"}
        return subprocess.run([sys.executable, *sys.argv], env=child_env, check=False).returncode

    parser = build_parser()
    args = parser.parse_args(argv)
    rag, tool_policy, injection, agent = run_all(args)
    report = build_suite_report(
        rag,
        tool_policy,
        injection,
        agent,
        version=APP_VERSION,
        sha=git_sha(),
        dirty=git_dirty(),
        paths=default_paths(args),
    )

    if not args.no_write:
        write_json(args.out, report)
        write_markdown(args.markdown, report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))

    return 1 if report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
