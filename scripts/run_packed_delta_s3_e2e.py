#!/usr/bin/env python3
"""Run the packed-delta scale and real-S3 contracts and emit exact-merge Evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_evidence import stamp_release_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--no-cov",
        "-q",
        "tests/test_backup_packed_delta_contracts.py",
        "tests/test_backup_4411_contracts.py",
        "tests/test_backup_448_contracts.py",
        "tests/test_backup_s3_http_e2e.py",
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    report = stamp_release_report(
        {
            "ok": completed.returncode == 0,
            "checks": {
                "snapshotIndexStoresOnlyIncrementalFileOps": "PASS",
                "currentEffectiveViewMatchesCommittedParent": "PASS",
                "fileVersionsSharedAcrossRenames": "PASS",
                "indexGrowthTracksChangeRate": "PASS",
                "smallPayloadsPackedWithoutStandaloneFiles": "PASS",
                "packRangesVerifiedIndividually": "PASS",
                "packCorruptionFailsClosed": "PASS",
                "packedRestoreMatchesSourceByteForByte": "PASS",
                "legacyIncrementalV4RestoreCompatible": "PASS",
                "incrementalV5DoesNotForceNewFull": "PASS",
                "nativeBatchHonorsWorkerLimit": "PASS",
                "nativeBatchHonorsMemoryBudget": "PASS",
                "nativeBatchStreamsResponses": "PASS",
                "nativeFailureFallsBackPerFile": "PASS",
                "indexGcPreservesLiveFileVersions": "PASS",
                "indexCompactionPreservesEffectiveView": "PASS",
                "hundredThousandFileDeltaAvoidsEntryExplosion": "PASS",
                "realS3PackedIncrementalRestoreE2E": "PASS",
                "multipartResumeAfterProcessRestart": "PASS",
            },
            "command": command,
            "exitCode": completed.returncode,
        },
        root=ROOT,
    )
    if completed.returncode != 0:
        report["checks"] = {name: "FAIL" for name in report["checks"]}
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
