from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


DEFAULT_REQUIREMENTS = Path("release/4_0_rc_requirements.json")
CI_ENV_PREFIX = "RC_CI_"
PASSING_CI_RESULTS = {"success"}


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return raw


def _workflow_jobs(root: Path) -> set[str]:
    text = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    return set(re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", text, flags=re.MULTILINE))


def _coverage_gate(root: Path) -> float:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^fail_under\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$", text, flags=re.MULTILINE)
    if not match:
        raise ValueError("pyproject.toml does not define coverage report fail_under")
    return float(match.group(1))


def _ci_env_name(job: str) -> str:
    return CI_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", job).upper()


def _result(requirement: dict[str, Any], passed: bool, observed: Any, detail: str) -> dict[str, Any]:
    return {
        "id": requirement["id"],
        "label": requirement["label"],
        "category": requirement["category"],
        "owner": requirement["owner"],
        "blocking": bool(requirement["blocking"]),
        "passed": passed,
        "required": requirement["required"],
        "observed": observed,
        "detail": detail,
        "evidence": requirement["evidence"],
    }


def _check_ci_results(root: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    required_jobs = [str(job) for job in requirement["required_jobs"]]
    missing_jobs = [job for job in required_jobs if job not in _workflow_jobs(root)]
    env_results = {job: os.getenv(_ci_env_name(job)) for job in required_jobs}
    supplied = {job: value for job, value in env_results.items() if value is not None}

    if missing_jobs:
        return _result(requirement, False, False, f"workflow jobs missing: {', '.join(missing_jobs)}")
    if supplied:
        absent = [job for job, value in env_results.items() if value is None]
        failed = [job for job, value in supplied.items() if str(value).lower() not in PASSING_CI_RESULTS]
        if absent:
            return _result(requirement, False, supplied, f"CI results missing: {', '.join(absent)}")
        if failed:
            return _result(requirement, False, supplied, f"CI jobs not green: {', '.join(failed)}")
        return _result(requirement, True, supplied, "live CI dependency results are green")

    observed = bool(requirement["observed"])
    return _result(requirement, observed, observed, "recorded merged baseline; live CI results were not supplied")


def _check_rag_cases(root: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    fixture = _load_json(root / "fixtures/rag/parity_cases.json")
    groups = ("normalization_cases", "ranking_cases", "citation_cases", "index_validation_cases")
    count = sum(len(fixture.get(group, [])) for group in groups)
    required = int(requirement["required"])
    return _result(requirement, count >= required, count, f"{count}/{required} deterministic cases present")


def _check_files_exist(root: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    missing = [path for path in requirement["evidence"] if not (root / path).is_file()]
    passed = not missing and bool(requirement["observed"])
    detail = "all contract files present" if not missing else f"files missing: {', '.join(missing)}"
    return _result(requirement, passed, not missing, detail)


def _check_files_contain(root: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    paths = [root / path for path in requirement["evidence"]]
    missing = [str(path.relative_to(root)) for path in paths if not path.is_file()]
    if missing:
        return _result(requirement, False, False, f"files missing: {', '.join(missing)}")
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    missing_needles = [needle for needle in requirement["needles"] if needle not in combined]
    passed = not missing_needles and bool(requirement["observed"])
    detail = "rollback contract present" if passed else f"rollback markers missing: {', '.join(missing_needles)}"
    return _result(requirement, passed, not missing_needles, detail)


def _check_workflow_job(root: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    job = str(requirement["job"])
    files_present = all((root / path).is_file() for path in requirement["evidence"])
    env_result = os.getenv(_ci_env_name(job))
    observed = bool(requirement["observed"]) if env_result is None else env_result.lower() in PASSING_CI_RESULTS
    passed = job in _workflow_jobs(root) and files_present and observed
    source = "recorded baseline" if env_result is None else f"live CI result: {env_result}"
    return _result(requirement, passed, observed, f"workflow job '{job}' and evidence files present; {source}")


def _check_default_rust_disabled(root: Path, requirement: dict[str, Any]) -> dict[str, Any]:
    env_text = (root / ".env.example").read_text(encoding="utf-8")
    compose_text = (root / "docker-compose.yml").read_text(encoding="utf-8")
    flags = ("GATEWAY", "MCP", "POLICY", "RAG")
    enabled = [flag for flag in flags if f"DEEPSEEK_RUST_{flag}=0" not in env_text]
    compose_opted_in = "rust-gateway:" in compose_text or "DEEPSEEK_RUST_" in compose_text
    passed = not enabled and not compose_opted_in
    detail = "all Rust flags are 0 and default Compose is Python-only"
    if enabled or compose_opted_in:
        detail = f"non-default state found: flags={enabled}, compose_opted_in={compose_opted_in}"
    return _result(requirement, passed, passed, detail)


def _evaluate_one(root: Path, requirement: dict[str, Any], coverage_override: float | None) -> dict[str, Any]:
    check = str(requirement["check"])
    if check == "ci_results":
        return _check_ci_results(root, requirement)
    if check == "pyproject_coverage_gate":
        observed = _coverage_gate(root)
        required = float(requirement["required"])
        return _result(requirement, observed >= required, observed, f"{observed:.2f}% >= {required:.2f}%")
    if check == "observed_number_gte":
        observed = float(coverage_override if coverage_override is not None else requirement["observed"])
        required = float(requirement["required"])
        return _result(requirement, observed >= required, observed, f"{observed:.2f}% vs {required:.2f}% required")
    if check == "workflow_job":
        return _check_workflow_job(root, requirement)
    if check == "rag_parity_cases":
        return _check_rag_cases(root, requirement)
    if check == "files_exist":
        return _check_files_exist(root, requirement)
    if check == "files_contain":
        return _check_files_contain(root, requirement)
    if check == "default_rust_disabled":
        return _check_default_rust_disabled(root, requirement)
    if check in {"decision_recorded", "capability_complete", "advisory"}:
        observed = bool(requirement["observed"])
        detail = "recorded as complete" if observed else requirement["description"]
        return _result(requirement, observed, observed, detail)
    raise ValueError(f"unsupported readiness check: {check}")


def evaluate_readiness(root: Path, requirements: dict[str, Any], coverage_override: float | None = None) -> dict[str, Any]:
    items = requirements.get("requirements")
    if not isinstance(items, list) or not items:
        raise ValueError("requirements must be a non-empty list")
    results = [_evaluate_one(root, item, coverage_override) for item in items]
    blockers = [item for item in results if item["blocking"] and not item["passed"]]
    advisories = [item for item in results if not item["blocking"] and not item["passed"]]
    return {
        "schema_version": 1,
        "baseline_version": requirements["baseline_version"],
        "target_version": requirements["target_version"],
        "ready": not blockers,
        "summary": {
            "passed": sum(1 for item in results if item["passed"]),
            "blocked": len(blockers),
            "advisories": len(advisories),
            "total": len(results),
        },
        "blocker_ids": [item["id"] for item in blockers],
        "advisory_ids": [item["id"] for item in advisories],
        "results": results,
    }


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value).lower() if isinstance(value, bool) else str(value)


def render_report(report: dict[str, Any]) -> str:
    lines = ["4.0.0 RC Readiness", ""]
    for item in report["results"]:
        if item["passed"]:
            prefix = "PASS"
        elif item["blocking"]:
            prefix = "BLOCK"
        else:
            prefix = "ADVISE"
        suffix = ""
        if item["id"] == "python_coverage_gate":
            suffix = f": {_format_value(item['observed'])}%"
        elif item["id"] == "python_measured_coverage":
            suffix = f": {_format_value(item['observed'])}% < {_format_value(item['required'])}%"
        elif item["id"] == "rag_parity_cases":
            suffix = f": {item['observed']}/{item['required']}"
        lines.append(f"{prefix:<6} {item['label']}{suffix}")
    lines.extend(["", f"Decision: {'READY' if report['ready'] else 'NOT READY'} FOR {report['target_version']}"])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check whether the repository is ready to create a 4.0.0 release candidate")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--python-coverage", type=float, help="override measured Python coverage for CI or local evidence")
    parser.add_argument("--json-out", type=Path, help="write the complete readiness report as JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", action="store_true", help="exit non-zero when any blocker remains")
    mode.add_argument("--report-only", action="store_true", help="report blockers without failing the process")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    requirements_path = args.requirements
    if not requirements_path.is_absolute():
        requirements_path = root / requirements_path
    try:
        requirements = _load_json(requirements_path)
        report = evaluate_readiness(root, requirements, coverage_override=args.python_coverage)
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"RC readiness configuration error: {exc}", file=sys.stderr)
        return 2

    print(render_report(report))
    if args.json_out:
        output = args.json_out if args.json_out.is_absolute() else root / args.json_out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if args.strict and not report["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
