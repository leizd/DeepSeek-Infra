"""Build a privacy-safe DeepSeek Infra release zip with manifest & checksum."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deepseek_infra.infra.diagnostics import release_manifest  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_manifest import sha256_of as evidence_sha256_of  # noqa: E402

# Runtime data / caches: excluded from the zip AND safe to delete with --clean-workspace.
EXCLUDED_DIRS = {
    ".file-cache",
    ".agent-runs",
    ".memory",
    ".media",
    ".projects",
    ".reminders",
    ".search-cache",
    ".budget",
    ".tool-audit",
    ".browser-audit",
    ".browser-downloads",
    ".browser-profiles",
    ".automation",
    ".scheduler",
    ".a2a",
    ".skills",
    ".local-rag",
    ".traces",
    ".semantic-cache",
    ".request-queue",
    ".generated",
    "artifacts",
    ".gradle",
    ".mypy_cache",
    ".npm-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".idea",
    "__pycache__",
    "dist",
    "build",
    "target",
}
# VCS / tooling metadata: excluded from the zip but NEVER deleted (clean_workspace must not touch these).
NEVER_PACKAGE_DIRS = {
    ".git",
    ".claude",
    "node_modules",
}
EXCLUDED_DIR_PATTERNS = {
    "pytest-cache-files-*",
    ".tmp-pytest-*",
    "audit-cleanup-*",
    ".test-*",
}
EXCLUDED_FILE_PATTERNS = {
    ".coverage",
    ".auth-token",
    ".env",
    ".env.local",
    ".launcher-config.json",
    ".launcher-config.json.tmp",
    "signing.properties",
    "keystore.properties",
    "*.jks",
    "*.keystore",
    "*.spec",
    "*.pyc",
    "*.pyo",
    ".server*.log",
    "server*.log",
}
GENERATED_PACKAGE_DIRS = (
    ("static", "ui"),
    ("docs", "evidence"),
    ("docs", "releases"),
    ("evals", "reports"),
)


def should_include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = set(relative.parts)
    if parts.intersection(EXCLUDED_DIRS | NEVER_PACKAGE_DIRS):
        return False
    if any(fnmatch.fnmatch(part, pattern) for part in relative.parts for pattern in EXCLUDED_DIR_PATTERNS):
        return False
    return not any(fnmatch.fnmatch(relative.name, pattern) for pattern in EXCLUDED_FILE_PATTERNS)


def tracked_project_files(root: Path) -> set[str] | None:
    """Return Git-owned files, or ``None`` when ``root`` is not a worktree."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return {item for item in result.stdout.split("\0") if item}


def collect_files(root: Path) -> list[Path]:
    root = root.resolve()
    tracked = tracked_project_files(root)
    if tracked is None:
        candidates = set(root.rglob("*"))
    else:
        candidates = {root / relative for relative in tracked}
        for parts in GENERATED_PACKAGE_DIRS:
            generated_root = root.joinpath(*parts)
            if generated_root.is_dir():
                candidates.update(generated_root.rglob("*"))
    return sorted(path for path in candidates if path.is_file() and should_include(path, root))


def git_sha(root: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def measured_python_coverage(root: Path) -> float | None:
    totals = _read_json(root / "artifacts" / "coverage.json").get("totals")
    if not isinstance(totals, dict):
        return None
    value = totals.get("percent_covered")
    return float(value) if isinstance(value, int | float) else None


def rust_coverage_summary(root: Path, version: str) -> tuple[float | None, int | None]:
    paths = (
        root / "artifacts" / "rust-coverage.json",
        root / "docs" / "evidence" / f"rust-coverage-v{version}.json",
    )
    for path in paths:
        data = _read_json(path)
        coverage = data.get("coverage")
        lines = coverage.get("lines") if isinstance(coverage, dict) else None
        percent = lines.get("percent") if isinstance(lines, dict) else None
        count = data.get("rustTestCount")
        if isinstance(percent, int | float) and isinstance(count, int):
            return float(percent), count
    return None, None


def parity_counts(root: Path, version: str) -> dict[str, object]:
    prefix = root / "docs" / "evidence"

    def summary_total(name: str) -> int | None:
        summary = _read_json(prefix / f"{name}-v{version}.json").get("summary")
        total = summary.get("total") if isinstance(summary, dict) else None
        return int(total) if isinstance(total, int) else None

    rag = _read_json(prefix / f"rag-parity-v{version}.json").get("summary")
    rag_total = None
    if isinstance(rag, dict):
        values = [item.get("total") for item in rag.values() if isinstance(item, dict)]
        if values:
            total = 0
            for value in values:
                if not isinstance(value, int):
                    break
                total += value
            else:
                rag_total = total
    binary = _read_json(prefix / f"rag-vector-binary-parity-v{version}.json")
    return {
        "gateway": summary_total("gateway-request-parity"),
        "mcp": summary_total("mcp-protocol-parity"),
        "rag": rag_total,
        "ragDocumentPreparation": summary_total("rag-document-preparation-parity"),
        "ragVectorBinary": {
            "valid": binary.get("validCaseCount"),
            "malformed": binary.get("malformedCaseCount"),
        },
    }


def file_sha256(path: Path) -> str:
    return release_manifest.sha256_of(path) if path.is_file() else ""


def rust_sidecar_image(root: Path, version: str) -> tuple[str, str]:
    data = _read_json(root / "docs" / "evidence" / f"rust-sidecar-image-v{version}.json")
    tag = data.get("tag")
    digest = data.get("digest")
    return (
        str(tag) if isinstance(tag, str) else "",
        str(digest) if isinstance(digest, str) else "",
    )


def evidence_manifest_summary(root: Path, version: str) -> dict[str, object]:
    path = root / "docs" / "evidence" / f"evidence-manifest-v{version}.json"
    data = _read_json(path)
    artifacts = data.get("artifacts")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": evidence_sha256_of(path) if path.is_file() else "",
        "artifactCount": len(artifacts) if isinstance(artifacts, list) else 0,
        "testedRevision": str(data.get("testedRevision") or "unknown"),
    }


def clean_workspace(root: Path) -> list[Path]:
    removed: list[Path] = []
    for directory_name in EXCLUDED_DIRS:
        for path in root.rglob(directory_name):
            if path.is_dir() and root in path.resolve().parents:
                shutil.rmtree(path)
                removed.append(path)
    for pattern in EXCLUDED_DIR_PATTERNS:
        for path in root.rglob(pattern):
            if path.is_dir() and root in path.resolve().parents:
                shutil.rmtree(path)
                removed.append(path)
    for pattern in EXCLUDED_FILE_PATTERNS:
        for path in root.rglob(pattern):
            if path.is_file() and root in path.resolve().parents:
                path.unlink()
                removed.append(path)
    return removed


def build_release_zip(root: Path, output_dir: Path, version: str) -> Path:
    root = root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"deepseek-infra-{version}.zip"
    legacy_path = output_dir / f"deepseek-mobile-{version}.zip"
    if archive_path.exists():
        archive_path.unlink()

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in collect_files(root):
            archive.write(path, path.relative_to(root).as_posix())

    # Keep legacy-name zip as copy (backward compatibility)
    if legacy_path.exists():
        legacy_path.unlink()
    shutil.copy2(archive_path, legacy_path)

    return archive_path


def build_frontend(root: Path) -> bool:
    frontend_package = root / "frontend" / "package.json"
    if not frontend_package.is_file():
        print("frontend build failed: frontend/package.json is missing", file=sys.stderr)
        return False
    script = root / "scripts" / "build_frontend.py"
    if not script.is_file():
        print("frontend build failed: scripts/build_frontend.py is missing", file=sys.stderr)
        return False
    result = subprocess.run([sys.executable, str(script), "--root", str(root)], cwd=root, check=False)
    return result.returncode == 0 and require_frontend_build(root)


def require_frontend_build(root: Path) -> bool:
    index = root / "static" / "ui" / "index.html"
    if index.is_file():
        return True
    print("frontend build failed: static/ui/index.html is missing", file=sys.stderr)
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root to package.")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "dist", help="Directory for the release zip.")
    parser.add_argument("--version", default="", help="Release version. Defaults to settings.app_version.")
    parser.add_argument("--clean-workspace", action="store_true", help="Remove excluded runtime files before packaging.")
    parser.add_argument("--dry-run", action="store_true", help="Enumerate the files that would be packaged without writing the zip, checksum or manifest.")
    parser.add_argument("--skip-frontend-build", action="store_true", help="Package an already-built static/ui tree without rebuilding it.")
    parser.add_argument("--coverage-gate", default="95%", help="Coverage gate stamped into the manifest.")
    parser.add_argument("--eval-report", default="evals/reports/latest.json", help="Eval report path stamped into the manifest.")
    parser.add_argument("--agent-report", default="evals/reports/agent-latest.json", help="Agent eval report path stamped into the manifest.")
    parser.add_argument("--python-coverage", type=float, help="Measured Python coverage stamped into the manifest.")
    parser.add_argument("--rust-coverage", type=float, help="Measured Rust line coverage stamped into the manifest.")
    parser.add_argument("--rust-test-count", type=int, help="Rust workspace test count stamped into the manifest.")
    parser.add_argument("--rust-sidecar-image-tag", default="", help="Rust sidecar image tag stamped into the manifest.")
    parser.add_argument(
        "--rust-sidecar-image-digest",
        default=os.environ.get("RUST_SIDECAR_IMAGE_DIGEST", ""),
        help="Rust sidecar image digest; defaults to RUST_SIDECAR_IMAGE_DIGEST or an honest not-published marker.",
    )
    parser.add_argument("--no-manifest", action="store_true", help="Skip writing the .zip.sha256 and .zip.manifest.json siblings.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = args.version
    if not version:
        from deepseek_infra.core.config import settings

        version = settings.app_version
    root = args.root.resolve()
    if args.dry_run:
        frontend_ready = require_frontend_build(root) if args.skip_frontend_build else build_frontend(root)
        if not frontend_ready:
            return 1
        files = collect_files(root)
        archive_name = f"deepseek-infra-{version}.zip"
        print(f"dry-run: would package {len(files)} files into {args.output_dir / archive_name}")
        return 0
    if args.clean_workspace:
        clean_workspace(root)
    frontend_ready = require_frontend_build(root) if args.skip_frontend_build else build_frontend(root)
    if not frontend_ready:
        return 1
    archive_path = build_release_zip(root, args.output_dir, version)
    if not args.no_manifest:
        sha256 = release_manifest.sha256_of(archive_path)
        release_manifest.write_checksum(archive_path, sha256)
        measured_rust_coverage, measured_rust_tests = rust_coverage_summary(root, version)
        measured_image_tag, measured_image_digest = rust_sidecar_image(root, version)
        manifest = release_manifest.build_manifest(
            version=version,
            commit=git_sha(root),
            python_version=platform.python_version(),
            coverage_gate=args.coverage_gate,
            eval_report=args.eval_report,
            agent_report=args.agent_report,
            artifact=archive_path,
            sha256=sha256,
            python_coverage=args.python_coverage if args.python_coverage is not None else measured_python_coverage(root),
            rust_coverage=args.rust_coverage if args.rust_coverage is not None else measured_rust_coverage,
            rust_test_count=args.rust_test_count if args.rust_test_count is not None else measured_rust_tests,
            parity_counts=parity_counts(root, version),
            architecture_decision_sha256=file_sha256(root / "release" / "4_0_runtime_decision.json"),
            protocol_contract_sha256=file_sha256(root / "release" / "4_0_protocol_contract.json"),
            rust_sidecar_image_tag=args.rust_sidecar_image_tag or measured_image_tag,
            rust_sidecar_image_digest=args.rust_sidecar_image_digest or measured_image_digest,
            evidence_manifest=evidence_manifest_summary(root, version),
        )
        release_manifest.write_manifest(archive_path, manifest)
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
