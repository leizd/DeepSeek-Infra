"""Renewable CAS leases protecting remote recovery object sets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace.backup_target_store import put_json_if_absent, put_json_if_match, read_json

DEFAULT_TTL_SECONDS = 6 * 3600
DEFAULT_RENEW_INTERVAL_SECONDS = 15 * 60
DEFAULT_EXPIRED_HOLD_GRACE_SECONDS = 24 * 3600
RECOVERY_REQUIRED_TTL_SECONDS = 24 * 3600
TERMINAL_PHASES = frozenset({"complete", "aborted", "rolled-back", "failed"})


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def renew(
    store: Any,
    key: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    current = read_json(store, key)
    meta = store.stat(key)
    if current is None or meta is None or not meta.etag:
        raise AppError("Recovery hold lease is missing", code=ErrorCode.NOT_FOUND, status=404)
    observed_generation = current.get("generation")
    generation = observed_generation if isinstance(observed_generation, int) and observed_generation >= 0 else 0
    current_time = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    current_expiry = _parse_time(current.get("expiresAt"))
    next_expiry = current_time + timedelta(seconds=ttl_seconds)
    if current_expiry is not None and next_expiry <= current_expiry:
        next_expiry = current_expiry + timedelta(seconds=1)
    renewed = dict(current)
    renewed.update(
        schemaVersion=max(3, int(current.get("schemaVersion") or 0)),
        generation=generation + 1,
        renewedAt=_utc_iso(current_time),
        expiresAt=_utc_iso(next_expiry),
    )
    try:
        put_json_if_match(store, key, renewed, expected_etag=meta.etag)
    except AppError as exc:
        raise AppError("Recovery hold lease renewal conflict", code=ErrorCode.INVALID_REQUEST, status=409) from exc
    return renewed


def renew_session(
    store: Any,
    session: dict[str, Any],
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    min_interval_seconds: int = DEFAULT_RENEW_INTERVAL_SECONDS,
) -> bool:
    if str(session.get("phase") or "") in TERMINAL_PHASES:
        return False
    current_time = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    last_renewed = _parse_time(session.get("lastHoldRenewedAt")) or _parse_time(session.get("createdAt"))
    if last_renewed is not None and (current_time - last_renewed).total_seconds() < min_interval_seconds:
        return False
    keys = list(session.get("holdKeys") or [])
    if not keys and session.get("holdKey"):
        keys = [str(session["holdKey"])]
    if not keys:
        return False
    normalized_keys: list[str] = []
    for raw_key in keys:
        key = str(raw_key)
        if ":" in key:
            current = read_json(store, key)
            if current is None:
                raise AppError("Recovery hold lease is missing", code=ErrorCode.NOT_FOUND, status=404)
            safe_key = key.replace(":", "-")
            migrated = dict(current)
            migrated.update(schemaVersion=3, generation=int(current.get("generation") or 0))
            try:
                put_json_if_absent(store, safe_key, migrated)
            except AppError:
                pass
            store.delete_if_match(key)
            key = safe_key
        renew(store, key, now=current_time, ttl_seconds=ttl_seconds)
        normalized_keys.append(key)
    if normalized_keys != [str(key) for key in keys]:
        session["holdKeys"] = normalized_keys
    session["lastHoldRenewedAt"] = _utc_iso(current_time)
    return True


def renew_recovery_hold(
    store: Any,
    hold_entry: dict[str, Any],
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Renew a specific recovery hold entry by its holdKey using CAS."""
    key = str(hold_entry.get("holdKey") or "")
    if not key:
        raise AppError("Missing holdKey in hold_entry", code=ErrorCode.INVALID_REQUEST, status=400)
    renewed = renew(store, key, now=now, ttl_seconds=ttl_seconds)
    meta = store.stat(key)
    return {
        **hold_entry,
        "generation": renewed.get("generation", int(hold_entry.get("generation", 1)) + 1),
        "etag": meta.etag if meta else hold_entry.get("etag"),
        "expiresAt": renewed.get("expiresAt", hold_entry.get("expiresAt")),
        "renewedAt": renewed.get("renewedAt"),
    }

