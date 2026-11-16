from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.native_runtime.authority import (
    RUST_DATA_DOMAINS,
    PythonWriterMechanicallyDeniedError,
)
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_dr_ledger,
    backup_scrub,
    backup_writer_lease,
    backups,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_LIB = REPO_ROOT / "rust/crates/deepseek-gateway/src/lib.rs"
RUST_WRITE_ROUTES = REPO_ROOT / "rust/crates/deepseek-gateway/src/backup_catalog_write_routes.rs"
RUST_UNLOCK_MOD = REPO_ROOT / "rust/crates/deepseek-policy/src/backup_unlock.rs"


def _seed_legacy_receipt(root: Path, backup_id: str, cipher_bytes: bytes, manifest_digest: str) -> Path:
    backup_file = root / "backups" / f"{backup_id}.dsibackup.age"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.write_bytes(cipher_bytes)
    backup_catalog.append_receipt(
        root,
        {
            "backupId": backup_id,
            "filename": backup_file.name,
            "policyId": "pol-parity",
            "createdAt": "2026-10-10T00:00:00Z",
            "size": len(cipher_bytes),
            "sizeBytes": len(cipher_bytes),
            "ciphertextSha256": hashlib.sha256(cipher_bytes).hexdigest(),
            "manifestDigest": manifest_digest,
        },
    )
    return backup_file


def test_python_catalog_pin_scrub_and_unlock_writers_denied_under_python_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    root = backups.BACKUP_DIR
    _seed_legacy_receipt(root, "bak-deny-1", b"age-encryption.org/v1\n-> dummy\n", "a" * 64)

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    assert "backup_catalog_store" in RUST_DATA_DOMAINS
    assert "backup_dr_ledger_store" in RUST_DATA_DOMAINS

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_catalog_store"):
        backup_catalog.pin_backup(root, "bak-deny-1", True)

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_catalog_store"):
        backup_catalog.record_scrub(root, "bak-deny-1", ok=True)

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_catalog_store"):
        backup_catalog.record_unlock_verification(root, "bak-deny-1")

    with pytest.raises(PythonWriterMechanicallyDeniedError, match="backup_dr_ledger_store"):
        backup_dr_ledger.record_scrub_evidence(
            target_id="managed-local",
            backup_id="bak-deny-1",
            observed_at="2026-10-10T00:00:00Z",
            result="success",
        )


def test_python_catalog_event_and_jsonl_wire_layout_parity(tmp_settings: Path) -> None:
    root = backups.BACKUP_DIR
    _seed_legacy_receipt(root, "bak-wire-1", b"cipher", "b" * 64)
    lease = backup_writer_lease.TargetWriterLease(
        root,
        target_id="managed-local",
        owner_run_id="adhoc-1234",
        owner_instance_id="inst-1",
        fencing_token=9,
    )
    with lease:
        entry = backup_catalog.pin_backup(root, "bak-wire-1", True, writer=lease)

    # Event file is indent=2 + sort_keys=True + trailing LF; catalog line uses default separators + sort_keys=True + LF.
    digest = str(entry["entryHash"])
    event_file = root / "events" / digest[:2] / f"{digest}.json"
    assert event_file.is_file()
    raw_event = event_file.read_text(encoding="utf-8")
    assert raw_event == json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    lines = (root / "catalog" / "catalog-v1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[1] == json.dumps(entry, ensure_ascii=False, sort_keys=True)
    assert backup_catalog.verify_chain(root)
    assert not lease.path.exists()


def test_python_scrub_report_and_dr_evidence_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_settings: Path,
) -> None:
    root = backups.BACKUP_DIR
    payload = b"age-encryption.org/v1\n-> X25519 stub\n--- deadbeef\n"
    backup_file = _seed_legacy_receipt(root, "bak-scrub-1", payload, "c" * 64)
    monkeypatch.setattr(backup_scrub.backup_crypto, "inspect_header", lambda _p: {"age": True, "passphrase": False})

    report: dict[str, Any] = backup_scrub.scrub_backup(root, "bak-scrub-1")
    assert report["backupId"] == "bak-scrub-1"
    assert report["ok"] is True
    assert report["objectsScrubbed"] == 1
    assert report["checks"] == {
        "exists": "PASS",
        "not-symlink": "PASS",
        "size": "PASS",
        "sha256": "PASS",
        "age-header": "PASS",
    }

    backup_file.write_bytes(b"corrupted")
    bad_report = backup_scrub.scrub_backup(root, "bak-scrub-1")
    assert bad_report["ok"] is False
    assert bad_report["checks"]["size"].startswith("FAIL:")
    assert bad_report["checks"]["sha256"].startswith("FAIL:")


def test_rust_s2_write_routes_and_declared_domains_alignment() -> None:
    lib_source = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    assert '"backup_catalog_store"' in lib_source
    assert '"backup_dr_ledger_store"' in lib_source

    route_source = RUST_WRITE_ROUTES.read_text(encoding="utf-8")
    for required in (
        "/api/workspace/backup-catalog/:backup_id/pin",
        "/api/workspace/backups/:backup_id/scrub",
        "/api/workspace/backups/:backup_id/verify-unlock",
        "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED",
        "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED",
        "crate::backup_commit::target_generation",
        "scrub_evidence",
    ):
        assert required in route_source

    unlock_source = RUST_UNLOCK_MOD.read_text(encoding="utf-8")
    for required in (
        "backup_crypto::decrypt_identity",
        "validate_frontend_envelope",
        "verify_control_metadata",
        "extract_component_archive",
        "verify_manifest_tree",
        "scrub_plaintext_file",
    ):
        assert required in unlock_source
