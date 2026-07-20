#!/usr/bin/env python3
"""Verify that route-level frontend code remains outside the initial workspace bundle."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402

MANIFEST_PATH = Path("static/ui/.vite/manifest.json")
ENTRY_KEY = "index.html"
TRACE_PAGE_KEY = "src/features/trace/TracePage.tsx"
TRACE_DETAIL_KEY = "src/features/trace/TraceDetailView.tsx"
TRACE_MARKERS = ("Category summary", "Span tree", "trace-primary-grid")
MAX_INITIAL_BYTES = 450_000


def _entry(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    value = manifest.get(key)
    if not isinstance(value, dict):
        raise AssertionError(f"Vite manifest is missing {key}")
    return value


def _asset(root: Path, entry: dict[str, Any]) -> Path:
    name = entry.get("file")
    if not isinstance(name, str) or not name:
        raise AssertionError("Vite manifest entry has no output file")
    path = root / "static" / "ui" / name
    if not path.is_file():
        raise AssertionError(f"Vite output is missing {name}")
    return path


def check_bundle(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_PATH
    if not manifest_path.is_file():
        raise AssertionError("React build manifest is missing; run npm run build --prefix frontend")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise AssertionError("Vite manifest root must be an object")

    entry = _entry(manifest, ENTRY_KEY)
    trace_page = _entry(manifest, TRACE_PAGE_KEY)
    trace_detail = _entry(manifest, TRACE_DETAIL_KEY)
    dynamic_imports = entry.get("dynamicImports")
    if not isinstance(dynamic_imports, list):
        raise AssertionError("frontend entry does not declare dynamic imports")
    missing = [key for key in (TRACE_PAGE_KEY, TRACE_DETAIL_KEY) if key not in dynamic_imports]
    if missing:
        raise AssertionError(f"Trace dynamic entries are missing from the workspace entry: {', '.join(missing)}")
    if trace_page.get("isDynamicEntry") is not True or trace_detail.get("isDynamicEntry") is not True:
        raise AssertionError("Trace page and detail view must remain dynamic entries")

    entry_asset = _asset(root, entry)
    trace_page_asset = _asset(root, trace_page)
    trace_detail_asset = _asset(root, trace_detail)
    if len({entry_asset.name, trace_page_asset.name, trace_detail_asset.name}) != 3:
        raise AssertionError("Trace outputs were merged into the initial workspace bundle")
    if entry_asset.stat().st_size > MAX_INITIAL_BYTES:
        raise AssertionError(
            f"initial frontend bundle exceeds budget: {entry_asset.stat().st_size} > {MAX_INITIAL_BYTES}"
        )

    entry_source = entry_asset.read_text(encoding="utf-8")
    detail_source = trace_detail_asset.read_text(encoding="utf-8")
    leaked = [marker for marker in TRACE_MARKERS if marker in entry_source]
    missing_markers = [marker for marker in TRACE_MARKERS if marker not in detail_source]
    if leaked:
        raise AssertionError(f"Trace implementation leaked into the initial bundle: {', '.join(leaked)}")
    if missing_markers:
        raise AssertionError(f"Trace detail chunk is missing expected markers: {', '.join(missing_markers)}")

    entry_css = set(entry.get("css") or [])
    trace_css = set(trace_detail.get("css") or [])
    if not trace_css:
        raise AssertionError("Trace detail chunk has no owned CSS output")
    if entry_css & trace_css:
        raise AssertionError("Trace CSS is included in the initial workspace stylesheet")
    for name in trace_css:
        if not (root / "static" / "ui" / str(name)).is_file():
            raise AssertionError(f"Trace CSS output is missing {name}")

    return {
        "initialEntry": entry_asset.relative_to(root).as_posix(),
        "initialBytes": entry_asset.stat().st_size,
        "initialBundleBudgetBytes": MAX_INITIAL_BYTES,
        "tracePageChunk": trace_page_asset.relative_to(root).as_posix(),
        "tracePageBytes": trace_page_asset.stat().st_size,
        "traceDetailChunk": trace_detail_asset.relative_to(root).as_posix(),
        "traceDetailBytes": trace_detail_asset.stat().st_size,
        "traceCss": sorted(trace_css),
        "checks": {
            "tracePageDynamicEntry": "PASS",
            "traceDetailDynamicEntry": "PASS",
            "traceImplementationDeferred": "PASS",
            "traceCssDeferred": "PASS",
            "initialBundleBudget": "PASS",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    try:
        report = check_bundle(root)
    except (AssertionError, json.JSONDecodeError, OSError) as exc:
        print(f"frontend bundle check failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "schemaVersion": 1,
        "version": _frontend_version(root),
        **evidence_revision(root),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "environment": {"os": platform.system(), "python": platform.python_version(), "ci": bool(os.getenv("CI"))},
        "status": "PASS",
        **report,
    }
    if args.out:
        output = args.out if args.out.is_absolute() else root / args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _frontend_version(root: Path) -> str:
    package = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    return str(package.get("version") or "")


if __name__ == "__main__":
    raise SystemExit(main())
