"""Offline cross-check of the native `/api/mcp` status algorithm against the oracle.

This is a migration-time verification model, not production code. It mirrors the
Rust `build_body` in `rust/crates/deepseek-gateway/src/mcp_status_routes.rs`
(environment precedence, Python int/float parsing, clamps, ordered server list and
per-server timeout overrides, native tool-catalog counts) and compares it with a
fresh run of the original `deepseek_infra.infra.mcp.server.mcp_status()` under the
identical environment. The Rust integration test does the same comparison in the
cloud; this script makes the design checkable without a native compiler.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ASSET = ROOT / "rust/crates/deepseek-policy/assets/tool_catalog_v1.json"
PYTHON = r"""
import json
from deepseek_infra.infra.mcp.server import mcp_status
print(json.dumps({"ok": True, "mcp": mcp_status()}))
"""
MCP_KEYS = [
    "MCP_ENABLED",
    "MCP_CAPABILITY",
    "MCP_EXPOSE_RESOURCES",
    "MCP_EXPOSE_PROMPTS",
    "MCP_CLIENT_ENABLED",
    "MCP_CLIENT_SERVERS",
    "MCP_CLIENT_TIMEOUT_SECONDS",
    "MCP_CLIENT_MAX_RETRIES",
    "MCP_CLIENT_RETRY_BACKOFF_SECONDS",
    "MCP_CLIENT_CIRCUIT_BREAKER_FAILURES",
    "MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS",
]
PROFILES = {
    "full": None,
    "researcher": ["web_search", "compare_search_results", "fetch_url"],
    "browser_reader": [
        "browser_open_url",
        "browser_read_page",
        "browser_screenshot",
        "browser_extract_links",
    ],
    "coder": ["search_files", "read_file_chunk", "python_eval"],
    "reasoner": [],
    "critic": [],
}


def catalog_names() -> list[str]:
    asset = json.loads(ASSET.read_text(encoding="utf-8"))
    return [
        item["function"]["name"]
        for item in asset
        if isinstance(item, dict)
        and isinstance(item.get("function"), dict)
        and item["function"].get("name")
    ]


def python_int(text: str) -> int | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return int(stripped.replace("_", ""))
    except ValueError:
        return None


def python_float(text: str) -> float | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return float(stripped.replace("_", ""))
    except ValueError:
        return None


def env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "")
    if not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int_clamped(env: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "")
    stripped = raw.strip()
    if not stripped:
        return default
    value = python_int(stripped)
    return min(high, max(low, default if value is None else value))


def env_float_clamped(env: dict[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name, "")
    stripped = raw.strip()
    if not stripped:
        return default
    value = python_float(stripped)
    return min(high, max(low, default if value is None else value))


def python_str_or(value: object) -> str:
    if value is None or value in (False, 0, "", [], {}):
        return ""
    if value is True:
        return "True"
    return str(value)


def build_model(env: dict[str, str]) -> dict[str, Any]:
    names = catalog_names()
    enabled = env_bool(env, "MCP_ENABLED", True)
    raw_capability = env.get("MCP_CAPABILITY", "").strip()
    capability = raw_capability if raw_capability in PROFILES else "full"
    profile = PROFILES[capability]
    allowed = set(names) if profile is None else set(profile)
    tool_count = sum(1 for name in names if name in allowed) if enabled else 0
    timeout_seconds = env_int_clamped(env, "MCP_CLIENT_TIMEOUT_SECONDS", 30, 1, 600)
    servers: list[tuple[str, str]] = []
    timeouts: dict[str, int] = {}
    raw = env.get("MCP_CLIENT_SERVERS", "").strip()
    items = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            items = parsed
    for item in items:
        if not isinstance(item, dict):
            continue
        name = python_str_or(item.get("name")).strip()
        url = python_str_or(item.get("url")).strip()
        if not name or not (url.startswith("http://") or url.startswith("https://")):
            continue
        servers.append((name, url))
        raw_timeout = item.get("timeoutSeconds", item.get("timeout_seconds"))
        if raw_timeout is None:
            continue
        value = python_int(python_str_or(raw_timeout))
        if value is None:
            continue
        timeouts[name] = min(600, max(1, value))
    return {
        "ok": True,
        "mcp": {
            "enabled": enabled,
            "protocolVersion": "2025-06-18",
            "endpoint": "/mcp",
            "capability": capability,
            "toolCount": tool_count,
            "exposeResources": env_bool(env, "MCP_EXPOSE_RESOURCES", True),
            "exposePrompts": env_bool(env, "MCP_EXPOSE_PROMPTS", True),
            "client": {
                "enabled": env_bool(env, "MCP_CLIENT_ENABLED", False),
                "servers": [
                    {
                        "name": name,
                        "url": url,
                        "timeoutSeconds": timeouts.get(name, timeout_seconds),
                    }
                    for name, url in servers
                ],
                "timeoutSeconds": timeout_seconds,
                "maxRetries": env_int_clamped(env, "MCP_CLIENT_MAX_RETRIES", 1, 0, 5),
                "retryBackoffSeconds": env_float_clamped(
                    env, "MCP_CLIENT_RETRY_BACKOFF_SECONDS", 0.25, 0.0, 10.0
                ),
                "circuitBreakerFailures": env_int_clamped(
                    env, "MCP_CLIENT_CIRCUIT_BREAKER_FAILURES", 3, 1, 20
                ),
                "circuitBreakerResetSeconds": env_int_clamped(
                    env, "MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS", 60, 1, 3600
                ),
            },
        },
    }


def base_environment() -> dict[str, str]:
    keep = {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "PYTHONUTF8",
    }
    return {key: value for key, value in os.environ.items() if key in keep}


def run_oracle(overrides: dict[str, str]) -> dict[str, Any]:
    root = tempfile.mkdtemp()
    env = base_environment()
    env["DEEPSEEK_INFRA_ROOT"] = root
    env["AUTH_TOKEN"] = "mcp-status-route-token"
    for key in MCP_KEYS:
        env[key] = ""
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", PYTHON],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    body = json.loads(result.stdout)
    leftovers = sorted(os.listdir(root))
    return {"body": body, "workspace": leftovers}


def cases() -> dict[str, dict[str, str]]:
    servers = json.dumps(
        [
            {"name": "docs", "url": "http://127.0.0.1:3000/mcp", "timeoutSeconds": 10},
            {"name": "other", "url": "https://example.invalid/mcp"},
            {"name": "", "url": "http://ignored.invalid/mcp"},
            {"name": "bad-scheme", "url": "ftp://127.0.0.1/mcp"},
            {"name": "float-timeout", "url": "http://example.invalid/mcp", "timeoutSeconds": 1.5},
            {"nope": 1},
        ]
    )
    return {
        "defaults": {},
        "disabled": {"MCP_ENABLED": "0"},
        "coder": {"MCP_ENABLED": "1", "MCP_CAPABILITY": " coder "},
        "unknown-capability": {"MCP_CAPABILITY": " not-a-profile "},
        "blank-and-control-whitespace": {
            "MCP_ENABLED": "   ",
            "MCP_EXPOSE_RESOURCES": "\x1c",
            "MCP_EXPOSE_PROMPTS": "off",
        },
        "expose-flags-off": {"MCP_EXPOSE_RESOURCES": "no", "MCP_EXPOSE_PROMPTS": "0"},
        "client-overrides-and-clamps": {
            "MCP_CLIENT_ENABLED": "yes",
            "MCP_CLIENT_SERVERS": servers,
            "MCP_CLIENT_TIMEOUT_SECONDS": "-5",
            "MCP_CLIENT_MAX_RETRIES": "99",
            "MCP_CLIENT_RETRY_BACKOFF_SECONDS": "-1.5",
            "MCP_CLIENT_CIRCUIT_BREAKER_FAILURES": "0",
            "MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS": "99999",
        },
        "invalid-servers-json": {"MCP_CLIENT_SERVERS": "[{not-json"},
        "enabled-without-servers": {"MCP_CLIENT_ENABLED": "yes"},
        "underscored-and-signed-ints": {
            "MCP_CLIENT_MAX_RETRIES": "1_0",
            "MCP_CLIENT_TIMEOUT_SECONDS": "+40",
        },
    }


def main() -> int:
    report: dict[str, Any] = {"cases": {}, "status": "PASS"}
    for name, overrides in cases().items():
        oracle = run_oracle(overrides)
        model = build_model({key: "" for key in MCP_KEYS} | overrides)
        matched = model == oracle["body"] and not oracle["workspace"]
        report["cases"][name] = {
            "matched": matched,
            "oracle": oracle["body"],
            "workspaceLeftovers": oracle["workspace"],
        }
        if not matched:
            report["status"] = "FAIL"
            report["cases"][name]["model"] = model
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
