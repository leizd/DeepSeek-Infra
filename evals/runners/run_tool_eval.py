#!/usr/bin/env python3
"""Tool Policy / 注入防御回归评测：离线重放安全闸门，无需服务、无需 API Key。

三类 golden 用例（``evals/golden/tool_policy_cases.jsonl``）：

- **policy**：把 (capability, tool, arguments) 喂给真实的 ``ToolPolicy.evaluate``，
  断言 allow / deny / confirm 与关键拒绝原因（SSRF、路径越界、密钥外泄、敏感记忆、
  能力越权、未知工具、人工确认、污染轮升级）；
- **sanitize**：把含注入指令的外部工具结果喂给 ``sanitize_tool_result``，断言注入行
  被红action、URL 等结构字段保留、良性文本不被误伤；
- **taint**：把不可信上下文文本喂给 ``context_taint.scan_text``，断言恶意指令被标记、
  良性文本不被误报。

输出 Tool Policy Pass Rate 与 Prompt Injection Defense Pass Rate；任何错判用例逐条列出。

Usage::

    python evals/runners/run_tool_eval.py
    python evals/runners/run_tool_eval.py --json --no-report
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.evaluation import harness  # noqa: E402
from deepseek_infra.infra.gateway import context_taint  # noqa: E402
from deepseek_infra.infra.tool_runtime.tool_policy import ToolPolicy, sanitize_tool_result  # noqa: E402

# 注入给 with_secret 用例的合成运行时凭证（绝非真实密钥）。
SYNTHETIC_SECRET = "EVAL-ONLY-RUNTIME-SECRET-0123456789abcdef"
EXPECT_TO_ACTION = {"allow": "allow", "deny": "deny", "confirm": "needs_confirmation"}


def substitute_secret(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("{{RUNTIME_SECRET}}", SYNTHETIC_SECRET)
    if isinstance(value, dict):
        return {key: substitute_secret(item) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_secret(item) for item in value]
    return value


def run_policy_case(case: dict[str, Any]) -> dict[str, Any]:
    arguments = substitute_secret(case.get("arguments") or {})
    policy = ToolPolicy(
        capability=str(case.get("capability") or "full"),
        approvals={str(item) for item in (case.get("approvals") or [])},
        require_confirm=True,
        audit=False,  # 评测不写 .tool-audit
        secrets=(SYNTHETIC_SECRET,) if case.get("with_secret") else (),
        tainted=bool(case.get("tainted")),
        taint_escalation=bool(case.get("tainted")),
    )
    started = time.perf_counter()
    decision = policy.evaluate(str(case.get("tool") or ""), arguments)
    latency_ms = (time.perf_counter() - started) * 1000.0
    expected_action = EXPECT_TO_ACTION.get(str(case.get("expect") or ""), "allow")
    reason_prefix = str(case.get("expect_reason") or "")
    reason_ok = (not reason_prefix) or any(reason.startswith(reason_prefix) for reason in decision.reasons)
    passed = decision.action == expected_action and reason_ok
    return {
        "id": str(case.get("id") or ""),
        "kind": "policy",
        "tool": decision.tool,
        "expected": expected_action,
        "actual": decision.action,
        "reasons": list(decision.reasons),
        "passed": passed,
        "latencyMs": round(latency_ms, 3),
    }


def run_sanitize_case(case: dict[str, Any]) -> dict[str, Any]:
    output = case.get("output") or {}
    started = time.perf_counter()
    cleaned, hits = sanitize_tool_result(str(case.get("tool") or ""), output)
    latency_ms = (time.perf_counter() - started) * 1000.0
    cleaned_text = str(cleaned)
    expect_redacted = bool(case.get("expect_redacted"))
    redaction_ok = (hits > 0) if expect_redacted else (hits == 0 and cleaned == output)
    kept_ok = all(marker in cleaned_text for marker in (case.get("must_keep") or []))
    passed = redaction_ok and kept_ok
    return {
        "id": str(case.get("id") or ""),
        "kind": "sanitize",
        "expected": "redacted" if expect_redacted else "untouched",
        "actual": f"hits={hits}",
        "passed": passed,
        "latencyMs": round(latency_ms, 3),
    }


def run_taint_case(case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    scan = context_taint.scan_text(str(case.get("text") or ""))
    latency_ms = (time.perf_counter() - started) * 1000.0
    flagged = (scan.injection + scan.exfiltration + scan.tool_directive) > 0
    passed = flagged == bool(case.get("expect_flagged"))
    return {
        "id": str(case.get("id") or ""),
        "kind": "taint",
        "expected": "flagged" if case.get("expect_flagged") else "clean",
        "actual": f"injection={scan.injection} exfiltration={scan.exfiltration} toolDirective={scan.tool_directive}",
        "passed": passed,
        "latencyMs": round(latency_ms, 3),
    }


def evaluate(golden: list[dict[str, Any]]) -> harness.EvalReport:
    rows: list[dict[str, Any]] = []
    for case in golden:
        kind = str(case.get("kind") or "policy")
        if kind == "policy":
            rows.append(run_policy_case(case))
        elif kind == "sanitize":
            rows.append(run_sanitize_case(case))
        elif kind == "taint":
            rows.append(run_taint_case(case))
        else:
            print(f"warning: unknown case kind: {kind}", file=sys.stderr)

    policy_rows = [row for row in rows if row["kind"] == "policy"]
    defense_rows = [row for row in rows if row["kind"] in {"sanitize", "taint"}]
    latency = harness.latency_benchmark([float(row["latencyMs"]) for row in rows])
    metrics = {
        "toolPolicyPassRate": harness.aggregate_ratio([bool(row["passed"]) for row in policy_rows]),
        "injectionDefensePassRate": harness.aggregate_ratio([bool(row["passed"]) for row in defense_rows]),
        "avgLatencyMs": latency["avgMs"],
        "p95LatencyMs": latency["p95Ms"],
    }
    return harness.EvalReport(suite="tool-policy", cases=len(rows), metrics=metrics, benchmarks={"latency": latency}, details=rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tool policy / injection defense regression eval")
    parser.add_argument("--golden", default=str(REPO_ROOT / "evals" / "golden" / "tool_policy_cases.jsonl"))
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = evaluate(harness.load_jsonl(args.golden))

    if args.json:
        import json

        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.to_text())
        failures = [row for row in report.details if not row.get("passed")]
        if failures:
            print("\nFailures:")
            for row in failures:
                print(f"  - {row['id']} ({row['kind']}): expected {row['expected']}, got {row['actual']} {row.get('reasons', '')}")
    if not args.no_report:
        path = report.write(args.report_dir)
        print(f"\nReport written to {path}", file=sys.stderr)
    failed = any(not row.get("passed") for row in report.details)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
