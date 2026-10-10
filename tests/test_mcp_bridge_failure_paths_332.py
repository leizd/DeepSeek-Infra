from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.infra.mcp import bridge


def _settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        mcp=SimpleNamespace(
            client_enabled=enabled,
            client_servers=(("remote", "https://remote.test/mcp"),),
            client_server_timeouts={"remote": 7},
            client_timeout_seconds=5,
            client_circuit_breaker_failures=2,
            client_circuit_breaker_reset_seconds=30,
        )
    )


class Client:
    def __init__(self, name: str = "remote", tools: list[Any] | None = None) -> None:
        self.name = name
        self.base_url = f"https://{name}.test/mcp"
        self.timeout_seconds = 5
        self.last_stats = SimpleNamespace(latency_ms=4, retry_count=1, timeout=False, error_type="")
        self.tools = tools or []
        self.initialized = 0

    def initialize(self) -> dict[str, Any]:
        self.initialized += 1
        return {}

    def list_tools(self) -> list[Any]:
        return self.tools


def test_refresh_disabled_empty_and_ttl_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = bridge.ExternalMCPToolRegistry(ttl_seconds=100)
    registry._profiles["stale"] = bridge.infer_profile("remote", {"name": "old", "inputSchema": {"type": "object"}})
    monkeypatch.setattr(bridge, "settings", _settings(enabled=False))
    registry.refresh(force=True)
    assert registry.list_profiles() == []
    assert registry.server_status()[0]["status"] == "disabled"

    monkeypatch.setattr(bridge, "settings", _settings(enabled=True))
    monkeypatch.setattr(bridge, "configured_clients", lambda: [])
    registry.refresh(force=True)
    assert registry.server_status()[0]["status"] == "unknown"
    registry._last_refresh = bridge.time.monotonic()
    registry.refresh(force=False)


def test_refresh_success_filters_bad_tools_and_handles_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = [
        None,
        {"name": "search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"name": "search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"name": "search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    ]
    client = Client(tools=tools)
    monkeypatch.setattr(bridge, "settings", _settings())
    monkeypatch.setattr(bridge, "configured_clients", lambda: [client])
    registry = bridge.ExternalMCPToolRegistry(ttl_seconds=0)
    registry.refresh(force=True)
    profiles = registry.list_profiles()
    assert len(profiles) == 3
    assert len({profile.bridged_name for profile in profiles}) == 3
    assert registry.resolve(profiles[0].bridged_name) is not None
    assert registry.is_unavailable("remote") is False


def test_refresh_and_resolve_circuit_open(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(tools=[{"name": "echo", "inputSchema": {"type": "object"}}])
    monkeypatch.setattr(bridge, "settings", _settings())
    monkeypatch.setattr(bridge, "configured_clients", lambda: [client])
    registry = bridge.ExternalMCPToolRegistry(ttl_seconds=0)
    health = bridge.ExternalMCPServerHealth(name="remote", url=client.base_url, timeout_seconds=5, circuit_open_until=bridge.time.monotonic() + 100)
    registry._health["remote"] = health
    registry.refresh(force=True)
    assert registry.is_unavailable("remote") is True
    assert client.initialized == 0

    profile = bridge.infer_profile("remote", {"name": "echo", "inputSchema": {"type": "object"}})
    registry._profiles[profile.bridged_name] = profile
    registry._by_client[profile.bridged_name] = (client, "echo")  # type: ignore[assignment]
    registry._health["remote"] = health
    assert registry.resolve(profile.bridged_name) is None


def test_call_health_success_failure_and_error_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "settings", _settings())
    registry = bridge.ExternalMCPToolRegistry()
    client = Client()
    registry.record_call_success("remote", client)  # type: ignore[arg-type]
    status = registry.server_status()[0]
    assert status["available"] is True and status["callCount"] == 1

    client.last_stats = SimpleNamespace(latency_ms=8, retry_count=2, timeout=True, error_type="")
    registry.record_call_failure("remote", client, TimeoutError("timed out"))  # type: ignore[arg-type]
    registry.record_call_failure("remote", client, RuntimeError("timed out"))  # type: ignore[arg-type]
    assert registry.is_unavailable("remote") is True
    assert registry.server_status()[0]["status"] == "circuit_open"

    assert bridge._error_type_from(Client(), RuntimeError("invalid json schema")) == "schema_error"  # type: ignore[arg-type]
    assert bridge._error_type_from(Client(), RuntimeError("HTTP 500")) == "http_error"  # type: ignore[arg-type]
    assert bridge._error_type_from(Client(), RuntimeError("connection unreachable")) == "unreachable"  # type: ignore[arg-type]
    assert bridge._error_type_from(Client(), RuntimeError("boom")) == "upstream_failure"  # type: ignore[arg-type]
