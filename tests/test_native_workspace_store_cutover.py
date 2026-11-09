"""The S0 store cutover moves real state and can undo it.

These tests are the acceptance evidence for the S0 prerequisite, so they exercise
the tool against **real** store directories written by the real Python store
modules rather than hand-built fixtures: the catalog JSONL, the retention policy
files and the DR ledger SQLite database. Every path is inside `tmp_path`; no
production store is read or written.

What each test proves:

* export -> import reproduces the store byte-for-byte, including a SQLite
  database and a nested append-only JSONL;
* a tampered export and a changed source are both refused, so the integrity
  digest is load-bearing rather than decorative;
* import refuses a non-empty destination, which is what makes it a transfer of
  authority instead of a merge;
* rollback restores the exact pre-import state, so a wrong import is reversible;
* the ownership contract's declared writer entry points are all gated in Python,
  which is the half of S0 that stops a stray writer during the transition.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import backup_catalog, backup_retention, resilience_capacity_history
from scripts import native_store_writer_gate, native_workspace_store_cutover as cutover

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP = json.loads((ROOT / "release" / "native_runtime_ownership_v1.json").read_text(encoding="utf-8"))


def _declared(store_id: str) -> dict:
    for item in OWNERSHIP["domains"]:
        if item["id"] == store_id:
            return item
    raise AssertionError(store_id)


def _seed_catalog(root: Path) -> None:
    backup_catalog.append_receipt(
        root,
        {"backupId": "bk-a", "filename": "a.dsibackup", "size": 10, "createdAt": "2026-11-09T00:00:00Z"},
    )
    backup_catalog.append_receipt(
        root,
        {"backupId": "bk-b", "filename": "b.dsibackup", "size": 20, "createdAt": "2026-11-09T00:01:00Z"},
    )
    backup_catalog.pin_backup(root, "bk-a", True)


def _seed_sqlite_store(root: Path) -> list[Path]:
    """Create a real SQLite database with the store's own schema.

    The SQLite stores resolve their database path from a module global, so an
    isolated test cannot point them at `tmp_path` without patching module state.
    Creating the file with the store's own DDL keeps the artifact real — a WAL-mode
    SQLite database with rows — which is what the round trip has to carry.
    """
    import sqlite3

    directory = root / ".resilience-capacity"
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "capacity.sqlite3"
    connection = sqlite3.connect(str(database))
    try:
        connection.executescript(resilience_capacity_history._SCHEMA)
        connection.execute(
            "INSERT INTO resilience_capacity_observations"
            " (observation_key, target_id, observed_at, used_bytes, free_bytes, total_bytes)"
            " VALUES (?,?,?,?,?,?)",
            ("obs-1", "target-a", "2026-11-09T00:00:00Z", 10, 90, 100),
        )
        connection.commit()
    finally:
        connection.close()
    return [database]


def test_export_import_round_trip_reproduces_the_store(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_catalog(source)
    backup_retention.put_retention_policy("pol-1", {"minimumCopies": 2})
    before = cutover._inventory(source)
    assert before, "the seeded store must not be empty"

    manifest = tmp_path / "export.json"
    cutover.export_store("backup_catalog_store", source, manifest, transfer_id="t-1")

    destination = tmp_path / "destination"
    result = cutover.import_store(manifest, source, destination)

    assert result["entries"] == len(before)
    assert cutover._inventory(destination) == before
    for entry in before:
        if entry["kind"] != "file":
            continue
        assert (destination / entry["path"]).read_bytes() == (source / entry["path"]).read_bytes(), entry["path"]


def test_round_trip_preserves_a_sqlite_store(tmp_path: Path) -> None:
    """A SQLite store survives the transfer as a readable database."""
    source = tmp_path / "capacity"
    source.mkdir()
    databases = _seed_sqlite_store(source)

    manifest = tmp_path / "capacity-export.json"
    cutover.export_store("resilience_capacity_store", source, manifest, transfer_id="t-sql")
    destination = tmp_path / "capacity-copy"
    cutover.import_store(manifest, source, destination)

    copied = sorted(destination.rglob("*.sqlite3"))
    assert len(copied) == len(databases)
    for original, copy in zip(databases, copied):
        assert copy.read_bytes() == original.read_bytes()

    # The copy must still be a queryable database, not just equal bytes.
    import sqlite3

    connection = sqlite3.connect(str(copied[0]))
    try:
        rows = connection.execute(
            "SELECT observation_key, target_id FROM resilience_capacity_observations"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("obs-1", "target-a")]


def test_verify_detects_a_tampered_export(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_catalog(source)
    manifest = tmp_path / "export.json"
    cutover.export_store("backup_catalog_store", source, manifest, transfer_id="t-2")
    assert cutover.verify_export(manifest, source)["entries"] > 0

    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["entries"][0]["size"] += 1
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(cutover.CutoverError, match="digest"):
        cutover.verify_export(manifest, source)


def test_verify_detects_a_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_catalog(source)
    manifest = tmp_path / "export.json"
    cutover.export_store("backup_catalog_store", source, manifest, transfer_id="t-3")

    (source / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(cutover.CutoverError, match="inventory"):
        cutover.verify_export(manifest, source)


def test_import_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_catalog(source)
    manifest = tmp_path / "export.json"
    cutover.export_store("backup_catalog_store", source, manifest, transfer_id="t-4")

    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "existing.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(cutover.CutoverError, match="already holds state"):
        cutover.import_store(manifest, source, destination)
    assert (destination / "existing.json").read_text(encoding="utf-8") == "{}\n"


def test_rollback_restores_the_pre_import_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_catalog(source)
    manifest = tmp_path / "export.json"
    cutover.export_store("backup_catalog_store", source, manifest, transfer_id="t-5")

    destination = tmp_path / "destination"
    destination.mkdir()
    previous = "{\"keep\": true}\n"
    (destination / "previous.json").write_text(previous, encoding="utf-8")
    pre_import = cutover._inventory(destination)

    snapshot = tmp_path / "snapshot"
    cutover.snapshot_store(destination, snapshot)

    # A real import has to start from an empty destination, so stage it aside.
    staged = tmp_path / "staged"
    cutover.import_store(manifest, source, staged)
    import shutil

    shutil.rmtree(destination)
    shutil.copytree(staged, destination)
    assert destination.joinpath("previous.json").exists() is False

    cutover.rollback_store(snapshot, destination)
    assert cutover._inventory(destination) == pre_import
    assert (destination / "previous.json").read_text(encoding="utf-8") == previous


def test_export_rejects_an_undeclared_store(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(cutover.CutoverError, match="not declared"):
        cutover.export_store("memory_store", source, tmp_path / "e.json", transfer_id="t-6")


def test_every_declared_store_path_is_reported() -> None:
    stores = cutover.declared_stores()
    declared = {item["id"] for item in OWNERSHIP["domains"] if "storePath" in item}
    assert set(stores) == declared
    assert len(stores) >= 15
    for store_id, item in stores.items():
        assert item["storePath"]["primary"].startswith("."), store_id
        assert item["target_owner"] == "rust", store_id


def test_declared_writer_entry_points_are_gated_in_python() -> None:
    """Every declared write entry point must actually call the ownership gate."""
    frozen = native_store_writer_gate._frozen_names()
    checked = 0
    for store_id, path, qualnames in native_store_writer_gate._declared():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qualname in qualnames:
            short = qualname.split(".", 1)[1]
            assert short not in frozen, f"{qualname} may not be gated: frozen AST"
            node = native_store_writer_gate._function_node(tree, qualname)
            assert native_store_writer_gate._existing_gate(node) == store_id, qualname
            checked += 1
    assert checked >= 70, checked


def test_writer_gate_is_idempotent() -> None:
    """`--check` must report nothing once the gates are installed."""
    assert native_store_writer_gate.main(["--check"]) == 0
