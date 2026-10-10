from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

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
    assert cache.fetch(
        digest,
        len(data),
        lambda _offset: (_ for _ in ()).throw(AssertionError("cache hit must not read")),
    ) == path
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


def _put(cache: backup_component_cache.ComponentCache, data: bytes) -> tuple[str, Path]:
    digest = hashlib.sha256(data).hexdigest()
    return digest, cache.fetch(digest, len(data), lambda offset: iter((data[offset:],)))


def test_cache_pins_contain_only_ciphertext_digests_and_survive_new_instance(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    cache = backup_component_cache.ComponentCache(root)
    first, _ = _put(cache, b"first")
    second, _ = _put(cache, b"second")

    pin_path = cache.pin("restore-private-project-name", [second, first, second])

    assert backup_component_cache.ComponentCache(root).pinned_digests() == {first, second}
    raw = pin_path.read_text(encoding="utf-8")
    assert "private-project" not in raw
    assert json.loads(raw) == {"digests": sorted([first, second]), "schemaVersion": 1}
    cache.unpin("restore-private-project-name")
    assert cache.pinned_digests() == set()


def test_cache_gc_is_lru_over_verified_unpinned_entries_only(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    oldest, oldest_path = _put(cache, b"oldest")
    pinned, pinned_path = _put(cache, b"pinned")
    newest, newest_path = _put(cache, b"newest")
    os.utime(oldest_path, (10, 10))
    os.utime(pinned_path, (20, 20))
    os.utime(newest_path, (30, 30))
    partial = cache.partial_path("f" * 64)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"partial-is-not-gc-candidate")
    cache.pin("active-restore", [pinned])

    report = cache.gc(quota_bytes=len(b"pinned") + len(b"newest"))

    assert report == {"beforeBytes": 18, "afterBytes": 12, "evicted": 1, "freedBytes": 6}
    assert not oldest_path.exists()
    assert pinned_path.exists()
    assert newest_path.exists()
    assert partial.exists()
    assert oldest not in cache.pinned_digests()


def test_cache_gc_refuses_malformed_pin_and_reports_pressure_when_all_pinned(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    digest, path = _put(cache, b"cannot-evict")
    cache.pin("active", [digest])

    report = cache.gc(quota_bytes=0)

    assert report["afterBytes"] == len(b"cannot-evict")
    assert report["overQuotaBytes"] == len(b"cannot-evict")
    assert path.exists()
    pin_dir = cache.root / "pins"
    (pin_dir / "corrupt.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(AppError, match="pin metadata"):
        cache.gc(quota_bytes=0)


def test_cache_rejects_invalid_control_inputs_and_pin_shapes(tmp_path: Path) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    assert backup_component_cache._digest_file(tmp_path / "missing") == (0, "")
    with pytest.raises(ValueError, match="owner_id"):
        cache.pin("", [])
    with pytest.raises(ValueError, match="non-negative"):
        cache.gc(quota_bytes=-1)
    with pytest.raises(ValueError, match="non-negative"):
        cache.inspect("a" * 64, -1)
    with pytest.raises(ValueError, match="non-negative"):
        cache.fetch("a" * 64, -1, lambda _offset: iter(()))
    cache.unpin("")

    pin_dir = cache.root / "pins"
    pin_dir.mkdir(parents=True)
    pin = pin_dir / "bad.json"
    for payload in ({"schemaVersion": 2, "digests": []}, {"schemaVersion": 1, "digests": [1]}, {"schemaVersion": 1, "digests": ["z" * 64]}):
        pin.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(AppError, match="pin metadata"):
            cache.pinned_digests()


def test_cache_promotes_complete_partial_and_bounds_stream_pieces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    data = b"complete-partial"
    digest = hashlib.sha256(data).hexdigest()
    partial = cache.partial_path(digest)
    partial.parent.mkdir(parents=True)
    partial.write_bytes(data)
    assert cache.fetch(digest, len(data), lambda _offset: (_ for _ in ()).throw(AssertionError("must not read"))).read_bytes() == data

    second = b"bounded-stream"
    second_digest = hashlib.sha256(second).hexdigest()
    oversized = cache.partial_path(second_digest)
    oversized.parent.mkdir(parents=True, exist_ok=True)
    oversized.write_bytes(second + b"stale")
    progress: list[int] = []
    path = cache.fetch(
        second_digest,
        len(second),
        lambda offset: iter((b"", second[offset:] + b"ignored", b"must-not-be-consumed")),
        progress=progress.append,
    )
    assert path.read_bytes() == second
    assert progress == [len(second)]

    monkeypatch.setattr(backup_component_cache.os, "utime", lambda *_args: (_ for _ in ()).throw(OSError("readonly")))
    assert cache.get(second_digest, len(second)) == path


def test_cache_empty_gc_ignores_noncanonical_files_and_unlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    assert cache.pinned_digests() == set()
    assert cache.gc(quota_bytes=0) == {"beforeBytes": 0, "afterBytes": 0, "evicted": 0, "freedBytes": 0}

    object_root = cache.root / "sha256" / "aa"
    object_root.mkdir(parents=True)
    (object_root / "not-a-digest.age").write_bytes(b"ignored")
    digest, path = _put(cache, b"locked-entry")
    real_unlink = Path.unlink

    def fail_target_unlink(target: Path, *args: Any, **kwargs: Any) -> None:
        if target == path:
            raise OSError("locked")
        real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target_unlink)
    report = cache.gc(quota_bytes=0)
    assert report["afterBytes"] == len(b"locked-entry")
    assert report["overQuotaBytes"] == len(b"locked-entry")
    assert report["evicted"] == 0
    assert digest not in cache.pinned_digests()


def test_cache_gc_ignores_candidate_that_disappears_during_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = backup_component_cache.ComponentCache(tmp_path / "cache")
    _digest, path = _put(cache, b"disappearing")
    real_stat = Path.stat

    def fail_candidate_stat(target: Path, *args: Any, **kwargs: Any) -> Any:
        if target == path:
            raise OSError("gone")
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_candidate_stat)
    assert cache.gc(quota_bytes=0) == {"beforeBytes": 0, "afterBytes": 0, "evicted": 0, "freedBytes": 0}
