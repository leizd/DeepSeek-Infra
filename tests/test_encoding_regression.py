from __future__ import annotations

import json
import re
import struct
import unittest
from pathlib import Path
from typing import Any

from scripts import preflight_release


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


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

    def test_tests_do_not_embed_machine_specific_temp_paths(self) -> None:
        machine_temp = re.compile(
            r"[A-Za-z]:[\\/]Users[\\/][^\\/]+[\\/]AppData[\\/]Local[\\/]Temp(?:[\\/]|$)",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for path in (ROOT / "tests").rglob("*.py"):
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if machine_temp.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{line_number}")
        self.assertEqual([], offenders)

    def test_tests_derive_the_current_release_version(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in (ROOT / "tests").rglob("*.py")
            if VERSION in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], offenders)

    def test_release_version_sync_current(self) -> None:
        readme = read_text("README.md")
        config = read_text("deepseek_infra/core/config.py")
        dockerfile = read_text("Dockerfile")
        build_gradle = read_text("android/app/build.gradle")
        changelog = read_text("CHANGELOG.md")
        ci = read_text(".github/workflows/ci.yml")
        frontend = read_text("frontend/index.html")

        self.assertIn(f"version-{VERSION}-blue", readme)
        self.assertIn(f'app_version: str = "{VERSION}"', config)
        self.assertIn(f"deepseek-infra:{VERSION}", dockerfile)
        self.assertIn(f'org.opencontainers.image.version="{VERSION}"', dockerfile)
        self.assertIn(f'versionName "{VERSION}"', build_gradle)
        self.assertIn("versionCode 400051", build_gradle)
        self.assertIn(f'<meta name="deepseek-infra-version" content="{VERSION}" />', frontend)
        self.assertIn(f"## [{VERSION}]", changelog)
        self.assertIn("Personal AI Runtime GA", readme)
        self.assertIn("python scripts/generate_release_evidence.py --version $RELEASE_VERSION", ci)
        self.assertIn("evidence-context:", ci)
        self.assertIn("evidence-assembly:", ci)
        self.assertIn("release-package:", ci)
        self.assertIn("RELEASE_VERSION=$(cat VERSION)", ci)
        self.assertIn("name: release-evidence-v${{ env.RELEASE_VERSION }}", ci)
        self.assertNotIn("release-evidence-v4.3.7", ci)
        self.assertNotIn("scripts/release.py --version $RELEASE_VERSION --dry-run", ci)

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
            "docs/releases/4.3.6.md",
            "docs/releases/4.3.7.md",
            "docs/releases/4.4.0.md",
            "docs/releases/4.4.1.md",
            "docs/releases/4.4.2.md",
            "docs/releases/4.4.3.md",
            "docs/releases/4.4.4.md",
            "docs/releases/4.4.5.md",
            "docs/releases/4.4.6.md",
            "docs/releases/4.4.7.md",
            f"docs/releases/{VERSION}.md",
        ):
            self.assertTrue((ROOT / rel).is_file(), rel)
            self.assertIn(rel, readme)

        self.assertIn(f"docs/evidence/ga-v{VERSION}.json", evidence_index)
        self.assertIn("evidence_paths(APP_VERSION)", manifest)
        self.assertIn("gaEvidence", manifest)

    def test_release_doc_headers_are_readable(self) -> None:
        header = f"适用版本：v{VERSION}。"
        for rel in ("docs/IMPLEMENTATION_STATUS.md", "evals/README.md"):
            self.assertIn(header, read_text(rel))

    def test_required_evidence_json_is_current_and_passes(self) -> None:
        from deepseek_infra.infra.diagnostics.evidence_manifest import required_evidence_paths

        index = read_text("docs/EVIDENCE_INDEX.md")
        required = required_evidence_paths(VERSION)
        self.assertIn(f"docs/evidence/ga-v{VERSION}.json", required)
        self.assertIn(f"docs/evidence/frontend-browser-v{VERSION}.json", required)
        for rel in required:
            self.assertIn(rel, index, rel)

    def test_ga_evidence_shape(self) -> None:
        data = read_json("docs/evidence/ga-v4.2.6.json")
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
