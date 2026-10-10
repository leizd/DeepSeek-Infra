"""Liveness and readiness probes for the local AI runtime."""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.config import APP_VERSION, settings
from deepseek_infra.infra.observability.observability import trace_status


def healthz() -> dict[str, Any]:
    """Liveness: the process is up and serving."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "runtime": "local",
        "provider": "deepseek",
        "auth_enabled": settings.auth.enabled,
    }


def readyz() -> dict[str, Any]:
    """Readiness: local stores are reachable and the upstream provider is configured."""
    checks: dict[str, str] = {}
    trace = trace_status()
    if not trace.get("enabled"):
        checks["tracing"] = "disabled"
    elif trace.get("lastError"):
        checks["tracing"] = "degraded"
    else:
        checks["tracing"] = "ok"
    checks["model_provider"] = "configured" if str(settings.deepseek_api_key or "").strip() else "unconfigured"
    ready = checks["tracing"] != "degraded"
    return {"status": "ready" if ready else "degraded", "version": APP_VERSION, "checks": checks}
