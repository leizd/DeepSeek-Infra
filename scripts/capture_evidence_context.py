#!/usr/bin/env python3
"""Capture one immutable schema-v2 Evidence context for every CI producer."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import capture_source_context  # noqa: E402

GENERATOR = "scripts/capture_evidence_context.py"


def capture(root: Path, version: str, output: Path) -> dict[str, object]:
    context = capture_source_context(root, version, generator=GENERATOR, schema_version=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return context


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.out if args.out.is_absolute() else args.root / args.out
    try:
        context = capture(args.root.resolve(), args.version, output.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evidence context capture failed: {exc}", file=sys.stderr)
        return 1
    github_output = args.github_output or (Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"revision={context['testedRevision']}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
