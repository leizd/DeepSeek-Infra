"""Feature flags for Rust-backed components."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_RUST_GATEWAY_URL = "http://127.0.0.1:8787"
DEFAULT_RUST_POLICY_FAILURE_MODE = "fallback"
RUST_POLICY_FAILURE_MODES = frozenset({"fallback", "deny", "error"})


@dataclass(frozen=True)
class RustComponentFlags:
    gateway: bool
    mcp: bool
    policy: bool
    rag: bool


def load_rust_flags() -> RustComponentFlags:
    return RustComponentFlags(
        gateway=_env_bool("DEEPSEEK_RUST_GATEWAY", False),
        mcp=_env_bool("DEEPSEEK_RUST_MCP", False),
        policy=_env_bool("DEEPSEEK_RUST_POLICY", False),
        rag=_env_bool("DEEPSEEK_RUST_RAG", False),
    )


def rust_gateway_url() -> str:
    return (
        os.environ.get("DEEPSEEK_RUST_GATEWAY_URL", DEFAULT_RUST_GATEWAY_URL).strip()
        or DEFAULT_RUST_GATEWAY_URL
    )


def rust_policy_failure_mode() -> str:
    configured = os.environ.get("DEEPSEEK_RUST_POLICY_FAILURE_MODE")
    if configured is not None:
        normalized = configured.strip().lower()
        return normalized if normalized in RUST_POLICY_FAILURE_MODES else DEFAULT_RUST_POLICY_FAILURE_MODE

    # v3.2.3 keeps the old boolean switch as a compatibility bridge. Disabling
    # fallback previously meant fail closed, which maps to the new deny mode.
    legacy_fallback = os.environ.get("DEEPSEEK_RUST_POLICY_FALLBACK")
    if legacy_fallback is not None and not _env_bool("DEEPSEEK_RUST_POLICY_FALLBACK", True):
        return "deny"
    return DEFAULT_RUST_POLICY_FAILURE_MODE


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")
