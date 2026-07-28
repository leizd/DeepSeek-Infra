"""Create a portable, verified workspace backup using the runtime core."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace.backups import backup_path, create_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified DeepSeek Infra workspace backup.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--include-history", action="store_true")
    parser.add_argument("--include-rebuildable-indexes", action="store_true")
    args = parser.parse_args()
    payload = {
        "mode": "project" if args.project else "full",
        "projectIds": args.project,
        "includeHistory": args.include_history,
        "includeDrafts": False,
        "includeRebuildableIndexes": args.include_rebuildable_indexes,
    }
    result = create_backup(payload)
    source = backup_path(str(result["backupId"]))
    target = args.out.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"Verified backup written: {target}")
    print(f"SHA-256 manifest: {result['manifestDigest']}")
    print("Encryption: none (protect this file as sensitive workspace data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
