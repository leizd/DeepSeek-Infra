#!/usr/bin/env python3
"""Verify that every release surface matches the canonical root ``VERSION`` file.

The root ``VERSION`` file is the single source of truth for the release
identity. This gate walks every surface that must carry that version —
Python config, frontend manifests, Android, Dockerfiles, badges, changelogs,
doc headers and the CI workflow - and fails (exit 1) on any mismatch.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
RELEASE_BRANCH_PATTERNS = (
    re.compile(r"release-(\d+\.\d+\.\d+)-"),
    re.compile(r"release/(\d+\.\d+\.\d+)"),
)
CI_RESOLVER_MARKER = "RELEASE_VERSION=$(cat VERSION)"
CI_HARDCODED_VERSION_ARG = re.compile(r"--version\s+\d+\.\d+\.\d+")
CI_VERSIONED_NAME_LITERAL = re.compile(r"-v\d+\.\d+\.\d+")
# evals/baselines/* are pinned historical inputs, not release-derived artifacts.
CI_VERSIONED_NAME_ALLOWLIST = re.compile(r"evals/baselines/")

CheckResult = tuple[bool, str]
CheckFunc = Callable[[Path, str], CheckResult]


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _contains(rel: str, needle: Callable[[str], str]) -> CheckFunc:
    def run(root: Path, version: str) -> CheckResult:
        text = _read(root, rel)
        if text is None:
            return False, f"{rel} is missing"
        want = needle(version)
        if want in text:
            return True, f"{rel} contains {ascii(want)}"
        return False, f"{rel} does not contain {ascii(want)}"

    return run


def _json_version(rel: str) -> CheckFunc:
    def run(root: Path, version: str) -> CheckResult:
        text = _read(root, rel)
        if text is None:
            return False, f"{rel} is missing"
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"{rel} is not valid JSON: {exc}"
        actual = data.get("version") if isinstance(data, dict) else None
        if actual == version:
            return True, f'{rel} "version" is {actual!r}'
        return False, f'{rel} "version" is {actual!r}, expected {version!r}'

    return run


def _regex(rel: str, pattern: Callable[[str], re.Pattern[str]], description: str) -> CheckFunc:
    def run(root: Path, version: str) -> CheckResult:
        text = _read(root, rel)
        if text is None:
            return False, f"{rel} is missing"
        compiled = pattern(version)
        match = compiled.search(text)
        if match:
            return True, f"{rel} matches {description} ({match.group(0)!r})"
        return False, f"{rel} has no {description} for {version}"

    return run


def _check_changelog(root: Path, version: str) -> CheckResult:
    text = _read(root, "CHANGELOG.md")
    if text is None:
        return False, "CHANGELOG.md is missing"
    for line in text.splitlines():
        if line.startswith("## ["):
            if line.startswith(f"## [{version}]"):
                return True, f"CHANGELOG.md first release header is {ascii(line)}"
            return False, f"CHANGELOG.md first release header is {ascii(line)}, expected '## [{version}]'"
    return False, "CHANGELOG.md has no release header"


def _check_ci(root: Path, version: str) -> CheckResult:
    del version  # the CI workflow must carry no version literal at all
    rel = ".github/workflows/ci.yml"
    text = _read(root, rel)
    if text is None:
        return False, f"{rel} is missing"
    problems: list[str] = []
    if CI_RESOLVER_MARKER not in text:
        problems.append(f"missing `{CI_RESOLVER_MARKER}` resolver step")
    for match in CI_HARDCODED_VERSION_ARG.finditer(text):
        problems.append(f"hardcoded `{match.group(0)}` argument")
    for line in text.splitlines():
        if CI_VERSIONED_NAME_ALLOWLIST.search(line):
            continue
        for match in CI_VERSIONED_NAME_LITERAL.finditer(line):
            problems.append(f"hardcoded `{match.group(0)}` evidence/artifact literal")
    if problems:
        return False, f"{rel}: " + "; ".join(problems)
    return True, f"{rel} derives the release version from VERSION"


def _check_release_note(root: Path, version: str) -> CheckResult:
    rel = f"docs/releases/{version}.md"
    text = _read(root, rel)
    if text is None:
        return False, f"{rel} is missing"
    for line in text.splitlines():
        if line.startswith("# "):
            if version in line:
                return True, f"{rel} H1 is {ascii(line)}"
            return False, f"{rel} H1 is {ascii(line)}, expected it to contain {version}"
    return False, f"{rel} has no H1"


def _current_branch(root: Path, env: dict[str, str]) -> str:
    branch = env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME")
    if branch:
        return branch
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip()


def check_release_branch(root: Path, version: str, env: dict[str, str] | None = None) -> CheckResult:
    branch = _current_branch(root, env if env is not None else dict(os.environ))
    if not branch:
        return True, "no branch detectable; skipping release-branch check"
    for pattern in RELEASE_BRANCH_PATTERNS:
        match = pattern.search(branch)
        if match:
            branch_version = match.group(1)
            if branch_version == version:
                return True, f"branch {ascii(branch)} matches VERSION {version}"
            return False, f"branch {ascii(branch)} targets {branch_version}, but VERSION is {version}"
    return True, f"branch {ascii(branch)} is not a release branch"


def build_checks(require_release_note: bool) -> list[tuple[str, CheckFunc]]:
    checks: list[tuple[str, CheckFunc]] = [
        ("core config app_version", _contains("deepseek_infra/core/config.py", lambda v: f'app_version: str = "{v}"')),
        ("frontend package.json", _json_version("frontend/package.json")),
        ("frontend package-lock.json", _json_version("frontend/package-lock.json")),
        ("android versionName", _contains("android/app/build.gradle", lambda v: f'versionName "{v}"')),
        ("Dockerfile OCI version", _contains("Dockerfile", lambda v: f'org.opencontainers.image.version="{v}"')),
        ("rust Dockerfile OCI version", _contains("rust/Dockerfile", lambda v: f'org.opencontainers.image.version="{v}"')),
        ("README.md badge", _contains("README.md", lambda v: f"version-{v}-blue")),
        ("README.en.md badge", _contains("README.en.md", lambda v: f"version-{v}-blue")),
        ("CHANGELOG first header", _check_changelog),
        ("IMPLEMENTATION_STATUS header", _contains("docs/IMPLEMENTATION_STATUS.md", lambda v: f"适用版本：v{v}。")),
        ("evals README header", _contains("evals/README.md", lambda v: f"适用版本：v{v}。")),
        ("FRONTEND_MODULES header", _contains("docs/FRONTEND_MODULES.md", lambda v: f"适用版本：v{v}。")),
        ("EVIDENCE_INDEX header", _contains("docs/EVIDENCE_INDEX.md", lambda v: f"Applicable version: v{v}.")),
        (
            "frontend index.html meta",
            _contains("frontend/index.html", lambda v: f'<meta name="deepseek-infra-version" content="{v}" />'),
        ),
        (
            "frontend build identity tag",
            _regex(
                "frontend/buildIdentity.ts",
                lambda v: re.compile(rf'FRONTEND_BUILD_CONFIGURATION_VERSION = "{re.escape(v)}-[^"]*"'),
                "config tag starting with the version",
            ),
        ),
        ("chatApi fallback version", _contains("frontend/src/api/chatApi.ts", lambda v: f'value.version : "{v}"')),
        ("CI release-version derivation", _check_ci),
    ]
    if require_release_note:
        checks.append(("release note", _check_release_note))
    return checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (defaults to the script's repo)")
    parser.add_argument(
        "--require-release-note",
        action="store_true",
        help="also require docs/releases/<VERSION>.md with a matching H1 (used at release time)",
    )
    parser.add_argument(
        "--strict-branch",
        action="store_true",
        help="fail when a release-* branch targets a version different from VERSION",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root: Path = args.root.resolve()
    version_file = root / "VERSION"
    if not version_file.is_file():
        print("FAIL: VERSION file is missing", flush=True)
        return 1
    version = version_file.read_text(encoding="utf-8").strip()
    if not SEMVER.match(version):
        print(f"FAIL: VERSION contains {version!r}, expected a semantic version", flush=True)
        return 1
    print(f"Canonical release version: {version} (root: {root})", flush=True)

    if args.strict_branch:
        ok, detail = check_release_branch(root, version)
        print(f"{'PASS' if ok else 'FAIL'}: release branch - {detail}", flush=True)
        if not ok:
            return 1

    failures = 0
    for label, check in build_checks(args.require_release_note):
        ok, detail = check(root, version)
        print(f"{'PASS' if ok else 'FAIL'}: {label} - {detail}", flush=True)
        if not ok:
            failures += 1
    if failures:
        print(f"FAIL: {failures} release surface(s) do not match VERSION {version}", flush=True)
        return 1
    print(f"PASS: all release surfaces match VERSION {version}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
