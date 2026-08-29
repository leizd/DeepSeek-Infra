"""Credential-free, digest-bound, read-only federation readiness (4.7.5 Gate N)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

SUPPORTED_WIRE_VERSIONS = frozenset(
    {
        "object-set-v1",
        "receipt-v4",
        "commit-v4",
        "fastcdc-v3",
        "control-authority-v1",
        "authority-checkpoint-v1",
        "dr-readiness-proof-v1",
        "evidence-proof-v2",
    }
)
FORBIDDEN_KEY_FRAGMENTS = (
    "credential",
    "secret",
    "password",
    "privatekey",
    "ageidentity",
    "identity",
    "token",
    "authorityprivate",
)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _contains_forbidden(value: Any, *, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower().replace("_", "").replace("-", "")
            next_path = f"{path}.{key}" if path else str(key)
            if any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS):
                found.append(next_path)
            found.extend(_contains_forbidden(item, path=next_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_contains_forbidden(item, path=f"{path}[{index}]"))
    return found


def build_federation_snapshot(
    *,
    fleet_id: str,
    wire_compatibility: list[str],
    available_failure_domains: list[str],
    forecast_headroom: int | None,
    cost_class: str,
    readiness: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    compatible = [str(item) for item in wire_compatibility]
    unknown = [item for item in compatible if item not in SUPPORTED_WIRE_VERSIONS]
    payload = {
        "fleetId": str(fleet_id),
        "wireCompatibility": compatible,
        "availableFailureDomains": list(available_failure_domains),
        "forecastHeadroom": forecast_headroom,
        "costClass": str(cost_class),
        "readiness": str(readiness),
        "status": "INCOMPATIBLE" if unknown else "OK",
        "incompatibleWireVersions": unknown,
        "generatedAt": _utc_iso(now),
    }
    forbidden = _contains_forbidden(payload)
    if forbidden:
        raise ValueError(f"federation snapshot contains forbidden keys: {forbidden}")
    payload["snapshotDigest"] = _digest(payload)
    return payload


def simulate_federated_placement(
    local_snapshot: dict[str, Any],
    remote_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Read-only cross-fleet thought experiment. Never mutates the remote fleet."""
    if str(remote_snapshot.get("status") or "") == "INCOMPATIBLE":
        return {
            "status": "INCOMPATIBLE",
            "reason": "incompatibleFleetWireVersionFailsClosed",
            "remoteMutations": 0,
            "localSnapshotDigest": local_snapshot.get("snapshotDigest"),
            "remoteSnapshotDigest": remote_snapshot.get("snapshotDigest"),
        }
    forbidden = _contains_forbidden(remote_snapshot)
    if forbidden:
        return {
            "status": "REJECTED",
            "reason": "federationSnapshotContainsCredentials",
            "remoteMutations": 0,
        }
    return {
        "status": "SIMULATED",
        "remoteMutations": 0,
        "authorityMutationCount": 0,
        "localSnapshotDigest": local_snapshot.get("snapshotDigest"),
        "remoteSnapshotDigest": remote_snapshot.get("snapshotDigest"),
        "combinedFailureDomains": sorted(
            set(str(item) for item in (local_snapshot.get("availableFailureDomains") or []))
            | set(str(item) for item in (remote_snapshot.get("availableFailureDomains") or []))
        ),
        "remoteForecastHeadroom": remote_snapshot.get("forecastHeadroom"),
        "remoteCostClass": remote_snapshot.get("costClass"),
        "remoteReadiness": remote_snapshot.get("readiness"),
    }
