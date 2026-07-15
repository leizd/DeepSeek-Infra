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

from deepseek_infra.infra.mcp.protocol_preparation import (  # noqa: E402
    MCP_PROTOCOL_PREPARATION_MAX_BYTES,
    prepare_mcp_protocol_json,
)
from scripts.release_evidence import stamp_release_report  # noqa: E402

DEFAULT_FIXTURE = ROOT / "fixtures" / "mcp" / "protocol_preparation_cases.json"
MINIMUM_CASES = 70


class ParityFailure(RuntimeError):
    pass


RequestFn = Callable[[str, bytes, float], dict[str, Any]]


def load_fixture(path: Path = DEFAULT_FIXTURE) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ParityFailure(f"fixture cases must be a list of objects: {path}")
    names = [str(case.get("name") or "") for case in cases]
    if len(cases) < MINIMUM_CASES:
        raise ParityFailure(f"fixture must contain at least {MINIMUM_CASES} cases")
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ParityFailure("fixture case names must be non-empty and unique")
    return cases


def _generated_payload(generator: str) -> Any:
    if generator == "excessive_nesting":
        nested: Any = "leaf"
        for _ in range(40):
            nested = {"next": nested}
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search", "arguments": nested},
        }
    if generator == "oversized":
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"blob": "x" * (MCP_PROTOCOL_PREPARATION_MAX_BYTES + 256)},
            },
        }
    raise ParityFailure(f"unknown fixture generator: {generator}")


def raw_case(case: dict[str, Any]) -> bytes:
    raw = case.get("raw")
    if isinstance(raw, str):
        return raw.encode("utf-8")
    generator = case.get("generator")
    payload = _generated_payload(generator) if isinstance(generator, str) else case.get("payload")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


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
        f"{base_url.rstrip('/')}/mcp/request/prepare",
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
        raise ParityFailure(f"MCP protocol preparation endpoint failed: {exc}") from exc


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


def comparable_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is True:
        return result
    comparable = {
        "ok": False,
        "code": result.get("code"),
        "jsonRpcCode": result.get("jsonRpcCode"),
        "notification": result.get("notification"),
    }
    if "messageType" in result:
        comparable["messageType"] = result.get("messageType")
    return comparable


def _fingerprint(result: dict[str, Any]) -> str:
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ok": result.get("ok") is True,
        "fingerprint": _fingerprint(comparable_result(result)),
    }
    if result.get("ok") is True:
        summary["messageType"] = result.get("messageType")
        routing = result.get("routing")
        if isinstance(routing, dict):
            summary["routing"] = {"owner": routing.get("owner"), "category": routing.get("category")}
    else:
        summary["code"] = result.get("code")
        summary["jsonRpcCode"] = result.get("jsonRpcCode")
    return summary


def _matches_expectation(result: dict[str, Any], expected: Any) -> bool:
    return isinstance(expected, dict) and all(result.get(key) == value for key, value in expected.items())


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
        name = str(case["name"])
        raw = raw_case(case)
        python = prepare_mcp_protocol_json(raw)
        rust = send(base_url, raw, timeout)
        passed = _matches_expectation(python, case.get("expect")) and comparable_result(python) == comparable_result(rust)
        results.append(
            {
                "name": name,
                "passed": passed,
                "expected": case.get("expect"),
                "python": _summary(python),
                "rust": _summary(rust),
            }
        )
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "ok": passed_count == len(results),
        "summary": {"passed": passed_count, "total": len(results)},
        "cases": results,
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    print(f"MCP protocol parity: {summary.get('passed', 0)}/{summary.get('total', 0)} passed")
    for result in report.get("cases") or []:
        if not result.get("passed"):
            print(f"FAIL {result.get('name')}: Python={result.get('python')} Rust={result.get('rust')}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare deterministic Python and Rust MCP protocol preparation.")
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
        print(f"MCP protocol parity failed: {exc}", file=sys.stderr)
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
