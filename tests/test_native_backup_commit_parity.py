"""Byte-level parity for the native commit-marker reader.

`backup_commit.rs` decides which published commit is newest, and every catalog append
records that generation. If the native reader disagreed with Python about the winner,
a new catalog entry would carry a different `targetGeneration` and the two chains would
diverge permanently -- a corruption that no single-marker test would reveal.

So this drives the *real Python functions* over generated directory layouts and
compares them with the Rust implementation's contract, which is exercised natively by
the unit tests in that module. The cases here are the ones where a reasonable
implementation goes wrong:

* a tie at the highest generation, where `max()` keeps the first maximal element and
  therefore depends on lexicographic path order;
* markers that must be skipped (unparsable, non-object, empty `commitHash`);
* a generation expressed as a numeric string, and falsy values collapsing to zero;
* an absent `commits/` directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_publish  # noqa: E402


def _marker(bucket: str, name: str, generation: object) -> dict:
    return {"commitHash": f"{bucket}/{name}", "targetGeneration": generation}


def _write(root: Path, bucket: str, name: str, payload: object) -> None:
    directory = root / "commits" / bucket
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def _oracle(root: Path) -> tuple[int, str]:
    """The two facts the native reader must reproduce: generation and commit hash."""
    latest = backup_publish.latest_commit(root)
    if latest is None:
        return 0, ""
    return int(latest.get("targetGeneration") or 0), str(latest.get("commitHash") or "")


def test_absent_commits_directory_yields_no_commit(tmp_path: Path) -> None:
    assert _oracle(tmp_path) == (0, "")


def test_highest_generation_wins_across_buckets(tmp_path: Path) -> None:
    _write(tmp_path, "aa", "one.json", _marker("aa", "one.json", 3))
    _write(tmp_path, "zz", "two.json", _marker("zz", "two.json", 9))
    assert _oracle(tmp_path) == (9, "zz/two.json")


def test_a_tied_generation_resolves_to_the_first_sorted_path(tmp_path: Path) -> None:
    """This is the case a naive `>=` scan gets wrong.

    `max(markers, key=...)` returns the first maximal element, and markers arrive in
    `sorted(commits_dir.glob("*/*.json"))` order, so `aa/bbb.json` wins over
    `zz/aaa.json` even though the file name sorts later.
    """
    _write(tmp_path, "aa", "bbb.json", _marker("aa", "bbb.json", 5))
    _write(tmp_path, "zz", "aaa.json", _marker("zz", "aaa.json", 5))
    _write(tmp_path, "mm", "ccc.json", _marker("mm", "ccc.json", 3))
    assert _oracle(tmp_path) == (5, "aa/bbb.json")


def test_the_tie_break_follows_the_bucket_before_the_file_name(tmp_path: Path) -> None:
    """Ordering is by full path, so the bucket decides before the file name does."""
    _write(tmp_path, "aa", "zzz.json", _marker("aa", "zzz.json", 4))
    _write(tmp_path, "bb", "aaa.json", _marker("bb", "aaa.json", 4))
    assert _oracle(tmp_path) == (4, "aa/zzz.json")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="null-document"),
        pytest.param([1, 2, 3], id="array-document"),
        pytest.param({"targetGeneration": 99}, id="no-commit-hash"),
        pytest.param({"commitHash": "", "targetGeneration": 99}, id="empty-commit-hash"),
        pytest.param({"commitHash": None, "targetGeneration": 99}, id="null-commit-hash"),
        pytest.param({"commitHash": 0, "targetGeneration": 99}, id="zero-commit-hash"),
    ],
)
def test_entries_that_are_not_markers_are_skipped(tmp_path: Path, payload: object) -> None:
    """A stray file must not become the winner, and must not fail the whole read."""
    _write(tmp_path, "aa", "bogus.json", payload)
    _write(tmp_path, "aa", "real.json", _marker("aa", "real.json", 2))
    assert _oracle(tmp_path) == (2, "aa/real.json")


def test_unparsable_json_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "commits" / "aa"
    directory.mkdir(parents=True)
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path, "aa", "real.json", _marker("aa", "real.json", 1))
    assert _oracle(tmp_path) == (1, "aa/real.json")


def test_a_generation_may_be_a_numeric_string(tmp_path: Path) -> None:
    _write(tmp_path, "aa", "text.json", {"commitHash": "aa/text.json", "targetGeneration": "7"})
    assert _oracle(tmp_path) == (7, "aa/text.json")


@pytest.mark.parametrize("falsy", [None, 0, ""])
def test_falsy_generations_collapse_to_zero(tmp_path: Path, falsy: object) -> None:
    _write(tmp_path, "aa", "zero.json", {"commitHash": "aa/zero.json", "targetGeneration": falsy})
    assert _oracle(tmp_path) == (0, "aa/zero.json")


def test_non_json_files_are_ignored(tmp_path: Path) -> None:
    directory = tmp_path / "commits" / "aa"
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("hello", encoding="utf-8")
    _write(tmp_path, "aa", "real.json", _marker("aa", "real.json", 1))
    assert _oracle(tmp_path) == (1, "aa/real.json")


def test_a_valid_marker_hashes_over_its_contents_minus_the_hash(tmp_path: Path) -> None:
    """`commit_marker_valid` is what the native reader exposes for verification."""
    marker = {
        "schemaVersion": 4,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "a" * 64,
        "controlObjectDigest": "b" * 64,
        "targetGeneration": 3,
        "previousCommitHash": backup_publish.GENESIS_COMMIT_HASH,
    }
    assert not backup_publish.commit_marker_valid(marker)
    marker["commitHash"] = backup_publish._commit_hash(marker)
    assert backup_publish.commit_marker_valid(marker)
    # Any covered field change invalidates it.
    marker["targetGeneration"] = 4
    assert not backup_publish.commit_marker_valid(marker)
