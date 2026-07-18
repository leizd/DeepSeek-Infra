from __future__ import annotations

import json
import struct
import unittest
from pathlib import Path
from typing import Any

from scripts import preflight_release


ROOT = Path(__file__).resolve().parents[1]
VERSION = "4.0.7"


def read_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def read_json(rel: str) -> dict[str, Any]:
    path = ROOT / rel
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"{rel} starts with a UTF-8 BOM")
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"{rel} must contain a JSON object")
    return parsed


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError(f"{path} is not a PNG file")
    return struct.unpack(">II", data[16:24])


class EncodingRegressionTests(unittest.TestCase):
    def test_python_sources_do_not_start_with_utf8_bom(self) -> None:
        python_files = (
            [ROOT / "app.py"]
            + list((ROOT / "deepseek_infra").rglob("*.py"))
            + list((ROOT / "scripts").rglob("*.py"))
            + list((ROOT / "tests").rglob("*.py"))
        )
        offenders = [str(path.relative_to(ROOT)) for path in python_files if path.read_bytes().startswith(b"\xef\xbb\xbf")]
        self.assertEqual([], offenders)

    def test_release_version_sync_current(self) -> None:
        readme = read_text("README.md")
        config = read_text("deepseek_infra/core/config.py")
        dockerfile = read_text("Dockerfile")
        build_gradle = read_text("android/app/build.gradle")
        changelog = read_text("CHANGELOG.md")
        ci = read_text(".github/workflows/ci.yml")
        frontend = read_text("static/index.html")

        self.assertIn("version-4.0.7-blue", readme)
        self.assertIn('app_version: str = "4.0.7"', config)
        self.assertIn("deepseek-infra:4.0.7", dockerfile)
        self.assertIn('org.opencontainers.image.version="4.0.7"', dockerfile)
        self.assertIn('versionName "4.0.7"', build_gradle)
        self.assertIn("versionCode 400010", build_gradle)
        self.assertIn('<meta name="deepseek-infra-version" content="4.0.7" />', frontend)
        self.assertIn("## [4.0.7] - React Default Entry", changelog)
        self.assertIn("Personal AI Runtime GA", readme)
        self.assertIn("python scripts/smoke_ga.py --offline --out docs/evidence/ga-v4.0.7.json", ci)
        self.assertIn("python scripts/preflight_release.py --version 4.0.7 --ga", ci)

    def test_release_docs_are_registered(self) -> None:
        readme = read_text("README.md")
        evidence_index = read_text("docs/EVIDENCE_INDEX.md")
        manifest = read_text("deepseek_infra/infra/diagnostics/release_manifest.py")

        for rel in (
            "docs/GETTING_STARTED.md",
            "docs/WORKSPACE.md",
            "docs/MEMORY.md",
            "docs/SKILLS.md",
            "docs/MEDIA.md",
            "docs/BROWSER_CONTROL.md",
            "docs/AUTOMATION.md",
            "docs/EXPORTS.md",
            "docs/SECURITY.md",
            "docs/DEPLOYMENT.md",
            "docs/DEMO_3_0.md",
            "docs/EVIDENCE_INDEX.md",
            "docs/releases/4.0.7.md",
        ):
            self.assertTrue((ROOT / rel).is_file(), rel)
            self.assertIn(rel, readme)

        self.assertIn("docs/evidence/ga-v4.0.7.json", evidence_index)
        self.assertIn("docs/evidence/ga-v{APP_VERSION}.json", manifest)
        self.assertIn("gaEvidence", manifest)

    def test_release_doc_headers_are_readable(self) -> None:
        header = "\u9002\u7528\u7248\u672c\uff1av4.0.7\u3002"
        for rel in ("docs/IMPLEMENTATION_STATUS.md", "evals/README.md"):
            self.assertIn(header, read_text(rel))

    def test_required_evidence_json_is_current_and_passes(self) -> None:
        required = (
            "docs/evidence/ga-v4.0.7.json",
            "docs/evidence/workspace-v4.0.7.json",
            "docs/evidence/edge-router-v4.0.7.json",
            "docs/evidence/media-v4.0.7.json",
            "docs/evidence/browser-v4.0.7.json",
            "docs/evidence/frontend-browser-v4.0.7.json",
            "docs/evidence/automation-v4.0.7.json",
            "docs/evidence/skills-v4.0.7.json",
            "docs/evidence/skills-ui-v4.0.7.json",
            "docs/evidence/skill-builder-v4.0.7.json",
            "docs/evidence/skill-packs-v4.0.7.json",
            "docs/evidence/skill-eval-dashboard-v4.0.7.json",
            "docs/evidence/skill-versioning-v4.0.7.json",
            "docs/evidence/skill-analytics-v4.0.7.json",
            "docs/evidence/skill-security-v4.0.7.json",
            "docs/evidence/skill-catalog-v4.0.7.json",
            "docs/evidence/context-taint-v4.0.7.json",
            "docs/evidence/semantic-cache-onnx-v4.0.7.json",
            "docs/evidence/rust-sidecar-performance-v4.0.7.json",
            "docs/evidence/rag-vector-binary-parity-v4.0.7.json",
            "evals/reports/latest.json",
            "evals/reports/agent-latest.json",
            "evals/reports/baseline-compare-latest.json",
            "evals/reports/security-latest.json",
            "evals/reports/skills-v4.0.7.json",
            "evals/reports/media-v4.0.7.json",
            "evals/reports/browser-v4.0.7.json",
            "evals/reports/automation-v4.0.7.json",
        )
        for rel in required:
            data = read_json(rel)
            self.assertEqual(VERSION, str(data.get("version")), rel)
            self.assertEqual("PASS", data.get("status"), rel)

    def test_ga_evidence_shape(self) -> None:
        data = read_json("docs/evidence/ga-v4.0.7.json")
        raw_checks = data.get("checks")
        if not isinstance(raw_checks, dict):
            self.fail("GA evidence checks must be an object")
        checks: dict[str, Any] = raw_checks
        for name in (
            "workspaceHome",
            "project",
            "memory",
            "skill",
            "media",
            "browserSnapshot",
            "savedItem",
            "artifact",
            "automation",
            "export",
            "provenance",
            "exportRedaction",
        ):
            self.assertEqual("PASS", checks.get(name), name)

    def test_ga_demo_assets_are_pngs(self) -> None:
        for rel in (
            "docs/assets/3.0-workspace-overview.png",
            "docs/assets/3.0-skill-run.png",
            "docs/assets/3.0-automation-run.png",
            "docs/assets/3.0-project-export.png",
        ):
            width, height = png_dimensions(ROOT / rel)
            self.assertGreater(width, 1, rel)
            self.assertGreater(height, 1, rel)

    def test_release_facing_docs_pass_encoding_gate(self) -> None:
        result = preflight_release.check_docs_encoding_sanity(ROOT)
        self.assertEqual("pass", result.status, result.detail)


if __name__ == "__main__":
    unittest.main()
