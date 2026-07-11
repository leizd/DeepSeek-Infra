from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.rag import local_rag  # noqa: E402


DEFAULT_FIXTURE = ROOT / "fixtures" / "rag" / "parity_cases.json"
SCORE_TOLERANCE = 1e-6


class ParityFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: dict[str, Any]
    raw: str


RequestFn = Callable[[str, str, dict[str, Any], float], HttpResult]


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ParityFailure(f"fixture must be a JSON object: {path}")
    required = ("normalization_cases", "ranking_cases", "citation_cases", "index_validation_cases")
    result: dict[str, list[dict[str, Any]]] = {}
    for key in required:
        cases = value.get(key)
        if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
            raise ParityFailure(f"fixture field {key} must be a list of objects")
        result[key] = cases
    return result


def _decode_body(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _post_json(base_url: str, path: str, payload: dict[str, Any], timeout: float) -> HttpResult:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied sidecar URL
            raw = response.read().decode("utf-8", errors="replace")
            return HttpResult(response.status, _decode_body(raw), raw)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return HttpResult(exc.code, _decode_body(raw), raw)
    except (URLError, TimeoutError, OSError) as exc:
        raise ParityFailure(f"POST {path} failed: {exc}") from exc


def wait_for_sidecar(base_url: str, *, wait_seconds: float = 60.0, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + wait_seconds
    last_error = "sidecar did not respond"
    while time.monotonic() < deadline:
        request = Request(f"{base_url.rstrip('/')}/healthz", method="GET", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied sidecar URL
                body = _decode_body(response.read().decode("utf-8", errors="replace"))
                if response.status == 200 and body.get("ok") is True:
                    return
                last_error = f"HTTP {response.status}: {body}"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise ParityFailure(f"Rust sidecar did not become healthy within {wait_seconds:g}s: {last_error}")


def _contract_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    existing_metadata = chunk.get("metadata")
    if isinstance(existing_metadata, dict):
        title = existing_metadata.get("title")
        extra = existing_metadata.get("extra", {})
    else:
        title = None
        extra = {}
    if "title" in chunk:
        title = chunk.get("title")
    result: dict[str, Any] = {
        "id": chunk.get("id"),
        "source": chunk.get("source"),
        "text": chunk.get("text"),
        "metadata": {"title": title, "extra": extra},
    }
    if "start_line" in chunk:
        result["start_line"] = chunk.get("start_line")
    if "end_line" in chunk:
        result["end_line"] = chunk.get("end_line")
    return result


def _expected_value(case: dict[str, Any]) -> dict[str, Any]:
    if "expected_error" in case:
        return {"error": case["expected_error"]}
    return {"value": case.get("expected")}


def _normalization_result(value: str) -> dict[str, Any]:
    try:
        return {"value": local_rag.python_rag_normalize_query(value)}
    except ValueError as exc:
        return {"error": str(exc)}


def _rust_normalization(result: HttpResult) -> dict[str, Any]:
    if result.status == 200 and isinstance(result.body.get("normalized"), str):
        return {"value": result.body["normalized"]}
    message = str(result.body.get("error") or result.body.get("raw") or "")
    return {"error": "empty_query" if "empty" in message.lower() else f"http_{result.status}"}


def _first_divergence(expected: list[str], actual: list[str]) -> str:
    for index, (expected_id, actual_id) in enumerate(zip(expected, actual, strict=False)):
        if expected_id != actual_id:
            return f"position {index + 1}: expected {expected_id!r}, got {actual_id!r}"
    if len(expected) != len(actual):
        return f"length: expected {len(expected)}, got {len(actual)}"
    return ""


def _ranking_result(ranked: list[tuple[str, float]], top_k: int) -> dict[str, Any]:
    selected = ranked[:top_k]
    return {"order": [item_id for item_id, _score in selected], "scores": [score for _item_id, score in selected]}


def _rust_ranking(result: HttpResult, top_k: int) -> dict[str, Any]:
    ranked = result.body.get("ranked") if result.status == 200 else None
    if not isinstance(ranked, list):
        return {"error": f"http_{result.status}", "body": result.body}
    pairs: list[tuple[str, float]] = []
    for item in ranked:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("score"), (int, float)):
            return {"error": "malformed_ranking", "body": result.body}
        pairs.append((item["id"], float(item["score"])))
    return _ranking_result(pairs, top_k)


def _scores_match(expected: list[Any], actual: list[Any]) -> bool:
    if len(expected) != len(actual):
        return False
    return all(
        isinstance(left, (int, float))
        and isinstance(right, (int, float))
        and math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=SCORE_TOLERANCE)
        for left, right in zip(expected, actual, strict=True)
    )


def _citation_result(source: str, start_line: int | None, end_line: int | None) -> dict[str, Any]:
    try:
        return {"value": local_rag.python_rag_format_citation(source, start_line, end_line)}
    except ValueError as exc:
        return {"error": str(exc)}


def _rust_citation(result: HttpResult) -> dict[str, Any]:
    if result.status == 200 and isinstance(result.body.get("citation"), str):
        return {"value": result.body["citation"]}
    message = str(result.body.get("error") or result.body.get("raw") or "")
    return {"error": "invalid_line_range" if "line range" in message.lower() else f"http_{result.status}"}


def _index_error_category(result: HttpResult) -> str | None:
    if result.status != 200:
        return "invalid_metadata"
    error = str(result.body.get("error") or "").lower()
    if not error:
        return None
    if "duplicate chunk id" in error:
        return "duplicate_chunk_id"
    if "chunk id is empty" in error:
        return "empty_chunk_id"
    if "chunk source is empty" in error:
        return "empty_chunk_source"
    if "chunk text is empty" in error:
        return "empty_chunk_text"
    if "line range" in error:
        return "invalid_line_range"
    return "invalid_metadata"


def _rust_index_validation(result: HttpResult) -> dict[str, Any]:
    if result.status == 200 and result.body.get("valid") is True:
        return {"valid": True, "error": None}
    return {"valid": False, "error": _index_error_category(result)}


def _case_result(
    category: str,
    case_id: str,
    expected: dict[str, Any],
    python: dict[str, Any],
    rust: dict[str, Any],
    *,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "category": category,
        "id": case_id,
        "passed": expected == python == rust,
        "expected": expected,
        "python": python,
        "rust": rust,
        "detail": detail,
    }


def run_parity(
    base_url: str,
    fixture: dict[str, list[dict[str, Any]]],
    *,
    timeout: float = 5.0,
    request_fn: RequestFn | None = None,
) -> dict[str, Any]:
    request = request_fn or _post_json
    results: list[dict[str, Any]] = []

    for case in fixture["normalization_cases"]:
        query = str(case.get("input") or "")
        expected = _expected_value(case)
        python = _normalization_result(query)
        rust = _rust_normalization(request(base_url, "/rag/query/normalize", {"query": query}, timeout))
        results.append(_case_result("normalization", str(case.get("id") or ""), expected, python, rust))

    for case in fixture["ranking_cases"]:
        query = str(case.get("query") or "")
        top_k = int(case.get("top_k") or len(case.get("chunks") or []))
        chunks = [_contract_chunk(chunk) for chunk in case.get("chunks", []) if isinstance(chunk, dict)]
        python = _ranking_result(local_rag.python_rag_rank_chunks(query, chunks), top_k)
        rust = _rust_ranking(request(base_url, "/rag/chunks/score", {"query": query, "chunks": chunks}, timeout), top_k)
        expected = {"order": list(case.get("expected_order") or []), "scores": list(case.get("expected_scores") or [])}
        passed = (
            python.get("order") == expected["order"] == rust.get("order")
            and _scores_match(expected["scores"], python.get("scores", []))
            and _scores_match(expected["scores"], rust.get("scores", []))
            and _scores_match(python.get("scores", []), rust.get("scores", []))
        )
        detail = _first_divergence(expected["order"], list(rust.get("order") or []))
        results.append(
            {
                "category": "ranking",
                "id": str(case.get("id") or ""),
                "passed": passed,
                "expected": expected,
                "python": python,
                "rust": rust,
                "detail": detail,
            }
        )

    for case in fixture["citation_cases"]:
        source = str(case.get("source") or "")
        start_line = case.get("start_line") if isinstance(case.get("start_line"), int) else None
        end_line = case.get("end_line") if isinstance(case.get("end_line"), int) else None
        payload: dict[str, Any] = {"source": source}
        if case.get("start_line") is not None:
            payload["start_line"] = case.get("start_line")
        if case.get("end_line") is not None:
            payload["end_line"] = case.get("end_line")
        expected = _expected_value(case)
        python = _citation_result(source, start_line, end_line)
        rust = _rust_citation(request(base_url, "/rag/citation/format", payload, timeout))
        results.append(_case_result("citation", str(case.get("id") or ""), expected, python, rust))

    for case in fixture["index_validation_cases"]:
        chunks = [_contract_chunk(chunk) for chunk in case.get("chunks", []) if isinstance(chunk, dict)]
        if case.get("roundtrip") is True:
            chunks = json.loads(json.dumps(chunks, ensure_ascii=False))
        raw_expected = case.get("expected")
        expected_value: dict[str, Any] = raw_expected if isinstance(raw_expected, dict) else {}
        expected = {"valid": bool(expected_value.get("valid")), "error": expected_value.get("error")}
        python = local_rag.python_rag_validate_index(chunks)
        rust = _rust_index_validation(request(base_url, "/rag/index/validate", {"chunks": chunks}, timeout))
        results.append(_case_result("validation", str(case.get("id") or ""), expected, python, rust))

    summary: dict[str, dict[str, int]] = {}
    for category in ("normalization", "ranking", "citation", "validation"):
        selected = [result for result in results if result["category"] == category]
        summary[category] = {"passed": sum(1 for result in selected if result["passed"]), "total": len(selected)}
    return {"ok": all(result["passed"] for result in results), "summary": summary, "cases": results}


def print_report(report: dict[str, Any]) -> None:
    labels = {
        "normalization": "Normalization",
        "ranking": "Ranking",
        "citation": "Citation",
        "validation": "Validation",
    }
    for category, label in labels.items():
        counts = report.get("summary", {}).get(category, {})
        print(f"{label + ':':<15} {counts.get('passed', 0)}/{counts.get('total', 0)} passed")
    passed = sum(int(value.get("passed", 0)) for value in report.get("summary", {}).values())
    total = sum(int(value.get("total", 0)) for value in report.get("summary", {}).values())
    print(f"\nOverall parity: {passed}/{total}")
    for result in report.get("cases", []):
        if result.get("passed"):
            continue
        print(f"\nCase: {result.get('id')}")
        print(f"Expected: {json.dumps(result.get('expected'), ensure_ascii=False)}")
        print(f"Python:   {json.dumps(result.get('python'), ensure_ascii=False)}")
        print(f"Rust:     {json.dumps(result.get('rust'), ensure_ascii=False)}")
        if result.get("detail"):
            print(f"First divergence: {result['detail']}")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare deterministic Python and Rust RAG hot-path contracts.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    try:
        fixture = load_fixture(args.fixture)
        wait_for_sidecar(args.base_url, wait_seconds=args.wait_seconds, timeout=args.timeout)
        report = run_parity(args.base_url, fixture, timeout=args.timeout)
    except (OSError, ValueError, ParityFailure) as exc:
        report = {"ok": False, "summary": {}, "cases": [], "fatal_error": str(exc)}
        print(f"RAG parity failed: {exc}", file=sys.stderr)
        if args.report is not None:
            _write_report(args.report, report)
        return 1

    print_report(report)
    if args.report is not None:
        _write_report(args.report, report)
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
