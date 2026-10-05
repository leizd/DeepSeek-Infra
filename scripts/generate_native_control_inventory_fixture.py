"""Regenerate isolated Python control-export fixtures for the Go importer tests.

This offline development tool creates a fresh temporary Python control SQLite
database. It never reads the repository's runtime data directories.

The source is laid out in the standard `.backup-control` shape so the export
binds the sibling legacy projection directories, and the exact projection bytes
are copied next to the export so the Go verifier can re-derive the same digest
from a temporary copy of the standard layout.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_control, backup_control_authority, backup_scheduler
from scripts.native_control_handoff import export_and_fence

FIXTURE_TIME = "2026-09-29T00:00:00Z"
PROJECTION_DIR_NAMES = {"policy": ".backup-policies", "target": ".backup-targets"}
PROJECTION_ID_FIELDS = {"policy": "policyId", "target": "targetId"}
PROJECTION_OUTPUT_DIRS = {"policy": "projection_policies", "target": "projection_targets"}


def _fixture_scheduler_time(value: datetime | None = None) -> str:
    return FIXTURE_TIME


def _write_projection(directory: Path, record: dict[str, Any], record_id: str) -> None:
    """Write one projection the way the Python list route writes it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{record_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )


def generate(output_dir: Path, *, empty: bool = False, target_health: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError("fixture output directory must be empty; inspect new fixtures before replacing existing files")
    prefix = "python_empty_" if empty else "python_"
    original_dir, original_db = backup_control.CONTROL_DIR, backup_control.CONTROL_DB
    original_mode = os.environ.get("DEEPSEEK_RUNTIME_MODE")
    original_go_control = os.environ.pop("DEEPSEEK_GO_CONTROL", None)
    original_control_time, original_authority_time = backup_control._utc_iso, backup_control_authority._utc_iso
    original_scheduler_dir, original_scheduler_time = backup_scheduler.BACKUP_SCHEDULER_DIR, backup_scheduler._utc_iso
    try:
        with tempfile.TemporaryDirectory(prefix="native-control-fixture-") as temporary:
            root = Path(temporary)
            backup_control.CONTROL_DIR = root / ".backup-control"
            backup_control.CONTROL_DB = backup_control.CONTROL_DIR / "control.sqlite3"
            backup_control._utc_iso = lambda: FIXTURE_TIME
            backup_control_authority._utc_iso = lambda: FIXTURE_TIME
            if target_health:
                backup_scheduler.BACKUP_SCHEDULER_DIR = root / ".backup-scheduler"
                backup_scheduler._utc_iso = _fixture_scheduler_time
                # Use the actual scheduler schema and writer, including for an
                # attested empty source; never fabricate a health-only SQLite.
                backup_scheduler.target_health()
                if not empty:
                    backup_scheduler.record_target_health("t-1", "blocked", "provider timeout")
                    backup_scheduler.record_target_health("old-target", "healthy")
            os.environ["DEEPSEEK_RUNTIME_MODE"] = "python_authoritative"
            backup_control_authority.configure_authority_anchor_roots(None)
            backup_control_authority.configure_authority_anchor_stores(None)
            if empty:
                # Create the real Python schema/boot row without inventing a
                # source record. Both empty domains still need a source fence.
                backup_control.list_policies()
                backup_control.list_targets()
            else:
                backup_control.create_policy({"policyId": "p-1", "enabled": True})
                backup_control.mutate_policy("p-1", expected_revision=1, mutate=lambda old: {**old, "enabled": False})
                backup_control.upsert_target({
                    "targetId": "t-1", "kind": "s3", "topologyGeneration": 1,
                    "credentialReference": "env:FIXTURE_TARGET",
                })
            checkpoint = backup_control_authority.snapshot_authority_from_control_db()
            backup_control_authority.record_local_authority_head(checkpoint)
            checkpoint_path = output_dir / f"{prefix}inventory_checkpoint_v1.json"
            checkpoint_path.write_text(
                json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            # The standard layout implies a sibling projection directory per
            # domain. Populate it from the authority rows, then export: the
            # manifest binds the directory state the Go transfer rechecks.
            records = {}
            for domain, directory_name in PROJECTION_DIR_NAMES.items():
                directory = root / directory_name
                directory.mkdir(parents=True, exist_ok=True)
                listed = backup_control.list_policies() if domain == "policy" else backup_control.list_targets()
                id_field = PROJECTION_ID_FIELDS[domain]
                for record in listed:
                    _write_projection(directory, record, str(record[id_field]))
                records[domain] = directory
            for domain in ("policy", "target"):
                include_health = target_health and domain == "target"
                export_and_fence(
                    backup_control.CONTROL_DB, checkpoint_path,
                    output_dir / f"{prefix}{domain}_inventory_export_v{2 if include_health else 1}.json", domain,
                    f"fixture-{'empty-' if empty else ''}{domain}",
                    scheduler_db=backup_scheduler.BACKUP_SCHEDULER_DIR / "scheduler.db" if include_health else None,
                )
            for domain, directory in records.items():
                projection_files = sorted(directory.glob("*.json"), key=lambda item: item.name.encode("utf-8"))
                if not projection_files:
                    continue
                target_dir = output_dir / f"{prefix}{PROJECTION_OUTPUT_DIRS[domain]}"
                target_dir.mkdir(parents=True, exist_ok=True)
                for path in projection_files:
                    (target_dir / path.name).write_bytes(path.read_bytes())
            # SQLite's backup API captures the fenced WAL state in a single
            # consistent, self-contained file for the Go read-only verifier.
            with closing(sqlite3.connect(backup_control.CONTROL_DB)) as source, closing(sqlite3.connect(
                output_dir / f"{prefix}control_source_v1.sqlite3"
            )) as copy:
                source.backup(copy)
            with closing(sqlite3.connect(output_dir / f"{prefix}control_source_v1.sqlite3")) as copy:
                copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if copy.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise RuntimeError("isolated source fixture is not a standalone SQLite file")
            if target_health:
                with closing(sqlite3.connect(backup_scheduler.BACKUP_SCHEDULER_DIR / "scheduler.db")) as scheduler, closing(sqlite3.connect(
                    output_dir / f"{prefix}scheduler_source_v1.sqlite3"
                )) as scheduler_copy:
                    scheduler.backup(scheduler_copy)
                    scheduler_copy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    scheduler_copy.execute("PRAGMA journal_mode=DELETE")
    finally:
        backup_control.CONTROL_DIR, backup_control.CONTROL_DB = original_dir, original_db
        backup_control._utc_iso, backup_control_authority._utc_iso = original_control_time, original_authority_time
        backup_scheduler.BACKUP_SCHEDULER_DIR, backup_scheduler._utc_iso = original_scheduler_dir, original_scheduler_time
        if original_mode is None:
            os.environ.pop("DEEPSEEK_RUNTIME_MODE", None)
        else:
            os.environ["DEEPSEEK_RUNTIME_MODE"] = original_mode
        if original_go_control is not None:
            os.environ["DEEPSEEK_GO_CONTROL"] = original_go_control


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--empty", action="store_true", help="generate a genuinely empty fenced Python source")
    parser.add_argument("--target-health", action="store_true", help="generate a v2 target export with fenced scheduler health")
    args = parser.parse_args()
    generate(args.output_dir, empty=args.empty, target_health=args.target_health)


if __name__ == "__main__":
    main()
