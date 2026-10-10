"""Tests for the Rust Gateway request-preparation boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.rust_core import gateway_client


@pytest.fixture(autouse=True)
def _clear_rust_gateway_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "DEEPSEEK_RUST_GATEWAY",
        "DEEPSEEK_RUST_GATEWAY_URL",
        "DEEPSEEK_RUST_GATEWAY_FALLBACK",
        "DEEPSEEK_RUST_GATEWAY_TIMEOUT_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def test_fallback_enabled_by_default() -> None:
    assert gateway_client.fallback_to_python_enabled() is True


def test_fallback_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY_FALLBACK", "0")
    assert gateway_client.fallback_to_python_enabled() is False


def test_request_preparation_disabled_does_not_call_sidecar(mock_urlopen: Any) -> None:
    result = gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"})

    assert result.ok is False
    assert result.error_kind == "rust_gateway_disabled"
    mock_urlopen.assert_not_called()


def test_request_preparation_posts_only_json_without_authorization(
    mock_urlopen: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mocked = mock_urlopen
    mocked.return_value.__enter__.return_value.status = 200
    mocked.return_value.__enter__.return_value.read.return_value = json.dumps(
        {"ok": True, "request": {"model": "deepseek-v4-pro", "messages": []}}
    ).encode("utf-8")

    result = gateway_client.prepare_request_with_rust(
        {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert result.ok is True
    request = mocked.call_args.args[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert "authorization" not in headers
    assert request.full_url.endswith("/gateway/request/prepare")


def test_gateway_client_rejects_non_object_json(mock_urlopen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.return_value.__enter__.return_value.status = 200
    mock_urlopen.return_value.__enter__.return_value.read.return_value = b"[]"

    result = gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"})

    assert result.ok is False
    assert result.error_kind == "rust_invalid_shape"


def test_gateway_client_direct_timeout_is_classified(mock_urlopen: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_RUST_GATEWAY", "1")
    mock_urlopen.side_effect = TimeoutError("timed out")

    result = gateway_client.prepare_request_with_rust({"model": "deepseek-v4-pro"})

    assert result.ok is False
    assert result.error_kind == "rust_backend_timeout"


def test_streaming_path_never_calls_request_preparation() -> None:
    source = Path("deepseek_infra/web/routes/chat.py").read_text(encoding="utf-8")
    assert "proxy_chat_to_rust" not in source
    assert "openai_chat_stream" in source


def test_models_route_is_not_delegated_to_rust() -> None:
    source = Path("deepseek_infra/web/routes/chat.py").read_text(encoding="utf-8")
    models_route = source[source.index('@router.get("/v1/models")') :]
    assert "proxy_models_to_rust" not in models_route
    assert "openai_models_list" in models_route
