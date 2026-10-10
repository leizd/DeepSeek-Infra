"""Edge-case tests for core/config.py to raise coverage."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.core.config import (
    Settings,
    _env_choice,
    _env_float_clamped,
    configure_logging,
    load_or_create_auth_token,
    _mcp_client_servers_from_env,
    _mcp_client_server_timeouts_from_env,
)


def test_runtime_root_frozen_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.core.config import _runtime_root
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(Path(__file__).resolve().parents[1] / "app.py"))
    root = _runtime_root()
    assert (root / "pyproject.toml").exists()


def test_bundled_static_dir_meipass(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.core.config import _bundled_static_dir
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/meipass", raising=False)
    assert _bundled_static_dir() == Path("/tmp/meipass") / "static"


def test_load_or_create_auth_token_write_oserror() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        token_path = root / ".auth-token"
        token_path.mkdir(parents=True, exist_ok=True)
        token = load_or_create_auth_token(root)
        assert len(token) > 0


def test_env_float_clamped_invalid() -> None:
    assert _env_float_clamped("VAR_NOT_SET", 1.0, 0.0, 2.0) == 1.0
    with patch.dict(os.environ, {"VAR_NOT_SET": "abc"}):
        assert _env_float_clamped("VAR_NOT_SET", 1.0, 0.0, 2.0) == 1.0


def test_env_choice_invalid() -> None:
    assert _env_choice("VAR_NOT_SET", {"a", "b"}, "a") == "a"
    with patch.dict(os.environ, {"VAR_NOT_SET": "c"}):
        assert _env_choice("VAR_NOT_SET", {"a", "b"}, "a") == "a"


def test_mcp_client_servers_from_env_malformed() -> None:
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": "not json"}, clear=True):
        assert _mcp_client_servers_from_env() == ()
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": '[{"name":"x"}]'}, clear=True):
        assert _mcp_client_servers_from_env() == ()
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": '[{"name":"x","url":"ftp://x"}]'}, clear=True):
        assert _mcp_client_servers_from_env() == ()


def test_mcp_client_server_timeouts_from_env_malformed() -> None:
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": "not json"}, clear=True):
        assert dict(_mcp_client_server_timeouts_from_env()) == {}
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": '[{"name":"x","url":"http://x","timeoutSeconds":"abc"}]'}, clear=True):
        assert dict(_mcp_client_server_timeouts_from_env()) == {}


def test_configure_logging_no_handlers() -> None:
    import logging
    root = logging.getLogger()
    handlers = list(root.handlers)
    for handler in handlers:
        root.removeHandler(handler)
    configure_logging()
    assert any(isinstance(h.formatter, type("JsonLogFormatter", (), {})) for h in root.handlers) or root.handlers
    for handler in root.handlers:
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)


def test_settings_from_env_invalid_mcp_servers() -> None:
    with patch.dict(
        os.environ,
        {
            "MCP_CLIENT_SERVERS": "[not valid",
            "MCP_CLIENT_SERVERS_TIMEOUT": "not used",
        },
        clear=True,
    ):
        loaded = Settings.from_env()
    assert loaded.mcp.client_servers == ()
    assert dict(loaded.mcp.client_server_timeouts) == {}
