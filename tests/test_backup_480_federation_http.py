from __future__ import annotations

import io
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from deepseek_infra import federation_app as federation_process
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import federation_node
from deepseek_infra.web import federation_app as federation_http
from deepseek_infra.web.federation_app import create_federation_app


class _Node:
    def __init__(self) -> None:
        self.component = b""

    def health(self) -> dict[str, Any]:
        return {"schema": "federation-node-health-v1", "fleetId": "fleet-b", "pid": 4810}

    def issue_readiness(self) -> dict[str, Any]:
        return {"schema": "federation-readiness-attestation-v1", "fleetId": "fleet-b"}

    def issue_challenge(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "challenge", **payload}

    def verify_challenge(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "challenge-verified", **payload}

    def verify_readiness(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "readiness-verified", **payload}

    def propose_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "proposed", **payload}

    def verify_ingress_grant(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "grant-verified", **payload}

    def mark_remote_verifying(self, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "remote-verifying", "transferId": transfer_id, **payload}

    def verify_replica_attestation(self, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "replica-verified", "transferId": transfer_id, **payload}

    def run_dr_drill(self, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "dr-run", "transferId": transfer_id, **payload}

    def verify_dr_attestation(self, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "dr-verified", "transferId": transfer_id, **payload}

    def respond_challenge(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "response", **payload}

    def issue_ingress_grant(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "grant", **payload}

    def declare_replica(self, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "declared", "transferId": transfer_id, **payload}

    def expected_component_size(self, transfer_id: str, component_digest: str, grant_id: str) -> int:
        assert transfer_id == "sha256:" + "a" * 64
        assert component_digest == "b" * 64
        assert grant_id == "grant-480"
        return 8

    def receive_component(
        self,
        transfer_id: str,
        component_digest: str,
        *,
        grant_id: str,
        write_id: str,
        content: BinaryIO,
    ) -> dict[str, Any]:
        self.component = content.read()
        return {
            "kind": "component",
            "transferId": transfer_id,
            "ciphertextDigest": component_digest,
            "grantId": grant_id,
            "writeId": write_id,
        }

    def reconcile_transfer(self, transfer_id: str, grant_id: str) -> dict[str, Any]:
        return {"kind": "reconciled", "transferId": transfer_id, "grantId": grant_id}

    def commit_replica(self, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "committed", "transferId": transfer_id, **payload}


class _Stdin:
    def __init__(self, content: bytes) -> None:
        self.buffer = io.BytesIO(content)


class _DomainError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _client(node: _Node, *, token: str = "operator-token-480") -> TestClient:
    return TestClient(
        create_federation_app(node=node, operator_token=token),
        base_url="http://127.0.0.1",
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
    )


def test_peer_routes_stream_exact_component_and_never_require_operator_token() -> None:
    node = _Node()
    client = _client(node)
    transfer_id = "sha256:" + "a" * 64
    digest = "b" * 64

    assert client.get("/federation/v1/health").json()["fleetId"] == "fleet-b"
    assert client.post("/federation/v1/peer/readiness", json={}).status_code == 200
    assert client.post("/federation/v1/peer/challenges/respond", json={"challenge": {"nonce": "n"}}).json()["kind"] == "response"
    assert client.post("/federation/v1/peer/ingress-grants", json={"transferId": transfer_id}).json()["kind"] == "grant"
    assert client.post(
        f"/federation/v1/peer/transfers/{transfer_id}/declaration",
        json={"grantId": "grant-480", "sourceReceipt": {"schemaVersion": 4}},
    ).json()["kind"] == "declared"

    uploaded = client.put(
        f"/federation/v1/peer/transfers/{transfer_id}/components/{digest}",
        params={"grantId": "grant-480", "writeId": "write-480"},
        content=b"age-data",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["writeId"] == "write-480"
    assert node.component == b"age-data"
    assert client.get(
        f"/federation/v1/peer/transfers/{transfer_id}",
        params={"grantId": "grant-480"},
    ).json()["kind"] == "reconciled"
    assert client.post(
        f"/federation/v1/peer/transfers/{transfer_id}/commit",
        json={"grantId": "grant-480"},
    ).json()["kind"] == "committed"


def test_operator_routes_are_loopback_and_constant_token_protected() -> None:
    node = _Node()
    client = _client(node)
    transfer_id = "sha256:" + "a" * 64
    routes: tuple[tuple[str, dict[str, object]], ...] = (
        ("/federation/v1/operator/challenges", {"destinationFleetId": "fleet-b"}),
        ("/federation/v1/operator/challenges/verify", {"challenge": {}, "response": {}}),
        ("/federation/v1/operator/readiness/verify", {"attestation": {}}),
        ("/federation/v1/operator/transfers", {"sourceReceipt": {}}),
        ("/federation/v1/operator/ingress-grants/verify", {"grant": {}}),
        (f"/federation/v1/operator/transfers/{transfer_id}/remote-verifying", {}),
        (f"/federation/v1/operator/transfers/{transfer_id}/replica-attestations/verify", {}),
        (f"/federation/v1/operator/transfers/{transfer_id}/dr-drills", {}),
        (f"/federation/v1/operator/transfers/{transfer_id}/dr-attestations/verify", {}),
    )
    for route, payload in routes:
        assert client.post(route, json=payload).status_code == 401
        accepted = client.post(route, json=payload, headers={"X-Federation-Operator-Token": "operator-token-480"})
        assert accepted.status_code == 200, (route, accepted.text)

    remote = TestClient(
        create_federation_app(node=node, operator_token="operator-token-480"),
        base_url="http://198.51.100.2",
        raise_server_exceptions=False,
        client=("198.51.100.2", 50000),
    )
    rejected = remote.post(
        "/federation/v1/operator/challenges",
        json={},
        headers={"X-Federation-Operator-Token": "operator-token-480"},
    )
    assert rejected.status_code == 403


def test_component_transport_fails_closed_before_node_write() -> None:
    node = _Node()
    client = _client(node)
    transfer_id = "sha256:" + "a" * 64
    digest = "b" * 64
    endpoint = f"/federation/v1/peer/transfers/{transfer_id}/components/{digest}"

    short = client.put(
        endpoint,
        params={"grantId": "grant-480", "writeId": "write-480"},
        content=io.BytesIO(b"short").read(),
    )
    assert short.status_code == 409
    assert short.json()["code"] == "FEDERATION_COMPONENT_LENGTH_MISMATCH"
    assert node.component == b""


def test_process_bind_requires_explicit_non_loopback_opt_in() -> None:
    federation_process._validate_bind("127.0.0.1", allow_non_loopback=False)
    with pytest.raises(federation_node.FederationNodeError) as rejected:
        federation_process._validate_bind("0.0.0.0", allow_non_loopback=False)
    assert rejected.value.code == "FEDERATION_NODE_NON_LOOPBACK_REQUIRES_OPT_IN"
    federation_process._validate_bind("0.0.0.0", allow_non_loopback=True)


def test_process_input_and_argument_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(federation_node.FederationNodeError) as invalid_host:
        federation_process._validate_bind("not-an-ip", allow_non_loopback=True)
    assert invalid_host.value.code == "FEDERATION_NODE_BIND_HOST_INVALID"
    assert federation_process._read_recovery_identity(False) is None

    monkeypatch.setattr(federation_process.sys, "stdin", _Stdin(b"recovery-identity\r\n"))
    assert federation_process._read_recovery_identity(True) == bytearray(b"recovery-identity")
    for content in (b"", b"x" * (federation_process.MAX_RECOVERY_IDENTITY_BYTES + 1)):
        monkeypatch.setattr(federation_process.sys, "stdin", _Stdin(content))
        with pytest.raises(federation_node.FederationNodeError) as missing_identity:
            federation_process._read_recovery_identity(True)
        assert missing_identity.value.code == "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED"

    config_path = tmp_path / "node.json"
    with pytest.raises(federation_node.FederationNodeError) as invalid_port:
        federation_process.main(["--config", str(config_path), "--port", "0"])
    assert invalid_port.value.code == "FEDERATION_NODE_PORT_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as invalid_tls:
        federation_process.main(
            ["--config", str(config_path), "--port", "8448", "--ssl-certfile", str(tmp_path / "cert.pem")]
        )
    assert invalid_tls.value.code == "FEDERATION_NODE_TLS_CONFIG_INVALID"

    monkeypatch.setenv(federation_process.OPERATOR_TOKEN_ENV, "short")
    monkeypatch.setenv(federation_process.SIGNER_PASSPHRASE_ENV, "s" * 20)
    with pytest.raises(federation_node.FederationNodeError) as invalid_token:
        federation_process.main(["--config", str(config_path), "--port", "8448"])
    assert invalid_token.value.code == "FEDERATION_OPERATOR_TOKEN_INVALID"
    monkeypatch.setenv(federation_process.OPERATOR_TOKEN_ENV, "operator-token-480")
    monkeypatch.delenv(federation_process.SIGNER_PASSPHRASE_ENV, raising=False)
    with pytest.raises(federation_node.FederationNodeError) as missing_signer:
        federation_process.main(["--config", str(config_path), "--port", "8448"])
    assert missing_signer.value.code == "FEDERATION_SIGNER_PASSPHRASE_REQUIRED"


def test_process_main_loads_and_zeroizes_scoped_identity_material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "node.json"
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    node = object()
    app = object()
    captured: dict[str, Any] = {}

    monkeypatch.setenv(federation_process.OPERATOR_TOKEN_ENV, "operator-token-480")
    monkeypatch.setenv(federation_process.SIGNER_PASSPHRASE_ENV, "s" * 20)
    monkeypatch.setattr(federation_process.sys, "stdin", _Stdin(b"recovery-identity\n"))

    def _load_node(
        path: Any,
        *,
        signer_passphrase: bytearray,
        recovery_age_identity: bytearray | None,
    ) -> object:
        captured["path"] = path
        captured["signerBefore"] = bytes(signer_passphrase)
        captured["recoveryBefore"] = None if recovery_age_identity is None else bytes(recovery_age_identity)
        captured["signerRef"] = signer_passphrase
        captured["recoveryRef"] = recovery_age_identity
        return node

    def _create_app(*, node: object, operator_token: str) -> object:
        captured["appNode"] = node
        captured["operatorToken"] = operator_token
        return app

    def _run_uvicorn(candidate: object, **kwargs: Any) -> None:
        captured["uvicornApp"] = candidate
        captured["uvicorn"] = kwargs

    monkeypatch.setattr(federation_process.federation_node, "load_federation_node", _load_node)
    monkeypatch.setattr(federation_process, "create_federation_app", _create_app)
    monkeypatch.setattr(federation_process.uvicorn, "run", _run_uvicorn)

    assert federation_process.main(
        [
            "--config",
            str(config_path),
            "--host",
            "0.0.0.0",
            "--port",
            "8448",
            "--allow-non-loopback",
            "--recovery-identity-stdin",
            "--ssl-certfile",
            str(cert_path),
            "--ssl-keyfile",
            str(key_path),
        ]
    ) == 0

    assert captured["path"] == config_path
    assert captured["signerBefore"] == b"s" * 20
    assert captured["recoveryBefore"] == b"recovery-identity"
    assert captured["signerRef"] == bytearray(20)
    assert captured["recoveryRef"] == bytearray(len(b"recovery-identity"))
    assert captured["appNode"] is node
    assert captured["operatorToken"] == "operator-token-480"
    assert captured["uvicornApp"] is app
    assert captured["uvicorn"] == {
        "host": "0.0.0.0",
        "port": 8448,
        "log_level": "info",
        "access_log": False,
        "ssl_certfile": str(cert_path),
        "ssl_keyfile": str(key_path),
    }
    assert federation_process.SIGNER_PASSPHRASE_ENV not in federation_process.os.environ


@pytest.mark.parametrize(
    ("code", "status"),
    (
        ("FEDERATION_OPERATOR_AUTH_REQUIRED", 401),
        ("FEDERATION_PEER_REVOKED", 403),
        ("FEDERATION_PEER_NOT_ACTIVE", 403),
        ("FEDERATION_TRANSFER_NOT_FOUND", 404),
        ("FEDERATION_COMPONENT_TOO_LARGE", 413),
        ("FEDERATION_TRANSFER_IDENTITY_CONFLICT", 409),
    ),
)
def test_http_domain_errors_have_stable_status(code: str, status: int) -> None:
    assert federation_http._domain_status(code) == status


def test_http_loopback_and_error_handlers_fail_closed() -> None:
    no_client_request = Request({"type": "http", "client": None})
    invalid_client_request = Request({"type": "http", "client": ("not-an-ip", 50000)})
    assert federation_http._is_loopback(no_client_request) is False
    assert federation_http._is_loopback(invalid_client_request) is False
    with pytest.raises(ValueError):
        create_federation_app(node=_Node(), operator_token="short")

    cases: tuple[tuple[Exception, int, str | None], ...] = (
        (AppError("invalid", status=422), 422, None),
        (_DomainError("FEDERATION_TRANSFER_NOT_FOUND"), 404, "FEDERATION_TRANSFER_NOT_FOUND"),
        (RuntimeError("internal detail must not escape"), 500, ErrorCode.INTERNAL.value),
    )
    for error, expected_status, expected_code in cases:
        node = _Node()

        def _fail(error: Exception = error) -> dict[str, Any]:
            raise error

        node.health = _fail  # type: ignore[method-assign]
        response = _client(node).get("/federation/v1/health")
        assert response.status_code == expected_status
        if expected_code is not None:
            assert response.json()["code"] == expected_code


def test_component_transport_rejects_invalid_size_and_stream_bounds() -> None:
    transfer_id = "sha256:" + "a" * 64
    digest = "b" * 64
    endpoint = f"/federation/v1/peer/transfers/{transfer_id}/components/{digest}"

    for expected_size in (0, federation_http.MAX_FEDERATION_COMPONENT_BYTES + 1):
        node = _Node()
        node.expected_component_size = lambda *_args, size=expected_size: size  # type: ignore[method-assign]
        rejected = _client(node).put(endpoint, params={"grantId": "grant-480", "writeId": "write-480"}, content=b"")
        assert rejected.status_code == 413
        assert rejected.json()["code"] == "FEDERATION_COMPONENT_TOO_LARGE"

    for content in (b"abc", b"abcdef"):
        node = _Node()
        node.expected_component_size = lambda *_args: 5  # type: ignore[method-assign]
        rejected = _client(node).put(
            endpoint,
            params={"grantId": "grant-480", "writeId": "write-480"},
            content=content,
            headers={"Content-Length": "5"},
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "FEDERATION_COMPONENT_LENGTH_MISMATCH"
        assert node.component == b""
