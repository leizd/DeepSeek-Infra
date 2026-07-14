from __future__ import annotations

import array
import json
import math
import struct
import urllib.error
import urllib.request
from io import BytesIO
from email.message import Message

import pytest

from deepseek_infra.infra.rust_core import rag_client, vector_binary
from deepseek_infra.infra.rust_core.config import rust_rag_vector_transport


class _Response:
    def __init__(self, body: bytes, content_type: str = vector_binary.CONTENT_TYPE, status: int = 200) -> None:
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type, "X-DeepSeek-Rust-Processing-Us": "7"}
        self.transport_us = 11
        self.connection_reused = True
        self.connection_count = 1

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _binary_response(index: int, similarity: float, *, magic: bytes = vector_binary.RESPONSE_MAGIC, reserved: int = 0) -> bytes:
    return struct.pack("<8sIId", magic, index, reserved, similarity)


def _http_error(code: int = 404) -> urllib.error.HTTPError:
    headers = Message()
    return urllib.error.HTTPError(
        "http://127.0.0.1:8787/rag/vectors/rank-binary",
        code,
        "not found",
        headers,
        BytesIO(b'{"code":"not_found"}'),
    )


@pytest.fixture(autouse=True)
def _binary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_RUST_RAG", raising=False)
    monkeypatch.delenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", raising=False)
    rag_client.reset_delegate_diagnostics()


def test_binary_encoder_roundtrip_and_little_endian() -> None:
    encoded = vector_binary.encode_rank_request([1.0, -0.0], [[0.5, 0.25], [-1.0, 2.0]])
    assert encoded.dimensions == 2
    assert encoded.candidate_count == 2
    assert encoded.scalar_count == 6
    assert encoded.body[:8] == vector_binary.REQUEST_MAGIC
    assert encoded.body[8:12] == b"\x02\x00\x00\x00"
    assert encoded.body[12:16] == b"\x02\x00\x00\x00"
    assert struct.unpack("<6d", encoded.body[16:]) == (1.0, -0.0, 0.5, 0.25, -1.0, 2.0)


def test_binary_encoder_handles_big_endian_host() -> None:
    values = array.array("d")
    values.frombytes(struct.pack(">2d", 1.0, -2.0))
    assert vector_binary._little_endian_bytes(values, host_byteorder="big") == struct.pack("<2d", 1.0, -2.0)


@pytest.mark.parametrize(
    ("query", "candidates", "code"),
    [
        ([], [[1.0]], "invalid_dimensions"),
        ([1.0], [], "invalid_candidate_count"),
        ([1.0], [[1.0, 2.0]], "invalid_dimensions"),
        ([math.nan], [[1.0]], "non_finite_vector"),
        ([1.0], [[math.inf]], "non_finite_vector"),
    ],
)
def test_binary_encoder_rejects_invalid_input(
    query: list[float], candidates: list[list[float]], code: str
) -> None:
    with pytest.raises(vector_binary.VectorBinaryError) as exc_info:
        vector_binary.encode_rank_request(query, candidates)
    assert exc_info.value.code == code


def test_binary_transport_disabled_uses_json(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    mock_urlopen.return_value = _Response(
        json.dumps({"index": 0, "similarity": 1.0}).encode(), "application/json"
    )
    assert rust_rag_vector_transport() == "json"
    assert rag_client.rank_vectors([1.0], [[1.0]]) == ((0, 1.0), True)
    request: urllib.request.Request = mock_urlopen.call_args.args[0]
    assert request.full_url.endswith("/rag/vectors/rank")
    assert request.headers["Content-type"] == "application/json"
    assert rag_client.last_delegate_diagnostics("rag_vector_rank")["transportEncoding"] == "json"


def test_invalid_transport_config_fails_closed_to_json(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "user-controlled-value")
    mock_urlopen.return_value = _Response(b'{"index":0,"similarity":1.0}', "application/json")
    assert rag_client.rank_vectors([1.0], [[1.0]]) == ((0, 1.0), True)
    diagnostics = rag_client.last_delegate_diagnostics("rag_vector_rank")
    assert diagnostics["transportEncoding"] == "json"
    assert diagnostics["transportConfigInvalid"] is True
    assert "user-controlled" not in json.dumps(diagnostics)


def test_binary_transport_success(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    mock_urlopen.return_value = _Response(_binary_response(1, 0.75))
    assert rag_client.rank_vectors([1.0, 0.0], [[0.5, 0.0], [0.75, 0.0]]) == ((1, 0.75), True)
    request: urllib.request.Request = mock_urlopen.call_args.args[0]
    assert request.full_url.endswith("/rag/vectors/rank-binary")
    assert request.headers["Content-type"] == vector_binary.CONTENT_TYPE
    diagnostics = rag_client.last_delegate_diagnostics("rag_vector_rank")
    assert diagnostics["transportEncoding"] == "binary"
    assert isinstance(request.data, bytes)
    assert diagnostics["requestPayloadBytes"] == len(request.data)
    assert diagnostics["responsePayloadBytes"] == 24
    assert diagnostics["rustProcessingUs"] == 7


@pytest.mark.parametrize(
    ("side_effect", "reason"),
    [
        (urllib.error.URLError("offline"), "rust_backend_unavailable"),
        (TimeoutError("timed out"), "rust_backend_timeout"),
        (_http_error(), "rust_http_failure"),
    ],
)
def test_binary_transport_failures_fall_back(
    mock_urlopen, monkeypatch: pytest.MonkeyPatch, side_effect: BaseException, reason: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    mock_urlopen.side_effect = side_effect
    assert rag_client.rank_vectors([1.0], [[1.0]]) == (None, False)
    diagnostics = rag_client.last_delegate_diagnostics("rag_vector_rank")
    assert diagnostics["fallback"] is True
    assert diagnostics["fallbackReason"] == reason
    assert mock_urlopen.call_count == 1
    assert mock_urlopen.call_args.args[0].full_url.endswith("/rag/vectors/rank-binary")


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_Response(b""), "rust_empty_response"),
        (_Response(_binary_response(0, 1.0), "application/json"), "rust_binary_content_type"),
        (_Response(b"short"), "invalid_binary_response_length"),
        (_Response(_binary_response(0, 1.0, magic=b"BADMAGIC")), "invalid_binary_response_magic"),
        (_Response(_binary_response(0, 1.0, reserved=1)), "invalid_binary_response_reserved"),
        (_Response(_binary_response(9, 1.0)), "invalid_binary_response_index"),
        (_Response(_binary_response(0, math.nan)), "invalid_binary_response_similarity"),
    ],
)
def test_binary_malformed_responses_fall_back(
    mock_urlopen, monkeypatch: pytest.MonkeyPatch, response: _Response, reason: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    mock_urlopen.return_value = response
    assert rag_client.rank_vectors([1.0], [[1.0]]) == (None, False)
    assert rag_client.last_delegate_diagnostics("rag_vector_rank")["fallbackReason"] == reason
    assert mock_urlopen.call_count == 1


def test_binary_no_match_sentinel_preserves_empty_result(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    mock_urlopen.return_value = _Response(_binary_response(vector_binary.NO_MATCH_INDEX, 0.0))
    assert rag_client.rank_vectors([0.0], [[1.0]]) == ((None, 0.0), True)


def test_binary_failure_does_not_retry_json_sidecar(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    mock_urlopen.return_value = _Response(b"malformed")
    assert rag_client.rank_vectors([1.0], [[1.0]]) == (None, False)
    assert mock_urlopen.call_count == 1
    assert "rank-binary" in mock_urlopen.call_args.args[0].full_url


def test_binary_diagnostics_do_not_include_vectors(mock_urlopen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_RAG", "1")
    monkeypatch.setenv("DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT", "binary")
    mock_urlopen.return_value = _Response(_binary_response(0, 1.0))
    query = [0.123456789]
    candidates = [[0.987654321]]
    rag_client.rank_vectors(query, candidates)
    rendered = json.dumps(rag_client.last_delegate_diagnostics("rag_vector_rank"))
    assert "0.123456789" not in rendered
    assert "0.987654321" not in rendered
    assert set(rag_client.last_delegate_diagnostics("rag_vector_rank")) >= {
        "pythonPreparationUs",
        "serializationUs",
        "transportUs",
        "rustProcessingUs",
        "pythonValidationUs",
        "totalDelegateUs",
        "transportEncoding",
        "requestPayloadBytes",
        "responsePayloadBytes",
    }
