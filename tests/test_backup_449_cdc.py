"""fastcdc-gear-v2 golden vectors and protocol contracts (4.4.9).

The protocol version is pinned: any change to the gear table, the boundary
masks, the min/avg/max sizes, or the rolling-hash update silently changes every
chunk boundary and therefore every downstream lineage. Golden vectors below
freeze the exact boundaries so a protocol change is a hard test failure, not a
silent data-layout drift.
"""

from __future__ import annotations

import io
import random

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_incremental


def _corpus_random(size: int, seed: int = 42) -> bytes:
    return random.Random(seed).randbytes(size)


def _corpus_pattern(size: int) -> bytes:
    return (b"abcdefghijklmnopqrstuvwxyz0123456789" * (size // 36 + 1))[:size]


def _corpus_jsonl(size: int) -> bytes:
    line = b'{"id":1,"text":"hello world","ts":1730000000}\n'
    return (line * (size // len(line) + 1))[:size]


def _corpus_binary(size: int) -> bytes:
    return (bytes(range(256)) * (size // 256 + 1))[:size]


def _bounds(chunks: list[dict]) -> list[tuple[int, int]]:
    return [(int(item["offset"]), int(item["length"])) for item in chunks]


def test_cdc_algorithm_is_v2() -> None:
    assert backup_incremental.CDC_ALGORITHM == "fastcdc-gear-v2"
    assert backup_incremental.CDC_ALGORITHM_V1 == "fastcdc-gear-v1"
    assert backup_incremental.CDC_MIN_CHUNK == 512 * 1024
    assert backup_incremental.CDC_AVG_CHUNK == 2 * 1024 * 1024
    assert backup_incremental.CDC_MAX_CHUNK == 8 * 1024 * 1024


def test_cdc_gearv2_golden_vectors() -> None:
    size = 8 * 1024 * 1024
    vectors: dict[str, list[tuple[int, int]]] = {
        "zeros": [(0, 8388608)],
        "random": [
            (0, 544413),
            (544413, 530770),
            (1075183, 533696),
            (1608879, 536561),
            (2145440, 526693),
            (2672133, 535978),
            (3208111, 537102),
            (3745213, 526188),
            (4271401, 538324),
            (4809725, 547065),
            (5356790, 530713),
            (5887503, 528723),
            (6416226, 559532),
            (6975758, 534981),
            (7510739, 530271),
            (8041010, 347598),
        ],
        "pattern": [(0, 8388608)],
        "jsonl": [(0, 8388608)],
        "binary": [(0, 8388608)],
    }
    corpora = {
        "zeros": b"\x00" * size,
        "random": _corpus_random(size),
        "pattern": _corpus_pattern(size),
        "jsonl": _corpus_jsonl(size),
        "binary": _corpus_binary(size),
    }
    for name, expected in vectors.items():
        chunks = backup_incremental.chunk_stream(io.BytesIO(corpora[name]), file_size=size)
        assert _bounds(chunks) == expected, f"golden vector drift for {name}"


def test_cdc_gearv2_contiguous_and_bounded() -> None:
    data = _corpus_random(16 * 1024 * 1024)
    chunks = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    previous = None
    for index, item in enumerate(chunks):
        # Every chunk except the file-tail remainder respects min/max bounds.
        if index < len(chunks) - 1:
            assert backup_incremental.CDC_MIN_CHUNK <= int(item["length"]) <= backup_incremental.CDC_MAX_CHUNK
        assert int(item["length"]) <= backup_incremental.CDC_MAX_CHUNK
        if previous is not None:
            assert int(item["offset"]) == previous["offset"] + previous["length"]
        previous = item
    assert chunks[0]["offset"] == 0
    assert previous is not None
    assert previous["offset"] + previous["length"] == len(data)
    assert sum(int(item["length"]) for item in chunks) == len(data)


def test_cdc_gearv2_single_chunk_below_max() -> None:
    data = _corpus_pattern(1 * 1024 * 1024)
    chunks = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    assert _bounds(chunks) == [(0, len(data))]


def test_cdc_gearv2_small_edit_local_pollution() -> None:
    data = bytearray(_corpus_random(16 * 1024 * 1024))
    base = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    base_hashes = {str(item["sha256"]) for item in base}
    data[8 * 1024 * 1024] ^= 0xFF
    changed = backup_incremental.chunk_stream(io.BytesIO(bytes(data)), file_size=len(data))
    changed_hashes = {str(item["sha256"]) for item in changed}
    # A single-byte edit must pollute only a local handful of chunks.
    assert len(base_hashes & changed_hashes) >= len(changed_hashes) - 3


def test_cdc_gearv2_small_insert_resyncs_tail() -> None:
    data = _corpus_random(16 * 1024 * 1024)
    insertion_size = 4 * 1024
    position = 4 * 1024 * 1024
    base = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    base_by_offset = {int(item["offset"]): str(item["sha256"]) for item in base}
    inserted = data[:position] + b"x" * insertion_size + data[position:]
    changed = backup_incremental.chunk_stream(io.BytesIO(inserted), file_size=len(inserted))
    tail_matched = 0
    tail_total = 0
    for item in changed:
        offset = int(item["offset"])
        if offset < position + insertion_size:
            continue
        tail_total += 1
        if base_by_offset.get(offset - insertion_size) == str(item["sha256"]):
            tail_matched += 1
    # Windows smaller than the min chunk size re-align the tail exactly.
    assert tail_total >= 3 and tail_matched == tail_total


def test_cdc_gearv2_middle_insertion_reuses_chunks() -> None:
    data = _corpus_random(8 * 1024 * 1024)
    base = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    base_hashes = {str(item["sha256"]) for item in base}
    inserted = data[: 2 * 1024 * 1024] + b"x" * (512 * 1024) + data[2 * 1024 * 1024 :]
    changed = backup_incremental.chunk_stream(io.BytesIO(inserted), file_size=len(inserted))
    changed_hashes = {str(item["sha256"]) for item in changed}
    assert len(base_hashes & changed_hashes) >= 1


@pytest.mark.slow
def test_cdc_gearv2_streams_with_bounded_memory() -> None:
    """A large stream is consumed in bounded blocks without buffering the file."""
    chunk_size = backup_incremental.CDC_MAX_CHUNK
    reads: list[int] = []

    class _Reader(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            if size is not None:
                reads.append(size)
            return super().read(size)

    data = _corpus_random(32 * 1024 * 1024)
    chunks = backup_incremental.chunk_stream(_Reader(data), file_size=len(data))
    assert sum(int(item["length"]) for item in chunks) == len(data)
    assert max(reads) <= chunk_size


def test_cdc_gearv2_single_byte_same_boundaries() -> None:
    data = _corpus_random(4 * 1024 * 1024)
    first = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    second = backup_incremental.chunk_stream(io.BytesIO(data), file_size=len(data))
    assert [str(item["sha256"]) for item in first] == [str(item["sha256"]) for item in second]
    assert _bounds(first) == _bounds(second)


def test_cdc_gearv2_rejects_non_covering_input(tmp_settings: None) -> None:
    with pytest.raises(AppError):
        backup_incremental.chunk_stream(io.BytesIO(b"short"), file_size=100)
