"""Reverse-audit which Python HTTP routes have no explicit native route declaration.

This is a *scope* aid for the migration matrix, not a completion signal. A route
appearing here means no Rust/Go source declares that path; a route disappearing
from here only means a native declaration exists, not that behavior is verified.

Path parameters are normalized so ``{media_id}`` (FastAPI) and ``:media_id``
(axum) compare equal.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PY_ROUTE_RE = re.compile(r'@router\.(get|post|put|patch|delete|api_route)\(\s*"([^"]+)"')
RUST_ROUTE_RE = re.compile(r'\.route\(\s*"([^"]+)"')
GO_ROUTE_RE = re.compile(r'HandleFunc\("([^"]+)"')
PY_PARAM_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
NATIVE_PARAM_RE = re.compile(r":([A-Za-z0-9_]+)")

# Paths served by an explicit auth/control allow list rather than a `.route(` call.
RUST_ALLOWLIST_RE = re.compile(r'"(/(?:api|v1|mcp|a2a)[^"]*)"')


def normalize(path: str) -> str:
    path = PY_PARAM_RE.sub("{}", path)
    path = NATIVE_PARAM_RE.sub("{}", path)
    return path


def scan() -> dict[str, list[str]]:
    python: dict[str, list[str]] = {}
    for source in sorted((ROOT / "deepseek_infra/web/routes").glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for match in PY_ROUTE_RE.finditer(text):
            python.setdefault(normalize(match.group(2)), []).append(source.name)

    native: set[str] = set()
    for source in (ROOT / "rust/crates").glob("*/src/**/*.rs"):
        text = source.read_text(encoding="utf-8")
        for match in RUST_ROUTE_RE.finditer(text):
            native.add(normalize(match.group(1)))
    for source in (ROOT / "go").glob("**/*.go"):
        text = source.read_text(encoding="utf-8", errors="replace")
        for match in GO_ROUTE_RE.finditer(text):
            native.add(normalize(match.group(1)))

    return {
        "python": python,
        "native": sorted(native),
        "missing": sorted(set(python) - native),
    }


def main(argv: list[str]) -> int:
    result = scan()
    python = result["python"]
    native = result["native"]
    missing = result["missing"]
    print(f"python routes (normalized): {len(python)}")
    print(f"native routes (normalized): {len(native)}")
    print(f"python routes with no native declaration: {len(missing)}")
    families: Counter[str] = Counter()
    for path in missing:
        parts = path.strip("/").split("/")
        families["/".join(parts[:2])] += 1
    print("\nby family:")
    for family, count in sorted(families.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {count:4d}  /{family}")
    owners: Counter[str] = Counter()
    for path in missing:
        for module in python[path]:
            owners[module] += 1
    print("\nby python module:")
    for module, count in sorted(owners.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {count:4d}  {module}")
    if "--list" in argv:
        print("\npaths:")
        for path in missing:
            print(f"  {path}  ({', '.join(python[path])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
