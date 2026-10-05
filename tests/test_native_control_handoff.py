"""The offline handoff fences the actual Python control writer on isolated data."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import backup_control, backup_control_authority
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from scripts.native_control_handoff import HandoffError, export_and_fence


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    root = tmp_path / ".backup-control"
    root.mkdir()
    database = root / "control.sqlite3"
    checkpoint = tmp_path / "checkpoint.json"
    output = tmp_path / "export.json"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", database)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_control.create_policy({"policyId": "p-1", "policyRevision": 1, "enabled": True})
    backup_control.mutate_policy("p-1", expected_revision=1, mutate=lambda old: {**old, "enabled": False})
    document = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(document)
    checkpoint.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return database, checkpoint, output


def test_fenced_export_preserves_nonempty_policy_and_denies_real_python_writer(
    source: tuple[Path, Path, Path],
) -> None:
    database, checkpoint, output = source
    manifest = export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    assert manifest["schema"] == "python-control-inventory-export-v1"
    assert manifest["rows"][0]["revision"] == 2
    assert json.loads(manifest["rows"][0]["payload_json"])["enabled"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == manifest
    assert export_and_fence(database, checkpoint, output, "policy", "transfer-p-1") == manifest
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.mutate_policy("p-1", expected_revision=2, mutate=lambda old: {**old, "enabled": True})
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.create_policy({"policyId": "p-2", "policyRevision": 1})
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.delete_policy("p-1", expected_revision=2)
    assert backup_control.get_policy("p-1") is not None
    assert backup_control.get_policy("p-2") is None
    # The fence freezes the state the transfer binds, not just the exported
    # rows: the production control connection cannot move the authority tip or
    # the boot epoch the Go attestation compares either.
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        with backup_control._connect() as conn:
            conn.execute("UPDATE control_boot_state SET boot_epoch = boot_epoch + 1 WHERE id = 1")
    with sqlite3.connect(database) as conn:
        linked = conn.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE 'native_control_fence_%'"
        ).fetchone()[0]
        assert conn.execute("SELECT boot_epoch FROM control_boot_state WHERE id=1").fetchone()[0] == 1
    assert linked == 18, "the linked control fence must cover every bound table"
    with sqlite3.connect(database) as conn:
        for statement in (
            "UPDATE native_control_handoff_fences SET source_digest='changed' WHERE domain='policy'",
            "DELETE FROM native_control_handoff_fences WHERE domain='policy'",
            "REPLACE INTO native_control_handoff_fences VALUES('policy','other','digest','digest',0)",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="NATIVE_CONTROL_HANDOFF_IMMUTABLE"):
                conn.execute(statement)
    with pytest.raises(HandoffError, match="different handoff"):
        export_and_fence(database, checkpoint, output, "policy", "another-transfer")


def test_export_refuses_unreconciled_legacy_policy_projection(
    source: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    database, checkpoint, output = source
    projections = tmp_path / ".backup-policies"
    projections.mkdir()
    with pytest.raises(HandoffError, match="does not exist"):
        export_and_fence(database, checkpoint, output, "policy", "legacy-missing-dir", projection_dir=projections / "missing")
    (projections / "p-2.json").write_text('{"policyId":"p-2","enabled":true}', encoding="utf-8")
    with pytest.raises(HandoffError, match="not reconciled"):
        export_and_fence(database, checkpoint, output, "policy", "legacy-missing", projection_dir=projections)
    assert not output.exists()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='native_control_handoff_fences'").fetchone()[0] == 0

    (projections / "p-2.json").unlink()
    (projections / "p-1.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(HandoffError, match="malformed"):
        export_and_fence(database, checkpoint, output, "policy", "legacy-invalid", projection_dir=projections)
    (projections / "p-1.json").write_text('{"policyId":"p-1","enabled":true}', encoding="utf-8")
    manifest = export_and_fence(database, checkpoint, output, "policy", "legacy-reconciled", projection_dir=projections)
    assert len(manifest["rows"]) == 1
    assert json.loads(manifest["rows"][0]["payload_json"])["enabled"] is False, "SQLite is authoritative over stale projection content"


def test_export_scans_hidden_json_and_rejects_json_directory(
    source: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    database, checkpoint, output = source
    projections = tmp_path / ".backup-policies"
    projections.mkdir()
    hidden = projections / ".p-2.json"
    hidden.write_text('{"policyId":".p-2"}', encoding="utf-8")
    with pytest.raises(HandoffError, match="not reconciled"):
        export_and_fence(database, checkpoint, output, "policy", "hidden-json", projection_dir=projections)
    hidden.unlink()
    (projections / "nested.json").mkdir()
    with pytest.raises(HandoffError, match="bounded regular file"):
        export_and_fence(database, checkpoint, output, "policy", "json-directory", projection_dir=projections)
    assert not output.exists()


def test_export_infers_canonical_policy_projection_directory(
    source: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    database, checkpoint, output = source
    projections = tmp_path / ".backup-policies"
    projections.mkdir()
    (projections / "p-2.json").write_text('{"policyId":"p-2"}', encoding="utf-8")
    with pytest.raises(HandoffError, match="not reconciled"):
        export_and_fence(database, checkpoint, output, "policy", "legacy-inferred")
    assert not output.exists()


def test_target_export_checks_registry_but_skips_target_checkpoints(
    source: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    database, checkpoint, output = source
    projections = tmp_path / ".backup-targets"
    projections.mkdir()
    (projections / "target_missing.json").write_text('{"targetId":"target_missing"}', encoding="utf-8")
    with pytest.raises(HandoffError, match="not reconciled"):
        export_and_fence(database, checkpoint, output, "target", "target-missing", projection_dir=projections)
    (projections / "target_missing.json").unlink()
    (projections / "target_missing.checkpoint.json").write_text("{}", encoding="utf-8")
    manifest = export_and_fence(database, checkpoint, output, "target", "target-checkpoint-only", projection_dir=projections)
    assert manifest["rows"] == []


def test_export_binds_the_inferred_projection_directory_state(
    source: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    database, checkpoint, output = source
    projections = tmp_path / ".backup-policies"
    projections.mkdir()
    (projections / "p-1.json").write_bytes(b'{"policyId":"p-1","enabled":false}\n')
    manifest = export_and_fence(database, checkpoint, output, "policy", "bound-projection")
    binding = manifest["legacyProjection"]
    assert binding["fileCount"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", binding["digest"]), "the binding is a canonical digest"
    # The same directory reproduces the same export, so the binding is stable.
    assert export_and_fence(database, checkpoint, output, "policy", "bound-projection") == manifest
    # Changing one projection byte changes only the binding: the SQLite rows the
    # export carries are untouched, which is exactly why the directory state has
    # to travel with the manifest instead of being re-derived from the source.
    (projections / "p-1.json").write_bytes(b'{"policyId":"p-1","enabled":true}\n')
    changed = export_and_fence(database, checkpoint, tmp_path / "changed.json", "policy", "bound-projection")
    assert changed["legacyProjection"]["fileCount"] == 1
    assert changed["legacyProjection"]["digest"] != binding["digest"]
    assert changed["sourceDigest"] == manifest["sourceDigest"]


def test_export_binds_an_explicit_projection_directory(
    source: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    database, checkpoint, output = source
    projections = tmp_path / "projection-store"
    projections.mkdir()
    (projections / "p-1.json").write_bytes(b'{"policyId":"p-1"}\n')
    manifest = export_and_fence(
        database, checkpoint, output, "policy", "explicit-projection", projection_dir=projections,
    )
    assert manifest["legacyProjection"]["fileCount"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["legacyProjection"]["digest"])


def test_nonstandard_source_cannot_omit_projection_directory(
    source: tuple[Path, Path, Path],
) -> None:
    database, checkpoint, output = source
    nonstandard = database.parent.parent / "python-control"
    database.parent.rename(nonstandard)
    database = nonstandard / "control.sqlite3"
    with pytest.raises(HandoffError, match="nonstandard source path requires"):
        export_and_fence(database, checkpoint, output, "policy", "missing-projection-dir")
    with pytest.raises(HandoffError, match="projection directory must be absolute"):
        export_and_fence(database, checkpoint, output, "policy", "relative-projection-dir", projection_dir=Path("relative"))
    assert not output.exists()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='native_control_handoff_fences'").fetchone()[0] == 0
    projections = database.parent.parent / ".backup-policies"
    projections.mkdir()
    manifest = export_and_fence(database, checkpoint, output, "policy", "explicit-projection-dir", projection_dir=projections)
    assert manifest["legacyProjection"]["fileCount"] == 0
    assert isinstance(manifest["legacyProjection"]["digest"], str)


def test_export_binds_no_projection_directory_when_there_is_none(
    source: tuple[Path, Path, Path],
) -> None:
    database, checkpoint, output = source
    manifest = export_and_fence(database, checkpoint, output, "policy", "unbound-projection")
    assert manifest["legacyProjection"] == {"fileCount": 0, "digest": None}


def test_restarted_python_writer_cannot_modify_fenced_source(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    manifest = export_and_fence(database, checkpoint, output, "policy", "restart-denial")
    process = subprocess.run(
        [sys.executable, "-c", "\n".join((
            "import sys",
            "from pathlib import Path",
            "from deepseek_infra.infra.workspace import backup_control",
            "backup_control.CONTROL_DB = Path(sys.argv[1])",
            "backup_control.CONTROL_DIR = backup_control.CONTROL_DB.parent",
            "backup_control.create_policy({'policyId': 'after-restart', 'enabled': True})",
        )), str(database)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DEEPSEEK_RUNTIME_MODE": "python_authoritative"},
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert process.returncode != 0
    assert "PYTHON_CONTROL_SOURCE_FENCED" in process.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM control_policies").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM control_policies WHERE policy_id='after-restart'").fetchone()[0] == 0
        assert conn.execute("SELECT source_digest FROM native_control_handoff_fences WHERE domain='policy'").fetchone()[0] == \
            manifest["sourceDigest"]


def test_export_refuses_stale_checkpoint_and_unsettled_remote_effects(
    source: tuple[Path, Path, Path],
) -> None:
    database, checkpoint, output = source
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE control_policies SET payload_json=? WHERE policy_id='p-1'", ('{"policyId":"p-1","policyRevision":2,"enabled":true}',))
    with pytest.raises(HandoffError, match="inventory differs"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE control_policies SET payload_json=? WHERE policy_id='p-1'", ('{"enabled": false, "policyId": "p-1", "policyRevision": 2}',))
        conn.execute(
            "INSERT INTO control_authority_mutations VALUES(?,?,?,?,?,?,?,?,?)",
            ("mutation-1", 1, "d" * 64, "policy-mutation", "{}", "remote-outcome-unknown", None, "now", "now"),
        )
    with pytest.raises(HandoffError, match="unsettled authority mutation"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    with sqlite3.connect(database) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name='native_control_handoff_fences'").fetchone()[0]
    assert count == 0
    assert not output.exists()


def test_fenced_export_recovers_after_output_publish_failure(
    source: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, checkpoint, output = source
    from scripts import native_control_handoff

    original_publish = native_control_handoff._publish
    monkeypatch.setattr(native_control_handoff, "_publish", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.delete_policy("p-1", expected_revision=2)
    monkeypatch.setattr(native_control_handoff, "_publish", original_publish)
    recovered = export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    assert json.loads(output.read_text(encoding="utf-8")) == recovered


def test_target_export_refuses_embedded_secret_until_key_custody_is_migrated(
    source: tuple[Path, Path, Path],
) -> None:
    database, checkpoint, output = source
    backup_control.upsert_target({"targetId": "t-1", "kind": "s3", "credentialReference": "env:TARGET"})
    document = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(document)
    checkpoint.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    manifest = export_and_fence(database, checkpoint, output, "target", "transfer-t-1")
    assert manifest["rows"][0]["target_id"] == "t-1"
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.upsert_target({"targetId": "t-2", "kind": "s3"})

    # A second isolated source containing raw credentials cannot be exported
    # by a Go-bound JSON manifest before its key custody has been redesigned.
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TRIGGER native_control_target_no_update")
        conn.execute("UPDATE control_targets SET payload_json=? WHERE target_id='t-1'", ('{"targetId":"t-1","accessKey":"raw"}',))
    with pytest.raises(HandoffError, match="key custody"):
        export_and_fence(database, checkpoint, output, "target", "transfer-t-1")


def test_export_refuses_an_unsettled_policy_lifecycle_intent(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO lifecycle_intents VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("intent-1", "policy-mutation", None, "p-1", None, 2, "prepared", "{}", "now", "now"),
        )
    with pytest.raises(HandoffError, match="unsettled source lifecycle intent"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    assert not output.exists()


def test_export_transaction_serializes_concurrent_python_policy_write(
    source: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import native_control_handoff

    database, checkpoint, output = source
    entered = threading.Event()
    release = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    results: dict[str, object] = {}
    original_check = native_control_handoff._check_source

    def pause_after_read(*args: Any) -> None:
        original_check(*args)
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release the export transaction")

    def run_export() -> None:
        try:
            results["export"] = export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
        except BaseException as exc:
            results["export_error"] = exc

    def run_writer() -> None:
        writer_started.set()
        try:
            backup_control.mutate_policy("p-1", expected_revision=2, mutate=lambda old: {**old, "enabled": True})
            results["writer_succeeded"] = True
        except BaseException as exc:
            results["writer_error"] = exc
        finally:
            writer_finished.set()

    monkeypatch.setattr(native_control_handoff, "_check_source", pause_after_read)
    exporter = threading.Thread(target=run_export)
    writer = threading.Thread(target=run_writer)
    exporter.start()
    try:
        assert entered.wait(5)
        writer.start()
        assert writer_started.wait(5)
        assert not writer_finished.wait(0.1), "Python write escaped an active SQLite export transaction"
    finally:
        release.set()
        exporter.join(10)
        if writer.ident is not None:
            writer.join(10)
    assert not exporter.is_alive() and not writer.is_alive()
    assert "export" in results, results
    assert isinstance(results.get("writer_error"), PythonWriterMechanicallyDeniedError), results
    assert backup_control.get_policy("p-1") == {"enabled": False, "policyId": "p-1", "policyRevision": 2}


def test_fenced_export_refuses_a_missing_trigger_on_recovery(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TRIGGER native_control_policy_no_update")
    with pytest.raises(HandoffError, match="fence schema is missing"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")


def test_offline_module_cli_exports_the_isolated_source(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    projections = database.parent.parent / ".backup-policies"
    projections.mkdir()
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.native_control_handoff", "--source-db", str(database),
         "--checkpoint", str(checkpoint), "--output", str(output), "--domain", "policy",
         "--transfer-id", "transfer-cli", "--projection-dir", str(projections)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30, check=True,
    )
    result = json.loads(completed.stdout)
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert result["manifestDigest"] == manifest["manifestDigest"]
    assert manifest["rows"][0]["revision"] == 2
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.delete_policy("p-1", expected_revision=2)


def test_export_refuses_policy_generation_not_in_authority_head(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE control_policies SET promotion_epoch=7 WHERE policy_id='p-1'")
    with pytest.raises(HandoffError, match="generations differ"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    assert not output.exists()


def test_export_refuses_an_unrepresented_source_column(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE control_policies ADD COLUMN legacy_sidecar TEXT DEFAULT 'unmigrated'")
    with pytest.raises(HandoffError, match="table schema drift"):
        export_and_fence(database, checkpoint, output, "policy", "transfer-p-1")
    assert not output.exists()


def test_target_export_refuses_payload_fields_excluded_from_checkpoint(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    backup_control.upsert_target({"targetId": "t-1", "kind": "s3", "opaqueConfig": "required-by-old-client"})
    document = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(document)
    checkpoint.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HandoffError, match="fields absent"):
        export_and_fence(database, checkpoint, output, "target", "transfer-t-1")
    assert not output.exists()


def test_target_export_refuses_unmigrated_receipt_mutation_generation(source: tuple[Path, Path, Path]) -> None:
    database, checkpoint, output = source
    backup_control.upsert_target({"targetId": "t-1", "kind": "s3"})
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO target_receipt_mutations VALUES('t-1', 3, 'now')")
    document = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(document)
    checkpoint.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(HandoffError, match="separate native import"):
        export_and_fence(database, checkpoint, output, "target", "transfer-t-1")
    assert not output.exists()


def test_generated_cross_language_fixtures_match_checked_in_exports(tmp_path: Path) -> None:
    from scripts.generate_native_control_inventory_fixture import generate

    generated = tmp_path / "regenerated"
    generate(generated)
    checked_in = Path(__file__).resolve().parents[1] / "go" / "internal" / "store" / "testdata"
    for name in (
        "python_inventory_checkpoint_v1.json",
        "python_policy_inventory_export_v1.json",
        "python_target_inventory_export_v1.json",
    ):
        assert (generated / name).read_bytes() == (checked_in / name).read_bytes()
    with sqlite3.connect(generated / "python_control_source_v1.sqlite3") as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT domain FROM native_control_handoff_fences ORDER BY domain").fetchall() == [
            ("policy",), ("target",),
        ]
        linked = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'native_control_fence_%'"
            )
        }
    # Six bound tables times three operations: the fence the Go verifier pins.
    assert len(linked) == 18
    assert "native_control_fence_control_authority_head_no_update" in linked
    assert "native_control_fence_control_boot_state_no_insert" in linked
    assert "native_control_fence_lifecycle_intents_no_delete" in linked
    assert "native_control_fence_target_receipt_mutations_no_insert" in linked


def test_generated_empty_control_source_is_really_fenced(tmp_path: Path) -> None:
    from scripts.generate_native_control_inventory_fixture import generate

    generated = tmp_path / "empty"
    generate(generated, empty=True)
    checked_in = Path(__file__).resolve().parents[1] / "go" / "internal" / "store" / "testdata"
    for name in (
        "python_empty_inventory_checkpoint_v1.json",
        "python_empty_policy_inventory_export_v1.json",
        "python_empty_target_inventory_export_v1.json",
    ):
        assert (generated / name).read_bytes() == (checked_in / name).read_bytes()
    with sqlite3.connect(generated / "python_empty_control_source_v1.sqlite3") as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM control_policies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM control_targets").fetchone()[0] == 0
        assert conn.execute("SELECT domain FROM native_control_handoff_fences ORDER BY domain").fetchall() == [
            ("policy",), ("target",),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE 'native_control_fence_%'"
        ).fetchone()[0] == 18
