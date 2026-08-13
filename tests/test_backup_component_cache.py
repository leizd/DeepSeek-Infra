from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_component_cache


def test_cache_fetch_commits_verified_ciphertext_and_reuses_hit(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    data = b"encrypted-cache-component" * 100
    digest = hashlib.sha256(data).hexdigest()
    offsets: list[int] = []

    def read_from(offset: int):
        offsets.append(offset)
        return iter((data[offset : offset + 37], data[offset + 37 :]))

    path = cache.fetch(digest, len(data), read_from)

    assert path == tmp_path / "cache" / "sha256" / digest[:2] / f"{digest}.age"
    assert path.read_bytes() == data
    assert cache.get(digest, len(data)) == path
    assert offsets == [0]
    assert sorted(item.name for item in path.parent.iterdir()) == [f"{digest}.age"]


def test_cache_corruption_is_scrubbed_and_never_returned(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    good = b"verified-ciphertext"
    digest = hashlib.sha256(good).hexdigest()
    path = cache.path_for(digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tampered-ciphertext")

    assert cache.get(digest, len(good)) is None
    assert not path.exists()
    assert cache.fetch(digest, len(good), lambda offset: iter((good[offset:],))).read_bytes() == good


def test_cache_short_read_keeps_partial_and_resumes_at_fsynced_offset(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    data = b"resumable-encrypted-component" * 100
    digest = hashlib.sha256(data).hexdigest()
    split = len(data) // 2

    with pytest.raises(AppError, match="truncated"):
        cache.fetch(digest, len(data), lambda offset: iter((data[offset:split],)))

    partial = cache.partial_path(digest)
    assert partial.read_bytes() == data[:split]
    offsets: list[int] = []

    def resume(offset: int):
        offsets.append(offset)
        return iter((data[offset:],))

    path = cache.fetch(digest, len(data), resume)

    assert offsets == [split]
    assert path.read_bytes() == data
    assert not partial.exists()


def test_cache_rejects_full_wrong_digest_and_canonicalizes_keys(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    expected = b"expected"
    wrong = b"wrong!!!"
    digest = hashlib.sha256(expected).hexdigest()

    with pytest.raises(AppError, match="digest mismatch"):
        cache.fetch(digest, len(expected), lambda _offset: iter((wrong,)))
    assert not cache.partial_path(digest).exists()
    with pytest.raises(ValueError, match="canonical"):
        cache.path_for("../secret")


def test_cache_fsyncs_partial_when_source_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    data = b"durable-prefix"
    digest = hashlib.sha256(data + b"suffix").hexdigest()
    fsync_calls: list[int] = []
    monkeypatch.setattr(backup_component_cache.os, "fsync", lambda fd: fsync_calls.append(fd))

    def broken(_offset: int):
        yield data
        raise OSError("source disconnected")

    with pytest.raises(OSError, match="disconnected"):
        cache.fetch(digest, len(data) + len(b"suffix"), broken)

    assert fsync_calls
    assert cache.partial_path(digest).read_bytes() == data
