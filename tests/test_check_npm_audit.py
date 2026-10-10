from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_npm_audit.py"

RSC_ADVISORY_URL = "https://github.com/advisories/GHSA-qwww-vcr4-c8h2"


def _run(report_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(report_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write(tmp_path: Path, report: dict) -> Path:
    path = tmp_path / "npm-audit.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _advisory_via(url: str, name: str = "react-router") -> dict:
    return {"title": "advisory", "url": url, "name": name, "severity": "high", "range": ">=7.12.0 <8.3.0"}


def test_empty_report_passes(tmp_path: Path) -> None:
    result = _run(_write(tmp_path, {"vulnerabilities": {}}))
    assert result.returncode == 0, result.stdout
    assert "PASS" in result.stdout


def test_documented_rsc_advisory_exception_passes(tmp_path: Path) -> None:
    report = {
        "vulnerabilities": {
            "react-router": {
                "severity": "high",
                "via": [_advisory_via(RSC_ADVISORY_URL)],
                "effects": ["react-router-dom"],
            },
            "react-router-dom": {
                "severity": "high",
                "via": ["react-router"],
                "effects": [],
            },
        }
    }
    result = _run(_write(tmp_path, report))
    assert result.returncode == 0, result.stdout
    assert "GHSA-qwww-vcr4-c8h2" in result.stdout
    assert "documented exception" in result.stdout


def test_unknown_high_advisory_blocks(tmp_path: Path) -> None:
    report = {
        "vulnerabilities": {
            "react-router": {
                "severity": "high",
                "via": [_advisory_via(RSC_ADVISORY_URL)],
            },
            "left-pad": {
                "severity": "high",
                "via": [_advisory_via("https://github.com/advisories/GHSA-aaaa-bbbb-cccc", "left-pad")],
            },
        }
    }
    result = _run(_write(tmp_path, report))
    assert result.returncode == 1
    assert "left-pad" in result.stdout
    assert "FAIL" in result.stdout


def test_transitive_chain_to_unknown_advisory_blocks(tmp_path: Path) -> None:
    report = {
        "vulnerabilities": {
            "inner": {
                "severity": "high",
                "via": [_advisory_via("https://github.com/advisories/GHSA-dddd-eeee-ffff", "inner")],
            },
            "outer": {"severity": "high", "via": ["inner"]},
        }
    }
    result = _run(_write(tmp_path, report))
    assert result.returncode == 1
    assert "GHSA-dddd-eeee-ffff" in result.stdout


def test_moderate_severity_is_not_gated(tmp_path: Path) -> None:
    report = {
        "vulnerabilities": {
            "pkg": {
                "severity": "moderate",
                "via": [_advisory_via("https://github.com/advisories/GHSA-1111-2222-3333", "pkg")],
            }
        }
    }
    result = _run(_write(tmp_path, report))
    assert result.returncode == 0, result.stdout


def test_missing_report_fails(tmp_path: Path) -> None:
    result = _run(tmp_path / "does-not-exist.json")
    assert result.returncode == 1
    assert "cannot read" in result.stdout
