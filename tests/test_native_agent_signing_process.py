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


def test_dependent_critic_request_requires_scope_summary_and_zero_tool_capability():
    result = {"actionId": "action-critic", "executionEpoch": 3, "dependentOperationId": "operation-critic"}
    exchange = {"actionId": "action-critic", "executionEpoch": "3", "operationId": "operation-critic", "model": "deepseek-v4-pro",
                "toolsPresent": False, "dependentTaskInPrompt": True, "predecessorSummaryInPrompt": True}
    RUNNER.validate_dependent_task_request(exchange, result)


@pytest.mark.parametrize("key,value", [
    ("actionId", "other-action"), ("executionEpoch", "2"), ("operationId", "other-operation"), ("model", "other-model"),
    ("toolsPresent", True), ("toolsPresent", None), ("toolsPresent", 0), ("dependentTaskInPrompt", False), ("predecessorSummaryInPrompt", False),
])
def test_dependent_critic_request_rejects_rebound_scope_missing_summary_and_extra_tools(key, value):
    result = {"actionId": "action-critic", "executionEpoch": 3, "dependentOperationId": "operation-critic"}
    exchange = {"actionId": "action-critic", "executionEpoch": "3", "operationId": "operation-critic", "model": "deepseek-v4-pro",
                "toolsPresent": False, "dependentTaskInPrompt": True, "predecessorSummaryInPrompt": True}
    exchange[key] = value
    with pytest.raises(ValueError, match="verified scope"):
        RUNNER.validate_dependent_task_request(exchange, result)


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


def synthesis_exchange():
    result = {"actionId": "task-action", "synthesisActionId": "own-synthesis-action", "synthesisExecutionEpoch": 4, "synthesisOperationId": "own-synthesis-operation"}
    exchange = {"actionId": "own-synthesis-action", "executionEpoch": "4", "operationId": "own-synthesis-operation", "model": "deepseek-v4-pro",
                "toolsPresent": False, "synthesisQueryInPrompt": True, "synthesisCoderInPrompt": True, "synthesisCriticInPrompt": True,
                "predecessorSummaryCount": 2, "privateReasoningInPrompt": False}
    return exchange, result


def test_synthesis_request_requires_own_fence_and_all_verified_predecessors():
    exchange, result = synthesis_exchange()
    RUNNER.validate_synthesis_request(exchange, result)


@pytest.mark.parametrize("key,value", [
    ("actionId", "task-action"), ("actionId", "other"), ("executionEpoch", "3"), ("operationId", "foreign"), ("model", "foreign"),
    ("toolsPresent", True), ("toolsPresent", None), ("synthesisQueryInPrompt", False), ("synthesisCoderInPrompt", False),
    ("synthesisCriticInPrompt", False), ("predecessorSummaryCount", 1), ("predecessorSummaryCount", 2.0), ("privateReasoningInPrompt", True),
])
def test_synthesis_request_denies_rebound_scope_missing_input_private_reasoning_and_tools(key, value):
    exchange, result = synthesis_exchange()
    exchange[key] = value
    with pytest.raises(ValueError, match="own verified scope"):
        RUNNER.validate_synthesis_request(exchange, result)


@pytest.mark.parametrize("family", ["planner", "task", "synthesis"])
def test_each_explicit_native_family_reaches_rust_provisioning_boundary_without_local_execution(tmp_path, monkeypatch, family):
    calls = []

    def stop_before_execution(command, **kwargs):
        calls.append(command)
        assert command == [(tmp_path / "provisioner").as_posix()]
        assert kwargs["env"]["DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE"] == (tmp_path / f"rust-{family}-custody/bundle.json").as_posix()
        raise RuntimeError("expected isolated provisioning boundary")

    monkeypatch.setattr(RUNNER.subprocess, "run", stop_before_execution)
    with pytest.raises(RuntimeError, match="expected isolated provisioning boundary"):
        RUNNER.planner_process_cases(tmp_path / "producer", {}, tmp_path / "worker", tmp_path / "provisioner", tmp_path, tmp_path, family)
    assert len(calls) == 1


def test_unknown_native_family_is_denied_before_custody_or_process_creation(tmp_path, monkeypatch):
    def forbidden_execution(*args, **kwargs):
        pytest.fail("unknown native family reached process execution")

    monkeypatch.setattr(RUNNER.subprocess, "run", forbidden_execution)
    with pytest.raises(ValueError, match="Unknown producer family"):
        RUNNER.planner_process_cases(tmp_path / "producer", {}, tmp_path / "worker", tmp_path / "provisioner", tmp_path, tmp_path, "unknown")
    assert list(tmp_path.iterdir()) == []


def grant_refresh_result():
    return {"grantRefreshAfterExpiryQualified": True, "grantRefreshReplayed": True, "expiredTaskGrantReexecutionDenied": True,
            "originalTaskGrantExpiresAt": "2026-10-13T02:00:00Z", "refreshedTaskGrantIssuedAt": "2026-10-13T02:00:01Z",
            "refreshedTaskGrantExpiresAt": "2026-10-13T02:01:01Z", "refreshedTaskGrantSha256": "a" * 64, "refreshedTaskGrantLength": 1700}


def test_real_grant_refresh_requires_expired_original_immutable_replay_and_custody():
    RUNNER.validate_agent_grant_refresh(grant_refresh_result())


@pytest.mark.parametrize("field,value", [
    ("grantRefreshAfterExpiryQualified", False), ("grantRefreshReplayed", 1), ("expiredTaskGrantReexecutionDenied", False),
    ("originalTaskGrantExpiresAt", "2026-10-13T02:02:00Z"), ("refreshedTaskGrantIssuedAt", "2026-10-13T02:00:00Z"),
    ("refreshedTaskGrantIssuedAt", "invalidZ"), ("refreshedTaskGrantIssuedAt", None),
    ("refreshedTaskGrantExpiresAt", "2026-10-13T02:10:01Z"), ("refreshedTaskGrantExpiresAt", "2026-10-13T02:01:01+00:00"),
    ("refreshedTaskGrantSha256", "g" * 64), ("refreshedTaskGrantLength", True), ("refreshedTaskGrantLength", 0),
])
def test_grant_refresh_rejects_missing_observation_false_dates_and_fabricated_custody(field, value):
    result = grant_refresh_result()
    result[field] = value
    with pytest.raises(ValueError):
        RUNNER.validate_agent_grant_refresh(result)
