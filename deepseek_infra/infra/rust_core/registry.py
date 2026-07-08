"""Component registry for Rust-backed runtime pieces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from deepseek_infra.infra.rust_core.config import (
    RustComponentFlags,
    load_rust_flags,
    rust_gateway_url,
)
from deepseek_infra.infra.rust_core.health import check_rust_gateway_health


@dataclass(frozen=True)
class RustRegistry:
    flags: RustComponentFlags = field(default_factory=load_rust_flags)
    gateway_url: str = field(default_factory=rust_gateway_url)

    def status(self) -> dict[str, Any]:
        gateway_enabled = self.flags.gateway
        gateway_healthy = (
            check_rust_gateway_health(self.gateway_url) if gateway_enabled else False
        )
        return {
            "enabled": {
                "gateway": gateway_enabled,
                "mcp": self.flags.mcp,
                "policy": self.flags.policy,
                "rag": self.flags.rag,
            },
            "components": {
                "gateway": {
                    "enabled": gateway_enabled,
                    "url": self.gateway_url if gateway_enabled else "",
                    "healthy": gateway_healthy,
                },
            },
        }


def rust_status() -> dict[str, Any]:
    return RustRegistry().status()
