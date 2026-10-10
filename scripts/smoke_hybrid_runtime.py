from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_evidence import stamp_release_report  # noqa: E402


DEFAULT_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.hybrid-test.yml")


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: dict[str, Any]
    raw: str
    headers: dict[str, str] = field(default_factory=dict)


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

def tracked(url, **kwargs):
    result = original(url, **kwargs)
    trace.update(
        ok=result.ok,
        status=result.status,
        allowed=result.allowed,
        reason=result.reason,
        code=result.code,
        decision_id=result.decision_id,
        trace_id=result.trace_id,
    )
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


RAG_DOCUMENT_PROBE = r"""
import hashlib
import json
from deepseek_infra.infra.rag import files
from deepseek_infra.infra.rust_core import rag_client

trace = {"calls": 0, "safePayload": False}
original = rag_client.prepare_document

def has_bytes(value):
    if isinstance(value, bytes):
        return True
    if isinstance(value, dict):
        return any(has_bytes(key) or has_bytes(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_bytes(item) for item in value)
    return False

def tracked(payload):
    trace["calls"] += 1
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).lower()
    forbidden = ("absolutepath", "temporarypath", "uploadpath", "cachepath", "authorization", "apikey", "token", "rawfilebytes", "databaselocation", "workspacesecret")
    trace["safePayload"] = not has_bytes(payload) and not any(field in encoded for field in forbidden)
    return original(payload)

rag_client.prepare_document = tracked
source = "First paragraph.\r\n\r\nUnicode \u4e2d\u6587 \U0001f680 e\u0301.\n\nLast paragraph.".encode("utf-8")
extracted = files.extract_uploaded_file("hybrid-rag-document.txt", "text/plain", source)
cached = files.load_cached_file(extracted["fileId"])
window = files.file_reader_window(extracted["fileId"])
chunks = cached.get("chunks", [])
semantic_chunks = [
    {
        "index": chunk.get("index"),
        "start": chunk.get("start"),
        "end": chunk.get("end"),
        "lineStart": chunk.get("lineStart"),
        "lineEnd": chunk.get("lineEnd"),
        "text": chunk.get("text"),
        "chunkId": chunk.get("chunkId"),
        "contentHash": chunk.get("contentHash"),
    }
    for chunk in chunks
    if isinstance(chunk, dict)
]
fingerprint = hashlib.sha256(
    json.dumps(semantic_chunks, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
window_chunks = window.get("chunks", []) if isinstance(window, dict) else []
print(json.dumps({
    "trace": trace,
    "diagnostics": extracted.get("ragDocumentPreparation", {}),
    "chunkCount": len(semantic_chunks),
    "fingerprint": fingerprint,
    "readerMatched": [chunk.get("text") for chunk in window_chunks if isinstance(chunk, dict)] == [chunk.get("text") for chunk in chunks if isinstance(chunk, dict)],
    "persistedByPython": bool(cached.get("id") == extracted.get("fileId") and chunks),
}, ensure_ascii=False, separators=(",", ":")))
"""


RAG_VECTOR_BINARY_PROBE = r"""
import json
import shutil
import sqlite3
import re
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from deepseek_infra.infra.gateway import semantic_cache
from deepseek_infra.infra.rust_core import rag_client

trace = {"binaryCalls": 0, "jsonCalls": 0}
original_binary = rag_client._binary_request
original_json = rag_client._request

def tracked_binary(*args, **kwargs):
    trace["binaryCalls"] += 1
    return original_binary(*args, **kwargs)

def tracked_json(*args, **kwargs):
    trace["jsonCalls"] += 1
    return original_json(*args, **kwargs)

def binary_success_count():
    try:
        with urllib.request.urlopen("http://rust-gateway:8787/metrics", timeout=2) as response:
            text = response.read().decode("utf-8")
    except Exception:
        return None
    match = re.search(r'vector_rank_transport_total\{encoding="binary",outcome="success"\} (\d+)', text)
    return int(match.group(1)) if match else 0

rag_client._binary_request = tracked_binary
rag_client._request = tracked_json
dimensions = 4
vectors = {
    "blob-source": [1.0, 0.0, 0.0, 0.0],
    "legacy-source": [0.8, 0.0, 0.0, 0.0],
    "corrupt-source": [0.6, 0.0, 0.0, 0.0],
    "lookup-query": [1.0, 0.0, 0.0, 0.0],
}

def embed(text):
    for marker, vector in vectors.items():
        if marker in text:
            return list(vector)
    return [0.0] * dimensions

cache_dir = Path(tempfile.mkdtemp(prefix="hybrid-semcache-binary-"))
semantic_cache.SEMANTIC_CACHE_ENABLED = True
semantic_cache.SEMANTIC_CACHE_DIR = cache_dir
semantic_cache.SEMANTIC_CACHE_DB = cache_dir / "cache.sqlite3"
semantic_cache.embedding_pipeline = lambda: SimpleNamespace(active_provider="hybrid-e2e", dimensions=dimensions, error="")
semantic_cache.embed_text = embed

def body(marker):
    return {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": marker}]}

try:
    stored = []
    for marker, answer in (
        ("blob-source", "blob winner"),
        ("legacy-source", "legacy candidate"),
        ("corrupt-source", "corrupt fallback candidate"),
    ):
        payload = {"messages": body(marker)["messages"]}
        stored.append(semantic_cache.store(payload, body(marker), {"content": answer}).get("stored") is True)
    with sqlite3.connect(semantic_cache.SEMANTIC_CACHE_DB) as conn:
        dual_rows = conn.execute(
            "SELECT cache_id, embedding, embedding_blob, embedding_dimensions, embedding_format, response_json "
            "FROM semantic_cache_items ORDER BY created_at, cache_id"
        ).fetchall()
        dual_write_ok = len(dual_rows) == 3 and all(
            isinstance(row[1], str)
            and isinstance(row[2], bytes)
            and row[3] == dimensions
            and row[4] == semantic_cache.EMBEDDING_FORMAT_F64LE_V1
            for row in dual_rows
        )
        by_content = {json.loads(row[5])["content"]: row[0] for row in dual_rows}
        conn.execute(
            "UPDATE semantic_cache_items SET embedding_blob=NULL, embedding_dimensions=0, embedding_format='' WHERE cache_id=?",
            (by_content["legacy candidate"],),
        )
        conn.execute(
            "UPDATE semantic_cache_items SET embedding_blob=?, embedding_dimensions=?, embedding_format=? WHERE cache_id=?",
            (b"short", dimensions, semantic_cache.EMBEDDING_FORMAT_F64LE_V1, by_content["corrupt fallback candidate"]),
        )
    before = binary_success_count()
    lookup = semantic_cache.lookup({"messages": body("lookup-query")["messages"]}, body("lookup-query"))
    after = binary_success_count()
    diagnostics = lookup.diagnostics.get("rustVectorRanking", {})
    print(json.dumps({
        "trace": trace,
        "selectedContent": lookup.result.get("content") if isinstance(lookup.result, dict) else None,
        "similarity": lookup.diagnostics.get("similarity"),
        "backend": lookup.diagnostics.get("rankingBackend"),
        "diagnostics": diagnostics,
        "storageDiagnostics": {
            "embeddingStorage": lookup.diagnostics.get("embeddingStorage"),
            "blobCandidates": lookup.diagnostics.get("blobCandidates"),
            "legacyCandidates": lookup.diagnostics.get("legacyCandidates"),
            "invalidBlobCandidates": lookup.diagnostics.get("invalidBlobCandidates"),
        },
        "metricsBinarySuccessDelta": after - before if isinstance(before, int) and isinstance(after, int) else None,
        "candidateCount": len(dual_rows),
        "dimensions": dimensions,
        "storedDual": all(stored) and dual_write_ok,
        "pythonOwnsSQLite": True,
    }, separators=(",", ":")))
finally:
    shutil.rmtree(cache_dir, ignore_errors=True)
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
            headers = {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        status = exc.code
        raw = exc.read().decode("utf-8", errors="replace")
        headers = {key.lower(): value for key, value in exc.headers.items()}
    except (URLError, TimeoutError, OSError) as exc:
        raise SmokeFailure(f"{method} {path} failed: {exc}") from exc
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{method} {path} returned HTTP {status} with invalid JSON: {raw[:500]}") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{method} {path} returned HTTP {status} with non-object JSON: {raw[:500]}")
    return HttpResult(status=status, body=value, raw=raw, headers=headers)


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


def check_gateway_request_preparation(base_url: str, *, expect_rust: bool = True, timeout: float = 5.0) -> CheckResult:
    models = _expect_ok(_request(base_url, "GET", "/v1/models", timeout=timeout), "GET /v1/models")
    entries = models.get("data")
    if not isinstance(entries, list) or not entries:
        raise SmokeFailure("Python model list is empty")
    _require(all(isinstance(item, dict) and item.get("owned_by") == "deepseek-infra" for item in entries), "model list did not stay Python-owned")
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
    _require(chat.get("id") == "chatcmpl-hybrid-upstream", "chat completion did not come from the offline upstream stub")
    _require(content == "hybrid upstream stub", "offline upstream stub content is missing")
    diagnostics = chat.get("diagnostics")
    preparation = diagnostics.get("gatewayRequestPreparation") if isinstance(diagnostics, dict) else None
    if not isinstance(preparation, dict):
        raise SmokeFailure("Gateway preparation diagnostics are missing")
    expected_runtime = "rust" if expect_rust else "python"
    _require(preparation.get("runtime") == expected_runtime, f"Gateway preparation runtime must be {expected_runtime}")
    _require(preparation.get("fallback") is (not expect_rust), "Gateway preparation fallback flag is incorrect")
    if not expect_rust:
        _require(preparation.get("fallbackReason") == "rust_backend_unavailable", "Gateway fallback reason is not stable")
    return CheckResult(
        "gateway-request-preparation",
        f"Python models and upstream HTTP passed with {expected_runtime} request preparation",
    )


def _mcp_call(
    base_url: str,
    request_id: str,
    method: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> tuple[dict[str, Any], HttpResult]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = _request(base_url, "POST", "/mcp", payload=payload, timeout=timeout)
    body = _expect_ok(response, f"POST /mcp ({method})")
    _require(body.get("jsonrpc") == "2.0", f"MCP {method} response is not JSON-RPC 2.0")
    _require(body.get("id") == request_id, f"MCP {method} did not preserve request id")
    _require("error" not in body, f"MCP {method} returned an error: {body.get('error')}")
    result = body.get("result")
    if not isinstance(result, dict):
        raise SmokeFailure(f"MCP {method} response has no result")
    return result, response


def _require_mcp_diagnostics(response: HttpResult, *, runtime: str, fallback: bool) -> None:
    headers = response.headers
    _require(
        headers.get("x-deepseek-mcp-preparation-runtime") == runtime,
        f"MCP protocol preparation runtime must be {runtime}",
    )
    _require(
        headers.get("x-deepseek-mcp-preparation-fallback") == ("1" if fallback else "0"),
        "MCP protocol preparation fallback flag is incorrect",
    )
    if fallback:
        _require(
            headers.get("x-deepseek-mcp-preparation-fallback-reason") == "rust_backend_unavailable",
            "MCP protocol preparation fallback reason is not stable",
        )


def _check_invalid_mcp_request(base_url: str, *, timeout: float) -> None:
    response = _request(
        base_url,
        "POST",
        "/mcp",
        payload={"jsonrpc": "2.0", "id": "invalid-tools-call", "method": "tools/call", "params": {}},
        timeout=timeout,
    )
    body = _expect_ok(response, "POST /mcp (invalid tools/call)")
    error = body.get("error")
    data = error.get("data") if isinstance(error, dict) else None
    _require(isinstance(error, dict) and error.get("code") == -32602, "invalid MCP request did not return JSON-RPC invalid params")
    _require(isinstance(data, dict) and data.get("code") == "invalid_params", "invalid MCP request lost its stable error category")
    _require_mcp_diagnostics(response, runtime="python", fallback=False)


def check_mcp_protocol_preparation(base_url: str, *, expect_rust: bool, timeout: float = 5.0) -> CheckResult:
    runtime = "rust" if expect_rust else "python"
    fallback = not expect_rust
    initialized, initialized_response = _mcp_call(
        base_url,
        "hybrid-init" if expect_rust else "fallback-init",
        "initialize",
        {"protocolVersion": "2024-11-05"},
        timeout,
    )
    _require_mcp_diagnostics(initialized_response, runtime=runtime, fallback=fallback)
    server_info = initialized.get("serverInfo")
    _require(isinstance(server_info, dict) and server_info.get("name") == "deepseek-infra", "MCP execution did not stay Python-owned")
    listed, listed_response = _mcp_call(
        base_url,
        "hybrid-list" if expect_rust else "fallback-list",
        "tools/list",
        {},
        timeout,
    )
    _require_mcp_diagnostics(listed_response, runtime=runtime, fallback=fallback)
    tools = listed.get("tools")
    names = {item.get("name") for item in tools if isinstance(item, dict)} if isinstance(tools, list) else set()
    _require("data_transform" in names, "Python MCP tool catalog is missing data_transform")
    called, called_response = _mcp_call(
        base_url,
        "hybrid-call" if expect_rust else "fallback-call",
        "tools/call",
        {"name": "data_transform", "arguments": {"operation": "number_summary", "input": "1 2 3 4"}},
        timeout,
    )
    _require_mcp_diagnostics(called_response, runtime=runtime, fallback=fallback)
    structured = called.get("structuredContent")
    summary = structured.get("result") if isinstance(structured, dict) else None
    _require(isinstance(summary, dict) and summary.get("count") == 4, "Python-owned MCP tool call returned the wrong result")
    _check_invalid_mcp_request(base_url, timeout=timeout)
    phase = "Rust protocol preparation" if expect_rust else "Python protocol fallback"
    return CheckResult(
        "mcp-protocol-preparation" if expect_rust else "mcp-fallback",
        f"{phase}; initialize, catalog, Python tool execution, and stable invalid_params passed",
    )


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
    probes = {
        "policy": POLICY_PROBE,
        "rag": RAG_PROBE,
        "rag-document": RAG_DOCUMENT_PROBE,
        "rag-vector-binary": RAG_VECTOR_BINARY_PROBE,
    }
    code = probes.get(probe, "")
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
        _require(client.get("code") == "localhost_blocked", "Rust Policy deny did not include the stable localhost code")
        _require(str(client.get("decision_id") or "").startswith("pd_"), "Rust Policy deny did not include a decision id")
        _require(output.get("code") == client.get("code"), "tool denial did not preserve the Rust Policy code")
        _require(output.get("decision_id") == client.get("decision_id"), "tool denial did not preserve the Rust decision id")
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


def check_rag_vector_binary(
    compose_files: tuple[str, ...],
    *,
    expect_rust: bool,
    expected_result: tuple[str, float] | None = None,
) -> tuple[CheckResult, tuple[str, float]]:
    payload = _run_container_probe("rag-vector-binary", compose_files, timeout=60.0)
    trace = payload.get("trace")
    diagnostics = payload.get("diagnostics")
    if not isinstance(trace, dict) or not isinstance(diagnostics, dict):
        raise SmokeFailure("binary vector probe is malformed")
    _require(trace.get("binaryCalls") == 1, "semantic-cache ranking did not issue exactly one binary request")
    _require(trace.get("jsonCalls") == 0, "binary failure retried the JSON Rust endpoint")
    _require(diagnostics.get("transportEncoding") == "binary", "vector ranking did not select binary transport")
    _require(payload.get("candidateCount") == 3 and payload.get("dimensions") == 4, "binary vector probe did not rank all storage modes")
    _require(payload.get("storedDual") is True, "fresh semantic-cache writes did not persist JSON and BLOB representations")
    _require(payload.get("pythonOwnsSQLite") is True, "semantic-cache persistence ownership changed")
    storage = payload.get("storageDiagnostics")
    if not isinstance(storage, dict):
        raise SmokeFailure("binary vector storage diagnostics are missing")
    _require(storage.get("embeddingStorage") == "mixed", "mixed BLOB/legacy storage was not reported")
    _require(storage.get("blobCandidates") == 1, "valid BLOB candidate count is incorrect")
    _require(storage.get("legacyCandidates") == 2, "legacy/fallback candidate count is incorrect")
    _require(storage.get("invalidBlobCandidates") == 1, "corrupt BLOB fallback count is incorrect")
    _require(diagnostics.get("payloadAssemblySource") == "blob", "binary request was not assembled from BLOB buffers")
    expected_backend = "rust" if expect_rust else "python"
    _require(payload.get("backend") == expected_backend, f"binary vector ranking backend must be {expected_backend}")
    _require(diagnostics.get("runtime") == expected_backend, "binary vector diagnostics runtime is incorrect")
    _require(diagnostics.get("fallback") is (not expect_rust), "binary vector fallback flag is incorrect")
    if expect_rust:
        _require(payload.get("metricsBinarySuccessDelta") == 1, "binary endpoint did not record exactly one successful request")
        _require(diagnostics.get("responsePayloadBytes") == 24, "binary vector response was not 24 bytes")
    else:
        _require(diagnostics.get("fallbackReason") == "rust_backend_unavailable", "binary vector fallback reason is not stable")
        _require(payload.get("metricsBinarySuccessDelta") is None, "stopped sidecar unexpectedly exposed binary metrics")
    selected_id = payload.get("selectedContent")
    similarity = payload.get("similarity")
    if not isinstance(selected_id, str) or not isinstance(similarity, (int, float)):
        raise SmokeFailure("binary vector probe returned an invalid ranking")
    result = (selected_id, float(similarity))
    if expected_result is not None:
        _require(result == expected_result, "binary Rust and Python fallback rankings differ")
    phase = "one binary Rust request with full Python parity" if expect_rust else "one failed binary request then direct Python fallback"
    return CheckResult("rag-vector-binary", phase), result


def _assert_rag_document_probe(payload: dict[str, Any], *, expect_rust: bool) -> str:
    trace = payload.get("trace")
    diagnostics = payload.get("diagnostics")
    if not isinstance(trace, dict) or not isinstance(diagnostics, dict):
        raise SmokeFailure("RAG document preparation probe is malformed")
    _require(trace.get("calls") == 1, "real Python ingestion did not call the Rust document preparation client exactly once")
    _require(trace.get("safePayload") is True, "Rust document preparation received a path, credential, or raw file bytes")
    expected_runtime = "rust" if expect_rust else "python"
    _require(diagnostics.get("runtime") == expected_runtime, f"RAG document preparation runtime must be {expected_runtime}")
    _require(diagnostics.get("fallback") is (not expect_rust), "RAG document preparation fallback flag is incorrect")
    if not expect_rust:
        _require(diagnostics.get("fallbackReason") == "rust_backend_unavailable", "RAG document fallback reason is not stable")
    _require(payload.get("persistedByPython") is True, "Python did not persist the prepared chunks")
    _require(payload.get("readerMatched") is True, "Python query/reader path did not return the persisted chunks")
    _require(isinstance(payload.get("chunkCount"), int) and payload["chunkCount"] > 0, "RAG document preparation returned no chunks")
    fingerprint = payload.get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise SmokeFailure("RAG document chunk fingerprint is invalid")
    return fingerprint


def check_rag_document_preparation(
    compose_files: tuple[str, ...],
    *,
    expect_rust: bool,
    expected_fingerprint: str | None = None,
) -> tuple[CheckResult, str]:
    payload = _run_container_probe("rag-document", compose_files)
    fingerprint = _assert_rag_document_probe(payload, expect_rust=expect_rust)
    if expected_fingerprint is not None:
        _require(fingerprint == expected_fingerprint, "Rust and Python fallback produced different document chunks")
    phase = "Rust preparation with Python persistence" if expect_rust else "Python preparation fallback with identical chunks"
    return CheckResult("rag-document-preparation", phase), fingerprint


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
    return check_gateway_request_preparation(base_url, expect_rust=False, timeout=timeout)


def _check_mcp_fallback(base_url: str, *, timeout: float) -> CheckResult:
    return check_mcp_protocol_preparation(base_url, expect_rust=False, timeout=timeout)


def check_fallbacks(
    base_url: str,
    compose_files: tuple[str, ...],
    *,
    timeout: float = 5.0,
    rag_document_fingerprint: str | None = None,
    rag_vector_result: tuple[str, float] | None = None,
) -> list[CheckResult]:
    rag_document, _ = check_rag_document_preparation(
        compose_files,
        expect_rust=False,
        expected_fingerprint=rag_document_fingerprint,
    )
    rag_vector, _ = check_rag_vector_binary(
        compose_files,
        expect_rust=False,
        expected_result=rag_vector_result,
    )
    return [
        check_rust_status(base_url, expect_healthy=False, timeout=timeout),
        _check_gateway_fallback(base_url, timeout=timeout),
        _check_mcp_fallback(base_url, timeout=timeout),
        check_policy_integration(compose_files, expect_rust=False),
        check_rag_integration(compose_files, expect_rust=False),
        rag_vector,
        rag_document,
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
    checks.append(check_gateway_request_preparation(base_url, timeout=timeout))
    checks.append(check_mcp_protocol_preparation(base_url, expect_rust=True, timeout=timeout))
    checks.append(check_policy_integration(compose_files, expect_rust=True))
    checks.append(check_rag_integration(compose_files, expect_rust=True))
    rag_vector, rag_vector_result = check_rag_vector_binary(compose_files, expect_rust=True)
    checks.append(rag_vector)
    rag_document, rag_document_fingerprint = check_rag_document_preparation(compose_files, expect_rust=True)
    checks.append(rag_document)
    if not keep_sidecar:
        checks.append(stop_sidecar(compose_files))
        checks.extend(
            check_fallbacks(
                base_url,
                compose_files,
                timeout=timeout,
                rag_document_fingerprint=rag_document_fingerprint,
                rag_vector_result=rag_vector_result,
            )
        )
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
        report = stamp_release_report({"ok": True, "checks": [asdict(check) for check in checks]}, root=ROOT)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"PASS {check.name}: {check.detail}")
        print(f"Hybrid runtime smoke passed ({len(checks)} checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
