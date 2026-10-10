"""Local credential storage for the GUI launcher.

API keys typed into the launcher are encrypted with a key derived from this
machine's fingerprint (MAC + platform + executable path + home directory) and
stored on disk with an HMAC tag. A tampered file or a file copied to a
different machine will fail to decrypt.

This is not protection against local malware running as the same user -- anyone
who can run code on this account can re-derive the same key. The goal is just
to avoid leaving plaintext API keys on disk and to prevent a casually copied
config file from working elsewhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import settings

CONFIG_FILE_NAME = ".launcher-config.json"
ENVELOPE_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


@dataclass(frozen=True, slots=True)
class LauncherCredentials:
    deepseek_api_key: str = ""
    tavily_api_key: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_lan: bool = False
    ocr_enabled: bool = False
    auth_disabled: bool = False

    def with_updates(self, **changes: Any) -> "LauncherCredentials":
        return replace(self, **changes)


def config_path() -> Path:
    return settings.root / CONFIG_FILE_NAME


def load() -> LauncherCredentials:
    path = config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return LauncherCredentials()
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return LauncherCredentials()
    if not isinstance(envelope, dict):
        return LauncherCredentials()

    body = _decrypt(envelope.get("data"))
    if body is None:
        return LauncherCredentials()
    return _from_dict(body)


def save(credentials: LauncherCredentials) -> None:
    path = config_path()
    payload = {
        "deepseek_api_key": credentials.deepseek_api_key,
        "tavily_api_key": credentials.tavily_api_key,
        "host": credentials.host,
        "port": int(credentials.port),
        "allow_lan": bool(credentials.allow_lan),
        "ocr_enabled": bool(credentials.ocr_enabled),
        "auth_disabled": bool(credentials.auth_disabled),
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    envelope = {"version": ENVELOPE_VERSION, "data": _encrypt(encoded)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)
    _restrict_permissions(path)


def clear() -> None:
    try:
        config_path().unlink()
    except FileNotFoundError:
        pass


def _machine_fingerprint() -> bytes:
    parts = [
        f"node={uuid.getnode():x}",
        f"platform={sys.platform}",
        f"home={Path.home().as_posix()}",
        f"exec={sys.executable}",
        f"project={settings.root.as_posix()}",
    ]
    return "|".join(parts).encode("utf-8")


def _machine_key() -> bytes:
    return hashlib.sha256(_machine_fingerprint()).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        blocks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _encrypt(plaintext: bytes) -> dict[str, str]:
    key = _machine_key()
    nonce = secrets.token_bytes(16)
    ciphertext = _xor(plaintext, _keystream(key, nonce, len(plaintext)))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "mac": base64.b64encode(tag).decode("ascii"),
    }


def _decrypt(envelope: Any) -> dict[str, Any] | None:
    if not isinstance(envelope, dict):
        return None
    try:
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
        expected = base64.b64decode(envelope["mac"])
    except (KeyError, TypeError, ValueError):
        return None
    key = _machine_key()
    actual = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        return None
    plaintext = _xor(ciphertext, _keystream(key, nonce, len(ciphertext)))
    try:
        body = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _from_dict(body: dict[str, Any]) -> LauncherCredentials:
    raw_host = str(body.get("host") or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(body.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    if port < 1 or port > 65535:
        port = DEFAULT_PORT
    allow_lan = bool(body.get("allow_lan", raw_host == LAN_HOST))
    return LauncherCredentials(
        deepseek_api_key=str(body.get("deepseek_api_key") or "").strip(),
        tavily_api_key=str(body.get("tavily_api_key") or "").strip(),
        host=LAN_HOST if allow_lan else raw_host,
        port=port,
        allow_lan=allow_lan,
        ocr_enabled=bool(body.get("ocr_enabled", False)),
        auth_disabled=bool(body.get("auth_disabled", False)),
    )


def _restrict_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass
