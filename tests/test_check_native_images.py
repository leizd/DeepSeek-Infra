from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_native_images.py"

from scripts.check_native_images import (  # noqa: E402
    audit_compose_mounts,
    audit_go_dockerfile,
    audit_rust_dockerfile,
    run_all_audits,
)


def test_native_images_audit_passes_on_current_repo() -> None:
    report = run_all_audits(ROOT)
    assert report["passed"] is True
    assert report["audits_failed"] == 0
    assert report["audits_passed"] == 3


def test_native_images_audit_cli_strict() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--strict"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    assert "Result: PASS" in result.stdout


def test_native_images_audit_cli_json() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert len(payload["results"]) == 3


def test_audit_go_dockerfile_detects_missing_file(tmp_path: Path) -> None:
    result = audit_go_dockerfile(tmp_path / "Dockerfile")
    assert result.passed is False
    assert "not found" in result.details


def test_audit_go_dockerfile_detects_violations(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM golang:1.27.1-alpine AS builder\n"
        "RUN go build -o /bin/deepseekd ./cmd/deepseekd\n"  # missing CGO_ENABLED=0
        "FROM alpine:3.20\n"
        "RUN apk add python3\n"  # forbidden python
        "COPY --from=builder /bin/deepseekd /usr/local/bin/deepseekd\n",
        encoding="utf-8",
    )
    result = audit_go_dockerfile(dockerfile)
    assert result.passed is False
    assert any("CGO_ENABLED=0" in v for v in result.violations)
    assert any("python3" in v for v in result.violations)


def test_audit_rust_dockerfile_detects_missing_file(tmp_path: Path) -> None:
    result = audit_rust_dockerfile(tmp_path / "Dockerfile")
    assert result.passed is False
    assert "not found" in result.details


def test_audit_rust_dockerfile_detects_violations(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM debian:bookworm-slim AS runtime\n"
        "RUN apt-get update && apt-get install -y python3-pip\n"  # forbidden python
        "FROM runtime AS worker\n"
        "CMD ['deepseek-worker']\n"  # missing USER deepseek
        "FROM runtime AS gateway\n"
        "USER deepseek\n",
        encoding="utf-8",
    )
    result = audit_rust_dockerfile(dockerfile)
    assert result.passed is False
    assert any("python3-pip" in v for v in result.violations)
    assert any("USER deepseek" in v for v in result.violations)


def test_audit_compose_mounts_detects_python_mount(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  deepseekd:\n"
        "    volumes:\n"
        "      - .:/app\n"  # forbidden repo root mount
        "      - /home/user/.venv:/venv\n",  # forbidden venv mount
        encoding="utf-8",
    )
    result = audit_compose_mounts(compose)
    assert result.passed is False
    assert len(result.violations) >= 2
