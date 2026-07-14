"""Compare Python and Rust deterministic RAG document preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.rag.document_preparation import (  # noqa: E402
    RAG_DOCUMENT_MAX_CHARACTERS,
    RAG_DOCUMENT_MAX_NESTING,
    RAG_DOCUMENT_MAX_REQUEST_BYTES,
    prepare_rag_document_json,
)

DEFAULT_FIXTURE = ROOT / "fixtures" / "rag" / "document_preparation_cases.json"
RequestFn = Callable[[str, bytes, float], dict[str, Any]]


class ParityFailure(RuntimeError):
    pass


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or len(cases) < 120:
        raise ValueError("RAG document preparation fixture must contain at least 120 cases")
    names = [str(case.get("name") or "") for case in cases if isinstance(case, dict)]
    if len(names) != len(cases) or not all(names) or len(names) != len(set(names)):
        raise ValueError("RAG document preparation case names must be non-empty and unique")
    return cases


def _base_payload(text: str = "fixture") -> dict[str, Any]:
    return {
        "documentId": "doc-generated",
        "text": text,
        "metadata": {"displayName": "fixture.txt", "sourceType": "text/plain"},
        "chunking": {"chunkChars": 6000, "chunkOverlap": 400},
    }


def raw_case(case: dict[str, Any]) -> bytes:
    generate = str(case.get("generate") or "")
    value: Any
    if generate == "request_too_large":
        return b" " * (RAG_DOCUMENT_MAX_REQUEST_BYTES + 1)
    if generate == "invalid_json":
        return b"{"
    if generate == "document_too_large":
        value = _base_payload("x" * (RAG_DOCUMENT_MAX_CHARACTERS + 1))
    elif generate == "excessive_nesting":
        nested: Any = {}
        for _ in range(RAG_DOCUMENT_MAX_NESTING + 1):
            nested = {"nested": nested}
        value = _base_payload()
        value["metadata"] = nested
    else:
        value = case.get("payload")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityFailure(f"Rust returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ParityFailure("Rust response must be an object")
    return value


def post_request(base_url: str, raw: bytes, timeout: float) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}/rag/documents/prepare",
        data=raw,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied local sidecar URL
            return _decode(response.read())
    except HTTPError as exc:
        return _decode(exc.read())
    except (URLError, TimeoutError, OSError) as exc:
        raise ParityFailure(f"RAG document preparation endpoint failed: {exc}") from exc


def wait_for_sidecar(base_url: str, *, wait_seconds: float = 60.0, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + wait_seconds
    last_error = "sidecar did not respond"
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(f"{base_url.rstrip('/')}/healthz", method="GET"), timeout=timeout) as response:  # noqa: S310
                if response.status == 200 and _decode(response.read()).get("ok") is True:
                    return
        except (HTTPError, URLError, TimeoutError, OSError, ParityFailure) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise ParityFailure(f"Rust sidecar did not become healthy within {wait_seconds:g}s: {last_error}")


def _fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_contract(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("ok") is not False:
        return value
    return {key: item for key, item in value.items() if key != "message"}


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"ok": value.get("ok") is True, "fingerprint": _fingerprint(_semantic_contract(value))}
    if value.get("ok") is True:
        document_value = value.get("document")
        document: dict[str, Any] = document_value if isinstance(document_value, dict) else {}
        chunks_value = value.get("chunks")
        chunks: list[Any] = chunks_value if isinstance(chunks_value, list) else []
        summary.update(
            documentIdHash=hashlib.sha256(str(document.get("documentId") or "").encode()).hexdigest()[:16],
            documentHash=document.get("contentHash"),
            characterCount=document.get("characterCount"),
            chunkCount=len(chunks),
            chunkIdFingerprint=hashlib.sha256(
                "\0".join(str(chunk.get("chunkId") or "") for chunk in chunks if isinstance(chunk, dict)).encode()
            ).hexdigest(),
        )
    else:
        summary["code"] = value.get("code")
    return summary


def _matches_expectation(value: dict[str, Any], expected: Any) -> bool:
    return isinstance(expected, dict) and all(value.get(key) == item for key, item in expected.items())


def _assert_relations(case: dict[str, Any], result: dict[str, Any], by_name: dict[str, dict[str, Any]]) -> bool:
    assertions = case.get("assert")
    if not isinstance(assertions, dict) or result.get("ok") is not True:
        return True
    document_value = result.get("document")
    document: dict[str, Any] = document_value if isinstance(document_value, dict) else {}
    chunks_value = result.get("chunks")
    chunks: list[Any] = chunks_value if isinstance(chunks_value, list) else []
    for key, reference_name in assertions.items():
        reference = by_name.get(str(reference_name))
        if not reference or reference.get("ok") is not True:
            return False
        reference_document_value = reference.get("document")
        reference_document: dict[str, Any] = reference_document_value if isinstance(reference_document_value, dict) else {}
        reference_chunks_value = reference.get("chunks")
        reference_chunks: list[Any] = reference_chunks_value if isinstance(reference_chunks_value, list) else []
        if key == "sameDocumentHashAs" and document.get("contentHash") != reference_document.get("contentHash"):
            return False
        if key == "differentDocumentHashFrom" and document.get("contentHash") == reference_document.get("contentHash"):
            return False
        if key == "differentChunkIdsFrom":
            ids = [chunk.get("chunkId") for chunk in chunks if isinstance(chunk, dict)]
            reference_ids = [chunk.get("chunkId") for chunk in reference_chunks if isinstance(chunk, dict)]
            if ids == reference_ids:
                return False
    return True


def run_parity(base_url: str, cases: list[dict[str, Any]], *, timeout: float = 15.0, request_fn: RequestFn | None = None) -> dict[str, Any]:
    send = request_fn or post_request
    results: list[dict[str, Any]] = []
    python_by_name: dict[str, dict[str, Any]] = {}
    for case in cases:
        name = str(case["name"])
        raw = raw_case(case)
        python = prepare_rag_document_json(raw)
        rust = send(base_url, raw, timeout)
        python_by_name[name] = python
        passed = _matches_expectation(python, case.get("expect")) and _semantic_contract(python) == _semantic_contract(rust)
        results.append({"name": name, "passed": passed, "python": _summary(python), "rust": _summary(rust)})
    for case, result in zip(cases, results, strict=True):
        if result["passed"]:
            result["passed"] = _assert_relations(case, python_by_name[str(case["name"])], python_by_name)
    passed_count = sum(1 for result in results if result["passed"])
    return {"ok": passed_count == len(results), "summary": {"passed": passed_count, "total": len(results)}, "cases": results}


def print_report(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    print(f"RAG document preparation parity: {summary.get('passed', 0)}/{summary.get('total', 0)} passed")
    for result in report.get("cases") or []:
        if not result.get("passed"):
            print(f"FAIL {result.get('name')}: Python={result.get('python')} Rust={result.get('rust')}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare Python and Rust RAG document preparation.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.fixture)
        wait_for_sidecar(args.base_url, wait_seconds=args.wait_seconds, timeout=min(args.timeout, 5.0))
        report = run_parity(args.base_url, cases, timeout=args.timeout)
    except (OSError, ValueError, ParityFailure) as exc:
        report = {"ok": False, "summary": {"passed": 0, "total": 0}, "cases": [], "fatalError": str(exc)}
        print(f"RAG document preparation parity failed: {exc}", file=sys.stderr)
        if args.report is not None:
            write_report(args.report, report)
        return 1
    print_report(report)
    if args.report is not None:
        write_report(args.report, report)
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
