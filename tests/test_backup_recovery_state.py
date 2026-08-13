from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import backup_recovery_state


def _component(tmp_path: Path, digest: str, *, size: int = 10, etag: str = "etag", version: str | None = None) -> dict[str, object]:
    return {
        "objectDigest": digest,
        "expectedBytes": size,
        "remoteETag": etag,
        "remoteVersionId": version,
        "ciphertextPath": str(tmp_path / f"{digest}.age"),
    }


def test_legacy_fetch_index_migrates_to_digest_keyed_states(tmp_path: Path) -> None:
    first_bytes = b"x" * 10
    first = hashlib.sha256(first_bytes).hexdigest()
    second = "b" * 64
    first_component = _component(tmp_path, first)
    Path(str(first_component["ciphertextPath"])).write_bytes(first_bytes)
    session: dict[str, object] = {
        "componentFetchIndex": 1,
        "chain": [{"requiredComponents": [first_component, _component(tmp_path, second)]}],
    }

    states = backup_recovery_state.ensure_component_states(session)

    assert states[first]["state"] == "verified"
    assert states[first]["downloadedBytes"] == 10
    assert states[second]["state"] == "queued"
    assert states[second]["downloadedBytes"] == 0
    assert "componentFetchIndex" not in session


def test_legacy_fetch_index_without_verified_local_file_restarts_from_zero(tmp_path: Path) -> None:
    digest = "9" * 64
    session: dict[str, object] = {
        "componentFetchIndex": 1,
        "chain": [{"requiredComponents": [_component(tmp_path, digest)]}],
    }

    states = backup_recovery_state.ensure_component_states(session)

    assert states[digest]["state"] == "queued"
    assert states[digest]["downloadedBytes"] == 0


def test_existing_partial_state_survives_when_source_and_length_match(tmp_path: Path) -> None:
    digest = "c" * 64
    component = _component(tmp_path, digest, version="v1")
    path = Path(str(component["ciphertextPath"]))
    path.write_bytes(b"1234")
    session: dict[str, object] = {
        "componentStates": {
            digest: {
                "state": "partial",
                "downloadedBytes": 4,
                "expectedBytes": 10,
                "remoteETag": "etag",
                "remoteVersionId": "v1",
            }
        },
        "chain": [{"requiredComponents": [component]}],
    }

    states = backup_recovery_state.ensure_component_states(session)

    assert states[digest]["state"] == "partial"
    assert states[digest]["downloadedBytes"] == 4
    assert path.read_bytes() == b"1234"


def test_invalid_partial_is_scrubbed_when_length_or_source_changes(tmp_path: Path) -> None:
    for suffix, state_override in (
        ("length", {"downloadedBytes": 4}),
        ("etag", {"downloadedBytes": 3, "remoteETag": "old-etag"}),
        ("version", {"downloadedBytes": 3, "remoteVersionId": "old-version"}),
    ):
        digest = ("d" if suffix == "length" else "e" if suffix == "etag" else "f") * 64
        component = _component(tmp_path, digest, version="v2")
        path = Path(str(component["ciphertextPath"]))
        path.write_bytes(b"123")
        state = {
            "state": "partial",
            "downloadedBytes": 3,
            "expectedBytes": 10,
            "remoteETag": "etag",
            "remoteVersionId": "v2",
            **state_override,
        }
        session: dict[str, object] = {
            "componentStates": {digest: state},
            "chain": [{"requiredComponents": [component]}],
        }

        states = backup_recovery_state.ensure_component_states(session)

        assert states[digest]["state"] == "queued"
        assert states[digest]["downloadedBytes"] == 0
        assert not path.exists()


def test_same_length_tampering_invalidates_verified_state(tmp_path: Path) -> None:
    good = b"verified!"
    digest = hashlib.sha256(good).hexdigest()
    component = _component(tmp_path, digest, size=len(good))
    path = Path(str(component["ciphertextPath"]))
    path.write_bytes(b"tampered!")
    session: dict[str, object] = {
        "componentStates": {
            digest: {
                "state": "verified",
                "downloadedBytes": len(good),
                "expectedBytes": len(good),
                "remoteETag": "etag",
                "remoteVersionId": None,
            }
        },
        "chain": [{"requiredComponents": [component]}],
    }

    states = backup_recovery_state.ensure_component_states(session)

    assert states[digest]["state"] == "queued"
    assert not path.exists()


def test_fsynced_full_download_promotes_to_verified_after_restart(tmp_path: Path) -> None:
    data = b"download-complete"
    digest = hashlib.sha256(data).hexdigest()
    component = _component(tmp_path, digest, size=len(data))
    Path(str(component["ciphertextPath"])).write_bytes(data)
    session: dict[str, object] = {
        "componentStates": {
            digest: {
                "state": "downloading",
                "downloadedBytes": len(data),
                "expectedBytes": len(data),
                "remoteETag": "etag",
                "remoteVersionId": None,
            }
        },
        "chain": [{"requiredComponents": [component]}],
    }

    states = backup_recovery_state.ensure_component_states(session)

    assert states[digest]["state"] == "verified"


def test_required_components_ignores_malformed_members() -> None:
    assert backup_recovery_state.required_components({}) == []
    assert backup_recovery_state.required_components({"chain": "bad"}) == []
    assert backup_recovery_state.required_components(
        {"chain": [None, {"requiredComponents": "bad"}, {"requiredComponents": [None, {"objectDigest": "a" * 64}]}]}
    ) == [{"objectDigest": "a" * 64}]


def test_state_rejects_bad_digest_and_update_rehydrates_missing_entry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canonical"):
        backup_recovery_state.ensure_component_states(
            {"chain": [{"requiredComponents": [_component(tmp_path, "bad")]}]}
        )

    digest = "a" * 64
    component = _component(tmp_path, digest)
    session: dict[str, object] = {"componentStates": {}, "chain": [{"requiredComponents": [component]}]}
    state = backup_recovery_state.update_component_state(session, component, state="failed", downloaded_bytes=3)
    assert state["state"] == "failed"
    assert state["downloadedBytes"] == 3
    with pytest.raises(ValueError, match="invalid component state"):
        backup_recovery_state.update_component_state(session, component, state="complete", downloaded_bytes=10)


def test_state_defaults_priority_and_ignores_empty_scrub_path(tmp_path: Path) -> None:
    component = _component(tmp_path, "b" * 64)
    component["priority"] = "high"
    session: dict[str, object] = {"chain": [{"requiredComponents": [component]}]}
    assert backup_recovery_state.ensure_component_states(session)["b" * 64]["priority"] == 2
    backup_recovery_state._scrub_partial({"ciphertextPath": ""})
