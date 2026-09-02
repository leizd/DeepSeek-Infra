from __future__ import annotations

import json
from pathlib import Path

from deepseek_infra.infra.mcp.protocol_preparation import prepare_mcp_protocol_json
from deepseek_infra.infra.workspace.federated_replica_attestation import REPLICA_ATTESTATION_FIELDS
from deepseek_infra.infra.workspace.federated_replica_commit import COMMIT_V4_FIELDS, RECEIPT_V4_FIELDS
from scripts import check_mcp_protocol_parity as mcp_parity
from scripts.native_runtime_contract import sha256_file, validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def test_corpus_hash_is_stable_across_crlf_checkouts(tmp_path: Path) -> None:
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{"ok":true}\n')
    crlf.write_bytes(b'{"ok":true}\r\n')
    assert sha256_file(lf) == sha256_file(crlf)


def test_canonical_corpora_match_frozen_digests() -> None:
    manifest = validate_corpus()
    ids = {item["id"] for item in manifest["corpora"]}
    assert {
        "mcp-protocol-preparation",
        "gateway-request-preparation",
        "rag-parity",
        "storage-wire-inventory",
        "federation-wire-inventory",
        "evidence-envelope",
        "state-legal-transitions",
        "http-rest-inventory",
    } <= ids


def test_storage_inventory_matches_python_4_8_0_field_sets() -> None:
    inventory = validate_corpus()
    path = next(item["path"] for item in inventory["corpora"] if item["id"] == "storage-wire-inventory")
    data = (ROOT / path).read_text(encoding="utf-8")
    assert "object-set-v1" in data
    stored = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert set(stored["receipt_v4_fields"]) == set(RECEIPT_V4_FIELDS)
    assert set(stored["commit_v4_fields"]) == set(COMMIT_V4_FIELDS)


def test_federation_inventory_matches_python_attestation_fields() -> None:
    path = ROOT / "compat/native-runtime/v1/federation/wire_inventory.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored["replica_attestation_fields"]) == set(REPLICA_ATTESTATION_FIELDS)


def test_python_mcp_oracle_still_replays_canonical_fixture() -> None:
    case = next(item for item in mcp_parity.load_fixture() if item["name"] == "ping_integer_id")
    result = prepare_mcp_protocol_json(mcp_parity.raw_case(case))
    assert mcp_parity._matches_expectation(result, case["expect"])
