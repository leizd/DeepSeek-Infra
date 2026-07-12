"""Tests for the launcher's encrypted credential store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import deepseek_infra.core.config as config
import deepseek_infra.launcher.credentials as credentials


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_settings = type(config.settings)(root=tmp_path)
    monkeypatch.setattr(credentials, "settings", fake_settings)
    return tmp_path


def test_round_trip_preserves_all_fields(isolated_root: Path) -> None:
    original = credentials.LauncherCredentials(
        deepseek_api_key="sk-deepseek-secret",
        tavily_api_key="tvly-secret",
        host=credentials.LAN_HOST,
        port=8123,
        allow_lan=True,
        ocr_enabled=True,
        auth_disabled=False,
    )
    credentials.save(original)
    loaded = credentials.load()
    assert loaded == original


def test_save_writes_no_plaintext_key(isolated_root: Path) -> None:
    creds = credentials.LauncherCredentials(deepseek_api_key="sk-this-is-secret-key-xyz")
    credentials.save(creds)
    raw = (isolated_root / credentials.CONFIG_FILE_NAME).read_text(encoding="utf-8")
    assert "sk-this-is-secret-key-xyz" not in raw
    envelope = json.loads(raw)
    assert envelope["version"] == credentials.ENVELOPE_VERSION
    assert set(envelope["data"]) == {"nonce", "ciphertext", "mac"}


def test_load_returns_defaults_when_file_missing(isolated_root: Path) -> None:
    assert credentials.load() == credentials.LauncherCredentials()


def test_load_returns_defaults_when_mac_tampered(isolated_root: Path) -> None:
    credentials.save(credentials.LauncherCredentials(deepseek_api_key="sk-tamper-test"))
    path = isolated_root / credentials.CONFIG_FILE_NAME
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["data"]["mac"] = "AAAA" + envelope["data"]["mac"][4:]
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert credentials.load() == credentials.LauncherCredentials()


def test_load_returns_defaults_when_machine_key_changes(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials.save(credentials.LauncherCredentials(deepseek_api_key="sk-mach-bound"))
    monkeypatch.setattr(credentials, "_machine_fingerprint", lambda: b"different-machine")
    assert credentials.load() == credentials.LauncherCredentials()


def test_clear_removes_file(isolated_root: Path) -> None:
    credentials.save(credentials.LauncherCredentials(deepseek_api_key="sk-temp"))
    assert (isolated_root / credentials.CONFIG_FILE_NAME).exists()
    credentials.clear()
    assert not (isolated_root / credentials.CONFIG_FILE_NAME).exists()
    credentials.clear()  # idempotent


def test_clear_noop_when_missing(isolated_root: Path) -> None:
    credentials.clear()  # must not raise FileNotFoundError


def test_allow_lan_overrides_host_to_zero(isolated_root: Path) -> None:
    creds = credentials.LauncherCredentials(
        deepseek_api_key="", host="anything", allow_lan=True
    )
    credentials.save(creds)
    loaded = credentials.load()
    assert loaded.allow_lan is True
    assert loaded.host == credentials.LAN_HOST


def test_invalid_port_falls_back_to_default(isolated_root: Path) -> None:
    path = isolated_root / credentials.CONFIG_FILE_NAME
    creds = credentials.LauncherCredentials(deepseek_api_key="sk", port=8123)
    credentials.save(creds)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    # Re-encrypt with broken port using the same helpers.
    raw_body = json.dumps(
        {"deepseek_api_key": "sk", "port": 999999, "host": "127.0.0.1"}
    ).encode("utf-8")
    envelope["data"] = credentials._encrypt(raw_body)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    loaded = credentials.load()
    assert loaded.port == credentials.DEFAULT_PORT


def test_restrict_permissions_contains_posix_chmod_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials.os, "name", "posix")
    monkeypatch.setattr(Path, "chmod", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")))
    credentials._restrict_permissions(tmp_path / "config")
