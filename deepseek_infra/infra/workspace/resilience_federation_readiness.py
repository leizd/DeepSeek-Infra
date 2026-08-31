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
REQUIRED_READ_ONLY_FEDERATION_WIRES = frozenset(
    {
        "object-set-v1",
        "receipt-v4",
        "commit-v4",
        "fastcdc-v3",
    }
)
FEDERATION_SNAPSHOT_SCHEMA = "federation-readiness-snapshot-v1"
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


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    return _digest({key: value for key, value in snapshot.items() if key != "snapshotDigest"})


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
    fleet = str(fleet_id).strip()
    if not fleet:
        raise ValueError("fleetId is required")
    compatible = sorted({str(item) for item in wire_compatibility})
    unknown = [item for item in compatible if item not in SUPPORTED_WIRE_VERSIONS]
    missing = sorted(REQUIRED_READ_ONLY_FEDERATION_WIRES - set(compatible))
    payload = {
        "snapshotSchema": FEDERATION_SNAPSHOT_SCHEMA,
        "fleetId": fleet,
        "wireCompatibility": compatible,
        "availableFailureDomains": list(available_failure_domains),
        "forecastHeadroom": forecast_headroom,
        "costClass": str(cost_class),
        "readiness": str(readiness),
        "status": "INCOMPATIBLE" if unknown or missing else "OK",
        "incompatibleWireVersions": unknown,
        "missingRequiredWireVersions": missing,
        "generatedAt": _utc_iso(now),
    }
    forbidden = _contains_forbidden(payload)
    if forbidden:
        raise ValueError(f"federation snapshot contains forbidden keys: {forbidden}")
    payload["snapshotDigest"] = _snapshot_digest(payload)
    return payload


def _validate_snapshot(
    snapshot: dict[str, Any],
    *,
    now: datetime,
    max_snapshot_age_seconds: int,
    max_future_skew_seconds: int,
) -> str | None:
    if _contains_forbidden(snapshot):
        return "federationSnapshotContainsCredentials"
    if str(snapshot.get("snapshotSchema") or "") != FEDERATION_SNAPSHOT_SCHEMA:
        return "federationSnapshotSchemaInvalid"
    if not str(snapshot.get("fleetId") or "").strip():
        return "federationFleetIdentityMissing"
    if str(snapshot.get("snapshotDigest") or "") != _snapshot_digest(snapshot):
        return "federationSnapshotDigestInvalid"
    generated = _parse_iso(snapshot.get("generatedAt"))
    if generated is None:
        return "federationSnapshotTimestampInvalid"
    age_seconds = (now - generated).total_seconds()
    if age_seconds > max_snapshot_age_seconds:
        return "federationSnapshotExpired"
    if age_seconds < -max_future_skew_seconds:
        return "federationSnapshotFromFuture"
    return None


def simulate_federated_placement(
    local_snapshot: dict[str, Any],
    remote_snapshot: dict[str, Any],
    *,
    now: datetime | None = None,
    max_snapshot_age_seconds: int = 300,
    max_future_skew_seconds: int = 30,
) -> dict[str, Any]:
    """Read-only cross-fleet thought experiment. Never mutates the remote fleet."""
    current = now or datetime.now(tz=timezone.utc)
    max_age = max(1, int(max_snapshot_age_seconds))
    max_future = max(0, int(max_future_skew_seconds))
    local_error = _validate_snapshot(local_snapshot, now=current, max_snapshot_age_seconds=max_age, max_future_skew_seconds=max_future)
    remote_error = _validate_snapshot(remote_snapshot, now=current, max_snapshot_age_seconds=max_age, max_future_skew_seconds=max_future)
    error = local_error or remote_error
    if error is not None:
        return {
            "status": "STALE" if error in {"federationSnapshotExpired", "federationSnapshotFromFuture"} else "REJECTED",
            "reason": error,
            "remoteMutations": 0,
            "authorityMutationCount": 0,
        }
    if str(local_snapshot.get("status") or "") == "INCOMPATIBLE" or str(remote_snapshot.get("status") or "") == "INCOMPATIBLE":
        return {
            "status": "INCOMPATIBLE",
            "reason": "incompatibleFleetWireVersionFailsClosed",
            "remoteMutations": 0,
            "localSnapshotDigest": local_snapshot.get("snapshotDigest"),
            "remoteSnapshotDigest": remote_snapshot.get("snapshotDigest"),
        }
    local_fleet = str(local_snapshot.get("fleetId") or "")
    remote_fleet = str(remote_snapshot.get("fleetId") or "")
    if local_fleet == remote_fleet:
        return {
            "status": "REJECTED",
            "reason": "federationFleetIdentityCollision",
            "remoteMutations": 0,
            "authorityMutationCount": 0,
        }
    return {
        "status": "SIMULATED",
        "remoteMutations": 0,
        "authorityMutationCount": 0,
        "localFleetId": local_fleet,
        "remoteFleetId": remote_fleet,
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
