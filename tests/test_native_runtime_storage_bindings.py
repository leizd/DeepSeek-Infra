from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_storage_receipt_v4_and_commit_v4_match_wire_inventory() -> None:
    inv_path = ROOT / "compat" / "native-runtime" / "v1" / "storage" / "wire_inventory.json"
    inv = json.loads(inv_path.read_text(encoding="utf-8"))

    receipt_rs = (ROOT / "rust" / "crates" / "deepseek-storage" / "src" / "receipt.rs").read_text(encoding="utf-8")
    for field in inv["receipt_v4_fields"]:
        # Convert camelCase to snake_case check
        # e.g. backupId -> backup_id, creationVerified -> creation_verified
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in field).lstrip("_")
        assert f"pub {snake}:" in receipt_rs, f"Field {field} (snake: {snake}) missing from ReceiptV4 in Rust"

    for field in inv["commit_v4_fields"]:
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in field).lstrip("_")
        assert f"pub {snake}:" in receipt_rs, f"Field {field} (snake: {snake}) missing from CommitV4 in Rust"


def test_storage_object_set_matches_schema() -> None:
    inv_path = ROOT / "compat" / "native-runtime" / "v1" / "storage" / "wire_inventory.json"
    inv = json.loads(inv_path.read_text(encoding="utf-8"))

    obj_rs = (ROOT / "rust" / "crates" / "deepseek-storage" / "src" / "object_set.rs").read_text(encoding="utf-8")
    assert f'"{inv["object_set"]}"' in obj_rs


def test_federation_attestation_matches_wire_inventory() -> None:
    inv_path = ROOT / "compat" / "native-runtime" / "v1" / "federation" / "wire_inventory.json"
    inv = json.loads(inv_path.read_text(encoding="utf-8"))

    att_rs = (ROOT / "rust" / "crates" / "deepseek-federation" / "src" / "attestation.rs").read_text(encoding="utf-8")
    for field in inv["replica_attestation_fields"]:
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in field).lstrip("_")
        assert f"pub {snake}:" in att_rs, f"Field {field} (snake: {snake}) missing from ReplicaAttestation in Rust"

    for field in inv["pinned_failure_domain_metadata"]:
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in field).lstrip("_")
        assert f"pub {snake}:" in att_rs, f"Field {field} (snake: {snake}) missing from FailureDomainMetadata in Rust"


def test_evidence_envelope_matches_frozen_proof_types() -> None:
    env_path = ROOT / "compat" / "native-runtime" / "v1" / "evidence" / "envelope.json"
    env = json.loads(env_path.read_text(encoding="utf-8"))

    proof_rs = (ROOT / "rust" / "crates" / "deepseek-proof" / "src" / "envelope.rs").read_text(encoding="utf-8")
    assert f'"{env["envelope"]}"' in proof_rs
    for pt in env["frozen_proof_types"]:
        assert f'"{pt}"' in proof_rs
