#!/usr/bin/env python3
"""Stage exactly one producer's owned Evidence paths for CI upload."""

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
from deepseek_infra.infra.diagnostics.evidence_assembly import prepare_producer_artifact  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import load_source_context, validate_source_context  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--producer", required=True)
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-sha", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args(argv)
    try:
        context = load_source_context(args.context.resolve())
        if context is None:
            raise ValueError("Evidence source context is required")
        errors = validate_source_context(context, version=args.version)
        if errors:
            raise ValueError("; ".join(errors))
        paths = prepare_producer_artifact(
            args.root.resolve(),
            args.out.resolve(),
            producer=args.producer,
            version=args.version,
            context=context,
            github_sha=args.github_sha or None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evidence producer staging failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"producer": args.producer, "status": "PASS", "paths": list(paths)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
