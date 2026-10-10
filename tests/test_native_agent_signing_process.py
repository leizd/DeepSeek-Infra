"""The offline process runner must not give Go custody files or a fake clock."""
import importlib.util
import base64
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_agent_signing_process", ROOT / "scripts/verify_agent_signing_process.py")
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("name", [
    "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE", "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE",
    "DEEPSEEK_WORKER_TLS_KEY_FILE", "DEEPSEEK_WORKER_AUTHORITY_NOW", "AWS_SECRET_ACCESS_KEY", "GH_TOKEN",
    "deepseek_worker_control_signer_bundle_file", "GO_CONTROL_ADDR",
])
def test_go_child_never_inherits_private_material_provider_access_or_fake_clock(name):
    source = {name: "sensitive-or-controlled-clock", "PATH": "tools", "GOCACHE": "shared-go-cache"}
    result = RUNNER.child_environment(source)
    assert name not in result
    assert result == {"PATH": "tools", "GOCACHE": "shared-go-cache"}
    assert source[name] == "sensitive-or-controlled-clock"


def public_binding():
    return {"schema": "native-control-signer-binding-v1", "signerPublicKey": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
            "fleetId": "fleet-a", "environment": "production", "domain": "action", "runtime": "go", "role": "control-plane"}


@pytest.mark.parametrize("change", [
    # Deliberate fake value exercises rejection of private provisioning metadata.
    {"privateKey": "must-never-be-exported"},  # pragma: allowlist secret
    {"signerPublicKey": "not-canonical"},
    {"role": "worker"}, {"fleetId": "foreign-fleet"}, {"signerPublicKey": base64.urlsafe_b64encode(bytes(range(16))).decode()},
])
def test_provisioning_metadata_rejects_private_fields_wrong_scope_and_malformed_key(change):
    with pytest.raises(ValueError):
        RUNNER.validate_public_binding(dict(public_binding(), **change))


def test_valid_public_metadata_contains_only_public_binding():
    assert RUNNER.validate_public_binding(public_binding()) == public_binding()


def test_disposable_tls_chain_authenticates_localhost_and_restricts_leaf_to_server(tmp_path):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca_path, cert_path, key_path = RUNNER.create_tls(tmp_path)
    ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    certificate.verify_directly_issued_by(ca)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == ["localhost"]
    assert list(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert key_path.parent == tmp_path
