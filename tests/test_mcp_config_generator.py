from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_generator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_mcp_client_config.py"
    spec = importlib.util.spec_from_file_location("generate_mcp_client_config_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cursor_auth_disabled_config_has_no_authorization_header() -> None:
    generator = _load_generator()
    config = generator.generate_config("cursor", auth_disabled=True)

    server = config["mcpServers"]["deepseek-infra"]
    assert server["url"] == "http://127.0.0.1:8000/mcp"
    assert "headers" not in server
    assert "secret-token" not in str(config)


def test_claude_direct_http_config_adds_bearer_header() -> None:
    generator = _load_generator()
    config = generator.generate_config("claude", token="local-token")

    server = config["mcpServers"]["deepseek-infra"]
    assert server["headers"] == {"Authorization": "Bearer local-token"}


def test_claude_stdio_bridge_config_uses_mcp_remote_header() -> None:
    generator = _load_generator()
    config = generator.generate_config("claude", token="local-token", stdio_bridge=True)

    server = config["mcpServers"]["deepseek-infra"]
    assert server["command"] == "npx"
    assert server["args"][:3] == ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
    assert "Authorization: Bearer local-token" in server["args"]


def test_cursor_stdio_bridge_is_rejected() -> None:
    generator = _load_generator()

    with pytest.raises(ValueError):
        generator.generate_config("cursor", stdio_bridge=True)
