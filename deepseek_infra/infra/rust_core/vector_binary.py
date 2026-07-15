"""Compact little-endian f64 contract for Rust vector ranking."""

from __future__ import annotations

import array
import math
import struct
import sys
from dataclasses import dataclass
from typing import Sequence

REQUEST_MAGIC = b"DSVRNK01"
RESPONSE_MAGIC = b"DSVRSP01"
CONTENT_TYPE = "application/vnd.deepseek.vector-rank.v1+octet-stream"
HEADER_BYTES = 16
RESPONSE_BYTES = 24
MAX_DIMENSIONS = 4_096
MAX_CANDIDATES = 50_000
MAX_SCALARS = 1_600_000
MAX_REQUEST_BYTES = HEADER_BYTES + MAX_SCALARS * 8
NO_MATCH_INDEX = 0xFFFF_FFFF


class VectorBinaryError(ValueError):
    """Stable local codec failure without vector values in its message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EncodedVectorRankRequest:
    body: bytes | bytearray
    dimensions: int
    candidate_count: int
    scalar_count: int


@dataclass(frozen=True)
class DecodedVectorRankResponse:
    index: int | None
    similarity: float


def _little_endian_bytes(values: array.array[float], *, host_byteorder: str = sys.byteorder) -> bytes:
    if values.itemsize != 8:
        raise VectorBinaryError("invalid_f64_width")
    if host_byteorder == "big":
        values.byteswap()
    elif host_byteorder != "little":
        raise VectorBinaryError("invalid_host_byteorder")
    return values.tobytes()


def _finite(values: Sequence[float]) -> bool:
    try:
        return all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError, OverflowError):
        return False


def encode_rank_request(query: Sequence[float], candidates: Sequence[Sequence[float]]) -> EncodedVectorRankRequest:
    dimensions = len(query)
    candidate_count = len(candidates)
    if not 0 < dimensions <= MAX_DIMENSIONS:
        raise VectorBinaryError("invalid_dimensions")
    if not 0 < candidate_count <= MAX_CANDIDATES:
        raise VectorBinaryError("invalid_candidate_count")
    if any(len(candidate) != dimensions for candidate in candidates):
        raise VectorBinaryError("invalid_dimensions")
    scalar_count = dimensions * (candidate_count + 1)
    if scalar_count > MAX_SCALARS:
        raise VectorBinaryError("payload_too_large")
    if not _finite(query) or any(not _finite(candidate) for candidate in candidates):
        raise VectorBinaryError("non_finite_vector")

    values = array.array("d", query)
    for candidate in candidates:
        values.extend(candidate)
    payload = _little_endian_bytes(values)
    body = struct.pack("<8sII", REQUEST_MAGIC, dimensions, candidate_count) + payload
    if len(body) != HEADER_BYTES + scalar_count * 8 or len(body) > MAX_REQUEST_BYTES:
        raise VectorBinaryError("payload_length_mismatch")
    return EncodedVectorRankRequest(
        body=body,
        dimensions=dimensions,
        candidate_count=candidate_count,
        scalar_count=scalar_count,
    )


def _blob_view(value: bytes | memoryview) -> memoryview:
    try:
        view = memoryview(value)
        if not view.contiguous:
            raise VectorBinaryError("invalid_blob_buffer")
        return view.cast("B")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, VectorBinaryError):
            raise
        raise VectorBinaryError("invalid_blob_buffer") from exc


def _f64le_blob_is_finite(view: memoryview) -> bool:
    """Reject IEEE-754 infinities/NaNs by exponent bits without decoding floats."""
    for offset in range(0, view.nbytes, 8):
        if (view[offset + 7] & 0x7F) == 0x7F and (view[offset + 6] & 0xF0) == 0xF0:
            return False
    return True


def encode_rank_request_from_blobs(
    query: Sequence[float],
    candidate_blobs: Sequence[bytes | memoryview],
    dimensions: int,
    *,
    blobs_validated: bool = False,
) -> EncodedVectorRankRequest:
    """Assemble one DSVRNK01 body by copying validated f64le candidate buffers.

    Candidate values are never materialized as Python floats or list-of-lists.
    Length and IEEE-754 exponent checks operate directly on byte views.
    """
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or not 0 < dimensions <= MAX_DIMENSIONS:
        raise VectorBinaryError("invalid_dimensions")
    if len(query) != dimensions:
        raise VectorBinaryError("invalid_dimensions")
    candidate_count = len(candidate_blobs)
    if not 0 < candidate_count <= MAX_CANDIDATES:
        raise VectorBinaryError("invalid_candidate_count")
    scalar_count = dimensions * (candidate_count + 1)
    if scalar_count > MAX_SCALARS:
        raise VectorBinaryError("payload_too_large")
    if not _finite(query):
        raise VectorBinaryError("non_finite_vector")

    candidate_bytes = dimensions * 8
    request_bytes = HEADER_BYTES + scalar_count * 8
    if candidate_bytes <= 0 or request_bytes > MAX_REQUEST_BYTES:
        raise VectorBinaryError("payload_too_large")

    query_values = array.array("d", query)
    query_payload = _little_endian_bytes(query_values)
    if len(query_payload) != candidate_bytes:
        raise VectorBinaryError("payload_length_mismatch")

    views: list[memoryview] = []
    for candidate_blob in candidate_blobs:
        view = _blob_view(candidate_blob)
        if view.nbytes != candidate_bytes:
            raise VectorBinaryError("payload_length_mismatch")
        if not blobs_validated and not _f64le_blob_is_finite(view):
            raise VectorBinaryError("non_finite_vector")
        views.append(view)

    body = bytearray(request_bytes)
    struct.pack_into("<8sII", body, 0, REQUEST_MAGIC, dimensions, candidate_count)
    body_view = memoryview(body)
    offset = HEADER_BYTES
    body_view[offset : offset + candidate_bytes] = query_payload
    offset += candidate_bytes
    for view in views:
        body_view[offset : offset + candidate_bytes] = view
        offset += candidate_bytes
    if offset != request_bytes:
        raise VectorBinaryError("payload_length_mismatch")
    return EncodedVectorRankRequest(
        body=body,
        dimensions=dimensions,
        candidate_count=candidate_count,
        scalar_count=scalar_count,
    )


def decode_rank_response(body: bytes, *, candidate_count: int) -> DecodedVectorRankResponse:
    if len(body) != RESPONSE_BYTES:
        raise VectorBinaryError("invalid_binary_response_length")
    magic, index, reserved, similarity = struct.unpack("<8sIId", body)
    if magic != RESPONSE_MAGIC:
        raise VectorBinaryError("invalid_binary_response_magic")
    if reserved != 0:
        raise VectorBinaryError("invalid_binary_response_reserved")
    if not math.isfinite(similarity) or not 0.0 <= similarity <= 1.0:
        raise VectorBinaryError("invalid_binary_response_similarity")
    if index == NO_MATCH_INDEX:
        return DecodedVectorRankResponse(index=None, similarity=similarity)
    if index >= candidate_count:
        raise VectorBinaryError("invalid_binary_response_index")
    return DecodedVectorRankResponse(index=index, similarity=similarity)
