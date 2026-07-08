"""HTTP proxy client for the Rust Policy sidecar."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import rust_gateway_url

DEFAULT_POLICY_TIMEOUT_MS = 3000


@dataclass(frozen=True)
class PolicyProxyResult:
    ok: bool
    status: int
    allowed: bool
    reason: str
    body: Any


def _rust_policy_enabled() -> bool:
    from deepseek_infra.infra.rust_core.config import load_rust_flags

    return load_rust_flags().policy


def _fallback_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_POLICY_FALLBACK", "1")
    return value.strip().lower() in ("1", "true", "yes", "on")


def _timeout_ms() -> int:
    try:
        return int(
            os.environ.get("DEEPSEEK_RUST_POLICY_TIMEOUT_MS", DEFAULT_POLICY_TIMEOUT_MS)
        )
    except ValueError:
        return DEFAULT_POLICY_TIMEOUT_MS


def _request(
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout_ms: int | None = None,
) -> PolicyProxyResult:
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    req_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers and "Authorization" in headers:
        req_headers["Authorization"] = headers["Authorization"]
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return PolicyProxyResult(ok=True, status=response.status, allowed=True, reason="", body={})
            body = json.loads(raw.decode("utf-8"))
            allowed = body.get("decision") == "Allow" or body.get("allowed") is True
            reason = body.get("reason", "")
            return PolicyProxyResult(ok=True, status=response.status, allowed=allowed, reason=reason, body=body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return PolicyProxyResult(ok=False, status=exc.code, allowed=False, reason=str(body), body=body)
    except Exception as exc:
        return PolicyProxyResult(ok=False, status=0, allowed=False, reason=str(exc), body=str(exc))


def check_url(url: str, headers: dict[str, str] | None = None) -> PolicyProxyResult:
    if not _rust_policy_enabled():
        return PolicyProxyResult(ok=False, status=0, allowed=False, reason="Rust Policy is disabled", body={})
    return _request("/policy/url", {"url": url}, headers=headers)


def check_path(root: str, requested: str, headers: dict[str, str] | None = None) -> PolicyProxyResult:
    if not _rust_policy_enabled():
        return PolicyProxyResult(ok=False, status=0, allowed=False, reason="Rust Policy is disabled", body={})
    return _request("/policy/path", {"root": root, "requested": requested}, headers=headers)


def check_capability(
    requested: str,
    granted: list[str],
    max_risk: str,
    headers: dict[str, str] | None = None,
) -> PolicyProxyResult:
    if not _rust_policy_enabled():
        return PolicyProxyResult(ok=False, status=0, allowed=False, reason="Rust Policy is disabled", body={})
    return _request(
        "/policy/capability",
        {"requested": requested, "granted": granted, "max_risk": max_risk},
        headers=headers,
    )


def rust_policy_enabled() -> bool:
    return _rust_policy_enabled()


def fallback_to_python_enabled() -> bool:
    return _fallback_enabled()
