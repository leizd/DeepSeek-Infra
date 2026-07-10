from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    endpoint: str


def _request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Accept": "application/json"})
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - target URL is operator supplied
            status = response.status
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} {path} returned HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SmokeFailure(f"{method} {path} failed: {exc}") from exc

    if status != 200:
        raise SmokeFailure(f"{method} {path} returned HTTP {status}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{method} {path} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{method} {path} returned a non-object JSON value")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def wait_for_health(base_url: str, *, wait_seconds: float, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + wait_seconds
    last_error = "sidecar did not respond"
    while time.monotonic() < deadline:
        try:
            health = _request_json(base_url, "GET", "/healthz", timeout=timeout)
            if health.get("ok") is True:
                return health
            last_error = f"unexpected health response: {health!r}"
        except SmokeFailure as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise SmokeFailure(f"Rust sidecar did not become healthy within {wait_seconds:g}s: {last_error}")


def run_smoke(base_url: str, *, wait_seconds: float = 60.0, timeout: float = 5.0) -> list[CheckResult]:
    health = wait_for_health(base_url, wait_seconds=wait_seconds, timeout=timeout)
    _require(health.get("service") == "deepseek-gateway-rs", "healthz returned the wrong service name")
    checks = [CheckResult("health", "GET /healthz")]

    models = _request_json(base_url, "GET", "/v1/models", timeout=timeout)
    model_data = models.get("data")
    _require(models.get("object") == "list", "models response is not an OpenAI-compatible list")
    _require(isinstance(model_data, list) and bool(model_data), "models response has no model entries")
    checks.append(CheckResult("models", "GET /v1/models"))

    chat = _request_json(
        base_url,
        "POST",
        "/v1/chat/completions",
        payload={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "offline smoke"}],
            "stream": False,
        },
        timeout=timeout,
    )
    choices = chat.get("choices")
    _require(chat.get("object") == "chat.completion", "chat response has the wrong object type")
    if not isinstance(choices, list) or not choices:
        raise SmokeFailure("chat response has no choices")
    first_choice = choices[0]
    first_message = first_choice.get("message") if isinstance(first_choice, dict) else None
    _require(isinstance(first_message, dict) and first_message.get("role") == "assistant", "chat response has no assistant message")
    checks.append(CheckResult("chat", "POST /v1/chat/completions"))

    mcp = _request_json(
        base_url,
        "POST",
        "/mcp",
        payload={
            "jsonrpc": "2.0",
            "id": "docker-smoke",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "docker-smoke", "version": "1.0.0"},
            },
        },
        timeout=timeout,
    )
    mcp_result = mcp.get("result")
    _require(mcp.get("jsonrpc") == "2.0", "MCP response is not JSON-RPC 2.0")
    if not isinstance(mcp_result, dict):
        raise SmokeFailure("MCP initialize response has no result")
    _require(mcp_result.get("protocolVersion") == "2024-11-05", "MCP protocol version mismatch")
    server_info = mcp_result.get("serverInfo")
    _require(isinstance(server_info, dict) and server_info.get("name") == "deepseek-mcp-rs", "MCP serverInfo mismatch")
    checks.append(CheckResult("mcp", "POST /mcp"))

    policy = _request_json(
        base_url,
        "POST",
        "/policy/url",
        payload={"url": "http://localhost:8080/admin"},
        timeout=timeout,
    )
    _require(policy.get("decision") == "Deny", "policy did not deny localhost")
    _require(isinstance(policy.get("reason"), str) and bool(policy["reason"]), "policy deny response has no reason")
    checks.append(CheckResult("policy", "POST /policy/url"))

    rag = _request_json(
        base_url,
        "POST",
        "/rag/query/normalize",
        payload={"query": "  Rust 语言  "},
        timeout=timeout,
    )
    _require(rag.get("normalized") == "rust 语言", "RAG normalization did not preserve the CJK query")
    _require(rag.get("tokens") == ["rust", "语言"], "RAG normalization returned unexpected tokens")
    checks.append(CheckResult("rag", "POST /rag/query/normalize"))

    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke test the standalone Rust Gateway sidecar.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        checks = run_smoke(args.base_url, wait_seconds=args.wait_seconds, timeout=args.timeout)
    except SmokeFailure as exc:
        print(f"Rust sidecar smoke failed: {exc}")
        return 1

    if args.as_json:
        print(json.dumps({"ok": True, "checks": [asdict(check) for check in checks]}, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"PASS {check.endpoint}")
        print(f"Rust sidecar smoke passed ({len(checks)} checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
