"""Inspect and optionally apply a verified workspace backup."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace.backups import apply_restore, inspect_archive, put_session_secret, unlock_restore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or restore a DeepSeek Infra workspace backup.")
    parser.add_argument("backup", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--mode", choices=("merge", "project-copy", "replace-empty"), default="merge")
    args = parser.parse_args()
    plan = inspect_archive(args.backup.expanduser().resolve(), filename=args.backup.name)
    if plan.get("phase") == "locked":
        kind = "passphrase" if plan.get("protection") == "passphrase" else "age-identity"
        label = "Backup passphrase: " if kind == "passphrase" else "Recovery Identity: "
        secret = getpass.getpass(label)
        put_session_secret(str(plan["restoreId"]), {"kind": kind, "secret": secret})
        secret = ""
        plan = unlock_restore(str(plan["restoreId"]))
    print(json.dumps({key: value for key, value in plan.items() if key != "manifest"}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    result = apply_restore(str(plan["restoreId"]), mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
