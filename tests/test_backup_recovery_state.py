from __future__ import annotations

from pathlib import Path

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
    first = "a" * 64
    second = "b" * 64
    first_component = _component(tmp_path, first)
    Path(str(first_component["ciphertextPath"])).write_bytes(b"x" * 10)
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
