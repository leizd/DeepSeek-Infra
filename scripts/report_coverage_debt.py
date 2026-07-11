from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


RISK_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

HIGH_RISK_MARKERS = (
    "infra/browser/",
    "infra/tool_runtime/ocr.py",
    "infra/tool_runtime/search.py",
    "infra/tool_runtime/tool_policy.py",
    "infra/skills/security.py",
    "infra/gateway/deepseek_client.py",
    "infra/rag/files.py",
    "launcher/credentials.py",
    "web/routes/downloads.py",
    "web/routes/files.py",
    "android_entry.py",
    "desktop_app.py",
)

MEDIUM_RISK_MARKERS = (
    "infra/automation/",
    "infra/media/",
    "infra/gateway/edge_inference.py",
    "infra/gateway/model_router.py",
    "infra/gateway/scheduler.py",
    "infra/skills/",
    "infra/agent_runtime/",
    "launcher/",
    "web/server.py",
)


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return raw


def normalize_module_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("./")
    prefix = "deepseek_infra/"
    return normalized[len(prefix) :] if normalized.startswith(prefix) else normalized


def classify_risk(path: str) -> tuple[str, str]:
    normalized = normalize_module_path(path)
    if any(marker in normalized for marker in HIGH_RISK_MARKERS):
        return "HIGH", "network, filesystem, browser, security, credential, or external-process boundary"
    if any(marker in normalized for marker in MEDIUM_RISK_MARKERS):
        return "MEDIUM", "stateful runtime, scheduling, media, routing, or execution boundary"
    return "LOW", "pure transformation, formatting, or lower-risk application support"


def _number(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"coverage summary field {key!r} must be numeric")
    return int(value)


def _percentage(covered: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(covered * 100.0 / total, 2)


def _module_debt(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"coverage file entry has no summary: {path}")

    statements = _number(summary, "num_statements")
    missing_statements = _number(summary, "missing_lines")
    branches = _number(summary, "num_branches")
    missing_branches = _number(summary, "missing_branches")
    risk, reason = classify_risk(path)
    return {
        "module": normalize_module_path(path),
        "risk": risk,
        "risk_reason": reason,
        "statements": statements,
        "covered_statements": max(0, statements - missing_statements),
        "missing_statements": missing_statements,
        "coverage_percent": _percentage(statements - missing_statements, statements),
        "branches": branches,
        "covered_branches": max(0, branches - missing_branches),
        "missing_branches": missing_branches,
        "branch_coverage_percent": _percentage(branches - missing_branches, branches),
    }


def build_report(coverage: dict[str, Any], *, source: str) -> dict[str, Any]:
    meta = coverage.get("meta")
    files = coverage.get("files")
    totals = coverage.get("totals")
    if not isinstance(meta, dict) or not isinstance(files, dict) or not isinstance(totals, dict):
        raise ValueError("coverage JSON must contain object-valued meta, files, and totals fields")

    modules = []
    for path, payload in files.items():
        if not isinstance(path, str) or not isinstance(payload, dict):
            raise ValueError("coverage files must map module paths to objects")
        debt = _module_debt(path, payload)
        if debt["statements"]:
            modules.append(debt)
    modules.sort(key=lambda item: (RISK_RANK[item["risk"]], -item["missing_statements"], -item["missing_branches"], item["module"]))

    total_statements = _number(totals, "num_statements")
    total_missing = _number(totals, "missing_lines")
    total_branches = _number(totals, "num_branches")
    total_missing_branches = _number(totals, "missing_branches")
    return {
        "schema_version": 1,
        "source": source,
        "coverage_format": meta.get("format"),
        "coverage_version": meta.get("version"),
        "generated_at": meta.get("timestamp"),
        "branch_coverage_enabled": bool(meta.get("branch_coverage", False)),
        "totals": {
            "statements": total_statements,
            "covered_statements": max(0, total_statements - total_missing),
            "missing_statements": total_missing,
            "coverage_percent": _percentage(total_statements - total_missing, total_statements),
            "branches": total_branches,
            "covered_branches": max(0, total_branches - total_missing_branches),
            "missing_branches": total_missing_branches,
            "branch_coverage_percent": _percentage(total_branches - total_missing_branches, total_branches),
        },
        "modules": modules,
    }


def render_report(report: dict[str, Any], *, limit: int = 30) -> str:
    lines = ["Coverage debt", ""]
    debt = [item for item in report["modules"] if item["missing_statements"] or item["missing_branches"]]
    for item in debt[:limit]:
        percent = item["coverage_percent"]
        coverage = "n/a" if percent is None else f"{percent:.1f}%"
        branch = ""
        if report["branch_coverage_enabled"]:
            branch = f", {item['missing_branches']} branches missing"
        lines.append(f"{item['risk']:<6} {item['module']:<48} {coverage:>6}   {item['missing_statements']} lines missing{branch}")
    totals = report["totals"]
    lines.extend(
        [
            "",
            f"Measured statement coverage: {totals['coverage_percent']:.2f}%",
            f"Missing statements: {totals['missing_statements']}",
        ]
    )
    if report["branch_coverage_enabled"]:
        branch_percent = totals["branch_coverage_percent"]
        lines.append(f"Measured branch coverage: {branch_percent:.2f}% ({totals['missing_branches']} branches missing)")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Report risk-weighted Python coverage debt from coverage.py JSON")
    parser.add_argument("--coverage", type=Path, required=True, help="coverage.py JSON input")
    parser.add_argument("--json-out", type=Path, required=True, help="machine-readable debt report output")
    parser.add_argument("--limit", type=int, default=30, help="maximum terminal debt rows")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        coverage = _load_json(args.coverage)
        report = build_report(coverage, source=str(args.coverage))
        if args.limit < 1:
            raise ValueError("--limit must be at least 1")
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        print(f"coverage debt report error: {exc}", file=sys.stderr)
        return 2
    print(render_report(report, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
