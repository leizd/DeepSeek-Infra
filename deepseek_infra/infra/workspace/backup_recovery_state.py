"""Durable per-component state for object-set recovery jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

COMPONENT_STATES = frozenset({"queued", "downloading", "partial", "verified", "failed"})


def required_components(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Return required payload descriptors in their stable chain order."""
    components: list[dict[str, Any]] = []
    chain = session.get("chain")
    if not isinstance(chain, list):
        return components
    for member in chain:
        if not isinstance(member, dict):
            continue
        raw_required = member.get("requiredComponents")
        if not isinstance(raw_required, list):
            continue
        components.extend(item for item in raw_required if isinstance(item, dict))
    return components


def _source_matches(state: dict[str, Any], component: dict[str, Any]) -> bool:
    return (
        str(state.get("remoteETag") or "") == str(component.get("remoteETag") or "")
        and str(state.get("remoteVersionId") or "") == str(component.get("remoteVersionId") or "")
        and int(state.get("expectedBytes") or -1) == int(component.get("expectedBytes") or -1)
    )


def _local_length(component: dict[str, Any]) -> int:
    path = Path(str(component.get("ciphertextPath") or ""))
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _scrub_partial(component: dict[str, Any]) -> None:
    path = Path(str(component.get("ciphertextPath") or ""))
    if not str(component.get("ciphertextPath") or ""):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # The subsequent download opens from offset zero and still verifies the
        # complete digest; inability to remove is surfaced by that write path.
        pass


def _queued_state(component: dict[str, Any]) -> dict[str, Any]:
    raw_priority = component.get("priority")
    return {
        "state": "queued",
        "downloadedBytes": 0,
        "expectedBytes": int(component.get("expectedBytes") or 0),
        "remoteETag": component.get("remoteETag"),
        "remoteVersionId": component.get("remoteVersionId"),
        "priority": int(raw_priority) if isinstance(raw_priority, int) else 2,
    }


def ensure_component_states(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize and migrate object-set payload progress in-place.

    Legacy sessions used one monotonically increasing ``componentFetchIndex``.
    The migration maps completed prefixes to verified digest-keyed entries and
    removes the scalar index. Existing partials survive only when their source
    identity, expected size, recorded byte count, and local file length agree.
    """
    components = required_components(session)
    raw_states = session.get("componentStates")
    prior_states = raw_states if isinstance(raw_states, dict) else {}
    legacy_index = max(0, int(session.get("componentFetchIndex") or 0))
    normalized: dict[str, dict[str, Any]] = {}

    for index, component in enumerate(components):
        digest = str(component.get("objectDigest") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("component digest must be canonical sha256")
        expected = int(component.get("expectedBytes") or 0)
        existing_raw = prior_states.get(digest)
        existing = dict(existing_raw) if isinstance(existing_raw, dict) else None
        # Only the legacy scalar checkpoint is a durable completion record.
        # The descriptor's ``fetched`` flag is a derived, mutable projection and
        # must never bypass source-identity validation in a fresh session.
        legacy_verified = index < legacy_index

        if existing is None:
            state = _queued_state(component)
            local_length = _local_length(component)
            if legacy_verified and local_length == expected:
                state.update(state="verified", downloadedBytes=expected)
            elif local_length:
                _scrub_partial(component)
            normalized[digest] = state
            continue

        state_name = str(existing.get("state") or "queued")
        downloaded = int(existing.get("downloadedBytes") or 0)
        valid_source = _source_matches(existing, component)
        local_length = _local_length(component)
        valid_verified = state_name == "verified" and valid_source and downloaded == expected and local_length == expected
        valid_partial = (
            state_name in {"partial", "downloading"}
            and valid_source
            and 0 < downloaded < expected
            and local_length == downloaded
        )

        if valid_verified:
            state = _queued_state(component)
            state.update(state="verified", downloadedBytes=expected)
        elif valid_partial:
            state = _queued_state(component)
            state.update(state="partial", downloadedBytes=downloaded)
        else:
            if local_length:
                _scrub_partial(component)
            state = _queued_state(component)
        normalized[digest] = state

    session["componentStates"] = normalized
    session.pop("componentFetchIndex", None)
    return normalized


def update_component_state(
    session: dict[str, Any],
    component: dict[str, Any],
    *,
    state: str,
    downloaded_bytes: int,
) -> dict[str, Any]:
    """Persist one normalized state in the caller-owned session mapping."""
    if state not in COMPONENT_STATES:
        raise ValueError(f"invalid component state: {state}")
    digest = str(component.get("objectDigest") or "")
    raw_states = session.get("componentStates")
    states = raw_states if isinstance(raw_states, dict) else ensure_component_states(session)
    if digest not in states:
        states = ensure_component_states(session)
    current = dict(states[digest])
    current["state"] = state
    current["downloadedBytes"] = int(downloaded_bytes)
    current["expectedBytes"] = int(component.get("expectedBytes") or 0)
    current["remoteETag"] = component.get("remoteETag")
    current["remoteVersionId"] = component.get("remoteVersionId")
    states[digest] = current
    session["componentStates"] = states
    return current
