"""Build the isolated React frontend into ``static/ui`` without network access."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Repository root.")
    parser.add_argument(
        "--install",
        action="store_true",
        help="Run npm ci before the build. This may access the npm registry.",
    )
    return parser.parse_args()


def npm_executable() -> str:
    executable = shutil.which("npm")
    if executable is None:
        raise RuntimeError("npm is required; install Node 22.12 or newer")
    return executable


def run_frontend_build(root: Path, *, install: bool = False) -> Path:
    frontend = root / "frontend"
    package = frontend / "package.json"
    lock = frontend / "package-lock.json"
    if not package.is_file() or not lock.is_file():
        raise RuntimeError("frontend/package.json and package-lock.json are required")
    npm = npm_executable()
    if install:
        subprocess.run([npm, "ci"], cwd=frontend, check=True)
    elif not (frontend / "node_modules").is_dir():
        raise RuntimeError("frontend dependencies are missing; run npm ci --prefix frontend")
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
    output = root / "static" / "ui" / "index.html"
    if not output.is_file():
        raise RuntimeError("frontend build did not produce static/ui/index.html")
    return output


def main() -> int:
    args = parse_args()
    try:
        output = run_frontend_build(args.root.resolve(), install=bool(args.install))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"frontend build failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
