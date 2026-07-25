from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_version.py"
VERSION = "4.3.6"


def _write_repo(root: Path, version: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    config_dir = root / "deepseek_infra" / "core"
    config_dir.mkdir(parents=True)
    (config_dir / "config.py").write_text(f'app_version: str = "{version}"\n', encoding="utf-8")
    frontend_api = root / "frontend" / "src" / "api"
    frontend_api.mkdir(parents=True)
    (root / "frontend" / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "frontend" / "package-lock.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "frontend" / "index.html").write_text(
        f'<meta name="deepseek-infra-version" content="{version}" />\n', encoding="utf-8"
    )
    (root / "frontend" / "buildIdentity.ts").write_text(
        f'export const FRONTEND_BUILD_CONFIGURATION_VERSION = "{version}-test-tag-v1";\n', encoding="utf-8"
    )
    (frontend_api / "chatApi.ts").write_text(f'version: typeof value.version === "string" ? value.version : "{version}",\n', encoding="utf-8")
    android_dir = root / "android" / "app"
    android_dir.mkdir(parents=True)
    (android_dir / "build.gradle").write_text(f'versionName "{version}"\n', encoding="utf-8")
    (root / "Dockerfile").write_text(f'org.opencontainers.image.version="{version}"\n', encoding="utf-8")
    rust_dir = root / "rust"
    rust_dir.mkdir()
    (rust_dir / "Dockerfile").write_text(f'org.opencontainers.image.version="{version}"\n', encoding="utf-8")
    (root / "README.md").write_text(f"![版本](https://img.shields.io/badge/version-{version}-blue)\n", encoding="utf-8")
    (root / "README.en.md").write_text(f"![Version](https://img.shields.io/badge/version-{version}-blue)\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"## [{version}] - Test Release\n\nbody\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "IMPLEMENTATION_STATUS.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (docs / "FRONTEND_MODULES.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    (docs / "EVIDENCE_INDEX.md").write_text(f"Applicable version: v{version}.\n", encoding="utf-8")
    evals = root / "evals"
    evals.mkdir()
    (evals / "README.md").write_text(f"适用版本：v{version}。\n", encoding="utf-8")
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        '- name: Resolve release version\n  run: echo "RELEASE_VERSION=$(cat VERSION)" >> "$GITHUB_ENV"\n',
        encoding="utf-8",
    )


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )


def test_check_release_version_passes_against_the_real_repo() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert f"VERSION {VERSION}" in result.stdout


def test_fixture_tree_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo, VERSION)
    result = _run("--root", str(repo))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"all release surfaces match VERSION {VERSION}" in result.stdout


def test_single_surface_mismatch_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo, VERSION)
    (repo / "frontend" / "package.json").write_text(json.dumps({"version": "0.0.0"}), encoding="utf-8")
    result = _run("--root", str(repo))
    assert result.returncode == 1
    assert "FAIL: frontend package.json" in result.stdout
    assert "PASS: core config app_version" in result.stdout


def test_ci_hardcoded_version_literal_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo, VERSION)
    ci = repo / ".github" / "workflows" / "ci.yml"
    ci.write_text(ci.read_text(encoding="utf-8") + f"- run: python scripts/release.py --version {VERSION}\n", encoding="utf-8")
    result = _run("--root", str(repo))
    assert result.returncode == 1
    assert "FAIL: CI release-version derivation" in result.stdout


def test_strict_branch_detects_mismatch_via_env(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo, VERSION)
    result = _run("--root", str(repo), "--strict-branch", env={"GITHUB_HEAD_REF": "release-9.9.9-other"})
    assert result.returncode == 1
    assert "FAIL: release branch" in result.stdout
    assert "9.9.9" in result.stdout


def test_strict_branch_accepts_matching_and_plain_branches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo, VERSION)
    matching = _run("--root", str(repo), "--strict-branch", env={"GITHUB_HEAD_REF": f"codex/release-{VERSION}-durable-checkpoints"})
    assert matching.returncode == 0, matching.stdout + matching.stderr
    plain = _run("--root", str(repo), "--strict-branch", env={"GITHUB_HEAD_REF": "main"})
    assert plain.returncode == 0, plain.stdout + plain.stderr


def test_require_release_note(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo, VERSION)
    skipped = _run("--root", str(repo))
    assert skipped.returncode == 0, skipped.stdout + skipped.stderr
    assert "release note" not in skipped.stdout

    missing = _run("--root", str(repo), "--require-release-note")
    assert missing.returncode == 1
    assert "FAIL: release note" in missing.stdout

    notes = repo / "docs" / "releases"
    notes.mkdir()
    (notes / f"{VERSION}.md").write_text(f"# DeepSeek Infra {VERSION} — Test Release\n", encoding="utf-8")
    present = _run("--root", str(repo), "--require-release-note")
    assert present.returncode == 0, present.stdout + present.stderr
    assert "PASS: release note" in present.stdout
