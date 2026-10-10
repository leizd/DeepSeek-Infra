"""Create a portable, verified workspace backup using the runtime core."""

from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace.backups import backup_path, create_session, finalize_session, put_session_secret  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified DeepSeek Infra workspace backup.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--include-history", action="store_true")
    parser.add_argument("--include-rebuildable-indexes", action="store_true")
    protection = parser.add_mutually_exclusive_group()
    protection.add_argument("--passphrase", action="store_true", help="Encrypt with an interactively entered age passphrase.")
    protection.add_argument("--recipient", action="append", default=[], help="Encrypt to a public age1 recipient; repeat for multiple recipients.")
    parser.add_argument("--coverage-policy", choices=("strict", "best-effort"), default="strict")
    parser.add_argument("--exclude-external-state", action="store_true")
    args = parser.parse_args()
    payload = {
        "mode": "project" if args.project else "full",
        "projectIds": args.project,
        "includeHistory": args.include_history,
        "includeDrafts": False,
        "includeRebuildableIndexes": args.include_rebuildable_indexes,
        "coveragePolicy": args.coverage_policy,
        "includeExternalState": not args.exclude_external_state,
        "requiresFrontendState": False,
    }
    secret_kind = ""
    secret = ""
    if args.passphrase:
        secret = getpass.getpass("Backup passphrase: ")
        if secret != getpass.getpass("Confirm passphrase: "):
            parser.error("passphrases do not match")
        payload["protection"] = {"mode": "passphrase"}
        secret_kind = "passphrase"
    elif args.recipient:
        payload["protection"] = {"mode": "age-recipient", "recipients": args.recipient}
        secret = getpass.getpass("Recovery Identity for ciphertext verification: ")
        secret_kind = "age-identity"
    else:
        payload["protection"] = {"mode": "none"}
    created = create_session(payload)
    backup_id = str(created["backupId"])
    if secret:
        put_session_secret(backup_id, {"kind": secret_kind, "secret": secret})
        secret = ""
    result = finalize_session(backup_id)
    source = backup_path(str(result["backupId"]))
    target = args.out.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    print(f"Verified backup written: {target}")
    print(f"SHA-256 manifest: {result['manifestDigest']}")
    print(f"Protection: {result.get('protection', {}).get('mode', 'none')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
