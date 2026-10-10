from __future__ import annotations

import argparse
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

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.gateway.request_preparation import prepare_gateway_request  # noqa: E402
from scripts.release_evidence import stamp_release_report  # noqa: E402

DEFAULT_FIXTURE = ROOT / "fixtures" / "gateway" / "request_preparation_cases.json"


class ParityFailure(RuntimeError):
    pass


RequestFn = Callable[[str, dict[str, Any], float], dict[str, Any]]


def load_fixture(path: Path = DEFAULT_FIXTURE) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ParityFailure(f"fixture cases must be a list of objects: {path}")
    return cases


def materialize(value: Any) -> Any:
    if isinstance(value, list):
        return [materialize(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$number"}:
        number = value["$number"]
        if number == "NaN":
            return float("nan")
        if number == "Infinity":
            return float("inf")
        raise ParityFailure(f"unknown special number: {number}")
    if set(value) == {"$repeat", "count"}:
        return str(value["$repeat"]) * int(value["count"])
    if set(value) == {"$nested_depth"}:
        nested: Any = "leaf"
        for _ in range(int(value["$nested_depth"])):
            nested = {"child": nested}
        return nested
    return {str(key): materialize(item) for key, item in value.items()}


def python_result(request: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "request": prepare_gateway_request(request)}
    except AppError as exc:
        return {"ok": False, "code": exc.code.value}


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParityFailure(f"Rust returned malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ParityFailure("Rust response must be an object")
    return value


def post_request(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=True).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}/gateway/request/prepare",
        data=data,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied local sidecar URL
            return _decode(response.read())
    except HTTPError as exc:
        return _decode(exc.read())
    except (URLError, TimeoutError, OSError) as exc:
        raise ParityFailure(f"request preparation endpoint failed: {exc}") from exc


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


def normalized_rust_result(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("ok") is True and isinstance(response.get("request"), dict):
        return {"ok": True, "request": response["request"]}
    code = response.get("code")
    return {"ok": False, "code": code if isinstance(code, str) else "invalid_rust_response"}


def run_parity(
    base_url: str,
    cases: list[dict[str, Any]],
    *,
    timeout: float = 5.0,
    request_fn: RequestFn | None = None,
) -> dict[str, Any]:
    send = request_fn or post_request
    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case.get("id") or "")
        request = materialize(case.get("request"))
        python = python_result(request)
        rust = normalized_rust_result(send(base_url, request, timeout))
        expected_error = case.get("expected_error")
        expectation_ok = (
            python.get("ok") is True and expected_error is None
            or python.get("ok") is False and python.get("code") == expected_error
        )
        results.append(
            {
                "id": case_id,
                "group": str(case.get("group") or ""),
                "passed": expectation_ok and python == rust,
                "expectedError": expected_error,
                "python": python,
                "rust": rust,
            }
        )
    passed = sum(1 for result in results if result["passed"])
    return {
        "ok": passed == len(results),
        "summary": {"passed": passed, "total": len(results)},
        "cases": results,
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    print(f"Gateway request parity: {summary.get('passed', 0)}/{summary.get('total', 0)} passed")
    for result in report.get("cases") or []:
        if result.get("passed"):
            continue
        print(f"FAIL {result.get('id')}: Python={result.get('python')} Rust={result.get('rust')}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare deterministic Python and Rust Gateway request preparation.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    try:
        cases = load_fixture(args.fixture)
        wait_for_sidecar(args.base_url, wait_seconds=args.wait_seconds, timeout=args.timeout)
        report = run_parity(args.base_url, cases, timeout=args.timeout)
    except (OSError, ValueError, ParityFailure) as exc:
        report = stamp_release_report(
            {"ok": False, "summary": {"passed": 0, "total": 0}, "cases": [], "fatalError": str(exc)}, root=ROOT
        )
        print(f"Gateway request parity failed: {exc}", file=sys.stderr)
        if args.report is not None:
            write_report(args.report, report)
        return 1
    report = stamp_release_report(report, root=ROOT)
    print_report(report)
    if args.report is not None:
        write_report(args.report, report)
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
