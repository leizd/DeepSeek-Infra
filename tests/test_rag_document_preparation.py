from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.rag import document_preparation as prep
from deepseek_infra.infra.rag import files
from deepseek_infra.infra.rust_core import rag_client


def request(
    text: object = "First paragraph.\n\nSecond paragraph.",
    *,
    document_id: object = "doc-123",
    metadata: object = None,
    chunk_chars: object = 18,
    chunk_overlap: object = 2,
) -> dict[str, object]:
    return {
        "documentId": document_id,
        "text": text,
        "metadata": {"displayName": "notes.txt", "sourceType": "text/plain"} if metadata is None else metadata,
        "chunking": {"chunkChars": chunk_chars, "chunkOverlap": chunk_overlap},
    }


def proxy_result(body: object, *, ok: bool = True, error_kind: str = "", latency_ms: int = 1) -> rag_client.RagProxyResult:
    return rag_client.RagProxyResult(ok=ok, status=200 if ok else 0, body=body, error_kind=error_kind, latency_ms=latency_ms)


def test_python_document_preparation_matches_existing_chunk_contract() -> None:
    value = request(" alpha  \r\n beta\r\ngamma ", chunk_chars=12, chunk_overlap=2)
    result = prep.prepare_rag_document(value)
    normalized = prep.normalize_document_text(str(value["text"]))

    assert result["ok"] is True
    assert result["document"]["characterCount"] == len(normalized)
    assert result["document"]["chunkCount"] == len(result["chunks"])
    assert result["diagnostics"]["normalized"] is True
    for index, chunk in enumerate(result["chunks"]):
        assert chunk["index"] == index
        assert chunk["text"] == normalized[chunk["start"] : chunk["end"]].strip()
        assert chunk["lineStart"] == normalized.count("\n", 0, chunk["start"]) + 1
        assert chunk["lineEnd"] == normalized.count("\n", 0, chunk["end"]) + 1
        assert chunk["contentHash"] == prep.chunk_content_hash(chunk["text"])


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ([], "invalid_request"),
        ({"documentId": "x"}, "invalid_text"),
        (request(document_id=""), "invalid_document_id"),
        (request(document_id=" doc "), "invalid_document_id"),
        (request(document_id="x" * 257), "invalid_document_id"),
        (request(document_id="x\ny"), "invalid_document_id"),
        (request(text=7), "invalid_text"),
        (request(text=" \r\n\x00 "), "invalid_text"),
        (request(metadata=[]), "invalid_metadata"),
        (request(metadata={"displayName": []}), "invalid_metadata"),
        (request(chunk_chars=0), "invalid_chunk_size"),
        (request(chunk_chars=-1), "invalid_chunk_size"),
        (request(chunk_chars=True), "invalid_chunk_size"),
        (request(chunk_chars=prep.RAG_DOCUMENT_MAX_CHUNK_CHARACTERS + 1), "invalid_chunk_size"),
        (request(chunk_overlap=-1), "invalid_chunk_overlap"),
        (request(chunk_overlap=True), "invalid_chunk_overlap"),
        (request(chunk_chars=4, chunk_overlap=4), "chunk_overlap_too_large"),
        (request(chunk_chars=4, chunk_overlap=5), "chunk_overlap_too_large"),
    ],
)
def test_stable_input_errors(value: object, code: str) -> None:
    assert prep.prepare_rag_document(value)["code"] == code


def test_request_size_nesting_unknown_and_sensitive_fields() -> None:
    value = request("safe")
    assert prep.prepare_rag_document(value, payload_size=prep.RAG_DOCUMENT_MAX_REQUEST_BYTES + 1)["code"] == "request_too_large"

    nested: object = {}
    for _ in range(prep.RAG_DOCUMENT_MAX_NESTING + 1):
        nested = {"nested": nested}
    deep = request(metadata=nested)
    assert prep.prepare_rag_document(deep)["code"] == "nesting_limit_exceeded"

    unknown = request()
    unknown["extra"] = True
    assert prep.prepare_rag_document(unknown)["code"] == "invalid_request"
    sensitive = request()
    sensitive["uploadPath"] = "C:/tmp/file"
    assert prep.prepare_rag_document(sensitive)["code"] == "invalid_request"
    sensitive_metadata = request(metadata={"apiKey": "secret"})
    assert prep.prepare_rag_document(sensitive_metadata)["code"] == "invalid_metadata"
    invalid_chunking = request()
    invalid_chunking["chunking"] = []
    assert prep.prepare_rag_document(invalid_chunking)["code"] == "invalid_request"
    unknown_chunking = request()
    unknown_chunking["chunking"] = {"chunkChars": 4, "chunkOverlap": 0, "path": "x"}
    assert prep.prepare_rag_document(unknown_chunking)["code"] == "invalid_request"


def test_unserializable_nonfinite_and_document_limit_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    assert prep.prepare_rag_document(cyclic)["code"] == "invalid_request"
    assert prep.prepare_rag_document(request(metadata={"displayName": float("nan")}))["code"] == "invalid_request"
    monkeypatch.setattr(prep, "RAG_DOCUMENT_MAX_CHARACTERS", 4)
    assert prep.prepare_rag_document(request("12345", chunk_chars=4, chunk_overlap=0))["code"] == "document_too_large"


def test_metadata_allowlist_and_hash_rules() -> None:
    first = prep.prepare_rag_document(
        request(
            "same text",
            metadata={"displayName": "a.txt", "sourceType": "text/plain", "kind": "text", "unknown": "drop", "ignored": 7},
            chunk_chars=5,
            chunk_overlap=1,
        )
    )
    second = prep.prepare_rag_document(
        request("same text", metadata={"displayName": "b.txt", "sourceType": "text/plain"}, chunk_chars=5, chunk_overlap=1)
    )
    changed = prep.prepare_rag_document(request("changed text", chunk_chars=5, chunk_overlap=1))
    other_id = prep.prepare_rag_document(request("same text", document_id="doc-other", chunk_chars=5, chunk_overlap=1))

    assert first["document"]["metadata"] == {"displayName": "a.txt", "kind": "text", "sourceType": "text/plain"}
    assert first["document"]["contentHash"] == second["document"]["contentHash"]
    assert first["document"]["contentHash"] != changed["document"]["contentHash"]
    assert first["document"]["contentHash"] == other_id["document"]["contentHash"]
    assert first["chunks"][0]["chunkId"] != other_id["chunks"][0]["chunkId"]
    assert len({chunk["chunkId"] for chunk in first["chunks"]}) == len(first["chunks"])


def test_crlf_hash_unicode_offsets_and_combining_characters_are_stable() -> None:
    crlf = prep.prepare_rag_document(request("中文\r\n🚀e\u0301", chunk_chars=3, chunk_overlap=1))
    lf = prep.prepare_rag_document(request("中文\n🚀e\u0301", chunk_chars=3, chunk_overlap=1))
    assert crlf["document"] == lf["document"]
    assert crlf["chunks"] == lf["chunks"]
    assert crlf["document"]["characterCount"] == 6
    assert crlf["chunks"][1]["start"] == 1
    assert crlf["chunks"][1]["end"] == 4
    assert crlf["chunks"][1]["end"] < len("中文\n🚀e\u0301".encode("utf-8"))


def test_prepare_json_parsing_and_size_errors() -> None:
    assert prep.prepare_rag_document_json(b"{")["code"] == "invalid_request"
    assert prep.prepare_rag_document_json(b"\xff")["code"] == "invalid_request"
    encoded = json.dumps(request("hello", chunk_chars=8, chunk_overlap=0)).encode()
    assert prep.prepare_rag_document_json(encoded)["ok"] is True
    with patch.object(prep, "RAG_DOCUMENT_MAX_REQUEST_BYTES", 1):
        assert prep.prepare_rag_document_json(encoded)["code"] == "request_too_large"


def test_rust_rag_document_prepare_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    value = request()
    local = prep.prepare_rag_document(value)
    with patch.object(rag_client, "prepare_document", return_value=proxy_result(local)) as rust:
        decision = prep.prepare_rag_document_with_optional_rust(value)
    assert decision.preparation == local
    assert decision.diagnostics["runtime"] == "rust"
    assert decision.diagnostics["fallback"] is False
    sent = rust.call_args.args[0]
    assert sent["text"] == value["text"]
    assert sent["metadata"] == local["document"]["metadata"]


def test_rust_rag_document_prepare_disabled_uses_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", raising=False)
    with patch.object(rag_client, "prepare_document", side_effect=AssertionError("must stay local")):
        decision = prep.prepare_rag_document_with_optional_rust(request())
    assert decision.preparation["ok"] is True
    assert decision.diagnostics["runtime"] == "python"


@pytest.mark.parametrize(
    ("name", "backend"),
    [
        ("rust_rag_document_prepare_connection_failure_falls_back", proxy_result(None, ok=False, error_kind="rust_backend_unavailable")),
        ("rust_rag_document_prepare_timeout_falls_back", proxy_result(None, ok=False, error_kind="rust_backend_timeout")),
        ("rust_rag_document_prepare_empty_response_falls_back", proxy_result(None, ok=False, error_kind="rust_empty_response")),
        ("rust_rag_document_prepare_malformed_json_falls_back", proxy_result(None, ok=False, error_kind="rust_malformed_json")),
    ],
)
def test_rust_backend_failures_fall_back(monkeypatch: pytest.MonkeyPatch, name: str, backend: rag_client.RagProxyResult) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    with patch.object(rag_client, "prepare_document", return_value=backend):
        decision = prep.prepare_rag_document_with_optional_rust(request())
    assert name.startswith("rust_rag_document_prepare_")
    assert decision.preparation["ok"] is True
    assert decision.diagnostics["fallback"] is True
    assert decision.diagnostics["fallbackReason"] == backend.error_kind


def _divergent_result(value: dict[str, object], mutate: str) -> dict[str, object]:
    candidate = deepcopy(prep.prepare_rag_document(value))
    chunks = candidate["chunks"]
    assert isinstance(chunks, list) and chunks
    first = chunks[0]
    assert isinstance(first, dict)
    if mutate == "contract":
        candidate.pop("document")
    elif mutate == "semantic":
        document = candidate["document"]
        assert isinstance(document, dict)
        document["contentHash"] = "0" * 24
    elif mutate == "offset":
        first["start"] = len(str(value["text"])) + 50
    elif mutate == "duplicate":
        if len(chunks) == 1:
            chunks.append(deepcopy(first))
            chunks[1]["index"] = 1
        chunks[1]["chunkId"] = first["chunkId"]
    elif mutate == "hash":
        first["contentHash"] = "f" * 24
    elif mutate == "metadata":
        document = candidate["document"]
        assert isinstance(document, dict)
        metadata = document["metadata"]
        assert isinstance(metadata, dict)
        metadata["internalPath"] = "C:/secret"
    return candidate


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        ("contract", "rust_contract_invalid"),
        ("semantic", "rust_semantic_divergence"),
        ("offset", "rust_chunk_offset_invalid"),
        ("duplicate", "rust_chunk_id_invalid"),
        ("hash", "rust_content_hash_mismatch"),
        ("metadata", "rust_sensitive_field_injected"),
    ],
)
def test_rust_invalid_results_fall_back(monkeypatch: pytest.MonkeyPatch, mutate: str, reason: str) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    value = request("abcdefghijklmnop", chunk_chars=6, chunk_overlap=1)
    with patch.object(rag_client, "prepare_document", return_value=proxy_result(_divergent_result(value, mutate))):
        decision = prep.prepare_rag_document_with_optional_rust(value)
    assert decision.preparation == prep.prepare_rag_document(value)
    assert decision.diagnostics["fallbackReason"] == reason


def test_rust_rag_document_prepare_user_error_remains_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    with patch.object(rag_client, "prepare_document", side_effect=AssertionError("user errors stay local")):
        decision = prep.prepare_rag_document_with_optional_rust(request(text=[]))
    assert decision.preparation["code"] == "invalid_text"
    assert decision.diagnostics["fallback"] is False


def test_rust_rag_document_prepare_never_receives_path_or_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    value = request(metadata={"displayName": "notes.txt", "sourceType": "text/plain", "unknown": "ignored"})
    local = prep.prepare_rag_document(value)
    with patch.object(rag_client, "prepare_document", return_value=proxy_result(local)) as rust:
        prep.prepare_rag_document_with_optional_rust(value)
    encoded = json.dumps(rust.call_args.args[0], ensure_ascii=False).lower()
    assert "path" not in encoded
    assert "authorization" not in encoded
    assert "apikey" not in encoded
    assert "token" not in encoded
    assert "rawfilebytes" not in encoded


def test_rust_rag_document_prepare_diagnostics_are_redacted(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    value = request("private document body", metadata={"displayName": "safe.txt", "sourceType": "text/plain"})
    local = prep.prepare_rag_document(value)
    with patch.object(rag_client, "prepare_document", return_value=proxy_result(local)):
        decision = prep.prepare_rag_document_with_optional_rust(value)
    with caplog.at_level(logging.INFO, logger="deepseek_infra.rag.document_preparation"):
        prep.log_rag_document_preparation(decision.diagnostics)
    record = caplog.records[-1]
    rendered = json.dumps(record.__dict__, ensure_ascii=False, default=str).lower()
    assert "private document body" not in rendered
    assert "safe.txt" not in rendered
    assert getattr(record, "document_id_hash", "")
    assert prep.public_rag_document_diagnostics(decision.diagnostics)["runtime"] == "rust"


def test_real_file_ingestion_delegates_after_python_parsing_and_persists_in_python(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")

    def rust_prepare(payload: dict[str, object]) -> rag_client.RagProxyResult:
        assert payload["text"] == "alpha\r\n中文🚀 beta"
        encoded = json.dumps(payload, ensure_ascii=False).lower()
        assert "path" not in encoded
        assert "rawfilebytes" not in encoded
        return proxy_result(prep.prepare_rag_document(payload))

    with patch.object(rag_client, "prepare_document", side_effect=rust_prepare) as rust:
        extracted = files.extract_uploaded_file("folder\\notes.txt", "text/plain", "alpha\r\n中文🚀 beta".encode())

    assert rust.call_count == 1
    assert extracted["ragDocumentPreparation"]["runtime"] == "rust"
    cached = files.load_cached_file(extracted["fileId"])
    assert cached["chunks"]
    assert cached["chunks"][0]["contentHash"]
    assert cached["chunks"][0]["vector"]
    assert files.file_reader_window(extracted["fileId"])["chunks"][0]["text"] == "alpha\n中文🚀 beta"


def test_default_file_ingestion_schema_is_unchanged(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", raising=False)
    extracted = files.extract_uploaded_file("notes.txt", "text/plain", b"default python path")
    cached = files.load_cached_file(extracted["fileId"])
    assert "ragDocumentPreparation" not in extracted
    assert "chunkId" not in cached["chunks"][0]
    assert cached["chunks"][0]["text"] == "default python path"


def test_real_file_ingestion_rejects_invalid_local_preparation_before_persistence(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "1")
    invalid = prep.RagDocumentPreparationDecision(
        {"ok": False, "code": "invalid_chunk_size", "message": "invalid local chunk configuration"},
        "plain text",
        {"runtime": "python", "fallback": False},
    )
    with (
        patch.object(files, "prepare_rag_document_with_optional_rust", return_value=invalid),
        patch.object(files, "cache_file_chunks") as cache,
        pytest.raises(AppError, match="invalid local chunk configuration") as raised,
    ):
        files.extract_uploaded_file("notes.txt", "text/plain", b"plain text")
    assert raised.value.code == ErrorCode.INVALID_PAYLOAD
    cache.assert_not_called()


def test_file_content_id_is_stable_and_python_owned() -> None:
    assert files.file_content_id("a.txt", b"same") == files.file_content_id("a.txt", b"same")
    assert files.file_content_id("a.txt", b"same") != files.file_content_id("b.txt", b"same")


def test_rag_client_document_flag_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.delenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", raising=False)
    assert rag_client.rust_rag_enabled() is True
    assert rag_client.rag_document_preparation_enabled() is False
    disabled = rag_client.prepare_document(request())
    assert disabled.error_kind == "rust_disabled"
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "yes")
    assert rag_client.rag_document_preparation_enabled() is True
