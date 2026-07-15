#!/usr/bin/env python3
"""Measure the complete Rust workspace and emit release-grade coverage evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402

WORKSPACE_CRATES = (
    "deepseek-core",
    "deepseek-gateway",
    "deepseek-mcp",
    "deepseek-policy",
    "deepseek-rag",
)


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=capture,
    )


def _git_commit() -> str:
    result = _run(["git", "rev-parse", "HEAD"], capture=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _tool_version() -> str:
    result = _run(["cargo", "llvm-cov", "--version"], capture=True)
    return (result.stdout or result.stderr).strip() if result.returncode == 0 else "unknown"


def _test_count() -> int:
    result = _run(
        [
            "cargo",
            "test",
            "--manifest-path",
            "rust/Cargo.toml",
            "--workspace",
            "--all-features",
            "--",
            "--list",
            "--format",
            "terse",
        ],
        capture=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "unable to enumerate Rust tests")
    return sum(1 for line in result.stdout.splitlines() if line.rstrip().endswith(": test"))


def _summary(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("cargo llvm-cov JSON must contain exactly one workspace summary")
    totals = data[0].get("totals")
    if not isinstance(totals, dict):
        raise ValueError("cargo llvm-cov JSON is missing totals")
    return totals


def _coverage_metric(totals: dict[str, Any], name: str) -> dict[str, float | int]:
    value = totals.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"cargo llvm-cov totals are missing {name}")
    return {
        "count": int(value.get("count", 0)),
        "covered": int(value.get("covered", 0)),
        "percent": float(value.get("percent", 0.0)),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enforce and record complete Rust workspace line coverage")
    parser.add_argument("--threshold", type=float, default=80.0)
    parser.add_argument("--artifact-out", type=Path, default=ROOT / "artifacts" / "rust-coverage.json")
    parser.add_argument("--lcov-out", type=Path, default=ROOT / "artifacts" / "rust-coverage.lcov")
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=ROOT / "docs" / "evidence" / f"rust-coverage-v{APP_VERSION}.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact_out = args.artifact_out if args.artifact_out.is_absolute() else ROOT / args.artifact_out
    lcov_out = args.lcov_out if args.lcov_out.is_absolute() else ROOT / args.lcov_out
    evidence_out = args.evidence_out if args.evidence_out.is_absolute() else ROOT / args.evidence_out
    raw_out = artifact_out.with_name(artifact_out.stem + "-raw.json")
    for path in (artifact_out, lcov_out, evidence_out, raw_out):
        path.parent.mkdir(parents=True, exist_ok=True)

    coverage_command = [
        "cargo",
        "llvm-cov",
        "--manifest-path",
        "rust/Cargo.toml",
        "--workspace",
        "--all-features",
        "--json",
        "--summary-only",
        "--output-path",
        str(raw_out),
    ]
    completed = _run(coverage_command)
    if completed.returncode != 0 or not raw_out.is_file():
        return completed.returncode or 2

    raw = json.loads(raw_out.read_text(encoding="utf-8"))
    totals = _summary(raw)
    line_metric = _coverage_metric(totals, "lines")
    passed = float(line_metric["percent"]) >= args.threshold

    lcov_command = [
        "cargo",
        "llvm-cov",
        "report",
        "--manifest-path",
        "rust/Cargo.toml",
        "--lcov",
        "--output-path",
        str(lcov_out),
    ]
    lcov = _run(lcov_command)
    if lcov.returncode != 0:
        return lcov.returncode

    test_count = _test_count()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "schemaVersion": "rust-coverage.v1",
        "version": APP_VERSION,
        "commit": _git_commit(),
        "generatedAt": generated_at,
        "status": "PASS" if passed else "FAIL",
        "threshold": {"metric": "line", "minimumPercent": args.threshold},
        "workspaceCrates": list(WORKSPACE_CRATES),
        "coverage": {
            "lines": line_metric,
            "functions": _coverage_metric(totals, "functions"),
            "regions": _coverage_metric(totals, "regions"),
        },
        "rustTestCount": test_count,
        "coreCrateExclusions": [],
        "coverageOmit": [],
        "tool": _tool_version(),
        "commands": {
            "measure": coverage_command,
            "lcov": lcov_command,
            "testInventory": "cargo test --manifest-path rust/Cargo.toml --workspace --all-features -- --list --format terse",
        },
        "artifacts": {
            "summary": str(artifact_out.relative_to(ROOT)).replace("\\", "/"),
            "lcov": str(lcov_out.relative_to(ROOT)).replace("\\", "/"),
        },
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    artifact_out.write_text(rendered, encoding="utf-8")
    evidence_out.write_text(rendered, encoding="utf-8")
    raw_out.unlink(missing_ok=True)
    print(
        f"Rust line coverage: {float(line_metric['percent']):.2f}% "
        f"({line_metric['covered']}/{line_metric['count']}); {test_count} tests"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
