#!/usr/bin/env python3
"""Validate Python, JSON Rust, and compact-binary Rust vector ranking parity."""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.rust_core import vector_binary  # noqa: E402

VERSION = "3.9.0"
SCHEMA_VERSION = "rag-vector-binary-parity.v1"


@dataclass(frozen=True)
class VectorCase:
    name: str
    query: list[float]
    candidates: list[list[float]]


def _dense_value(seed: int, index: int) -> float:
    value = ((seed + 17) * 1_103_515_245 + (index + 29) * 12_345) % 19_999
    return round((value - 9_999) / 1_000_000.0, 6)


def _generated_case(name: str, seed: int, dimensions: int, candidate_count: int) -> VectorCase:
    query = [_dense_value(seed, index) for index in range(dimensions)]
    candidates: list[list[float]] = []
    for candidate_index in range(candidate_count):
        scale = 0.35 + (0.6 * candidate_index / max(1, candidate_count - 1))
        candidates.append(
            [
                round(
                    query[index] * scale + _dense_value(seed + candidate_index + 101, index) * 0.0005,
                    6,
                )
                for index in range(dimensions)
            ]
        )
    return VectorCase(name, query, candidates)


def valid_cases() -> Iterator[VectorCase]:
    yield VectorCase("single_candidate", [1.0], [[0.5]])
    yield VectorCase("multiple_candidates", [1.0, 0.0], [[0.1, 0.0], [0.8, 0.0], [0.2, 0.0]])
    yield VectorCase("identical", [0.6, 0.8], [[0.6, 0.8]])
    yield VectorCase("orthogonal", [1.0, 0.0], [[0.0, 1.0]])
    yield VectorCase("negative_similarity", [1.0], [[-1.0], [-0.5]])
    yield VectorCase("zero_vector", [0.0, 0.0], [[1.0, 0.0], [0.0, 1.0]])
    yield VectorCase("first_match_tie", [1.0, 0.0], [[0.9, 0.0], [0.9, 0.0]])
    yield VectorCase("non_first_best", [1.0, 0.0], [[0.2, 0.0], [0.7, 0.0]])
    yield VectorCase("all_equal", [1.0], [[0.25], [0.25], [0.25]])
    yield VectorCase("negative_zero", [-0.0, 1.0], [[0.0, 0.5], [-0.0, 0.75]])
    yield VectorCase("very_small_finite", [1e-150], [[1e-150], [2e-150]])
    yield VectorCase("very_large_finite", [1e150], [[1e-150], [5e-151]])
    yield VectorCase("six_decimal_embedding", [0.123456, -0.654321], [[0.111111, -0.222222], [0.333333, -0.444444]])
    yield VectorCase("near_tie", [1.0], [[0.500000000001], [0.5]])
    yield _generated_case("dimensions_384", 384, 384, 3)
    yield _generated_case("dimensions_768", 768, 768, 3)
    yield _generated_case("dimensions_1536", 1536, 1536, 3)
    for seed in range(90):
        dimensions = (1, 2, 3, 8, 16, 32, 64)[seed % 7]
        candidate_count = 1 + (seed % 7)
        yield _generated_case(f"generated_{seed:03d}", seed, dimensions, candidate_count)
    yield _generated_case("16_candidates_x_384_dimensions", 16_384, 384, 16)
    yield _generated_case("128_candidates_x_768_dimensions", 128_768, 768, 128)
    yield _generated_case("1000_candidates_x_1536_dimensions", 1_000_1536, 1536, 1000)


def python_rank(query: list[float], candidates: list[list[float]]) -> tuple[int | None, float]:
    best_index: int | None = None
    best_similarity = 0.0
    for index, candidate in enumerate(candidates):
        similarity = min(1.0, max(0.0, sum(left * right for left, right in zip(query, candidate))))
        if similarity > best_similarity:
            best_index = index
            best_similarity = similarity
    return best_index, best_similarity


def _post(url: str, body: bytes, content_type: str, accept: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": accept},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - local sidecar contract
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _rank_json(base_url: str, case: VectorCase) -> tuple[tuple[int | None, float], int, int]:
    body = json.dumps(
        {"query": case.query, "candidates": case.candidates},
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    status, content_type, response = _post(
        f"{base_url}/rag/vectors/rank",
        body,
        "application/json",
        "application/json",
    )
    if status != 200 or not content_type.lower().startswith("application/json"):
        raise RuntimeError(f"JSON vector endpoint failed with status {status}")
    value = json.loads(response)
    return (value.get("index"), float(value["similarity"])), len(body), len(response)


def _rank_binary(base_url: str, case: VectorCase) -> tuple[tuple[int | None, float], int, int]:
    encoded = vector_binary.encode_rank_request(case.query, case.candidates)
    status, content_type, response = _post(
        f"{base_url}/rag/vectors/rank-binary",
        encoded.body,
        vector_binary.CONTENT_TYPE,
        vector_binary.CONTENT_TYPE,
    )
    if status != 200 or content_type.lower() != vector_binary.CONTENT_TYPE:
        raise RuntimeError(f"binary vector endpoint failed with status {status}")
    decoded = vector_binary.decode_rank_response(response, candidate_count=len(case.candidates))
    return (decoded.index, decoded.similarity), len(encoded.body), len(response)


def _malformed_cases() -> list[tuple[str, bytes, str, str]]:
    valid = vector_binary.encode_rank_request([1.0], [[1.0]]).body
    non_finite_query = struct.pack("<8sII2d", vector_binary.REQUEST_MAGIC, 1, 1, math.nan, 1.0)
    non_finite_candidate = struct.pack("<8sII2d", vector_binary.REQUEST_MAGIC, 1, 1, 1.0, math.inf)
    return [
        ("empty_body", b"", vector_binary.CONTENT_TYPE, "invalid_binary_header"),
        ("wrong_magic", b"BADMAGIC" + valid[8:], vector_binary.CONTENT_TYPE, "invalid_binary_magic"),
        ("truncated_header", vector_binary.REQUEST_MAGIC, vector_binary.CONTENT_TYPE, "invalid_binary_header"),
        ("zero_dimensions", struct.pack("<8sII", vector_binary.REQUEST_MAGIC, 0, 1), vector_binary.CONTENT_TYPE, "invalid_dimensions"),
        ("zero_candidates", struct.pack("<8sII", vector_binary.REQUEST_MAGIC, 1, 0), vector_binary.CONTENT_TYPE, "invalid_candidate_count"),
        ("body_short_one", valid[:-1], vector_binary.CONTENT_TYPE, "payload_length_mismatch"),
        ("body_trailing_one", valid + b"\x00", vector_binary.CONTENT_TYPE, "payload_length_mismatch"),
        ("declared_length_mismatch", valid[:8] + struct.pack("<II", 2, 1) + valid[16:], vector_binary.CONTENT_TYPE, "payload_length_mismatch"),
        ("arithmetic_overflow", struct.pack("<8sII", vector_binary.REQUEST_MAGIC, 0xFFFF_FFFF, 0xFFFF_FFFF), vector_binary.CONTENT_TYPE, "arithmetic_overflow"),
        ("payload_too_large", struct.pack("<8sII", vector_binary.REQUEST_MAGIC, 1536, 1041), vector_binary.CONTENT_TYPE, "payload_too_large"),
        ("invalid_dimensions", struct.pack("<8sII", vector_binary.REQUEST_MAGIC, 4097, 1), vector_binary.CONTENT_TYPE, "invalid_dimensions"),
        ("invalid_candidate_count", struct.pack("<8sII", vector_binary.REQUEST_MAGIC, 1, 50_001), vector_binary.CONTENT_TYPE, "invalid_candidate_count"),
        ("non_finite_query", non_finite_query, vector_binary.CONTENT_TYPE, "non_finite_vector"),
        ("non_finite_candidate", non_finite_candidate, vector_binary.CONTENT_TYPE, "non_finite_vector"),
        ("wrong_content_type", valid, "application/octet-stream", "invalid_content_type"),
        ("random_bytes", b"0123456789abcdef", vector_binary.CONTENT_TYPE, "invalid_binary_magic"),
    ]


def run(base_url: str) -> dict[str, object]:
    base_url = base_url.rstrip("/")
    case_results: list[dict[str, object]] = []
    size_comparisons: dict[str, dict[str, int]] = {}
    for case in valid_cases():
        expected = python_rank(case.query, case.candidates)
        json_result, json_bytes, json_response_bytes = _rank_json(base_url, case)
        binary_result, binary_bytes, binary_response_bytes = _rank_binary(base_url, case)
        parity = (
            json_result[0] == expected[0] == binary_result[0]
            and math.isclose(json_result[1], expected[1], rel_tol=1e-9, abs_tol=1e-12)
            and math.isclose(binary_result[1], expected[1], rel_tol=1e-9, abs_tol=1e-12)
        )
        if not parity:
            raise RuntimeError(f"vector parity failed: {case.name}")
        case_results.append(
            {
                "name": case.name,
                "dimensions": len(case.query),
                "candidateCount": len(case.candidates),
                "status": "PASS",
                "bestIndex": expected[0],
                "similarity": expected[1],
                "jsonRequestBytes": json_bytes,
                "binaryRequestBytes": binary_bytes,
                "jsonResponseBytes": json_response_bytes,
                "binaryResponseBytes": binary_response_bytes,
            }
        )
        if case.name in {
            "16_candidates_x_384_dimensions",
            "128_candidates_x_768_dimensions",
            "1000_candidates_x_1536_dimensions",
        }:
            size_comparisons[case.name] = {"jsonRequestBytes": json_bytes, "binaryRequestBytes": binary_bytes}

    malformed_results: list[dict[str, object]] = []
    for name, body, content_type, expected_code in _malformed_cases():
        status, response_type, response = _post(
            f"{base_url}/rag/vectors/rank-binary",
            body,
            content_type,
            vector_binary.CONTENT_TYPE,
        )
        value = json.loads(response)
        code = value.get("code")
        if status < 400 or code != expected_code or not response_type.lower().startswith("application/json"):
            raise RuntimeError(f"malformed binary contract failed: {name}")
        rendered = json.dumps(value, ensure_ascii=False).lower()
        if "query" in rendered or "candidate" in rendered and "candidate count" not in rendered:
            raise RuntimeError(f"malformed response exposed vector input: {name}")
        malformed_results.append({"name": name, "status": "PASS", "httpStatus": status, "errorCode": code})

    large = size_comparisons["1000_candidates_x_1536_dimensions"]
    if large["binaryRequestBytes"] >= large["jsonRequestBytes"]:
        raise RuntimeError("1000 x 1536 binary payload is not smaller than equivalent JSON")
    if any(result["binaryResponseBytes"] != vector_binary.RESPONSE_BYTES for result in case_results):
        raise RuntimeError("binary success response is not fixed at 24 bytes")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": VERSION,
        "status": "PASS",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "validCaseCount": len(case_results),
        "malformedCaseCount": len(malformed_results),
        "cases": case_results,
        "malformedCases": malformed_results,
        "largePayloads": size_comparisons,
        "binaryResponseBytes": vector_binary.RESPONSE_BYTES,
        "tolerance": {"relative": 1e-9, "absolute": 1e-12},
        "containsVectorValues": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--strict", action="store_true", help="accepted for parity-runner consistency; checks are always strict")
    parser.add_argument(
        "--output",
        "--report",
        dest="output",
        type=Path,
        default=ROOT / "artifacts" / "rag-vector-binary-parity.json",
    )
    args = parser.parse_args()
    report = run(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"PASS: {report['validCaseCount']} valid vector cases and "
        f"{report['malformedCaseCount']} malformed binary cases"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
