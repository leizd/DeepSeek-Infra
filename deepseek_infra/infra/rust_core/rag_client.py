"""HTTP proxy client for Rust-backed RAG hot paths."""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import rust_gateway_url

DEFAULT_RAG_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class RagProxyResult:
    ok: bool
    status: int
    body: Any


def _rust_rag_enabled() -> bool:
    from deepseek_infra.infra.rust_core.config import load_rust_flags

    return load_rust_flags().rag


def _fallback_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_RAG_FALLBACK", "1")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _timeout_ms() -> int:
    try:
        return int(os.environ.get("DEEPSEEK_RUST_RAG_TIMEOUT_MS", DEFAULT_RAG_TIMEOUT_MS))
    except ValueError:
        return DEFAULT_RAG_TIMEOUT_MS


def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_ms: int | None = None,
) -> RagProxyResult:
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return RagProxyResult(ok=True, status=response.status, body={})
            return RagProxyResult(
                ok=True, status=response.status, body=json.loads(raw.decode("utf-8"))
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return RagProxyResult(ok=False, status=exc.code, body=body)
    except Exception as exc:
        return RagProxyResult(ok=False, status=0, body=str(exc))


def _should_use_result(result: RagProxyResult) -> bool:
    if not result.ok:
        return False
    if result.status != 200:
        return False
    if not isinstance(result.body, dict):
        return False
    return True


def normalize_query(query: str) -> tuple[str | None, bool]:
    """Return (normalized, used_rust). Falls back to None if Rust fails or is disabled."""
    if not _rust_rag_enabled():
        return None, False
    result = _request("POST", "/rag/query/normalize", payload={"query": query})
    if _should_use_result(result):
        body = result.body
        normalized = body.get("normalized")
        if isinstance(normalized, str):
            return normalized, True
    if _fallback_enabled():
        return None, False
    return None, False


def score_chunks(
    query: str, chunks: list[dict[str, Any]]
) -> tuple[list[tuple[str, float]] | None, bool]:
    """Return (ranked_id_score_pairs, used_rust). Falls back to None."""
    if not _rust_rag_enabled() or not chunks:
        return None, False
    result = _request(
        "POST", "/rag/chunks/score", payload={"query": query, "chunks": chunks}
    )
    if _should_use_result(result):
        ranked = result.body.get("ranked")
        if isinstance(ranked, list):
            pairs: list[tuple[str, float]] = []
            for item in ranked:
                if isinstance(item, dict):
                    item_id = item.get("id")
                    item_score = item.get("score")
                    if isinstance(item_id, str) and isinstance(item_score, (int, float)):
                        pairs.append((item_id, float(item_score)))
            return pairs, True
    if _fallback_enabled():
        return None, False
    return None, False


def format_citation(
    source: str, start_line: int | None, end_line: int | None
) -> tuple[str | None, bool]:
    """Return (citation, used_rust). Falls back to None."""
    if not _rust_rag_enabled():
        return None, False
    payload: dict[str, Any] = {"source": source}
    if start_line is not None:
        payload["start_line"] = start_line
    if end_line is not None:
        payload["end_line"] = end_line
    result = _request("POST", "/rag/citation/format", payload=payload)
    if _should_use_result(result):
        citation = result.body.get("citation")
        if isinstance(citation, str):
            return citation, True
    if _fallback_enabled():
        return None, False
    return None, False


def rank_vectors(
    query: list[float], candidates: list[list[float]]
) -> tuple[tuple[int | None, float] | None, bool]:
    """Return ((best_index, similarity), used_rust) for semantic-cache vectors."""
    if not _rust_rag_enabled() or not query or not candidates:
        return None, False
    result = _request(
        "POST", "/rag/vectors/rank", payload={"query": query, "candidates": candidates}
    )
    if _should_use_result(result):
        index = result.body.get("index")
        similarity = result.body.get("similarity")
        valid_index = index is None or (
            isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(candidates)
        )
        valid_similarity = (
            isinstance(similarity, (int, float))
            and not isinstance(similarity, bool)
            and math.isfinite(float(similarity))
            and 0.0 <= float(similarity) <= 1.0
        )
        if valid_index and valid_similarity:
            return (index, float(similarity)), True
    if _fallback_enabled():
        return None, False
    return None, False


def validate_index(chunks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Return (validation_result, used_rust). Falls back to None."""
    if not _rust_rag_enabled() or not chunks:
        return None, False
    result = _request("POST", "/rag/index/validate", payload={"chunks": chunks})
    if _should_use_result(result):
        body = result.body
        if isinstance(body, dict):
            return {"valid": bool(body.get("valid")), "error": body.get("error")}, True
    if _fallback_enabled():
        return None, False
    return None, False


def rust_rag_enabled() -> bool:
    return _rust_rag_enabled()


def fallback_to_python_enabled() -> bool:
    return _fallback_enabled()
