"""Build a privacy-safe DeepSeek Infra release zip."""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Runtime data / caches: excluded from the zip AND safe to delete with --clean-workspace.
EXCLUDED_DIRS = {
    ".file-cache",
    ".agent-runs",
    ".memory",
    ".projects",
    ".reminders",
    ".search-cache",
    ".budget",
    ".tool-audit",
    ".scheduler",
    ".a2a",
    ".mypy_cache",
    ".npm-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".idea",
    "__pycache__",
    "dist",
    "build",
}
# VCS / tooling metadata: excluded from the zip but NEVER deleted (clean_workspace must not touch these).
NEVER_PACKAGE_DIRS = {
    ".git",
    ".claude",
}
EXCLUDED_DIR_PATTERNS = {
    "pytest-cache-files-*",
    "audit-cleanup-*",
    ".test-*",
}
EXCLUDED_FILE_PATTERNS = {
    ".coverage",
    ".auth-token",
    ".launcher-config.json",
    ".launcher-config.json.tmp",
    "*.spec",
    "*.pyc",
    "*.pyo",
    ".server*.log",
    "server*.log",
}


def should_include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = set(relative.parts)
    if parts.intersection(EXCLUDED_DIRS | NEVER_PACKAGE_DIRS):
        return False
    if any(fnmatch.fnmatch(part, pattern) for part in relative.parts for pattern in EXCLUDED_DIR_PATTERNS):
        return False
    return not any(fnmatch.fnmatch(relative.name, pattern) for pattern in EXCLUDED_FILE_PATTERNS)


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
    archive_path = output_dir / f"deepseek-mobile-{version}.zip"
    if archive_path.exists():
        archive_path.unlink()

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not should_include(path, root):
                continue
            archive.write(path, path.relative_to(root).as_posix())

    return archive_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root to package.")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd() / "dist", help="Directory for the release zip.")
    parser.add_argument("--version", default="", help="Release version. Defaults to settings.app_version.")
    parser.add_argument("--clean-workspace", action="store_true", help="Remove excluded runtime files before packaging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = args.version
    if not version:
        from deepseek_infra.core.config import settings

        version = settings.app_version
    root = args.root.resolve()
    if args.clean_workspace:
        clean_workspace(root)
    archive_path = build_release_zip(root, args.output_dir, version)
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
