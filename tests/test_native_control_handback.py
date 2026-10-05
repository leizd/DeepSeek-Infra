"""The fenced Python source re-owns its tables only on a verified Go handback."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import backup_control, backup_control_authority
from scripts.native_control_handoff import HandoffError, _digest, export_and_fence, revoke_handoff

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "go" / "internal" / "store" / "testdata"
TRANSFER_ID = "fixture-policy"

# The child process is a real Python restart: it reopens the same control
# database through the production writer and reports the outcome. Empty output
# means the write succeeded.
_CHILD_WRITE = """
import sys
from pathlib import Path
from deepseek_infra.infra.workspace import backup_control, backup_control_authority

root = Path(sys.argv[1])
backup_control.CONTROL_DIR = root
backup_control.CONTROL_DB = root / "control.sqlite3"
backup_control_authority.configure_authority_anchor_roots(None)
backup_control_authority.configure_authority_anchor_stores(None)
try:
    backup_control.create_policy({"policyId": "p-restart", "policyRevision": 1})
except Exception as exc:
    print(type(exc).__name__ + ": " + str(exc))
"""


def _restart_writer(root: Path) -> str:
    env = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_GO_CONTROL"}
    env["DEEPSEEK_RUNTIME_MODE"] = "python_authoritative"
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_WRITE, str(root)],
        cwd=ROOT, capture_output=True, text=True, timeout=180, check=True, env=env,
    )
    return completed.stdout.strip()


def _publish(path: Path, document: dict[str, object], *, separators: tuple[str, str] = (",", ":")) -> Path:
    """Write one canonical document, byte for byte, without newline translation."""
    path.write_bytes(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=separators).encode("utf-8") + b"\n"
    )
    return path


@pytest.fixture
def fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Path]:
    """A real Python source fenced by the real exporter, plus the Go handback."""
    root = tmp_path / ".backup-control"
    root.mkdir()
    database = root / "control.sqlite3"
    shutil.copyfile(FIXTURES / "python_control_source_v1.sqlite3", database)
    for domain, directory in (("policies", ".backup-policies"), ("targets", ".backup-targets")):
        shutil.copytree(FIXTURES / f"python_projection_{domain}", tmp_path / directory)
    manifest = tmp_path / "export.json"
    shutil.copyfile(FIXTURES / "python_policy_inventory_export_v1.json", manifest)
    handback = tmp_path / "handback.json"
    shutil.copyfile(FIXTURES / "python_policy_inventory_handback_v1.json", handback)
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", database)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    return database, manifest, handback, receipt


def test_go_handback_document_is_canonical_and_bound_to_the_export() -> None:
    raw = (FIXTURES / "python_policy_inventory_handback_v1.json").read_bytes()
    manifest = json.loads((FIXTURES / "python_policy_inventory_export_v1.json").read_text(encoding="utf-8"))
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    document = json.loads(raw.decode("utf-8"))
    assert list(document) == sorted(document), "the Go document must be emitted with sorted keys"
    assert document["schema"] == "control-inventory-handback-v1"
    assert document["domain"] == manifest["domain"] == "policy"
    assert document["transferId"] == manifest["transferId"] == TRANSFER_ID
    assert document["manifestDigest"] == manifest["manifestDigest"]
    assert document["sourceDigest"] == manifest["sourceDigest"]
    assert document["authorityDigest"] == manifest["authorityDigest"]
    assert document["authorityGeneration"] == manifest["authorityGeneration"]
    assert document["rolledBackRecords"] == 1 and document["rolledBackEvents"] == 1


def test_verified_go_handback_lifts_the_fence_and_restores_the_real_writer(
    fenced: tuple[Path, Path, Path, Path],
) -> None:
    database, manifest, handback, receipt = fenced
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.create_policy({"policyId": "p-early", "policyRevision": 1})
    assert "PythonWriterMechanicallyDeniedError" in _restart_writer(database.parent)
    document = revoke_handoff(database, manifest, handback, receipt, TRANSFER_ID)
    assert document["schema"] == "python-control-inventory-handback-receipt-v1"
    assert document["domain"] == "policy" and document["transferId"] == TRANSFER_ID
    assert document["handbackDigest"] == json.loads(handback.read_text(encoding="utf-8"))["handbackDigest"]
    assert json.loads(receipt.read_text(encoding="utf-8")) == document
    assert document["receiptDigest"]
    with sqlite3.connect(database) as conn:
        # Only the handed-back domain loses its fence; the target fence the
        # fixture source still carries is untouched.
        assert conn.execute(
            "SELECT COUNT(*) FROM native_control_handoff_fences WHERE domain='policy'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM native_control_handoff_fences WHERE domain='target'"
        ).fetchone()[0] == 1
        revocations = conn.execute(
            "SELECT domain, transfer_id, manifest_digest, source_digest, handback_digest "
            "FROM native_control_handoff_revocations"
        ).fetchall()
    assert revocations == [(
        "policy", TRANSFER_ID, document["manifestDigest"], document["sourceDigest"], document["handbackDigest"],
    )]
    # A real restart of the Python service can write again, and the revocation
    # journal itself is append-only.
    assert _restart_writer(database.parent) == ""
    assert backup_control.create_policy({"policyId": "p-late", "policyRevision": 1})["policyId"] == "p-late"
    with sqlite3.connect(database) as conn:
        for statement in (
            "UPDATE native_control_handoff_revocations SET source_digest='changed'",
            "DELETE FROM native_control_handoff_revocations",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="NATIVE_CONTROL_HANDBACK_IMMUTABLE"):
                conn.execute(statement)
    with pytest.raises(HandoffError, match="not fenced for this handback"):
        revoke_handoff(database, manifest, handback, receipt, TRANSFER_ID)


def test_a_handed_back_source_can_be_fenced_for_a_new_transfer(
    fenced: tuple[Path, Path, Path, Path], tmp_path: Path,
) -> None:
    database, manifest, handback, receipt = fenced
    revoke_handoff(database, manifest, handback, receipt, TRANSFER_ID)
    second = tmp_path / "second-export.json"
    refenced = export_and_fence(
        database, FIXTURES / "python_inventory_checkpoint_v1.json", second, "policy", "second-transfer",
    )
    assert refenced["transferId"] == "second-transfer"
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.create_policy({"policyId": "p-refenced", "policyRevision": 1})


def test_revocation_refuses_unbound_or_tampered_handbacks(
    fenced: tuple[Path, Path, Path, Path], tmp_path: Path,
) -> None:
    database, manifest, handback, receipt = fenced
    original = json.loads(handback.read_text(encoding="utf-8"))

    def publish(document: dict[str, object]) -> Path:
        return _publish(tmp_path / "handback-variant.json", document)

    def redigest(document: dict[str, object]) -> Path:
        unsigned = {key: value for key, value in document.items() if key != "handbackDigest"}
        document["handbackDigest"] = _digest(unsigned)
        return publish(document)

    forged = dict(original)
    forged["sourceDigest"] = "0" * 64
    with pytest.raises(HandoffError, match="does not match the fenced export"):
        revoke_handoff(database, manifest, redigest(forged), receipt, TRANSFER_ID)
    unsigned_digest = dict(original)
    unsigned_digest["handbackDigest"] = "0" * 64
    with pytest.raises(HandoffError, match="digest does not match"):
        revoke_handoff(database, manifest, publish(unsigned_digest), receipt, TRANSFER_ID)
    extra = dict(original)
    extra["extraField"] = True
    with pytest.raises(HandoffError, match="unexpected handback document fields"):
        revoke_handoff(database, manifest, redigest(extra), receipt, TRANSFER_ID)
    swapped = dict(original)
    swapped["transferId"] = "other-transfer"
    with pytest.raises(HandoffError, match="does not match the fenced export"):
        revoke_handoff(database, manifest, redigest(swapped), receipt, TRANSFER_ID)
    with pytest.raises(HandoffError, match="does not belong to this transfer"):
        revoke_handoff(database, manifest, handback, receipt, "other-transfer")
    pretty = tmp_path / "pretty.json"
    pretty.write_bytes(json.dumps(original, indent=2).encode("utf-8") + b"\n")
    with pytest.raises(HandoffError, match="not canonical JSON"):
        revoke_handoff(database, manifest, pretty, receipt, TRANSFER_ID)
    assert not receipt.exists()


def test_revocation_refuses_a_source_that_changed_after_fencing(
    fenced: tuple[Path, Path, Path, Path],
) -> None:
    database, manifest, handback, receipt = fenced
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TRIGGER native_control_policy_no_update")
        conn.execute(
            "UPDATE control_policies SET payload_json=? WHERE policy_id='p-1'",
            ('{"enabled": true, "policyId": "p-1", "policyRevision": 2}',),
        )
    with pytest.raises(HandoffError, match="changed after it was fenced"):
        revoke_handoff(database, manifest, handback, receipt, TRANSFER_ID)
    assert not receipt.exists()


def test_two_step_revocation_lifts_the_linked_fence_only_at_the_end(
    fenced: tuple[Path, Path, Path, Path], tmp_path: Path,
) -> None:
    database, manifest, handback, receipt = fenced
    target_manifest = tmp_path / "target-export.json"
    shutil.copyfile(FIXTURES / "python_target_inventory_export_v1.json", target_manifest)
    target_handback = tmp_path / "target-handback.json"
    shutil.copyfile(FIXTURES / "python_target_inventory_handback_v1.json", target_handback)
    target_receipt = tmp_path / "target-receipt.json"

    def linked_triggers() -> set[str]:
        with sqlite3.connect(database) as conn:
            return {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'native_control_fence_%'"
                )
            }

    def authority_write_is_denied() -> bool:
        # A column the checkpoint does not bind, so "allowed" and "denied" can be
        # compared without invalidating the exported checkpoint.
        try:
            with backup_control._connect() as conn:
                conn.execute("UPDATE control_boot_state SET reason = 'handback-check' WHERE id = 1")
        except PythonWriterMechanicallyDeniedError:
            return True
        return False

    # The fixture source is fenced for both domains: six bound tables times three
    # operations. The first revocation must keep the whole linked fence, because
    # the still-held target transfer depends on the authority tip and boot epoch.
    assert len(linked_triggers()) == 18
    revoke_handoff(database, manifest, handback, receipt, TRANSFER_ID)
    assert len(linked_triggers()) == 18
    with sqlite3.connect(database) as conn:
        remaining = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
    assert "native_control_policy_no_update" not in remaining
    assert "native_control_target_no_update" in remaining
    assert authority_write_is_denied()

    # The last revocation restores the source to its exact unfenced state.
    revoke_handoff(database, target_manifest, target_handback, target_receipt, "fixture-target")
    assert linked_triggers() == set()
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM native_control_handoff_fences").fetchone()[0] == 0
        triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
    assert not [name for name in triggers if name.startswith("native_control_fence_")]
    assert not [name for name in triggers if name.startswith("native_control_policy_")]
    assert not [name for name in triggers if name.startswith("native_control_target_")]
    assert "native_control_handoff_no_delete" not in triggers
    # The append-only revocation journal survives the lift, so the transfer
    # history is auditable after the source is writable again.
    assert "native_control_handoff_revocations_no_delete" in triggers
    assert not authority_write_is_denied()
    # Both domains can be exported and re-fenced again.
    third = export_and_fence(
        database, FIXTURES / "python_inventory_checkpoint_v1.json",
        tmp_path / "third-export.json", "policy", "third-transfer",
    )
    assert third["transferId"] == "third-transfer"
    assert len(linked_triggers()) == 18
    assert authority_write_is_denied()


def test_revocation_refuses_an_export_manifest_that_does_not_match(
    fenced: tuple[Path, Path, Path, Path], tmp_path: Path,
) -> None:
    database, manifest, handback, receipt = fenced
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["manifestDigest"] = "0" * 64
    tampered = _publish(tmp_path / "manifest.json", document)
    with pytest.raises(HandoffError, match="manifest digest does not match"):
        revoke_handoff(database, tampered, handback, receipt, TRANSFER_ID)


def test_offline_module_cli_performs_the_verified_handback(
    fenced: tuple[Path, Path, Path, Path],
) -> None:
    database, manifest, handback, receipt = fenced
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.native_control_handoff", "--source-db", str(database),
         "--transfer-id", TRANSFER_ID, "--rollback", "--manifest", str(manifest),
         "--handback", str(handback), "--receipt", str(receipt)],
        cwd=ROOT, capture_output=True, text=True, timeout=180, check=True,
    )
    result = json.loads(completed.stdout)
    document = json.loads(receipt.read_text(encoding="utf-8"))
    assert result["receiptDigest"] == document["receiptDigest"]
    assert result["domain"] == "policy"
    assert backup_control.create_policy({"policyId": "p-cli", "policyRevision": 1})["policyId"] == "p-cli"
    missing = subprocess.run(
        [sys.executable, "-m", "scripts.native_control_handoff", "--source-db", str(database),
         "--transfer-id", TRANSFER_ID, "--rollback", "--handback", str(handback), "--receipt", str(receipt)],
        cwd=ROOT, capture_output=True, text=True, timeout=180, check=False,
    )
    assert missing.returncode == 2 and "--manifest" in missing.stderr
    export_missing = subprocess.run(
        [sys.executable, "-m", "scripts.native_control_handoff", "--source-db", str(database),
         "--transfer-id", TRANSFER_ID, "--output", str(receipt)],
        cwd=ROOT, capture_output=True, text=True, timeout=180, check=False,
    )
    assert export_missing.returncode == 2 and "--checkpoint" in export_missing.stderr
