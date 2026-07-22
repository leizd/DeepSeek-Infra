#!/usr/bin/env python3
"""Compare a current offline eval report against a versioned baseline."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402

EPSILON = 1e-9

CHECKS: tuple[dict[str, Any], ...] = (
    {"metric": "rag.recallAt5", "direction": "drop", "limit": 0.03, "label": "RAG Recall@5"},
    {"metric": "rag.citationAccuracy", "direction": "drop", "limit": 0.05, "label": "Citation Accuracy"},
    {"metric": "toolPolicy.passRate", "direction": "drop", "limit": 0.0, "label": "Tool Policy Pass Rate"},
    {"metric": "injection.bypassRate", "direction": "increase", "limit": 0.03, "label": "Injection Bypass Rate"},
    {"metric": "injection.falsePositiveRate", "direction": "increase", "limit": 0.03, "label": "Injection False Positive Rate"},
)

AGENT_CHECKS: tuple[dict[str, Any], ...] = (
    {"metric": "agent.agentSuccessRate", "direction": "drop", "limit": 0.05, "label": "Agent Success Rate"},
)


def load_report(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_environment() -> dict[str, Any]:
    return {
        "os": platform.system(),
        "python": platform.python_version(),
        "ci": bool(os.environ.get("CI")),
    }


def _report_sha(report: dict[str, Any]) -> str:
    return str(report.get("commit") or report.get("gitSha") or "unknown")


def metric_value(report: dict[str, Any], dotted_path: str) -> float:
    value: Any = report
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted_path)
        value = value[part]
    return float(value)


def compare_one(baseline: dict[str, Any], current: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    metric = str(check["metric"])
    limit = float(check["limit"])
    direction = str(check["direction"])
    try:
        old = metric_value(baseline, metric)
        new = metric_value(current, metric)
    except KeyError:
        return {
            "metric": metric,
            "label": check["label"],
            "status": "FAIL",
            "message": "metric missing from baseline or current report",
        }

    delta = round(new - old, 4)
    if direction == "drop":
        regression = old - new
        failed = regression > limit + EPSILON
        warned = regression > EPSILON
        rule = f"drop <= {limit:.4f}"
    else:
        regression = new - old
        failed = regression > limit + EPSILON
        warned = regression > EPSILON
        rule = f"increase <= {limit:.4f}"

    status = "FAIL" if failed else ("WARNING" if warned else "PASS")
    if limit == 0.0 and warned:
        status = "FAIL"

    return {
        "metric": metric,
        "label": check["label"],
        "baseline": round(old, 4),
        "current": round(new, 4),
        "delta": delta,
        "allowed": rule,
        "status": status,
    }


def compare_reports(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    agent_baseline: dict[str, Any] | None = None,
    agent_current: dict[str, Any] | None = None,
    version: str = APP_VERSION,
    generated_at: str | None = None,
) -> dict[str, Any]:
    checks = [compare_one(baseline, current, check) for check in CHECKS]
    agent_source = agent_current or (current if isinstance(current.get("agent"), dict) else None)
    if agent_baseline is not None or agent_source is not None:
        if agent_baseline is None or agent_source is None:
            checks.append(
                {
                    "metric": "agent.agentSuccessRate",
                    "label": "Agent Success Rate",
                    "status": "FAIL",
                    "message": "agent baseline or current report missing",
                }
            )
        else:
            checks.extend(compare_one(agent_baseline, agent_source, check) for check in AGENT_CHECKS)
    if any(check["status"] == "FAIL" for check in checks):
        status = "FAIL"
    elif any(check["status"] == "WARNING" for check in checks):
        status = "WARNING"
    else:
        status = "PASS"
    return {
        "schemaVersion": "offline-eval-compare.v1",
        "version": version,
        "commit": _report_sha(current),
        **evidence_revision(REPO_ROOT),
        "generatedAt": generated_at or utc_now(),
        "environment": build_environment(),
        "status": status,
        "baseline": {
            "version": baseline.get("version", "unknown"),
            "gitSha": _report_sha(baseline),
        },
        "current": {
            "version": current.get("version", "unknown"),
            "gitSha": _report_sha(current),
        },
        "checks": checks,
    }


def render_text(result: dict[str, Any]) -> str:
    lines = [
        "=== Eval Baseline Compare ===",
        f"Baseline: {result['baseline']['version']} ({result['baseline']['gitSha']})",
        f"Current: {result['current']['version']} ({result['current']['gitSha']})",
        f"Overall: {result['status']}",
        "",
    ]
    for check in result["checks"]:
        if check["status"] == "FAIL" and "baseline" not in check:
            lines.append(f"- {check['label']}: {check['message']} [FAIL]")
            continue
        lines.append(
            f"- {check['label']}: {check['baseline']:.4f} -> {check['current']:.4f} "
            f"(delta {check['delta']:+.4f}; allowed {check['allowed']}) [{check['status']}]"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare offline eval report against a versioned baseline")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--agent-baseline", default="", help="Optional Agent Eval baseline report.")
    parser.add_argument("--agent-current", default="", help="Optional Agent Eval current report; defaults to --current when it has an agent block.")
    parser.add_argument("--out", default="")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on WARNING or FAIL, for CI regression blocking.")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable compare result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = load_report(args.current)
    agent_baseline = load_report(args.agent_baseline) if args.agent_baseline else None
    agent_current = load_report(args.agent_current) if args.agent_current else None
    result = compare_reports(
        load_report(args.baseline),
        current,
        agent_baseline=agent_baseline,
        agent_current=agent_current,
        version=str(current.get("version") or APP_VERSION),
    )
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 1 if result["status"] == "FAIL" or (args.strict and result["status"] != "PASS") else 0


if __name__ == "__main__":
    raise SystemExit(main())
