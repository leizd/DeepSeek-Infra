from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_zero_python_runtime.py"

from scripts.check_zero_python_runtime import (  # noqa: E402
    check_compose_topology,
    check_ownership_matrix,
    run_all_checks,
)


def test_gate_passes_on_current_repository() -> None:
    report = run_all_checks(ROOT)
    assert report["status"] == "PASS"
    assert report["passed"] is True
    assert report["checks_failed"] == 0
    assert report["checks_passed"] == 6


def test_gate_cli_invocation_strict() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--strict"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    assert "Overall Verdict: PASS" in result.stdout


def test_gate_cli_invocation_json() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["passed"] is True
    assert len(payload["results"]) == 6


def test_gate_detects_forbidden_python_in_compose(tmp_path: Path) -> None:
    # Set up dummy repo with Python service in compose
    compose = tmp_path / "docker-compose.native.yml"
    compose.write_text(
        "services:\n"
        "  deepseek-edge:\n"
        "    image: edge\n"
        "  deepseekd:\n"
        "    image: go\n"
        "  deepseek-worker:\n"
        "    image: worker\n"
        "  python-backend:\n"
        "    image: python:3.11\n",
        encoding="utf-8",
    )
    result = check_compose_topology(tmp_path)
    assert result.passed is False
    assert "Forbidden 'python' reference found" in result.details


def test_gate_detects_python_target_owner_in_ownership_matrix(tmp_path: Path) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True)
    matrix = release_dir / "native_runtime_ownership_v1.json"
    matrix.write_text(
        json.dumps({
            "domains": [
                {
                    "id": "bad_domain",
                    "plane": "control",
                    "target_owner": "python",
                    "production": True,
                }
            ]
        }),
        encoding="utf-8",
    )
    result = check_ownership_matrix(tmp_path)
    assert result.passed is False
    assert "Forbidden Python target owner for production domains" in result.details
