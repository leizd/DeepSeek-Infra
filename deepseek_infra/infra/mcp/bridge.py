"""External MCP tool bridge: profile, cache, and namespace external MCP servers.

Every tool from an external MCP server gets an :class:`ExternalMCPToolProfile`
before it enters the local agent's tool surface. The profile carries a conservative
risk assessment inferred from the server's annotations and schema shape — external
servers are never trusted to self-report accurately.

The :class:`ExternalMCPToolRegistry` is a caching layer that wraps the low-level
``MCPClient`` pool. It refreshes on startup, on demand, and on TTL expiry, so
slow or unavailable servers never block local tool execution.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from deepseek_infra.core.config import settings
from deepseek_infra.infra.mcp.client import MCPClient, configured_clients
from deepseek_infra.infra.tool_runtime.tool_policy import TOOL_METADATA, ToolMetadata, tool_metadata

logger = logging.getLogger("deepseek_infra.mcp.bridge")

BRIDGE_PREFIX = "mcp__"
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


# --- Name mangling ---------------------------------------------------------------


def _safe_name(s: str) -> str:
    """Normalise a server or tool name to a valid identifier segment."""
    return _SANITIZE_RE.sub("_", str(s).strip().lower())


def bridged_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` — OpenAI function-call compatible (no colons)."""
    return f"{BRIDGE_PREFIX}{_safe_name(server)}__{_safe_name(tool)}"


def parse_bridged_name(name: str) -> tuple[str, str] | None:
    """Parse ``mcp__<server>__<tool>`` → ``(server, tool)`` or *None*."""
    if not str(name or "").startswith(BRIDGE_PREFIX):
        return None
    parts = str(name)[len(BRIDGE_PREFIX) :].split("__", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


# --- External tool profile -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalMCPToolProfile:
    """Security and routing profile for one bridged external MCP tool."""

    server: str
    tool: str  # original name on the external server
    bridged_name: str  # mcp__<server>__<tool>
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk: str = "medium"  # low | medium | high | critical
    network: bool = False
    filesystem: bool = False
    env: bool = False
    requires_approval: bool = False
    external_output: bool = True  # external results are untrusted by default

    def to_metadata(self) -> ToolMetadata:
        """Project onto a :class:`ToolMetadata` so ``ToolPolicy.evaluate()`` works."""
        return ToolMetadata(
            name=self.bridged_name,
            risk=self.risk,
            network=self.network,
            filesystem=self.filesystem,
            requires_confirm=self.requires_approval,
            timeout_seconds=settings.mcp.client_timeout_seconds,
            max_output_chars=24_000,
            external_output=self.external_output,
            sensitive_sink=self.env,
            capability="external",
        )


# --- Conservative risk inference -------------------------------------------------

# Schema property keys that hint at I/O behaviour.
_URL_KEYS = frozenset({"url", "uri", "endpoint", "base_url", "host", "domain"})
_PATH_KEYS = frozenset({"path", "file", "filename", "filepath", "directory", "folder", "dir"})
_SENSITIVE_KEYS = frozenset({"env", "token", "secret", "key", "password", "apikey", "api_key", "credential", "auth"})


def _collect_keys(schema: dict[str, Any]) -> set[str]:
    """Collect property names from a JSON Schema object."""
    keys: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        keys.update(str(k).lower() for k in properties)
    # Walk one level of nested objects (common in MCP tool schemas).
    if isinstance(properties, dict):
        for prop in properties.values():
            if isinstance(prop, dict) and isinstance(prop.get("properties"), dict):
                keys.update(str(k).lower() for k in prop["properties"])
    return keys


def infer_profile(
    server_name: str,
    tool_def: dict[str, Any],
    *,
    timeout: int = 30,
) -> ExternalMCPToolProfile:
    """Build a conservative security profile from an external MCP tool definition.

    External annotations (``readOnlyHint``, ``destructiveHint``, ``openWorldHint``)
    are treated as advisory only — schema shape and description heuristics serve as
    a backstop so a server that omits annotations doesn't get a free pass.
    """
    annotations = tool_def.get("annotations") or {}
    schema = tool_def.get("inputSchema") or {}
    description = str(tool_def.get("description") or "").lower()
    original_name = str(tool_def.get("name") or "")

    # --- Hints from annotations (advisory) ---------------------------------------
    destructive = bool(annotations.get("destructiveHint"))
    open_world = bool(annotations.get("openWorldHint"))

    # --- Heuristics from schema shape --------------------------------------------
    schema_keys = _collect_keys(schema)
    has_url_key = bool(schema_keys & _URL_KEYS)
    has_path_key = bool(schema_keys & _PATH_KEYS)
    has_sensitive_key = bool(schema_keys & _SENSITIVE_KEYS)
    has_destructive_desc = any(
        word in description
        for word in ("delete", "remove", "destroy", "drop", "truncate", "overwrite")
    )

    # --- Risk ladder (external tools start at medium) ----------------------------
    risk = "medium"
    if destructive:
        risk = "high"
    if has_sensitive_key or has_destructive_desc:
        risk = "high"
    if has_url_key and has_sensitive_key:
        risk = "critical"
    if "critical" in description or "sensitive" in description:
        risk = max(risk, "high", key=lambda r: {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(r, 0))

    network = open_world or has_url_key
    filesystem = has_path_key

    requires_approval = destructive or has_sensitive_key or risk in ("high", "critical")
    env_sensitive = has_sensitive_key

    return ExternalMCPToolProfile(
        server=server_name,
        tool=original_name,
        bridged_name=bridged_name(server_name, original_name),
        input_schema=schema,
        risk=risk,
        network=network,
        filesystem=filesystem,
        env=env_sensitive,
        requires_approval=requires_approval,
        external_output=True,
    )


# --- Registry --------------------------------------------------------------------


class ExternalMCPToolRegistry:
    """Cached registry of bridged external MCP tools.

    Refreshes on startup, on demand, and on TTL expiry. A failed server is marked
    *unavailable* but never blocks the registry — local tools keep working exactly
    as before.
    """

    def __init__(self, *, ttl_seconds: float = 60.0) -> None:
        self._profiles: dict[str, ExternalMCPToolProfile] = {}  # bridged_name → profile
        self._by_client: dict[str, tuple[MCPClient, str]] = {}  # bridged_name → (client, original_name)
        self._clients: dict[str, MCPClient] = {}  # server_name → client
        self._unavailable: set[str] = set()  # server names that failed last refresh
        self._last_refresh: float = 0.0
        self._ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()

    # -- public API ----------------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Connect to every configured external MCP server and rebuild the profile catalog.

        Called once at startup, then on demand or when the TTL expires. A server
        that fails to connect or returns badly-shaped data is marked unavailable;
        its previously-cached tools are evicted.
        """
        if not settings.mcp.client_enabled:
            with self._lock:
                self._profiles.clear()
                self._by_client.clear()
                self._clients.clear()
                self._unavailable.clear()
            return

        now = time.monotonic()
        if not force and (now - self._last_refresh) < self._ttl_seconds:
            return

        clients = configured_clients()
        if not clients:
            with self._lock:
                self._profiles.clear()
                self._by_client.clear()
                self._clients.clear()
                self._unavailable.clear()
            return

        new_profiles: dict[str, ExternalMCPToolProfile] = {}
        new_by_client: dict[str, tuple[MCPClient, str]] = {}
        new_clients: dict[str, MCPClient] = {}
        new_unavailable: set[str] = set()

        for client in clients:
            server_name = client.name
            try:
                client.initialize()
                tools = client.list_tools()
            except Exception:
                logger.warning("external_mcp_server_unavailable", extra={"server": server_name}, exc_info=True)
                new_unavailable.add(server_name)
                continue

            new_clients[server_name] = client
            for tool_def in tools:
                if not isinstance(tool_def, dict):
                    continue
                profile = infer_profile(server_name, tool_def, timeout=settings.mcp.client_timeout_seconds)
                new_profiles[profile.bridged_name] = profile
                new_by_client[profile.bridged_name] = (client, profile.tool)

        with self._lock:
            self._profiles = new_profiles
            self._by_client = new_by_client
            self._clients = new_clients
            self._unavailable = new_unavailable
            self._last_refresh = now

        logger.info(
            "external_mcp_registry_refreshed",
            extra={
                "tool_count": len(new_profiles),
                "server_count": len(new_clients),
                "unavailable": list(new_unavailable),
            },
        )

    def list_profiles(self) -> list[ExternalMCPToolProfile]:
        """All currently-cached profiles (thread-safe copy)."""
        with self._lock:
            return list(self._profiles.values())

    def get_profile(self, bridged_name: str) -> ExternalMCPToolProfile | None:
        """Look up one profile by its bridged name."""
        with self._lock:
            return self._profiles.get(str(bridged_name or ""))

    def resolve(self, bridged_name: str) -> tuple[MCPClient, str] | None:
        """Resolve a bridged name → (client, original_tool_name) for execution."""
        with self._lock:
            return self._by_client.get(str(bridged_name or ""))

    def is_unavailable(self, server: str) -> bool:
        with self._lock:
            return str(server or "") in self._unavailable

    def metadata_provider(self, tool_name: str) -> ToolMetadata | None:
        """A ``Callable[[str], ToolMetadata | None]`` ready for ``ToolPolicy``.

        Looks up external profiles first, then falls through to the local
        ``TOOL_METADATA`` table so both local and bridged tools are covered.
        """
        name = str(tool_name or "").strip()
        if name.startswith(BRIDGE_PREFIX):
            profile = self.get_profile(name)
            return profile.to_metadata() if profile is not None else None
        return tool_metadata(name)


# Singleton — the rest of the codebase imports this one instance.
external_mcp_registry = ExternalMCPToolRegistry()
