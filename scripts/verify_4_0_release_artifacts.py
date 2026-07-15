#!/usr/bin/env python3
"""Verify the 4.0.0-rc.2 ZIP, checksum, manifest, provenance, and privacy contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402

BANNED_PARTS = {
    ".file-cache",
    ".projects",
    ".local-rag",
    ".traces",
    ".semantic-cache",
    ".request-queue",
    ".generated",
    ".tool-audit",
    ".scheduler",
    ".a2a",
    ".budget",
    ".memory",
    ".reminders",
    ".agent-runs",
    ".media",
    ".automation",
    ".skills",
    "artifacts",
    "target",
}
BANNED_NAMES = {".env", ".auth-token", "signing.properties", "keystore.properties"}
BANNED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".jks", ".keystore"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def forbidden_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(
        set(path.parts) & BANNED_PARTS
        or path.name in BANNED_NAMES
        or path.suffix.lower() in BANNED_SUFFIXES
        or path.name.startswith(".server")
        or path.name.endswith(".log")
    )


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", dest="archive", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    archive = args.archive.resolve()
    checksum = args.checksum.resolve()
    manifest_path = args.manifest.resolve()
    digest = sha256(archive)
    checksum_value = checksum.read_text(encoding="utf-8").split()[0]
    manifest = _json(manifest_path)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    errors: list[str] = []
    if checksum_value != digest:
        errors.append("checksum does not match ZIP")
    if manifest.get("version") != APP_VERSION:
        errors.append("manifest version is not rc.2")
    if manifest.get("commit") != commit:
        errors.append("manifest commit is not the exact validation commit")
    if manifest.get("sha256") != digest or manifest.get("archiveSha256") != digest:
        errors.append("manifest archive digest does not match ZIP")
    defaults = manifest.get("runtimeDefaults")
    if not isinstance(defaults, dict) or defaults.get("authoritativeRuntime") != "python" or defaults.get("defaultCompose") != "python-only":
        errors.append("manifest does not preserve Python-only defaults")
    python_coverage = manifest.get("pythonCoverage")
    rust_coverage = manifest.get("rustCoverage")
    if not isinstance(python_coverage, dict) or not isinstance(python_coverage.get("percent"), (int, float)):
        errors.append("manifest Python coverage is missing")
    if not isinstance(rust_coverage, dict) or float(rust_coverage.get("linePercent") or 0) < 80.0:
        errors.append("manifest Rust coverage is below 80% or missing")
    if not isinstance(manifest.get("rustTestCount"), int):
        errors.append("manifest Rust test count is missing")
    parity = manifest.get("parityCounts")
    expected = {"gateway": 68, "mcp": 105, "rag": 38, "ragDocumentPreparation": 125}
    if not isinstance(parity, dict) or any(parity.get(key) != value for key, value in expected.items()):
        errors.append("manifest parity counts do not match the freeze")
    binary = parity.get("ragVectorBinary") if isinstance(parity, dict) else None
    if not isinstance(binary, dict) or binary.get("valid") != 110 or binary.get("malformed") != 16:
        errors.append("manifest binary parity counts do not match 110 + 16")
    for field in ("architectureDecisionSha256", "protocolContractSha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or len(value) != 64:
            errors.append(f"manifest {field} is missing")
    image = manifest.get("rustSidecarImage")
    if not isinstance(image, dict) or image.get("tag") != f"deepseek-rust-gateway:{APP_VERSION}":
        errors.append("manifest Rust image tag is missing")
    elif not str(image.get("digest") or "").startswith("sha256:"):
        errors.append("manifest Rust image digest is missing")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    forbidden = [name for name in names if forbidden_member(name)]
    if forbidden:
        errors.append(f"ZIP contains private/runtime members: {forbidden[:10]}")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    if "rust-gateway:" in compose or "DEEPSEEK_RUST_" in compose:
        errors.append("default Compose is not Python-only")
    perf = _json(ROOT / "docs" / "evidence" / f"rust-sidecar-performance-v{APP_VERSION}.json")
    redaction = perf.get("redaction")
    if not isinstance(redaction, dict) or any(value is not False for value in redaction.values()):
        errors.append("performance evidence redaction contract failed")
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {archive.name} matches checksum, manifest, provenance, privacy, and Python-only defaults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
