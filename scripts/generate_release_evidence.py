#!/usr/bin/env python3
"""Generate release evidence from one clean, immutable source snapshot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_manifest import (  # noqa: E402
    build_evidence_manifest,
    required_evidence_paths,
    write_evidence_manifest,
)
from deepseek_infra.infra.diagnostics.evidence_revision import (  # noqa: E402
    EVIDENCE_SOURCE_CONTEXT_ENV,
    capture_source_context,
    revision_from_context,
)


GENERATOR = "scripts/generate_release_evidence.py"


def _read_frontend_version(root: Path) -> str:
    data = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    return str(data.get("version") or "")


def validate_release_version(root: Path, version: str) -> None:
    if version != APP_VERSION:
        raise ValueError(f"requested version {version!r} does not match backend version {APP_VERSION!r}")
    frontend_version = _read_frontend_version(root)
    if frontend_version != version:
        raise ValueError(f"frontend version {frontend_version!r} does not match requested version {version!r}")


def evidence_commands(root: Path, version: str) -> list[list[str]]:
    python = sys.executable
    evidence = root / "docs" / "evidence"
    return [
        [python, str(root / "scripts" / "smoke_release.py"), "--offline"],
        [python, str(root / "scripts" / "smoke_ga.py"), "--offline", "--out", str(evidence / f"ga-v{version}.json")],
        [python, str(root / "scripts" / "smoke_frontend_browser.py"), "--out", str(evidence / f"frontend-browser-v{version}.json")],
        [python, str(root / "scripts" / "check_frontend_bundle.py"), "--out", str(evidence / f"frontend-bundle-v{version}.json")],
        [python, str(root / "scripts" / "smoke_context_taint.py"), "--offline", "--out", str(evidence / f"context-taint-v{version}.json")],
        [python, str(root / "scripts" / "smoke_mcp_headless_bridge.py"), "--out", str(evidence / "headless-mcp-bridge.json")],
        [python, str(root / "scripts" / "smoke_a2a_external_peer.py"), "--out", str(evidence / "a2a-external-peer.json")],
        [python, str(root / "scripts" / "generate_4_0_contract_evidence.py"), "--kind", "upgrade", "--out", str(evidence / f"upgrade-rollback-v{version}.json")],
        [python, str(root / "scripts" / "generate_4_0_contract_evidence.py"), "--kind", "protocol", "--out", str(evidence / f"protocol-contract-v{version}.json")],
    ]


def run_commands(commands: Sequence[Sequence[str]], *, root: Path, context_path: Path) -> None:
    env = dict(os.environ)
    env[EVIDENCE_SOURCE_CONTEXT_ENV] = str(context_path)
    for command in commands:
        completed = subprocess.run(list(command), cwd=root, env=env, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"evidence command failed ({completed.returncode}): {' '.join(command)}")


def stamp_generated_evidence(root: Path, context: dict[str, Any], artifact_paths: Sequence[str]) -> None:
    revision = revision_from_context(context)
    for rel in artifact_paths:
        path = root / rel
        if not path.is_file():
            raise ValueError(f"required evidence was not generated: {rel}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"required evidence must contain a JSON object: {rel}")
        if data.get("version") != context["version"]:
            raise ValueError(f"evidence version mismatch for {rel}: {data.get('version')!r}")
        data.update(revision)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_release_evidence(root: Path, version: str) -> Path:
    root = root.resolve()
    validate_release_version(root, version)
    context = capture_source_context(root, version, generator=GENERATOR)
    context_path = root / "docs" / "evidence" / f"evidence-source-context-v{version}.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    run_commands(evidence_commands(root, version), root=root, context_path=context_path)
    artifacts = required_evidence_paths(version)
    stamp_generated_evidence(root, context, artifacts)
    manifest = build_evidence_manifest(
        root,
        version=version,
        tested_revision=str(context["testedRevision"]),
        artifact_paths=artifacts,
        source_context=context,
    )
    manifest_path = root / "docs" / "evidence" / f"evidence-manifest-v{version}.json"
    write_evidence_manifest(manifest_path, manifest)

    env = dict(os.environ)
    env[EVIDENCE_SOURCE_CONTEXT_ENV] = str(context_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "preflight_release.py"),
            "--root",
            str(root),
            "--version",
            version,
            "--ga",
            "--provenance-strict",
            "--expected-revision",
            str(context["testedRevision"]),
        ],
        cwd=root,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("strict provenance preflight failed")
    return manifest_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--version", default=APP_VERSION)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest_path = generate_release_evidence(args.root, args.version)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"release evidence generation failed: {exc}", file=sys.stderr)
        return 1
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
