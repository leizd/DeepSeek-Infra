"""Gap tests for core/config.py to cover remaining uncovered lines."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.core.config import (
    JsonLogFormatter,
    configure_logging,
    _env_path,
    _mcp_client_servers_from_env,
    _mcp_client_server_timeouts_from_env,
)


UNIQUE_ENV_VAR = "_TEST_CONFIG_GAPS_ENV_PATH"


def test_env_path_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    tmp = tempfile.mkdtemp()
    try:
        monkeypatch.setenv(UNIQUE_ENV_VAR, os.path.join(tmp, "sub"))
        result = _env_path((UNIQUE_ENV_VAR,), Path("/default"))
        assert result == Path(os.path.join(tmp, "sub")).resolve()
    finally:
        monkeypatch.delenv(UNIQUE_ENV_VAR, raising=False)


def test_mcp_client_servers_from_env_skips_non_dict_items() -> None:
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": '["not a dict"]'}, clear=True):
        assert _mcp_client_servers_from_env() == ()


def test_mcp_client_server_timeouts_skips_non_dict_items() -> None:
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": '["not a dict"]'}, clear=True):
        assert dict(_mcp_client_server_timeouts_from_env()) == {}


def test_mcp_client_server_timeouts_skips_invalid_name_or_url() -> None:
    with patch.dict(os.environ, {"MCP_CLIENT_SERVERS": '[{"name":"x"}]'}, clear=True):
        assert dict(_mcp_client_server_timeouts_from_env()) == {}


def test_json_log_formatter_includes_extra_fields() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    record.extra = "value"
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["message"] == "hello"
    assert parsed["extra"] == "value"
    assert parsed["level"] == "INFO"


def test_json_log_formatter_includes_exception() -> None:
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom")
    except Exception:
        record = logging.LogRecord("test", logging.ERROR, "", 0, "error", (), sys.exc_info())
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["message"] == "error"
    assert "ValueError" in parsed["exc_info"]
    assert "boom" in parsed["exc_info"]


def test_configure_logging_reuses_existing_handlers() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        configure_logging(logging.WARNING)
        assert root.level == logging.WARNING
        assert isinstance(handler.formatter, JsonLogFormatter)
    finally:
        root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
