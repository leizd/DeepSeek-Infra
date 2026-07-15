#!/usr/bin/env python3
"""Record the exact locally built Rust sidecar image identity for release evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from scripts.release_evidence import git_commit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=f"deepseek-rust-gateway:{APP_VERSION}")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.tag],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    digest = result.stdout.strip()
    passed = result.returncode == 0 and digest.startswith("sha256:")
    payload = {
        "schemaVersion": "rust-sidecar-image.v1",
        "version": APP_VERSION,
        "commit": git_commit(ROOT),
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "PASS" if passed else "FAIL",
        "tag": args.tag,
        "digest": digest,
        "digestKind": "local-image-id",
        "publishedRegistryDigest": False,
    }
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Rust sidecar image: {payload['status']} {args.tag} {digest}")
    if not passed and result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
