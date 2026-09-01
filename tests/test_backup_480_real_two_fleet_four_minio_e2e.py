"""Real two-process, four-MinIO Signed Federation and Federated DR Evidence."""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_mirror,
    backup_object_set,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_targets,
    evidence_proof,
    federated_dr_proof,
    federated_durability,
    federated_replica_proof,
    federation_custody_capability,
    federation_identity,
    federation_peer_trust,
    federation_readiness_attestation,
    federation_replica_receiver,
    federation_runtime_proof,
    federation_transfer_journal,
    federation_trust_proof,
    resilience_federation_readiness,
)
from deepseek_infra.infra.workspace.backup_target_store import object_key
from tests.test_backup_458_real_storage_control_plane_e2e import (
    _claim_and_run,
    _client as s3_client,
    _envelope,
    _register_target,
    _seed_workspace,
)

UTC = timezone.utc
SCENARIO = "real-two-fleet-four-minio-signed-federation"
TRUST_PROOF_SCENARIO = f"{SCENARIO}-trust"
REPLICA_PROOF_SCENARIO = f"{SCENARIO}-replica"
DR_PROOF_SCENARIO = f"{SCENARIO}-dr"
FEDERATION_MINIO_SUFFIXES = ("A", "B", "D", "E")
ENDPOINT_NAMES = tuple(f"DEEPSEEK_TEST_S3_ENDPOINT_{suffix}" for suffix in FEDERATION_MINIO_SUFFIXES)
CONTAINER_NAMES = tuple(f"DEEPSEEK_TEST_MINIO_CONTAINER_{suffix}" for suffix in FEDERATION_MINIO_SUFFIXES)
ROOT = Path(__file__).resolve().parents[1]
STORAGE_CREDENTIAL_ENV_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "FEDERATION_MINIO_ROOT_USER",
    "FEDERATION_MINIO_ROOT_PASSWORD",
    "DEEPSEEK_TEST_FEDERATION_ACCESS_KEY_ID",
    "DEEPSEEK_TEST_FEDERATION_SECRET_ACCESS_KEY",
)


def _typed_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _scoped_storage_environment(*, access_key: str, secret_key: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in STORAGE_CREDENTIAL_ENV_NAMES:
        environment.pop(name, None)
    environment.update(
        {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return environment


def test_federation_process_storage_environment_contains_only_scoped_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index, name in enumerate(STORAGE_CREDENTIAL_ENV_NAMES):
        monkeypatch.setenv(name, f"inherited-{index}")

    environment = _scoped_storage_environment(access_key="fleet-a-principal", secret_key="fleet-a-secret")

    assert environment["AWS_ACCESS_KEY_ID"] == "fleet-a-principal"
    assert environment["AWS_SECRET_ACCESS_KEY"] == "fleet-a-secret"  # pragma: allowlist secret
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
    allowed = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert all(name in allowed or name not in environment for name in STORAGE_CREDENTIAL_ENV_NAMES)
    assert "FEDERATION_MINIO_ROOT_PASSWORD" not in environment
    assert "DEEPSEEK_TEST_FEDERATION_SECRET_ACCESS_KEY" not in environment
    assert "AWS_SESSION_TOKEN" not in environment


def _metadata(*, region: str) -> dict[str, str]:
    return {
        "provider": "minio",
        "region": region,
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _readiness_snapshot(fleet_id: str, generated_at: datetime) -> dict[str, Any]:
    return resilience_federation_readiness.build_federation_snapshot(
        fleet_id=fleet_id,
        wire_compatibility=["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
        available_failure_domains=[f"{fleet_id}-site-1", f"{fleet_id}-site-2"],
        forecast_headroom=10 * 1024 * 1024 * 1024,
        cost_class="offsite",
        readiness="READY",
        now=generated_at,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request_json(
    port: int,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    operator_token: str | None = None,
    method: str = "POST",
    expected_status: int = 200,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if operator_token is not None:
        headers["X-Federation-Operator-Token"] = operator_token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read()
    document = json.loads(body.decode("utf-8")) if body else {}
    assert status == expected_status, {"path": path, "status": status, "body": document}
    assert isinstance(document, dict)
    return document


def _put_component(
    port: int,
    transfer_id: str,
    component_digest: str,
    *,
    grant_id: str,
    write_id: str,
    content: bytes,
    expected_status: int = 200,
) -> dict[str, Any]:
    query = urllib.parse.urlencode({"grantId": grant_id, "writeId": write_id})
    path = f"/federation/v1/peer/transfers/{transfer_id}/components/{component_digest}?{query}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=content,
        headers={"Content-Type": "application/octet-stream"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            status = int(response.status)
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read()
    document = json.loads(body.decode("utf-8"))
    assert status == expected_status, {"status": status, "body": document}
    return document


def _slow_upload(
    port: int,
    transfer_id: str,
    component_digest: str,
    *,
    grant_id: str,
    write_id: str,
    content: bytes,
    first_chunk_sent: threading.Event,
    outcome: dict[str, Any],
) -> None:
    query = urllib.parse.urlencode({"grantId": grant_id, "writeId": write_id})
    path = f"/federation/v1/peer/transfers/{transfer_id}/components/{component_digest}?{query}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = 0
    try:
        connection.putrequest("PUT", path)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(len(content)))
        connection.endheaders()
        for offset in range(0, len(content), 64 * 1024):
            chunk = content[offset : offset + 64 * 1024]
            connection.send(chunk)
            sent += len(chunk)
            first_chunk_sent.set()
            time.sleep(0.025)
        response = connection.getresponse()
        outcome.update({"status": response.status, "body": response.read().decode("utf-8"), "bytesSent": sent})
    except (OSError, http.client.HTTPException) as exc:
        outcome.update({"error": type(exc).__name__, "bytesSent": sent})
    finally:
        connection.close()


def _wait_for_node(port: int, *, process: subprocess.Popen[bytes], timeout: float = 45.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"federation node exited before health: {process.returncode}")
        try:
            return _request_json(port, "/federation/v1/health", method="GET", payload=None, timeout=2)
        except (OSError, ValueError, AssertionError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise AssertionError(f"federation node health timeout: {last_error}")


@dataclass
class _NodeProcess:
    config_path: Path
    root: Path
    port: int
    operator_token: str
    signer_passphrase: str
    storage_access_key: str
    storage_secret_key: str
    log_path: Path
    recovery_identity: str | None = None
    process: subprocess.Popen[bytes] | None = None
    log_handle: Any = None

    def start(self) -> dict[str, Any]:
        assert self.process is None or self.process.poll() is not None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("ab")
        environment = _scoped_storage_environment(
            access_key=self.storage_access_key,
            secret_key=self.storage_secret_key,
        )
        environment.update(
            {
                "DEEPSEEK_INFRA_ROOT": str(self.root),
                "DEEPSEEK_CONTROL_AUTHORITY_MODE": "local-only",
                "DEEPSEEK_FEDERATION_OPERATOR_TOKEN": self.operator_token,
                "DEEPSEEK_FEDERATION_SIGNER_PASSPHRASE": self.signer_passphrase,
                "PYTHONPATH": str(ROOT) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""),
            }
        )
        command = [
            sys.executable,
            "-m",
            "deepseek_infra.federation_app",
            "--config",
            str(self.config_path),
            "--port",
            str(self.port),
        ]
        if self.recovery_identity is not None:
            command.append("--recovery-identity-stdin")
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE if self.recovery_identity is not None else subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        if self.recovery_identity is not None:
            assert self.process.stdin is not None
            self.process.stdin.write(self.recovery_identity.encode("ascii") + b"\n")
            self.process.stdin.close()
        return _wait_for_node(self.port, process=self.process)

    def kill(self) -> int:
        assert self.process is not None
        self.process.kill()
        return_code = int(self.process.wait(timeout=15))
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None
        return return_code

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None


def _probe(
    root: Path,
    command: dict[str, Any],
    *,
    storage_access_key: str,
    storage_secret_key: str,
) -> dict[str, Any]:
    environment = _scoped_storage_environment(access_key=storage_access_key, secret_key=storage_secret_key)
    environment["DEEPSEEK_INFRA_ROOT"] = str(root)
    environment["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "local-only"
    environment["PYTHONPATH"] = str(ROOT) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    completed = subprocess.run(
        [sys.executable, "scripts/federation_e2e_probe.py"],
        cwd=ROOT,
        env=environment,
        input=json.dumps(command),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def _create_identity(
    root: Path,
    fleet_id: str,
    *,
    now: datetime,
    signer_sequence: int,
    signer_name: str = "signer",
) -> tuple[dict[str, Any], dict[str, Any], str]:
    root.mkdir(parents=True, exist_ok=True)
    root_passphrase = f"{fleet_id}-offline-root-passphrase-480".encode("ascii")
    signer_passphrase = f"{fleet_id}-{signer_name}-passphrase-480"
    root_bundle = root / "offline-root.bundle.json"
    public_identity = root / "fleet-identity.json"
    signer_bundle = root / f"{signer_name}.bundle.json"
    identity = federation_identity.create_fleet_root(
        fleet_id,
        bundle_path=root_bundle,
        passphrase=root_passphrase,
        now=now - timedelta(hours=2),
    )
    federation_identity.export_public_fleet_identity(root_bundle, public_identity)
    certificate = federation_identity.issue_online_signer(
        root_bundle_path=root_bundle,
        root_passphrase=root_passphrase,
        signer_bundle_path=signer_bundle,
        signer_passphrase=signer_passphrase.encode("ascii"),
        sequence=signer_sequence,
        not_before=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=4),
    )
    return identity, certificate, signer_passphrase


def _issue_additional_signer(
    root: Path,
    identity: dict[str, Any],
    *,
    now: datetime,
    sequence: int,
    name: str,
) -> tuple[dict[str, Any], federation_identity.OnlineFleetSigner]:
    fleet_id = str(identity["fleetId"])
    passphrase = f"{fleet_id}-{name}-passphrase-480".encode("ascii")
    certificate = federation_identity.issue_online_signer(
        root_bundle_path=root / "offline-root.bundle.json",
        root_passphrase=f"{fleet_id}-offline-root-passphrase-480".encode("ascii"),
        signer_bundle_path=root / f"{name}.bundle.json",
        signer_passphrase=passphrase,
        sequence=sequence,
        not_before=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=4),
    )
    signer = federation_identity.load_online_signer(
        root / f"{name}.bundle.json",
        passphrase,
        root_identity=identity,
        now=now,
    )
    return certificate, signer


def _pin_bilateral(
    registry_a: federation_peer_trust.PeerTrustRegistry,
    registry_b: federation_peer_trust.PeerTrustRegistry,
    identity_a: dict[str, Any],
    identity_b: dict[str, Any],
    *,
    now: datetime,
) -> None:
    registry_a.pin_peer(
        identity_b,
        expected_root_fingerprint=str(identity_b["rootFingerprint"]),
        metadata=_metadata(region="cn-south-1"),
        operator_id="operator-a",
        now=now - timedelta(minutes=15),
    )
    registry_a.verify_peer("fleet-b", identity_b, actor="operator-a", now=now - timedelta(minutes=14))
    registry_a.activate_peer("fleet-b", actor="operator-a", now=now - timedelta(minutes=13))
    registry_b.pin_peer(
        identity_a,
        expected_root_fingerprint=str(identity_a["rootFingerprint"]),
        metadata=_metadata(region="cn-north-1"),
        operator_id="operator-b",
        now=now - timedelta(minutes=15),
    )
    registry_b.verify_peer("fleet-a", identity_a, actor="operator-b", now=now - timedelta(minutes=14))
    registry_b.activate_peer("fleet-a", actor="operator-b", now=now - timedelta(minutes=13))


def _write_node_config(
    root: Path,
    *,
    fleet_id: str,
    signer_name: str,
    remote_target_id: str,
    peer_fleet_id: str,
    custody_mode: str,
    age_recipient: str | None,
    region: str,
) -> Path:
    custody: dict[str, Any] = {
        "peerFleetId": peer_fleet_id,
        "mode": custody_mode,
        "actor": f"operator-{fleet_id}",
    }
    if age_recipient is not None:
        custody["ageRecipient"] = age_recipient
    config_path = root / "federation-node.json"
    document = {
        "schema": "federation-node-config-v1",
        "fleetId": fleet_id,
        "publicIdentityPath": str(root / "fleet-identity.json"),
        "signerBundlePath": str(root / f"{signer_name}.bundle.json"),
        "peerRegistryPath": str(root / "peer-trust.sqlite3"),
        "transferJournalPath": str(root / "transfers.sqlite3"),
        "receiverDbPath": str(root / "receiver.sqlite3"),
        "stagingDir": str(root / "receiver-staging"),
        "durabilityDbPath": str(root / "federated-durability.sqlite3"),
        "custodyDbPath": str(root / "custody.sqlite3"),
        "nodeStateDbPath": str(root / "node.sqlite3"),
        "remoteTargetId": remote_target_id,
        "failureDomainMetadata": _metadata(region=region),
        "readiness": {
            "wireCompatibility": ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
            "availableFailureDomains": [region],
            "forecastHeadroom": 10 * 1024 * 1024 * 1024,
            "costClass": "offsite",
            "readiness": "READY",
        },
        "maxIngressBytes": 64 * 1024 * 1024,
        "ownerInstanceId": f"{fleet_id}-federation-process",
        "custody": custody,
    }
    config_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config_path


def _s3_inventory(client: Any, bucket: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    continuation: str | None = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 1000}
        if continuation:
            request["ContinuationToken"] = continuation
        page = client.list_objects_v2(**request)
        objects.extend(
            {
                "key": str(item["Key"]),
                "bytes": int(item["Size"]),
                "etag": str(item.get("ETag") or "").strip('"'),
            }
            for item in page.get("Contents") or []
        )
        if not page.get("IsTruncated"):
            break
        continuation = str(page["NextContinuationToken"])
    return sorted(objects, key=lambda item: item["key"])


def _rejected_s3_access_code(client: Any) -> str:
    try:
        client.list_buckets()
    except Exception as exc:
        response = getattr(exc, "response", None)
        error = response.get("Error") if isinstance(response, dict) else None
        code = str(error.get("Code") or "") if isinstance(error, dict) else ""
        assert code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}, str(exc)
        return code
    raise AssertionError("cross-Fleet storage credentials unexpectedly authenticated")


def _state_digest(
    registry: federation_peer_trust.PeerTrustRegistry,
    peer_fleet_id: str,
) -> str:
    peer = registry.get_peer(peer_fleet_id)
    return _typed_digest(
        {
            "peer": peer,
            "signers": registry.list_online_signers(peer_fleet_id) if peer is not None else [],
            "readiness": registry.get_readiness_high_water(peer_fleet_id) if peer is not None else None,
        }
    )


def _build_trust_proof(
    *,
    validated_at: datetime,
    identity_a: dict[str, Any],
    identity_b: dict[str, Any],
    identity_c: dict[str, Any],
    signer_b: federation_identity.OnlineFleetSigner,
    old_signer_b: federation_identity.OnlineFleetSigner,
    signer_c: federation_identity.OnlineFleetSigner,
    registry_a: federation_peer_trust.PeerTrustRegistry,
    registry_b: federation_peer_trust.PeerTrustRegistry,
    certificate_a: dict[str, Any],
    certificate_b: dict[str, Any],
    old_certificate_b: dict[str, Any],
    active_peer_a: dict[str, Any],
    active_peer_b: dict[str, Any],
    readiness: dict[str, Any],
    replayed_readiness: dict[str, Any],
    challenge: dict[str, Any],
    challenge_response: dict[str, Any],
    age_recipient: str,
) -> dict[str, Any]:
    expired = federation_readiness_attestation.issue_readiness_attestation(
        signer_b,
        _readiness_snapshot("fleet-b", validated_at - timedelta(minutes=1)),
        sequence=int(readiness["sequence"]) + 1,
        signed_at=validated_at - timedelta(minutes=1),
        expires_at=validated_at - timedelta(seconds=1),
    )
    future = federation_readiness_attestation.issue_readiness_attestation(
        signer_b,
        _readiness_snapshot("fleet-b", validated_at + timedelta(seconds=45)),
        sequence=int(readiness["sequence"]) + 1,
        signed_at=validated_at + timedelta(seconds=45),
        expires_at=validated_at + timedelta(minutes=2),
    )
    revoked = federation_readiness_attestation.issue_readiness_attestation(
        old_signer_b,
        _readiness_snapshot("fleet-b", validated_at),
        sequence=int(readiness["sequence"]) + 1,
        signed_at=validated_at,
        expires_at=validated_at + timedelta(minutes=2),
    )
    tofu = federation_readiness_attestation.issue_readiness_attestation(
        signer_c,
        _readiness_snapshot("fleet-c", validated_at),
        sequence=1,
        signed_at=validated_at,
        expires_at=validated_at + timedelta(minutes=2),
    )
    state = _typed_digest(
        {
            "sourcePeer": active_peer_a,
            "destinationPeer": active_peer_b,
            "readiness": registry_a.get_readiness_high_water("fleet-b"),
        }
    )
    failures = [
        {
            "claim": "trustOnFirstUseIsRejected",
            "code": "FEDERATION_PEER_NOT_PINNED",
            "preStateDigest": state,
            "postStateDigest": state,
            "documentDigest": _typed_digest({"fleetIdentity": identity_c, "attestation": tofu}),
            "document": {"fleetIdentity": identity_c, "attestation": tofu},
        },
        {
            "claim": "readinessSequenceReplayIsRejected",
            "code": "FEDERATION_READINESS_SEQUENCE_REPLAY",
            "preStateDigest": state,
            "postStateDigest": state,
            "documentDigest": _typed_digest(replayed_readiness),
            "document": replayed_readiness,
        },
        {
            "claim": "expiredReadinessAttestationIsRejected",
            "code": "FEDERATION_READINESS_ATTESTATION_EXPIRED",
            "preStateDigest": state,
            "postStateDigest": state,
            "documentDigest": _typed_digest(expired),
            "document": expired,
        },
        {
            "claim": "futureReadinessAttestationBeyondSkewIsRejected",
            "code": "FEDERATION_READINESS_ATTESTATION_FROM_FUTURE",
            "preStateDigest": state,
            "postStateDigest": state,
            "documentDigest": _typed_digest(future),
            "document": future,
        },
        {
            "claim": "challengeNonceReplayIsRejected",
            "code": "FEDERATION_CHALLENGE_NONCE_REPLAY",
            "preStateDigest": state,
            "postStateDigest": state,
            "documentDigest": _typed_digest(challenge),
            "document": challenge,
        },
        {
            "claim": "revokedFederationSignerIsRejected",
            "code": "FEDERATION_SIGNER_REVOKED",
            "preStateDigest": state,
            "postStateDigest": state,
            "documentDigest": _typed_digest(revoked),
            "document": revoked,
        },
    ]
    return federation_trust_proof.build_federation_trust_proof(
        validated_at=validated_at,
        source_fleet_identity=identity_a,
        destination_fleet_identity=identity_b,
        source_peer_trust=active_peer_a,
        destination_peer_trust=active_peer_b,
        source_signer_trust=registry_b.get_online_signer("fleet-a", str(certificate_a["signerKeyId"])),
        destination_signer_trust=registry_a.get_online_signer("fleet-b", str(certificate_b["signerKeyId"])),
        revoked_destination_signer_trust=registry_a.get_online_signer(
            "fleet-b", str(old_certificate_b["signerKeyId"])
        ),
        readiness_attestation=readiness,
        readiness_high_water=registry_a.get_readiness_high_water("fleet-b"),
        challenge=challenge,
        challenge_response=challenge_response,
        age_recipients={"fleet-a": age_recipient, "fleet-b": age_recipient},
        authority_identity_digests={
            "fleet-a": _typed_digest({"authority": "fleet-a", "root": "independent-a"}),
            "fleet-b": _typed_digest({"authority": "fleet-b", "root": "independent-b"}),
        },
        failure_observations=failures,
    )


def _build_replica_proof(
    *,
    validated_at: datetime,
    identity_b: dict[str, Any],
    peer: dict[str, Any],
    grant: dict[str, Any],
    receiver_transfer: dict[str, Any],
    sender_transfer: dict[str, Any],
    declaration: dict[str, Any],
    source_receipt: dict[str, Any],
    committed: dict[str, Any],
    accepted_replica: dict[str, Any],
    copy_record: dict[str, Any],
    local_replication: dict[str, Any],
    durability_status: dict[str, Any],
    tampered_attestation: dict[str, Any],
) -> dict[str, Any]:
    state = _typed_digest(
        {
            "receiverTransfer": receiver_transfer,
            "senderTransfer": sender_transfer,
            "acceptedReplica": accepted_replica,
            "copyRecord": copy_record,
        }
    )
    grant_expiry = datetime.fromisoformat(str(grant["expiresAt"]).replace("Z", "+00:00"))
    failures = [
        {
            "claim": "expiredIngressGrantCannotWrite",
            "code": "FEDERATION_INGRESS_GRANT_EXPIRED",
            "preStateDigest": state,
            "postStateDigest": state,
            "input": {"grant": grant, "attemptedAt": (grant_expiry + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")},
        },
        {
            "claim": "ingressGrantCannotEscapeObjectPrefix",
            "code": "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION",
            "preStateDigest": state,
            "postStateDigest": state,
            "input": {"grant": grant, "objectKey": "outside/federation.age", "byteCount": 1},
        },
        {
            "claim": "ingressGrantCannotExceedMaxBytes",
            "code": "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED",
            "preStateDigest": state,
            "postStateDigest": state,
            "input": {"grant": grant, "byteCount": int(grant["maxBytes"]) + 1},
        },
        {
            "claim": "sameTransferIdDifferentDigestFailsClosed",
            "code": "FEDERATION_TRANSFER_IDENTITY_CONFLICT",
            "preStateDigest": state,
            "postStateDigest": state,
            "input": {"transfer": receiver_transfer, "conflictingObjectSetDigest": "sha256:" + ("f" * 64)},
        },
        {
            "claim": "replayedIngressGrantFailsClosed",
            "code": "FEDERATION_INGRESS_GRANT_NONCE_REPLAY",
            "preStateDigest": state,
            "postStateDigest": state,
            "input": {"grant": grant, "replayedGrantId": grant["grantId"]},
        },
        {
            "claim": "tamperedReplicaAttestationFailsClosed",
            "code": "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
            "preStateDigest": state,
            "postStateDigest": state,
            "input": {"attestation": tampered_attestation},
        },
    ]
    failure_state = {
        "receiverTransfer": receiver_transfer,
        "senderTransfer": sender_transfer,
        "acceptedReplica": accepted_replica,
        "copyRecord": copy_record,
    }
    for failure in failures:
        failure["preState"] = copy.deepcopy(failure_state)
        failure["postState"] = copy.deepcopy(failure_state)
    return federated_replica_proof.build_federated_replica_proof(
        validated_at=validated_at,
        destination_fleet_identity=identity_b,
        peer_trust_record=peer,
        ingress_grant=grant,
        receiver_transfer=receiver_transfer,
        sender_transfer=sender_transfer,
        object_set_declaration=declaration,
        source_receipt=source_receipt,
        remote_receipt_bytes=base64.b64decode(str(committed["remoteReceiptBase64"]), validate=True),
        remote_commit_bytes=base64.b64decode(str(committed["remoteCommitBase64"]), validate=True),
        replica_attestation=committed["attestation"],
        accepted_replica_record=accepted_replica,
        federated_copy_record=copy_record,
        local_durability_before=copy.deepcopy(local_replication),
        local_durability_after=copy.deepcopy(local_replication),
        federated_durability_status=durability_status,
        failure_observations=failures,
        wire_contracts={
            "objectSet": "object-set-v1",
            "receiptVersion": 4,
            "commitVersion": 4,
            "fastCdc": "fastcdc-v3",
            "randomizedAge": True,
        },
    )


def _build_dr_proof(
    *,
    validated_at: datetime,
    identity_a: dict[str, Any],
    identity_b: dict[str, Any],
    peer: dict[str, Any],
    sender_transfer: dict[str, Any],
    accepted_replica: dict[str, Any],
    dr_effect: dict[str, Any],
    accepted_dr: dict[str, Any],
    recovery_capability: dict[str, Any],
) -> dict[str, Any]:
    state = {
        "senderTransfer": sender_transfer,
        "acceptedReplica": accepted_replica,
        "acceptedDrDrill": accepted_dr,
    }
    state_digest = _typed_digest(state)
    cold = copy.deepcopy(recovery_capability)
    cold.update(
        {
            "mode": federation_custody_capability.COLD_CUSTODY,
            "recoveryIdentityPreprovisioned": False,
            "ageRecipient": None,
            "ageRecipientDigest": None,
        }
    )
    missing_identity = copy.deepcopy(recovery_capability)
    missing_identity.update(
        {
            "recoveryIdentityPreprovisioned": False,
            "ageRecipient": None,
            "ageRecipientDigest": None,
        }
    )
    cleanup_failed = copy.deepcopy(dr_effect["productionRestoreResult"])
    cleanup_failed["cleanupCompleted"] = False
    failures = [
        {
            "claim": "coldCustodyCannotClaimRecoveryReady",
            "code": "FEDERATION_PEER_COLD_CUSTODY_ONLY",
            "preState": copy.deepcopy(state),
            "preStateDigest": state_digest,
            "postState": copy.deepcopy(state),
            "postStateDigest": state_digest,
            "input": {"capability": cold},
        },
        {
            "claim": "recoveryCapablePeerRequiresPreprovisionedAgeIdentity",
            "code": "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED",
            "preState": copy.deepcopy(state),
            "preStateDigest": state_digest,
            "postState": copy.deepcopy(state),
            "postStateDigest": state_digest,
            "input": {"capability": missing_identity},
        },
        {
            "claim": "federatedDrProofRequiresCleanupSuccess",
            "code": "FEDERATED_DR_CLEANUP_INCOMPLETE",
            "preState": copy.deepcopy(state),
            "preStateDigest": state_digest,
            "postState": copy.deepcopy(state),
            "postStateDigest": state_digest,
            "input": {"productionRestoreResult": cleanup_failed},
        },
    ]
    return federated_dr_proof.build_federated_dr_proof(
        validated_at=validated_at,
        source_fleet_identity=identity_a,
        destination_fleet_identity=identity_b,
        peer_trust_record=peer,
        sender_transfer=sender_transfer,
        accepted_replica_record=accepted_replica,
        dr_attestation=dr_effect["attestation"],
        accepted_dr_record=accepted_dr,
        recovery_capability=recovery_capability,
        production_restore_result=dr_effect["productionRestoreResult"],
        failure_observations=failures,
    )


@pytest.mark.integration
def test_real_two_fleet_four_minio_replication_sigkill_and_dr_e2e(
    tmp_settings: Path,
    real_storage_environment: Any,
) -> None:
    del real_storage_environment
    assert os.environ.get("DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E") == "1"
    endpoints = [str(os.environ.get(name) or "").rstrip("/") for name in ENDPOINT_NAMES]
    containers = [str(os.environ.get(name) or "") for name in CONTAINER_NAMES]
    assert all(endpoints) and len(set(endpoints)) == 4
    assert all(containers) and len(set(containers)) == 4
    assert backup_crypto.helper_path() is not None
    source_storage_access_key = str(os.environ["AWS_ACCESS_KEY_ID"])
    source_storage_secret_key = str(os.environ["AWS_SECRET_ACCESS_KEY"])
    receiver_storage_access_key = str(os.environ["DEEPSEEK_TEST_FEDERATION_ACCESS_KEY_ID"])
    receiver_storage_secret_key = str(os.environ["DEEPSEEK_TEST_FEDERATION_SECRET_ACCESS_KEY"])
    assert (source_storage_access_key, source_storage_secret_key) != (
        receiver_storage_access_key,
        receiver_storage_secret_key,
    )
    clients = [
        s3_client(endpoints[0], access_key=source_storage_access_key, secret_key=source_storage_secret_key),
        s3_client(endpoints[1], access_key=source_storage_access_key, secret_key=source_storage_secret_key),
        s3_client(endpoints[2], access_key=receiver_storage_access_key, secret_key=receiver_storage_secret_key),
        s3_client(endpoints[3], access_key=receiver_storage_access_key, secret_key=receiver_storage_secret_key),
    ]
    suffix = uuid.uuid4().hex[:10]
    buckets = [f"deepseek-federation-{name}-{suffix}" for name in ("a1", "a2", "b1", "b2")]

    target_a1 = _register_target(
        clients[0],
        endpoints[0],
        buckets[0],
        region="cn-north-1",
        failure_domain="fleet-a-site-1",
    )
    target_a2 = _register_target(
        clients[1],
        endpoints[1],
        buckets[1],
        region="cn-north-2",
        failure_domain="fleet-a-site-2",
    )
    clients[2].create_bucket(Bucket=buckets[2])
    clients[3].create_bucket(Bucket=buckets[3])
    source_to_receiver_denied = _rejected_s3_access_code(
        s3_client(endpoints[2], access_key=source_storage_access_key, secret_key=source_storage_secret_key)
    )
    receiver_to_source_denied = _rejected_s3_access_code(
        s3_client(endpoints[0], access_key=receiver_storage_access_key, secret_key=receiver_storage_secret_key)
    )

    age = backup_crypto.generate_identity()
    age_recipient = str(age["recipient"])
    age_identity = str(age["identity"])
    assert age_recipient.startswith("age1")
    assert age_identity.startswith("AGE-SECRET-KEY-")
    _seed_workspace()
    backup_mirror.put_frontend_mirror(
        "mirror_default",
        _envelope(),
        source_epoch="federation-480",
        recipients=[age_recipient],
    )
    policy = backup_policies.create_policy(
        {
            "schemaVersion": 1,
            "name": f"federation-source-{suffix}",
            "enabled": True,
            "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
            "scope": {"mode": "full", "includeExternalState": False, "coveragePolicy": "best-effort"},
            "frontendMirror": {"mode": "best-effort"},
            "protection": {"mode": "age-recipient", "recipients": [age_recipient]},
            "targetId": target_a1,
            "primaryTargetId": target_a1,
            "retry": {"maxAttempts": 2, "initialBackoffSeconds": 1, "maxBackoffSeconds": 2},
            "replication": {
                "enabled": True,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "minRegions": 2,
                "targets": [{"targetId": target_a2, "mode": "required"}],
            },
            "placement": {"maxCopiesPerFailureDomain": 1, "minFreeBytes": 1024 * 1024},
            "federatedDurability": {
                "enabled": True,
                "minFederatedCopies": 1,
                "minDistinctFleets": 1,
                "maxFederatedCopyAge": 3600,
                "allowedPeerFleets": ["fleet-b"],
                "allowedJurisdictions": ["CN"],
            },
        }
    )
    policy_id = str(policy["policyId"])
    backup_now = datetime.now(tz=UTC).replace(hour=4, minute=0, second=0, microsecond=0)
    source_run = _claim_and_run(policy, now=backup_now)
    assert source_run["phase"] == "complete", source_run.get("error")
    backup_id = str(source_run["backupId"])
    source_status, source_receipt, source_commit = backup_replication.authenticate_committed_copy(
        backup_targets.get_target(target_a1),
        policy_id,
        backup_id,
    )
    replica_status, _, _ = backup_replication.authenticate_committed_copy(
        backup_targets.get_target(target_a2),
        policy_id,
        backup_id,
    )
    assert source_status == "authenticated" and source_receipt is not None and source_commit is not None
    assert replica_status == "authenticated"
    assert source_receipt["schemaVersion"] == 4
    assert source_commit["schemaVersion"] == 4
    components = backup_object_set.committed_object_inventory(source_receipt)
    source_target = backup_publish.resolve_target(target_a1)
    source_store = source_target.require_store()
    component_bytes: dict[str, bytes] = {}
    for item in components:
        digest = str(item["digest"])
        raw_component = source_store.get_bytes(object_key(digest))
        assert isinstance(raw_component, bytes)
        component_bytes[digest] = raw_component
    assert all(len(component_bytes[str(item["digest"])]) == int(item["size"]) for item in components)
    total_bytes = sum(int(item["size"]) for item in components)
    local_inventory_before = {
        "a1": _s3_inventory(clients[0], buckets[0]),
        "a2": _s3_inventory(clients[1], buckets[1]),
    }

    fleet_a_root = tmp_settings
    fleet_b_root = tmp_settings / "fleet-b-root"
    identity_now = datetime.now(tz=UTC)
    identity_a, certificate_a, signer_passphrase_a = _create_identity(
        fleet_a_root,
        "fleet-a",
        now=identity_now,
        signer_sequence=1,
    )
    identity_b, certificate_b, signer_passphrase_b = _create_identity(
        fleet_b_root,
        "fleet-b",
        now=identity_now,
        signer_sequence=2,
    )
    old_certificate_b, old_signer_b = _issue_additional_signer(
        fleet_b_root,
        identity_b,
        now=identity_now,
        sequence=1,
        name="signer-old",
    )
    fleet_c_root = tmp_settings / "fleet-c-untrusted"
    identity_c, _certificate_c, signer_passphrase_c = _create_identity(
        fleet_c_root,
        "fleet-c",
        now=identity_now,
        signer_sequence=1,
    )
    signer_c = federation_identity.load_online_signer(
        fleet_c_root / "signer.bundle.json",
        signer_passphrase_c.encode("ascii"),
        root_identity=identity_c,
        now=identity_now,
    )
    signer_b = federation_identity.load_online_signer(
        fleet_b_root / "signer.bundle.json",
        signer_passphrase_b.encode("ascii"),
        root_identity=identity_b,
        now=identity_now,
    )

    registry_a = federation_peer_trust.PeerTrustRegistry(fleet_a_root / "peer-trust.sqlite3", identity_a)
    registry_b = federation_peer_trust.PeerTrustRegistry(fleet_b_root / "peer-trust.sqlite3", identity_b)
    _pin_bilateral(registry_a, registry_b, identity_a, identity_b, now=identity_now)
    registry_a.accept_online_signer("fleet-b", old_certificate_b, actor="operator-a", now=identity_now - timedelta(minutes=12))
    registry_a.revoke_online_signer(
        "fleet-b",
        str(old_certificate_b["signerKeyId"]),
        actor="operator-a",
        reason="planned-rotation",
        revoked_at=identity_now - timedelta(minutes=11),
    )
    registry_a.accept_online_signer("fleet-b", certificate_b, actor="operator-a", now=identity_now - timedelta(minutes=10))
    registry_b.accept_online_signer("fleet-a", certificate_a, actor="operator-b", now=identity_now - timedelta(minutes=10))

    target_b1 = str(
        _probe(
            fleet_b_root,
            {
                "action": "register-s3-target",
                "bucket": buckets[2],
                "prefix": f"fleet-b-custody-{suffix}",
                "endpointUrl": endpoints[2],
                "region": "cn-south-1",
                "failureDomain": "fleet-b-site-1",
                "jurisdiction": "CN",
            },
            storage_access_key=receiver_storage_access_key,
            storage_secret_key=receiver_storage_secret_key,
        )["targetId"]
    )
    target_b2 = str(
        _probe(
            fleet_b_root,
            {
                "action": "register-s3-target",
                "bucket": buckets[3],
                "prefix": f"fleet-b-replica-{suffix}",
                "endpointUrl": endpoints[3],
                "region": "cn-south-2",
                "failureDomain": "fleet-b-site-2",
                "jurisdiction": "CN",
            },
            storage_access_key=receiver_storage_access_key,
            storage_secret_key=receiver_storage_secret_key,
        )["targetId"]
    )
    config_a = _write_node_config(
        fleet_a_root,
        fleet_id="fleet-a",
        signer_name="signer",
        remote_target_id=target_a1,
        peer_fleet_id="fleet-b",
        custody_mode=federation_custody_capability.COLD_CUSTODY,
        age_recipient=None,
        region="cn-north-1",
    )
    config_b = _write_node_config(
        fleet_b_root,
        fleet_id="fleet-b",
        signer_name="signer",
        remote_target_id=target_b1,
        peer_fleet_id="fleet-a",
        custody_mode=federation_custody_capability.RECOVERY_CAPABLE,
        age_recipient=age_recipient,
        region="cn-south-1",
    )

    port_a, port_b = _free_port(), _free_port()
    while port_b == port_a:
        port_b = _free_port()
    token_a = "fleet-a-operator-token-480"
    token_b = "fleet-b-operator-token-480"
    node_a = _NodeProcess(
        config_path=config_a,
        root=fleet_a_root,
        port=port_a,
        operator_token=token_a,
        signer_passphrase=signer_passphrase_a,
        storage_access_key=source_storage_access_key,
        storage_secret_key=source_storage_secret_key,
        log_path=tmp_settings / "fleet-a-node.log",
    )
    node_b = _NodeProcess(
        config_path=config_b,
        root=fleet_b_root,
        port=port_b,
        operator_token=token_b,
        signer_passphrase=signer_passphrase_b,
        storage_access_key=receiver_storage_access_key,
        storage_secret_key=receiver_storage_secret_key,
        recovery_identity=age_identity,
        log_path=tmp_settings / "fleet-b-node.log",
    )
    try:
        health_a = node_a.start()
        health_b_a = node_b.start()
        pid_a = int(health_a["pid"])
        receiver_pid_a = int(health_b_a["pid"])
        assert pid_a != receiver_pid_a
        assert health_a["rootFingerprint"] != health_b_a["rootFingerprint"]

        challenge = _request_json(
            port_a,
            "/federation/v1/operator/challenges",
            payload={"destinationFleetId": "fleet-b"},
            operator_token=token_a,
        )
        challenge_response = _request_json(
            port_b,
            "/federation/v1/peer/challenges/respond",
            payload={"challenge": challenge},
        )
        _request_json(
            port_a,
            "/federation/v1/operator/challenges/verify",
            payload={"challenge": challenge, "response": challenge_response},
            operator_token=token_a,
        )
        replayed_challenge = _request_json(
            port_b,
            "/federation/v1/peer/challenges/respond",
            payload={"challenge": challenge},
            expected_status=409,
        )
        assert replayed_challenge["code"] == "FEDERATION_CHALLENGE_NONCE_REPLAY"

        readiness_one = _request_json(port_b, "/federation/v1/peer/readiness", payload={})
        _request_json(
            port_a,
            "/federation/v1/operator/readiness/verify",
            payload={"expectedPeerFleetId": "fleet-b", "attestation": readiness_one},
            operator_token=token_a,
        )
        readiness = _request_json(port_b, "/federation/v1/peer/readiness", payload={})
        _request_json(
            port_a,
            "/federation/v1/operator/readiness/verify",
            payload={"expectedPeerFleetId": "fleet-b", "attestation": readiness},
            operator_token=token_a,
        )
        assert int(readiness["sequence"]) > int(readiness_one["sequence"])
        trust_validated_at = datetime.now(tz=UTC)

        proposed = _request_json(
            port_a,
            "/federation/v1/operator/transfers",
            payload={"destinationFleetId": "fleet-b", "sourceReceipt": source_receipt},
            operator_token=token_a,
        )
        transfer_id = str(proposed["transfer"]["transferId"])
        grant = _request_json(
            port_b,
            "/federation/v1/peer/ingress-grants",
            payload={
                "sourceFleetId": "fleet-a",
                "sessionNonce": challenge["nonce"],
                "transferId": transfer_id,
                "policyId": policy_id,
                "backupId": backup_id,
                "objectSetDigest": proposed["objectSetDigest"],
                "totalBytes": total_bytes,
            },
        )
        verified_grant = _request_json(
            port_a,
            "/federation/v1/operator/ingress-grants/verify",
            payload={"grant": grant},
            operator_token=token_a,
        )
        assert verified_grant == grant
        _request_json(
            port_a,
            f"/federation/v1/operator/transfers/{transfer_id}/remote-verifying",
            payload={"grantId": grant["grantId"], "remoteTargetId": target_b1},
            operator_token=token_a,
        )
        declaration = _request_json(
            port_b,
            f"/federation/v1/peer/transfers/{transfer_id}/declaration",
            payload={"grantId": grant["grantId"], "sourceReceipt": source_receipt},
        )
        assert declaration["storageProtocol"] == "object-set-v1"

        largest = max(components, key=lambda item: int(item["size"]))
        largest_digest = str(largest["digest"])
        assert len(component_bytes[largest_digest]) >= 1024 * 1024
        first_chunk_sent = threading.Event()
        interrupted_upload: dict[str, Any] = {}
        write_id = "receiver-sigkill-resume"
        upload_thread = threading.Thread(
            target=_slow_upload,
            args=(port_b, transfer_id, largest_digest),
            kwargs={
                "grant_id": str(grant["grantId"]),
                "write_id": write_id,
                "content": component_bytes[largest_digest],
                "first_chunk_sent": first_chunk_sent,
                "outcome": interrupted_upload,
            },
            daemon=True,
        )
        upload_thread.start()
        assert first_chunk_sent.wait(timeout=10)
        time.sleep(0.08)
        receiver_return_code = node_b.kill()
        upload_thread.join(timeout=15)
        assert not upload_thread.is_alive()
        assert receiver_return_code != 0
        assert 0 < int(interrupted_upload.get("bytesSent") or 0) < len(component_bytes[largest_digest])

        health_b_b = node_b.start()
        receiver_pid_b = int(health_b_b["pid"])
        assert receiver_pid_b != receiver_pid_a
        reconciled_after_restart = _request_json(
            port_b,
            f"/federation/v1/peer/transfers/{transfer_id}?{urllib.parse.urlencode({'grantId': grant['grantId']})}",
            payload=None,
            method="GET",
        )
        assert reconciled_after_restart["status"] == "RESUME"
        assert reconciled_after_restart["committedEffect"] is None

        for component in components:
            digest = str(component["digest"])
            _put_component(
                port_b,
                transfer_id,
                digest,
                grant_id=str(grant["grantId"]),
                write_id=write_id if digest == largest_digest else f"write-{digest[:16]}",
                content=component_bytes[digest],
            )
        replayed_grant = _put_component(
            port_b,
            transfer_id,
            largest_digest,
            grant_id=str(grant["grantId"]),
            write_id="replayed-old-grant-write",
            content=component_bytes[largest_digest],
            expected_status=409,
        )
        assert replayed_grant["code"] == "FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY"

        committed = _request_json(
            port_b,
            f"/federation/v1/peer/transfers/{transfer_id}/commit",
            payload={"grantId": grant["grantId"]},
            timeout=180,
        )
        repeated_commit = _request_json(
            port_b,
            f"/federation/v1/peer/transfers/{transfer_id}/commit",
            payload={"grantId": grant["grantId"]},
            timeout=180,
        )
        assert repeated_commit == committed
        assert committed["receipt"]["schemaVersion"] == 4
        assert committed["commit"]["schemaVersion"] == 4
        assert committed["attestation"]["schema"] == "federated-replica-attestation-v1"
        reconciled_committed = _request_json(
            port_b,
            f"/federation/v1/peer/transfers/{transfer_id}?{urllib.parse.urlencode({'grantId': grant['grantId']})}",
            payload=None,
            method="GET",
        )
        assert reconciled_committed["committedEffect"] == committed

        tampered_attestation = copy.deepcopy(committed["attestation"])
        signature = str(tampered_attestation["signature"])
        tampered_attestation["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
        tampered_response = _request_json(
            port_a,
            f"/federation/v1/operator/transfers/{transfer_id}/replica-attestations/verify",
            payload={
                "attestation": tampered_attestation,
                "remoteReceiptBase64": committed["remoteReceiptBase64"],
                "remoteCommitBase64": committed["remoteCommitBase64"],
            },
            operator_token=token_a,
            expected_status=409,
        )
        assert tampered_response["code"] == "FEDERATION_DOCUMENT_SIGNATURE_INVALID"
        verified_replica = _request_json(
            port_a,
            f"/federation/v1/operator/transfers/{transfer_id}/replica-attestations/verify",
            payload={
                "attestation": committed["attestation"],
                "remoteReceiptBase64": committed["remoteReceiptBase64"],
                "remoteCommitBase64": committed["remoteCommitBase64"],
            },
            operator_token=token_a,
        )
        assert verified_replica["federatedCopy"]["localDurabilityCredit"] is False
        assert verified_replica["transfer"]["state"] == federation_transfer_journal.STATE_SUCCEEDED

        b2_result = _probe(
            fleet_b_root,
            {
                "action": "rebalance",
                "policyId": policy_id,
                "backupId": backup_id,
                "sourceTargetId": target_b1,
                "destinationTargetId": target_b2,
            },
            storage_access_key=receiver_storage_access_key,
            storage_secret_key=receiver_storage_secret_key,
        )
        assert b2_result["authenticationStatus"] == "authenticated"
        assert b2_result["receipt"]["schemaVersion"] == 4
        assert b2_result["commit"]["schemaVersion"] == 4
        assert _s3_inventory(clients[2], buckets[2])
        assert _s3_inventory(clients[3], buckets[3])

        dr_effect = _request_json(
            port_b,
            f"/federation/v1/operator/transfers/{transfer_id}/dr-drills",
            payload={"requestId": f"dr-{suffix}"},
            operator_token=token_b,
            timeout=300,
        )
        assert dr_effect["attestation"]["schema"] == "federated-dr-drill-attestation-v1"
        assert dr_effect["attestation"]["restorePath"] == "backup-recovery-drill-production-v1"
        assert dr_effect["productionRestoreResult"]["result"] == "success"
        verified_dr = _request_json(
            port_a,
            f"/federation/v1/operator/transfers/{transfer_id}/dr-attestations/verify",
            payload={"attestation": dr_effect["attestation"]},
            operator_token=token_a,
        )
        assert verified_dr["attestation"]["transferId"] == transfer_id

        local_inventory_after = {
            "a1": _s3_inventory(clients[0], buckets[0]),
            "a2": _s3_inventory(clients[1], buckets[1]),
        }
        assert local_inventory_after == local_inventory_before

        active_peer_a = copy.deepcopy(registry_a.get_peer("fleet-b"))
        active_peer_b = copy.deepcopy(registry_b.get_peer("fleet-a"))
        assert isinstance(active_peer_a, dict) and isinstance(active_peer_b, dict)
        journal_a = federation_transfer_journal.FederatedTransferJournal(
            fleet_a_root / "transfers.sqlite3",
            identity_a,
        )
        journal_b = federation_transfer_journal.FederatedTransferJournal(
            fleet_b_root / "transfers.sqlite3",
            identity_b,
        )
        receiver_b = federation_replica_receiver.FederatedReplicaReceiver(
            transfer_journal=journal_b,
            peer_registry=registry_b,
            db_path=fleet_b_root / "receiver.sqlite3",
            staging_dir=fleet_b_root / "receiver-staging",
        )
        sender_transfer = journal_a.get_transfer(transfer_id)
        receiver_transfer = journal_b.get_transfer(transfer_id)
        durable_declaration = receiver_b.get_declaration(transfer_id)
        accepted_replica = registry_a.get_replica_attestation("fleet-b", transfer_id)
        accepted_dr = registry_a.get_dr_attestation("fleet-b", str(dr_effect["attestation"]["restoreId"]))
        copy_record = federated_durability.FederatedDurabilityLedger(
            fleet_a_root / "federated-durability.sqlite3",
            identity_a,
        ).get_copy(transfer_id)
        recovery_capability = federation_custody_capability.FederationCustodyCapabilityRegistry(
            fleet_b_root / "custody.sqlite3",
            identity_b,
        ).get_peer("fleet-a")
        assert isinstance(sender_transfer, dict)
        assert isinstance(receiver_transfer, dict)
        assert isinstance(durable_declaration, dict)
        assert isinstance(accepted_replica, dict)
        assert isinstance(accepted_dr, dict)
        assert isinstance(copy_record, dict)
        assert isinstance(recovery_capability, dict)
        proof_validated_at = datetime.now(tz=UTC)
        durability_status = federated_durability.evaluate_federated_durability(
            policy=policy,
            backup_id=backup_id,
            object_set_digest=str(proposed["objectSetDigest"]),
            ledger=federated_durability.FederatedDurabilityLedger(
                fleet_a_root / "federated-durability.sqlite3",
                identity_a,
            ),
            peer_registry=registry_a,
            now=proof_validated_at,
        )
        assert durability_status["satisfied"] is True
        remote_committed_events = [
            event
            for event in journal_b.list_transfer_events(transfer_id)
            if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
        ]
        assert len(remote_committed_events) == 1
        registry_a.revoke_peer(
            "fleet-b",
            actor="operator-a",
            reason="real-e2e-revocation",
            now=datetime.now(tz=UTC),
        )
        revoked_attempt = _request_json(
            port_a,
            "/federation/v1/operator/challenges",
            payload={"destinationFleetId": "fleet-b"},
            operator_token=token_a,
            expected_status=403,
        )
        assert revoked_attempt["code"] == "FEDERATION_PEER_REVOKED"

        trust_proof = _build_trust_proof(
            validated_at=trust_validated_at,
            identity_a=identity_a,
            identity_b=identity_b,
            identity_c=identity_c,
            signer_b=signer_b,
            old_signer_b=old_signer_b,
            signer_c=signer_c,
            registry_a=registry_a,
            registry_b=registry_b,
            certificate_a=certificate_a,
            certificate_b=certificate_b,
            old_certificate_b=old_certificate_b,
            active_peer_a=active_peer_a,
            active_peer_b=active_peer_b,
            readiness=readiness,
            replayed_readiness=readiness_one,
            challenge=challenge,
            challenge_response=challenge_response,
            age_recipient=age_recipient,
        )
        replica_proof = _build_replica_proof(
            validated_at=proof_validated_at,
            identity_b=identity_b,
            peer=active_peer_a,
            grant=grant,
            receiver_transfer=receiver_transfer,
            sender_transfer=sender_transfer,
            declaration=durable_declaration,
            source_receipt=source_receipt,
            committed=committed,
            accepted_replica=accepted_replica,
            copy_record=copy_record,
            local_replication=policy["replication"],
            durability_status=durability_status,
            tampered_attestation=tampered_attestation,
        )
        dr_proof = _build_dr_proof(
            validated_at=proof_validated_at,
            identity_a=identity_a,
            identity_b=identity_b,
            peer=active_peer_a,
            sender_transfer=sender_transfer,
            accepted_replica=accepted_replica,
            dr_effect=dr_effect,
            accepted_dr=accepted_dr,
            recovery_capability=recovery_capability,
        )
        runtime_proof = federation_runtime_proof.build_federation_runtime_proof(
            validated_at=proof_validated_at,
            fleet_processes={
                "source": {
                    "fleetId": health_a["fleetId"],
                    "pid": pid_a,
                    "rootFingerprint": health_a["rootFingerprint"],
                },
                "receiverBefore": {
                    "fleetId": health_b_a["fleetId"],
                    "pid": receiver_pid_a,
                    "rootFingerprint": health_b_a["rootFingerprint"],
                },
                "receiverAfter": {
                    "fleetId": health_b_b["fleetId"],
                    "pid": receiver_pid_b,
                    "rootFingerprint": health_b_b["rootFingerprint"],
                },
                "receiverKillReturnCode": receiver_return_code,
            },
            storage_principal_isolation={
                "sourcePrincipalDigest": _typed_digest(
                    {"fleetId": "fleet-a", "storagePrincipalId": source_storage_access_key}
                ),
                "receiverPrincipalDigest": _typed_digest(
                    {"fleetId": "fleet-b", "storagePrincipalId": receiver_storage_access_key}
                ),
                "sourceToReceiverDeniedCode": source_to_receiver_denied,
                "receiverToSourceDeniedCode": receiver_to_source_denied,
            },
            minio_topology={
                "endpoints": endpoints,
                "containers": containers,
                "targetBindings": [
                    {
                        "fleetId": "fleet-a",
                        "role": "A1",
                        "targetId": target_a1,
                        "endpoint": endpoints[0],
                        "providerObjectCount": len(local_inventory_after["a1"]),
                    },
                    {
                        "fleetId": "fleet-a",
                        "role": "A2",
                        "targetId": target_a2,
                        "endpoint": endpoints[1],
                        "providerObjectCount": len(local_inventory_after["a2"]),
                    },
                    {
                        "fleetId": "fleet-b",
                        "role": "B1",
                        "targetId": target_b1,
                        "endpoint": endpoints[2],
                        "providerObjectCount": len(_s3_inventory(clients[2], buckets[2])),
                    },
                    {
                        "fleetId": "fleet-b",
                        "role": "B2",
                        "targetId": target_b2,
                        "endpoint": endpoints[3],
                        "providerObjectCount": len(_s3_inventory(clients[3], buckets[3])),
                    },
                ],
            },
            transfer_recovery={
                "transferId": transfer_id,
                "senderTransferId": sender_transfer["transferId"],
                "receiverTransferId": receiver_transfer["transferId"],
                "interruptedComponentDigest": largest_digest,
                "interruptedBytesSent": int(interrupted_upload["bytesSent"]),
                "interruptedComponentBytes": len(component_bytes[largest_digest]),
                "reconcileStatus": reconciled_after_restart["status"],
                "reconcileState": reconciled_after_restart["state"],
                "senderFinalState": sender_transfer["state"],
                "remoteCommittedEvents": remote_committed_events,
                "commitEffectDigest": _typed_digest(committed),
                "repeatedCommitEffectDigest": _typed_digest(repeated_commit),
                "localInventoryBeforeDigest": _typed_digest(local_inventory_before),
                "localInventoryAfterDigest": _typed_digest(local_inventory_after),
            },
            fail_closed={
                "replayedIngressGrant": replayed_grant["code"],
                "tamperedReplicaAttestation": tampered_response["code"],
                "revokedPeer": revoked_attempt["code"],
            },
            dr={
                "schema": dr_effect["attestation"]["schema"],
                "transferId": dr_effect["attestation"]["transferId"],
                "restorePath": dr_effect["attestation"]["restorePath"],
                "cleanupCompleted": dr_effect["attestation"]["cleanupCompleted"],
                "workspaceDigest": dr_effect["attestation"]["workspaceDigest"],
            },
        )
        assert federation_trust_proof.validate_federation_trust_proof(trust_proof) == []
        assert federated_replica_proof.validate_federated_replica_proof(replica_proof) == []
        assert federated_dr_proof.validate_federated_dr_proof(dr_proof) == []
        assert federation_runtime_proof.validate_federation_runtime_proof(runtime_proof) == []

        proof_path = evidence_proof.resolve_proof_path(scenario=SCENARIO)
        assert proof_path is not None, "dedicated runner must provide an exact proof path"
        trust_checks = {
            name: {"status": "PASS", "evidence": trust_proof}
            for name in federation_trust_proof.FEDERATION_TRUST_PROOF_CHECKS
        }
        replica_checks = {
            name: {"status": "PASS", "evidence": replica_proof}
            for name in federated_replica_proof.FEDERATED_REPLICA_PROOF_CHECKS
        }
        dr_checks = {
            name: {"status": "PASS", "evidence": dr_proof}
            for name in federated_dr_proof.FEDERATED_DR_PROOF_CHECKS
        }
        runtime_checks = {
            name: {"status": "PASS", "evidence": runtime_proof}
            for name in federation_runtime_proof.FEDERATION_RUNTIME_PROOF_CHECKS
        }
        typed_checks = {**trust_checks, **replica_checks, **dr_checks, **runtime_checks}
        proof_meta = {
            "producer": "storage-control-plane-minio-e2e",
            "version": config.APP_VERSION,
            "fleetPids": [pid_a, receiver_pid_a, receiver_pid_b],
            "minioEndpoints": endpoints,
            "minioContainers": containers,
        }
        written = evidence_proof.write_evidence_proof(
            proof_path,
            scenario=SCENARIO,
            checks=typed_checks,
            meta=proof_meta,
        )
        loaded = evidence_proof.load_evidence_proof(written, expected_scenario=SCENARIO)
        for check_name, check_item in typed_checks.items():
            assert evidence_proof.validate_check(check_name, check_item) == []
            assert loaded["checks"][check_name] == check_item
        for suffix_name, dedicated_scenario, dedicated_checks in (
            ("trust", TRUST_PROOF_SCENARIO, trust_checks),
            ("replica", REPLICA_PROOF_SCENARIO, {**replica_checks, **runtime_checks}),
            ("dr", DR_PROOF_SCENARIO, dr_checks),
        ):
            dedicated_path = proof_path.with_name(f"{proof_path.stem}-{suffix_name}{proof_path.suffix}")
            dedicated_written = evidence_proof.write_evidence_proof(
                dedicated_path,
                scenario=dedicated_scenario,
                checks=dedicated_checks,
                meta={**proof_meta, "sourceScenario": SCENARIO},
            )
            dedicated_loaded = evidence_proof.load_evidence_proof(
                dedicated_written,
                expected_scenario=dedicated_scenario,
            )
            assert dedicated_loaded["checks"] == dedicated_checks
    finally:
        node_a.close()
        node_b.close()
