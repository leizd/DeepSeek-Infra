#!/usr/bin/env python3
"""Assemble producer-isolated CI Artifacts into exact-merge release Evidence."""

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
from deepseek_infra.infra.diagnostics.evidence_assembly import assemble_evidence  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import load_source_context  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--github-sha", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args(argv)
    try:
        context = load_source_context(args.context.resolve())
        if context is None:
            raise ValueError("Evidence source context is required")
        manifest = assemble_evidence(
            args.downloads_root.resolve(),
            args.out.resolve(),
            version=args.version,
            context=context,
            github_sha=args.github_sha,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evidence assembly failed: {exc}", file=sys.stderr)
        return 1
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
