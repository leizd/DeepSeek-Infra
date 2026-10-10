#!/usr/bin/env python3
"""Generate the release-readiness producer's Evidence without rewriting provenance."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_inventory import evidence_paths_for_producer  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import (  # noqa: E402
    EVIDENCE_SOURCE_CONTEXT_ENV,
    capture_source_context,
    load_source_context,
    validate_source_context,
)

GENERATOR = "scripts/generate_release_evidence.py"
PRODUCER = "release-readiness"


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
    temporary = root / "artifacts" / "release-readiness"
    return [
        [python, str(root / "scripts" / "smoke_ga.py"), "--offline", "--out", str(evidence / f"ga-v{version}.json")],
        [python, str(root / "scripts" / "smoke_workspace.py"), "--offline", "--out", str(evidence / f"workspace-v{version}.json")],
        [python, str(root / "scripts" / "smoke_edge_router.py"), "--offline", "--out", str(evidence / f"edge-router-v{version}.json")],
        [python, str(root / "scripts" / "smoke_media.py"), "--offline", "--out", str(evidence / f"media-v{version}.json")],
        [python, str(root / "scripts" / "smoke_browser.py"), "--offline", "--out", str(evidence / f"browser-v{version}.json"), "--version", version],
        [python, str(root / "scripts" / "smoke_automation.py"), "--offline", "--out", str(evidence / f"automation-v{version}.json"), "--version", version],
        [python, str(root / "scripts" / "smoke_skills.py"), "--offline", "--out", str(evidence / f"skills-v{version}.json")],
        [python, str(root / "scripts" / "smoke_skills_ui.py"), "--offline", "--out", str(evidence / f"skills-ui-v{version}.json")],
        [python, str(root / "scripts" / "smoke_skill_builder.py"), "--offline", "--out", str(evidence / f"skill-builder-v{version}.json")],
        [python, str(root / "scripts" / "smoke_skill_packs.py"), "--offline", "--out", str(evidence / f"skill-packs-v{version}.json")],
        [
            python,
            str(root / "scripts" / "smoke_skill_eval_dashboard.py"),
            "--offline",
            "--out",
            str(evidence / f"skill-eval-dashboard-v{version}.json"),
            "--report-out",
            str(temporary / "skill-eval.json"),
        ],
        [python, str(root / "scripts" / "smoke_skill_versioning.py"), "--offline", "--out", str(evidence / f"skill-versioning-v{version}.json")],
        [python, str(root / "scripts" / "smoke_skill_analytics.py"), "--offline", "--out", str(evidence / f"skill-analytics-v{version}.json")],
        [python, str(root / "scripts" / "smoke_skill_security.py"), "--offline", "--out", str(evidence / f"skill-security-v{version}.json")],
        [python, str(root / "scripts" / "smoke_skill_catalog.py"), "--offline", "--out", str(evidence / f"skill-catalog-v{version}.json")],
        [python, str(root / "scripts" / "smoke_context_taint.py"), "--offline", "--out", str(evidence / f"context-taint-v{version}.json")],
        [
            python,
            str(root / "benchmarks" / "bench_semantic_cache.py"),
            "--compare",
            "--out",
            str(evidence / f"semantic-cache-onnx-v{version}.json"),
        ],
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


def validate_generated_evidence(root: Path, version: str, context: dict[str, object]) -> None:
    for rel in evidence_paths_for_producer(PRODUCER, version):
        path = root / rel
        if not path.is_file():
            raise ValueError(f"producer did not create required Evidence: {rel}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"producer Evidence is not a JSON object: {rel}")
        failures = []
        if data.get("version") != version:
            failures.append("version")
        if data.get("status") != "PASS":
            failures.append("status")
        if data.get("testedRevision") != context["testedRevision"]:
            failures.append("testedRevision")
        if data.get("sourceRevision") != context["testedRevision"]:
            failures.append("sourceRevision")
        if data.get("sourceTreeDirty") is not False:
            failures.append("sourceTreeDirty")
        if data.get("sourceContext") != context:
            failures.append("sourceContext")
        if not data.get("generatedAt"):
            failures.append("generatedAt")
        if failures:
            raise ValueError(f"producer wrote invalid Evidence {rel}: {', '.join(failures)}")


def generate_release_evidence(root: Path, version: str, context_path: Path | None = None) -> tuple[str, ...]:
    root = root.resolve()
    validate_release_version(root, version)
    if context_path is None:
        inherited = os.environ.get(EVIDENCE_SOURCE_CONTEXT_ENV, "").strip()
        context_path = Path(inherited).resolve() if inherited else None
    if context_path is None:
        context = capture_source_context(root, version, generator=GENERATOR, schema_version=2)
        context_path = root / "docs" / "evidence" / f"evidence-source-context-v{version}.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        loaded_context = load_source_context(context_path)
        if loaded_context is None:
            raise ValueError("Evidence source context is required")
        context = loaded_context
        errors = validate_source_context(context, version=version)
        if errors:
            raise ValueError("invalid Evidence source context: " + "; ".join(errors))

    owned_paths = evidence_paths_for_producer(PRODUCER, version)
    for rel in owned_paths:
        path = root / rel
        if path.is_file():
            path.unlink()
    run_commands(evidence_commands(root, version), root=root, context_path=context_path)
    validate_generated_evidence(root, version, context)
    return owned_paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--source-context", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        paths = generate_release_evidence(args.root, args.version, args.source_context)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"release evidence generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"producer": PRODUCER, "status": "PASS", "paths": list(paths)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
