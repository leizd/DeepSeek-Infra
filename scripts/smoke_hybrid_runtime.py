from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.hybrid-test.yml")


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: dict[str, Any]
    raw: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    detail: str


POLICY_PROBE = r"""
import json
from deepseek_infra.infra.tool_runtime import tools
from deepseek_infra.infra.tool_runtime.tool_policy import ToolPolicy

trace = {}
original = tools.rust_check_url

def tracked(url):
    result = original(url)
    trace.update(ok=result.ok, status=result.status, allowed=result.allowed, reason=result.reason)
    return result

tools.rust_check_url = tracked
output = tools.execute_tool_call(
    {"function": {"name": "fetch_url", "arguments": {"url": "http://127.0.0.1/admin"}}},
    policy=ToolPolicy.permissive(),
)
print(json.dumps({"client": trace, "output": output}, ensure_ascii=False, separators=(",", ":")))
"""


RAG_PROBE = r"""
import json
from deepseek_infra.infra.rag import local_rag

delegated = {}
original_normalize = local_rag._rust_rag.normalize_query
original_score = local_rag._rust_rag.score_chunks
original_citation = local_rag._rust_rag.format_citation

def tracked_normalize(query):
    value, used = original_normalize(query)
    delegated["normalize"] = used
    return value, used

def tracked_score(query, chunks):
    value, used = original_score(query, chunks)
    delegated["score"] = used
    return value, used

def tracked_citation(source, start_line, end_line):
    value, used = original_citation(source, start_line, end_line)
    delegated["citation"] = used
    return value, used

local_rag._rust_rag.normalize_query = tracked_normalize
local_rag._rust_rag.score_chunks = tracked_score
local_rag._rust_rag.format_citation = tracked_citation

file_id = "e" * 32
cached = {
    "id": file_id,
    "name": "hybrid-e2e.md",
    "kind": "markdown",
    "chunks": [
        {"index": 0, "text": "hybrid sentinel partial", "lineStart": 1, "lineEnd": 3},
        {"index": 1, "text": "hybrid sentinel exact phrase", "lineStart": 10, "lineEnd": 20},
    ],
}
indexed = local_rag.index_file_payload(cached)
normalized = local_rag.normalize_search_query("  Rust 语言  ")
results = local_rag.search(
    "hybrid sentinel exact phrase",
    collection=local_rag.COLLECTION_FILES,
    limit=2,
    source_id=file_id,
    project_id="",
)
lineage = local_rag.chunk_lineage(results[0]) if results else {}
ranked = [
    {"chunkIndex": item.chunk_index, "text": item.text, "score": item.score}
    for item in results
]
print(json.dumps({
    "delegated": delegated,
    "indexed": indexed,
    "normalized": normalized,
    "ranked": ranked,
    "citation": lineage.get("citation", ""),
}, ensure_ascii=False, separators=(",", ":")))
"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> HttpResult:
    url = f"{base_url.rstrip('/')}{path}"
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Accept": "application/json"})
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local operator-supplied URL
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise SmokeFailure(f"{method} {path} failed: {exc}") from exc
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{method} {path} returned HTTP {status} with invalid JSON: {raw[:500]}") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{method} {path} returned HTTP {status} with non-object JSON: {raw[:500]}")
    return HttpResult(status=status, body=value, raw=raw)


def _expect_ok(result: HttpResult, endpoint: str) -> dict[str, Any]:
    if result.status != 200:
        raise SmokeFailure(f"{endpoint} returned HTTP {result.status}: {result.raw[:1000]}")
    return result.body


def wait_for_service(base_url: str, *, wait_seconds: float = 90.0, timeout: float = 5.0) -> CheckResult:
    deadline = time.monotonic() + wait_seconds
    last_error = "service did not respond"
    while time.monotonic() < deadline:
        try:
            result = _request(base_url, "GET", "/healthz", timeout=timeout)
            if result.status == 200 and result.body.get("status") == "ok":
                return CheckResult("python-health", "GET /healthz")
            last_error = f"HTTP {result.status}: {result.raw[:300]}"
        except SmokeFailure as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise SmokeFailure(f"Python service did not become healthy within {wait_seconds:g}s: {last_error}")


def check_rust_status(base_url: str, *, expect_healthy: bool, timeout: float = 5.0) -> CheckResult:
    body = _expect_ok(_request(base_url, "GET", "/api/rust/status", timeout=timeout), "GET /api/rust/status")
    rust = body.get("rust")
    if not isinstance(rust, dict):
        raise SmokeFailure("Rust status response has no rust object")
    enabled = rust.get("enabled")
    if not isinstance(enabled, dict):
        raise SmokeFailure("Rust status response has no enabled flags")
    for component in ("gateway", "mcp", "policy", "rag"):
        _require(enabled.get(component) is True, f"Rust {component} flag is not enabled")
    components = rust.get("components")
    gateway = components.get("gateway") if isinstance(components, dict) else None
    if not isinstance(gateway, dict):
        raise SmokeFailure("Rust status response has no gateway component")
    _require(gateway.get("healthy") is expect_healthy, f"Rust gateway healthy must be {expect_healthy}")
    phase = "healthy" if expect_healthy else "unhealthy after stop"
    return CheckResult("rust-status", f"all flags enabled; gateway {phase}")


def check_gateway_proxy(base_url: str, *, timeout: float = 5.0) -> CheckResult:
    models = _expect_ok(_request(base_url, "GET", "/v1/models", timeout=timeout), "GET /v1/models")
    entries = models.get("data")
    if not isinstance(entries, list) or not entries:
        raise SmokeFailure("Rust-proxied model list is empty")
    _require(all(isinstance(item, dict) and item.get("owned_by") == "deepseek" for item in entries), "model list did not come from Rust")
    chat = _expect_ok(
        _request(
            base_url,
            "POST",
            "/v1/chat/completions",
            payload={"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hybrid smoke"}], "stream": False},
            timeout=timeout,
        ),
        "POST /v1/chat/completions",
    )
    choices = chat.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    _require(chat.get("id") == "chatcmpl-stub", "chat completion did not come from the Rust stub")
    _require("deepseek-gateway-rs" in str(content), "Rust stub fingerprint is missing from chat content")
    return CheckResult("gateway-proxy", "Python /v1 routes returned Rust models and deterministic chat stub")


def _mcp_call(base_url: str, request_id: str, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    body = _expect_ok(_request(base_url, "POST", "/mcp", payload=payload, timeout=timeout), f"POST /mcp ({method})")
    _require(body.get("jsonrpc") == "2.0", f"MCP {method} response is not JSON-RPC 2.0")
    _require(body.get("id") == request_id, f"MCP {method} did not preserve request id")
    _require("error" not in body, f"MCP {method} returned an error: {body.get('error')}")
    result = body.get("result")
    if not isinstance(result, dict):
        raise SmokeFailure(f"MCP {method} response has no result")
    return result


def check_mcp_proxy(base_url: str, *, timeout: float = 5.0) -> CheckResult:
    initialized = _mcp_call(base_url, "hybrid-init", "initialize", {"protocolVersion": "2024-11-05"}, timeout)
    server_info = initialized.get("serverInfo")
    _require(isinstance(server_info, dict) and server_info.get("name") == "deepseek-mcp-rs", "MCP initialize did not reach Rust")
    tools = _mcp_call(base_url, "hybrid-list", "tools/list", {}, timeout).get("tools")
    names = {item.get("name") for item in tools if isinstance(item, dict)} if isinstance(tools, list) else set()
    _require({"echo", "health"} <= names, "Rust MCP tool list is incomplete")
    called = _mcp_call(
        base_url,
        "hybrid-echo",
        "tools/call",
        {"name": "echo", "arguments": {"message": "hello from hybrid e2e"}},
        timeout,
    )
    content = called.get("content")
    text = content[0].get("text") if isinstance(content, list) and content and isinstance(content[0], dict) else None
    _require(text == "hello from hybrid e2e", "Rust MCP echo returned the wrong payload")
    return CheckResult("mcp-proxy", "initialize, tools/list, and echo passed through Python /mcp")


def _compose_command(compose_files: tuple[str, ...], *args: str) -> list[str]:
    command = ["docker", "compose"]
    for path in compose_files:
        command.extend(("-f", path))
    command.extend(args)
    return command


def _parse_json_output(output: str, label: str) -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise SmokeFailure(f"{label} produced no output")
    try:
        value = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{label} returned invalid JSON: {output[-1000:]}") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label} returned non-object JSON")
    return value


def _run_container_probe(probe: str, compose_files: tuple[str, ...], *, timeout: float = 30.0) -> dict[str, Any]:
    code = POLICY_PROBE if probe == "policy" else RAG_PROBE if probe == "rag" else ""
    if not code:
        raise SmokeFailure(f"unknown container probe: {probe}")
    command = _compose_command(compose_files, "exec", "-T", "deepseek-infra", "python", "-c", code)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure(f"{probe} container probe failed to run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-2000:]
        raise SmokeFailure(f"{probe} container probe exited {completed.returncode}: {detail}")
    return _parse_json_output(completed.stdout, f"{probe} container probe")


def _assert_policy_probe(payload: dict[str, Any], *, expect_rust: bool) -> None:
    client = payload.get("client")
    output = payload.get("output")
    if not isinstance(client, dict) or not isinstance(output, dict):
        raise SmokeFailure("policy probe response is malformed")
    _require(output.get("ok") is False, "private URL was not denied by the Python tool entry")
    serialized = json.dumps(output, ensure_ascii=False)
    if expect_rust:
        _require(client.get("ok") is True and client.get("allowed") is False, "Rust Policy client did not return a deny decision")
        _require("rust_policy" in serialized.lower(), "tool denial did not identify Rust Policy")
    else:
        _require(client.get("ok") is False, "stopped Rust Policy unexpectedly returned a response")
        _require("ssrf_blocked" in serialized.lower(), "Python Tool Policy fallback did not enforce SSRF denial")


def check_policy_integration(compose_files: tuple[str, ...], *, expect_rust: bool) -> CheckResult:
    payload = _run_container_probe("policy", compose_files)
    _assert_policy_probe(payload, expect_rust=expect_rust)
    phase = "Rust Policy deny" if expect_rust else "Python Tool Policy fallback deny"
    return CheckResult("policy-integration", phase)


def _assert_rag_probe(payload: dict[str, Any], *, expect_rust: bool) -> None:
    delegated = payload.get("delegated")
    ranked = payload.get("ranked")
    if not isinstance(delegated, dict):
        raise SmokeFailure("RAG probe has no delegation trace")
    expected = expect_rust
    for path in ("normalize", "score", "citation"):
        _require(delegated.get(path) is expected, f"RAG {path} delegation must be {expected}")
    _require(payload.get("indexed") == 2, "RAG probe did not index both chunks")
    _require(payload.get("normalized") == "rust 语言", "RAG normalization did not preserve CJK text")
    if not isinstance(ranked, list) or len(ranked) != 2:
        raise SmokeFailure("RAG probe did not return both chunks")
    _require(isinstance(ranked[0], dict) and ranked[0].get("chunkIndex") == 1, "exact-match RAG chunk did not rank first")
    _require(str(payload.get("citation") or "").endswith(":L10-L20"), "RAG citation did not preserve the line range")


def check_rag_integration(compose_files: tuple[str, ...], *, expect_rust: bool) -> CheckResult:
    payload = _run_container_probe("rag", compose_files)
    _assert_rag_probe(payload, expect_rust=expect_rust)
    phase = "Rust RAG hot paths" if expect_rust else "Python RAG fallbacks"
    return CheckResult("rag-integration", phase)


def stop_sidecar(compose_files: tuple[str, ...], *, timeout: float = 30.0) -> CheckResult:
    command = _compose_command(compose_files, "stop", "rust-gateway")
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure(f"failed to stop Rust sidecar: {exc}") from exc
    if completed.returncode != 0:
        raise SmokeFailure(f"failed to stop Rust sidecar: {(completed.stderr or completed.stdout)[-2000:]}")
    return CheckResult("sidecar-stop", "rust-gateway stopped for fallback verification")


def _check_gateway_fallback(base_url: str, *, timeout: float) -> CheckResult:
    models = _expect_ok(_request(base_url, "GET", "/v1/models", timeout=timeout), "GET /v1/models fallback")
    entries = models.get("data")
    if not isinstance(entries, list) or not entries:
        raise SmokeFailure("Python fallback model list is empty")
    _require(all(isinstance(item, dict) and item.get("owned_by") == "deepseek-infra" for item in entries), "Gateway did not fall back to Python")
    chat = _request(
        base_url,
        "POST",
        "/v1/chat/completions",
        payload={"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "fallback smoke"}], "stream": False},
        timeout=timeout,
    )
    _require(chat.status == 400 and chat.body.get("code") == "missing_api_key", "Python chat fallback did not return a structured missing-key error")
    return CheckResult("gateway-fallback", "Python models served; chat returned structured offline error")


def _check_mcp_fallback(base_url: str, *, timeout: float) -> CheckResult:
    initialized = _mcp_call(base_url, "fallback-init", "initialize", {}, timeout)
    server_info = initialized.get("serverInfo")
    _require(isinstance(server_info, dict) and server_info.get("name") == "deepseek-infra", "MCP did not fall back to Python")
    tools = _mcp_call(base_url, "fallback-list", "tools/list", {}, timeout).get("tools")
    names = {item.get("name") for item in tools if isinstance(item, dict)} if isinstance(tools, list) else set()
    _require("data_transform" in names, "Python MCP fallback tool catalog is missing data_transform")
    result = _mcp_call(
        base_url,
        "fallback-call",
        "tools/call",
        {"name": "data_transform", "arguments": {"operation": "number_summary", "input": "1 2 3 4"}},
        timeout,
    )
    structured = result.get("structuredContent")
    summary = structured.get("result") if isinstance(structured, dict) else None
    _require(isinstance(summary, dict) and summary.get("count") == 4, "Python MCP fallback tool call returned the wrong result")
    return CheckResult("mcp-fallback", "Python MCP initialize, catalog, and tool execution passed")


def check_fallbacks(base_url: str, compose_files: tuple[str, ...], *, timeout: float = 5.0) -> list[CheckResult]:
    return [
        check_rust_status(base_url, expect_healthy=False, timeout=timeout),
        _check_gateway_fallback(base_url, timeout=timeout),
        _check_mcp_fallback(base_url, timeout=timeout),
        check_policy_integration(compose_files, expect_rust=False),
        check_rag_integration(compose_files, expect_rust=False),
    ]


def run_smoke(
    base_url: str,
    compose_files: tuple[str, ...] = DEFAULT_COMPOSE_FILES,
    *,
    wait_seconds: float = 90.0,
    timeout: float = 5.0,
    keep_sidecar: bool = False,
) -> list[CheckResult]:
    checks = [wait_for_service(base_url, wait_seconds=wait_seconds, timeout=timeout)]
    checks.append(check_rust_status(base_url, expect_healthy=True, timeout=timeout))
    checks.append(check_gateway_proxy(base_url, timeout=timeout))
    checks.append(check_mcp_proxy(base_url, timeout=timeout))
    checks.append(check_policy_integration(compose_files, expect_rust=True))
    checks.append(check_rag_integration(compose_files, expect_rust=True))
    if not keep_sidecar:
        checks.append(stop_sidecar(compose_files))
        checks.extend(check_fallbacks(base_url, compose_files, timeout=timeout))
    return checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exercise Python-to-Rust delegation and Python fallbacks in the hybrid Compose runtime.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--compose-file", action="append", dest="compose_files")
    parser.add_argument("--wait-seconds", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--keep-sidecar", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    compose_files = tuple(args.compose_files or DEFAULT_COMPOSE_FILES)
    try:
        checks = run_smoke(
            args.base_url,
            compose_files,
            wait_seconds=args.wait_seconds,
            timeout=args.timeout,
            keep_sidecar=args.keep_sidecar,
        )
    except SmokeFailure as exc:
        print(f"Hybrid runtime smoke failed: {exc}")
        return 1
    if args.as_json:
        print(json.dumps({"ok": True, "checks": [asdict(check) for check in checks]}, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"PASS {check.name}: {check.detail}")
        print(f"Hybrid runtime smoke passed ({len(checks)} checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
