from __future__ import annotations

import io
from typing import Any, BinaryIO

import pytest
from fastapi.testclient import TestClient

from deepseek_infra import federation_app as federation_process
from deepseek_infra.infra.workspace import federation_node
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
