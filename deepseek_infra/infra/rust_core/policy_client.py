"""HTTP proxy client for the Rust Policy sidecar."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import rust_gateway_url, rust_policy_failure_mode

DEFAULT_POLICY_TIMEOUT_MS = 3000
POLICY_BACKEND_UNAVAILABLE = "policy_backend_unavailable"
INVALID_POLICY_REQUEST = "invalid_policy_request"
_AUTHORIZATION_PATTERN = re.compile(r"(?i)(authorization[\"'\s:=]+(?:bearer\s+)?)[^\s,}\]]+")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+")


@dataclass(frozen=True)
class PolicyProxyResult:
    ok: bool
    status: int
    allowed: bool
    reason: str
    body: Any
    code: str = ""
    decision_id: str = ""
    trace_id: str = ""
    capability: str = ""
    risk_level: str = ""


def _rust_policy_enabled() -> bool:
    from deepseek_infra.infra.rust_core.config import load_rust_flags

    return load_rust_flags().policy


def _timeout_ms() -> int:
    try:
        return int(os.environ.get("DEEPSEEK_RUST_POLICY_TIMEOUT_MS", DEFAULT_POLICY_TIMEOUT_MS))
    except (TypeError, ValueError):
        return DEFAULT_POLICY_TIMEOUT_MS


def _redact_error(value: Any) -> str:
    text = str(value or "")
    text = _AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", text)
    return _BEARER_PATTERN.sub("Bearer [REDACTED]", text)


def _failure_result(status: int, reason: str, body: Any, *, code: str = POLICY_BACKEND_UNAVAILABLE) -> PolicyProxyResult:
    return PolicyProxyResult(
        ok=False,
        status=status,
        allowed=False,
        reason=_redact_error(reason),
        body=body,
        code=code,
    )


def _parse_response(status: int, raw: bytes) -> PolicyProxyResult:
    if not raw:
        return _failure_result(status, "Rust Policy returned an empty response", {})
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failure_result(status, f"Rust Policy returned malformed JSON: {exc}", {})
    if not isinstance(body, dict):
        return _failure_result(status, "Rust Policy response must be a JSON object", body)

    allowed = body.get("allowed")
    legacy = False
    if not isinstance(allowed, bool):
        # Accept the pre-3.2.3 enum shape during rolling upgrades, but reject
        # unrelated or incomplete JSON instead of silently allowing it.
        legacy_decision = body.get("decision")
        if legacy_decision not in ("Allow", "Deny"):
            return _failure_result(status, "Rust Policy response is missing allowed", body)
        allowed = legacy_decision == "Allow"
        legacy = True

    if not legacy:
        required_fields = ("code", "reason", "decision_id", "capability", "risk_level")
        missing = [field for field in required_fields if not isinstance(body.get(field), str) or not body.get(field)]
        if missing:
            return _failure_result(
                status,
                f"Rust Policy response is missing structured fields: {', '.join(missing)}",
                body,
            )

    default_code = "allowed" if allowed else "invalid_policy_request"
    return PolicyProxyResult(
        ok=True,
        status=status,
        allowed=allowed,
        reason=str(body.get("reason") or ""),
        body=body,
        code=str(body.get("code") or default_code),
        decision_id=str(body.get("decision_id") or ""),
        trace_id=str(body.get("trace_id") or ""),
        capability=str(body.get("capability") or ""),
        risk_level=str(body.get("risk_level") or ""),
    )


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
            return _parse_response(response.status, response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = str(exc)
        return _failure_result(exc.code, str(body), body)
    except Exception as exc:
        return _failure_result(0, str(exc), str(exc))


def check_url(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    trace_id: str = "",
    capability: str = "NetworkFetch",
    risk_level: str = "High",
) -> PolicyProxyResult:
    if not _rust_policy_enabled():
        return _failure_result(0, "Rust Policy is disabled", {})
    return _request(
        "/policy/url",
        {
            "url": url,
            "trace_id": trace_id,
            "capability": capability,
            "risk_level": risk_level,
        },
        headers=headers,
    )


def check_path(
    root: str,
    requested: str,
    headers: dict[str, str] | None = None,
    *,
    trace_id: str = "",
    capability: str = "ReadFile",
    risk_level: str = "High",
) -> PolicyProxyResult:
    if not _rust_policy_enabled():
        return _failure_result(0, "Rust Policy is disabled", {})
    return _request(
        "/policy/path",
        {
            "root": root,
            "requested": requested,
            "trace_id": trace_id,
            "capability": capability,
            "risk_level": risk_level,
        },
        headers=headers,
    )


def check_capability(
    requested: str,
    granted: list[str],
    max_risk: str,
    headers: dict[str, str] | None = None,
    *,
    trace_id: str = "",
) -> PolicyProxyResult:
    if not _rust_policy_enabled():
        return _failure_result(0, "Rust Policy is disabled", {})
    return _request(
        "/policy/capability",
        {
            "requested": requested,
            "granted": granted,
            "max_risk": max_risk,
            "trace_id": trace_id,
        },
        headers=headers,
    )


def rust_policy_enabled() -> bool:
    return _rust_policy_enabled()


def fallback_to_python_enabled() -> bool:
    return rust_policy_failure_mode() == "fallback"


def failure_mode() -> str:
    return rust_policy_failure_mode()
