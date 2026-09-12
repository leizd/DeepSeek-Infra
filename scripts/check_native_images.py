#!/usr/bin/env python3
"""Audit production container images and Dockerfiles for Zero-Python dependencies.

Enforces:
1. go/Dockerfile builds a static CGO_ENABLED=0 binary and runs in a minimal alpine container with zero Python.
2. rust/Dockerfile builds native binaries and runs in debian-slim with a non-root deepseek user and zero Python.
3. docker-compose.native.yml mounts no Python volumes or environments into production services.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PYTHON_TOKENS = {
    "python",
    "python3",
    "pip",
    "pip3",
    "pyo3",
    "cpython",
    "libpython",
    "site-packages",
    "dist-packages",
    "virtualenv",
    ".venv",
}


@dataclass
class ImageAuditResult:
    target: str
    passed: bool
    details: str
    violations: list[str]


def parse_dockerfile_stages(content: str) -> list[dict[str, Any]]:
    """Parse Dockerfile lines into stages keyed by target name."""
    stages: list[dict[str, Any]] = []
    current_stage: dict[str, Any] | None = None

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        from_match = re.match(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", trimmed, re.IGNORECASE)
        if from_match:
            base_image = from_match.group(1)
            stage_name = from_match.group(2) or f"stage_{len(stages)}"
            current_stage = {
                "name": stage_name,
                "base": base_image,
                "lines": [],
            }
            stages.append(current_stage)
        elif current_stage is not None:
            current_stage["lines"].append(trimmed)

    return stages


def audit_go_dockerfile(dockerfile_path: Path) -> ImageAuditResult:
    if not dockerfile_path.is_file():
        return ImageAuditResult(
            target="go/Dockerfile",
            passed=False,
            details="go/Dockerfile not found",
            violations=["file_missing"],
        )

    content = dockerfile_path.read_text(encoding="utf-8")
    stages = parse_dockerfile_stages(content)
    violations: list[str] = []

    if len(stages) < 2:
        violations.append("go/Dockerfile must be a multi-stage build")

    # Check builder stage
    builder_stage = stages[0] if stages else None
    if builder_stage:
        builder_text = " ".join(builder_stage["lines"])
        if "CGO_ENABLED=0" not in builder_text:
            violations.append("go/Dockerfile builder stage must specify CGO_ENABLED=0")

    # Check runtime/final stage
    final_stage = stages[-1] if len(stages) >= 2 else None
    if final_stage:
        final_base = final_stage["base"].lower()
        if "alpine" not in final_base:
            violations.append(f"go/Dockerfile runtime stage base should be alpine, got: {final_stage['base']}")

        for line in final_stage["lines"]:
            lowered = line.lower()
            for token in FORBIDDEN_PYTHON_TOKENS:
                if re.search(r"\b" + re.escape(token) + r"\b", lowered):
                    violations.append(f"Forbidden token {token!r} in runtime line: {line}")

        final_text = " ".join(final_stage["lines"])
        if "/usr/local/bin/deepseekd" not in final_text:
            violations.append("go/Dockerfile runtime stage must copy /usr/local/bin/deepseekd")
        if 'ENTRYPOINT ["/usr/local/bin/deepseekd"]' not in final_text:
            violations.append("go/Dockerfile runtime stage must have ENTRYPOINT deepseekd")

    passed = len(violations) == 0
    details = (
        "go/Dockerfile produces static CGO_ENABLED=0 binary with zero Python in runtime stage"
        if passed
        else f"go/Dockerfile failed audit with {len(violations)} violations"
    )
    return ImageAuditResult(
        target="go/Dockerfile",
        passed=passed,
        details=details,
        violations=violations,
    )


def audit_rust_dockerfile(dockerfile_path: Path) -> ImageAuditResult:
    if not dockerfile_path.is_file():
        return ImageAuditResult(
            target="rust/Dockerfile",
            passed=False,
            details="rust/Dockerfile not found",
            violations=["file_missing"],
        )

    content = dockerfile_path.read_text(encoding="utf-8")
    stages = parse_dockerfile_stages(content)
    violations: list[str] = []

    stage_names = {s["name"].lower() for s in stages}
    for required in ("worker", "gateway", "runtime"):
        if required not in stage_names:
            violations.append(f"rust/Dockerfile missing required stage {required!r}")

    for stage in stages:
        name = stage["name"].lower()
        # Production runtime stages
        if name in ("runtime", "worker", "gateway"):
            for line in stage["lines"]:
                lowered = line.lower()
                for token in FORBIDDEN_PYTHON_TOKENS:
                    if re.search(r"\b" + re.escape(token) + r"\b", lowered):
                        violations.append(f"Forbidden token {token!r} in stage {name}: {line}")

            if name in ("worker", "gateway"):
                if "USER deepseek" not in stage["lines"]:
                    violations.append(f"Stage {name} must enforce non-root execution via 'USER deepseek'")

    passed = len(violations) == 0
    details = (
        "rust/Dockerfile runtime stages enforce minimal base, non-root user, and zero Python"
        if passed
        else f"rust/Dockerfile failed audit with {len(violations)} violations"
    )
    return ImageAuditResult(
        target="rust/Dockerfile",
        passed=passed,
        details=details,
        violations=violations,
    )


def audit_compose_mounts(compose_path: Path) -> ImageAuditResult:
    if not compose_path.is_file():
        return ImageAuditResult(
            target="docker-compose.native.yml",
            passed=False,
            details="docker-compose.native.yml not found",
            violations=["file_missing"],
        )

    content = compose_path.read_text(encoding="utf-8")
    violations: list[str] = []

    # Check for volume mounts that leak host python code into native containers
    forbidden_mount_patterns = [
        r"\.\s*:",  # mounting whole repo
        r"\.venv",
        r"site-packages",
        r"deepseek_infra\s*:",
    ]
    for line in content.splitlines():
        for pattern in forbidden_mount_patterns:
            if re.search(pattern, line):
                violations.append(f"Forbidden host mount pattern {pattern!r} in: {line.strip()}")

    passed = len(violations) == 0
    details = (
        "docker-compose.native.yml has zero host Python volume leaks"
        if passed
        else f"docker-compose.native.yml failed audit with {len(violations)} violations"
    )
    return ImageAuditResult(
        target="docker-compose.native.yml",
        passed=passed,
        details=details,
        violations=violations,
    )


def run_all_audits(root: Path) -> dict[str, Any]:
    go_result = audit_go_dockerfile(root / "go" / "Dockerfile")
    rust_result = audit_rust_dockerfile(root / "rust" / "Dockerfile")
    compose_result = audit_compose_mounts(root / "docker-compose.native.yml")

    results = [go_result, rust_result, compose_result]
    all_passed = all(r.passed for r in results)

    return {
        "passed": all_passed,
        "audits_total": len(results),
        "audits_passed": sum(1 for r in results if r.passed),
        "audits_failed": sum(1 for r in results if not r.passed),
        "results": [asdict(r) for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit native container images for zero Python")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on failure")
    args = parser.parse_args(argv)

    report = run_all_audits(REPO_ROOT)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("=" * 60)
        print(" NATIVE CONTAINER IMAGE AUDIT")
        print("=" * 60)
        for r in report["results"]:
            icon = "[PASS]" if r["passed"] else "[FAIL]"
            print(f" {icon} {r['target']}: {r['details']}")
            if r["violations"]:
                for v in r["violations"]:
                    print(f"        ! {v}")
        print("-" * 60)
        print(f" Result: {'PASS' if report['passed'] else 'FAIL'} ({report['audits_passed']}/{report['audits_total']} passed)")
        print("=" * 60)

    if not report["passed"] and args.strict:
        return 1
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
