from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import federation_identity
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SIGNER_PASSPHRASE = b"signer-passphrase16"
PASSPHRASES = {
    "signer": SIGNER_PASSPHRASE,
    "short": b"short",
    "wrong": b"wrong-passphrase-16",
    "nul": b"root-passphrase-16\x00",
}


def test_v20_federation_custody_matches_python_issuance(tmp_path: Path) -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v20/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    bundle_path = tmp_path / "signer.bundle.json"
    bundle_path.write_text(json.dumps(fixture["signer_bundle"]), encoding="utf-8")
    now = datetime.fromisoformat(fixture["now"].replace("Z", "+00:00")).astimezone(timezone.utc)
    signer = federation_identity.load_online_signer(
        bundle_path,
        SIGNER_PASSPHRASE,
        root_identity=fixture["root_identity"],
        now=now,
    )
    assert "private" not in repr(signer).casefold()
    signed = federation_identity.sign_federation_document(
        signer,
        copy.deepcopy(fixture["unsigned_document"]),
        purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )
    assert signed == fixture["signed_document"]
    verified = federation_identity.verify_federation_document(
        signed,
        certificate=fixture["certificate"],
        root_identity=fixture["root_identity"],
        expected_schema="federated-replica-attestation-v1",
        now=now,
        required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )
    assert verified["signature"] == fixture["signed_document"]["signature"]
    for case in fixture["sign_cases"]:
        if case["expected_error"] is None:
            federation_identity.sign_federation_document(
                signer,
                copy.deepcopy(case["document"]),
                purpose=case["purpose"],
            )
            continue
        with pytest.raises(federation_identity.FederationIdentityError) as raised:
            federation_identity.sign_federation_document(
                signer,
                copy.deepcopy(case["document"]),
                purpose=case["purpose"],
            )
        assert raised.value.code == case["expected_error"], case["name"]
    for case in fixture["envelope_cases"]:
        try:
            federation_identity._load_private_key(
                case["envelope"],
                PASSPHRASES[case["passphrase_kind"]],
                binding=case["binding"],
            )
            error = None
        except federation_identity.FederationIdentityError as exc:
            error = exc.code
        assert error == case["expected_error"], case["name"]
