"""Offline qualification of DELETE observers; never execute native compilers."""
from __future__ import annotations

import argparse
import copy
import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("media_delete_probe", ROOT / "scripts/verify_native_media_delete.py")
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def observations() -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = dict(media=[dict(mediaId="media_keep"), dict(mediaId="media_other")],
        rows=[["target-a", 1, "media", "media_keep", "a"], ["target-b", 2, "media", "media_keep", "b"],
              ["other", 3, "media", "media_other", "a"], ["files", 4, "files", "media_keep", "a"]],
        rowContents={"target-a":"one", "target-b":"two", "other":"three", "files":"four"},
        vectors=[dict(rowid=n,sha256=str(n)) for n in (1,2,3,4)], files={
            ".media/objects/media_keep/source.pdf": "target-source", ".media/segments/media_keep.json": "target-segment",
            ".media/objects/media_other/source.pdf": "other-source", ".media/segments/media_other.json": "other-segment"})
    after = copy.deepcopy(before)
    after["media"] = before["media"][1:]
    after["rows"] = before["rows"][2:]
    after["rowContents"] = {"other":"three", "files":"four"}
    after["vectors"] = before["vectors"][2:]
    after["files"][".media/objects/media_keep/source.pdf"] = None
    after["files"][".media/segments/media_keep.json"] = None
    return before, after


def test_delete_observer_requires_target_cleanup_and_unrelated_retention() -> None:
    before, after = observations()
    probe.require_deleted(before, after, after)
    for part in ("media", "rows", "vectors", "rowContents"):
        wrong = copy.deepcopy(after)
        wrong[part] = []
        with pytest.raises(probe.patch.ProbeFailure):
            probe.require_deleted(before, wrong, after)
    wrong = copy.deepcopy(after)
    wrong["vectors"][0]["sha256"] = "changed-embedding"
    with pytest.raises(probe.patch.ProbeFailure, match="VECTOR_ROWS_DIFFER"):
        probe.require_deleted(before, wrong, after)
    for relative in after["files"]:
        wrong = copy.deepcopy(after)
        wrong["files"][relative] = "unexpected" if after["files"][relative] is None else None
        with pytest.raises(probe.patch.ProbeFailure):
            probe.require_deleted(before, wrong, after)


def test_delete_snapshot_never_creates_an_absent_store(tmp_path: Path) -> None:
    with pytest.raises(probe.patch.ProbeFailure, match="LIBRARY_REQUIRED"):
        probe.snapshot(tmp_path, "sqlite")
    assert not list(tmp_path.iterdir())


def test_delete_snapshot_refuses_linked_roots_and_libraries(tmp_path: Path) -> None:
    root = tmp_path / "profile"
    (root / ".media").mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text('{"media":[]}')
    try:
        (root / ".media/library.json").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    with pytest.raises(probe.patch.ProbeFailure, match="LINK_REFUSED"):
        probe.snapshot(root, "sqlite")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(probe.patch.ProbeFailure, match="LINK_REFUSED"):
        probe.snapshot(alias, "sqlite")
    assert target.read_text() == '{"media":[]}'


def test_delete_crash_observation_requires_all_cleanup_to_be_unfinished() -> None:
    before, after = observations()
    probe.require_cleanup_not_started(before, before)
    with pytest.raises(probe.patch.ProbeFailure, match="CLEANUP_ALREADY_STARTED"):
        probe.require_cleanup_not_started(before, after)
    for relative in before["files"]:
        partial = copy.deepcopy(before)
        partial["files"][relative] = None
        with pytest.raises(probe.patch.ProbeFailure, match="CLEANUP_ALREADY_STARTED"):
            probe.require_cleanup_not_started(before, partial)
    for part in ("rows", "rowContents", "vectors"):
        partial = copy.deepcopy(before)
        partial[part] = []
        with pytest.raises(probe.patch.ProbeFailure, match="CLEANUP_ALREADY_STARTED"):
            probe.require_cleanup_not_started(before, partial)


def test_delete_fixture_pair_uses_identical_seeded_data_before_the_oracle(tmp_path: Path) -> None:
    profile, mirror, out = tmp_path / "profile", tmp_path / "oracle", tmp_path / "logs"
    profile.mkdir()
    out.mkdir()
    before, expected, oracle = probe.seed_pair(profile, mirror, "sqlite", out)
    assert oracle["first"] == dict(ok=True, deleted=1)
    assert probe.snapshot(profile, "sqlite") == before
    probe.require_cleanup_not_started(before, probe.snapshot(profile, "sqlite"))
    probe.require_deleted(before, expected, expected)


def test_delete_native_probe_refuses_local_execution_before_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("native build or process must not execute locally")
    monkeypatch.setattr(probe.subprocess, "check_output", forbidden)
    with pytest.raises(probe.patch.ProbeFailure, match="CLOUD_ONLY"):
        probe.run(argparse.Namespace(), {})


def test_offline_original_asgi_delete_removes_only_its_seeded_state(tmp_path: Path) -> None:
    profile = tmp_path / "oracle"
    seeded = probe.offline(profile, "sqlite", probe.SEED, tmp_path / "seed.log")
    assert seeded["rows"] == 4 and seeded["vector"] is False
    before = probe.snapshot(profile, "sqlite")
    result = probe.offline(profile, "sqlite", probe.ORACLE, tmp_path / "oracle.log")
    assert result == dict(refused=401, firstStatus=200, first=dict(ok=True, deleted=1),
        repeatStatus=200, repeat=dict(ok=True, deleted=0))
    after = probe.snapshot(profile, "sqlite")
    probe.require_deleted(before, after, after)
