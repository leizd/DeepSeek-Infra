"""HTTP proxy client for the Rust Policy sidecar."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepseek_infra.infra.rust_core.config import rust_gateway_url, rust_policy_failure_mode
from deepseek_infra.infra.rust_core import transport

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
    serialization_us: int | None = None
    transport_us: int | None = None
    rust_processing_us: int | None = None
    total_us: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    correlation_id: str = ""
    connection_reused: bool | None = None
    connection_count: int | None = None

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "pythonPreparationUs": None,
            "serializationUs": self.serialization_us,
            "transportUs": self.transport_us,
            "rustProcessingUs": self.rust_processing_us,
            "pythonValidationUs": None,
            "totalDelegateUs": self.total_us,
            "requestBytes": self.request_bytes,
            "responseBytes": self.response_bytes,
            "connectionReused": self.connection_reused,
            "connectionCount": self.connection_count,
            "correlationId": self.correlation_id,
        }


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


def _failure_result(
    status: int,
    reason: str,
    body: Any,
    *,
    code: str = POLICY_BACKEND_UNAVAILABLE,
    **timing: Any,
) -> PolicyProxyResult:
    return PolicyProxyResult(
        ok=False,
        status=status,
        allowed=False,
        reason=_redact_error(reason),
        body=body,
        code=code,
        **timing,
    )


def _parse_response(status: int, raw: bytes, **timing: Any) -> PolicyProxyResult:
    if not raw:
        return _failure_result(status, "Rust Policy returned an empty response", {}, **timing)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _failure_result(status, f"Rust Policy returned malformed JSON: {exc}", {}, **timing)
    if not isinstance(body, dict):
        return _failure_result(status, "Rust Policy response must be a JSON object", body, **timing)

    allowed = body.get("allowed")
    legacy = False
    if not isinstance(allowed, bool):
        # Accept the pre-3.2.3 enum shape during rolling upgrades, but reject
        # unrelated or incomplete JSON instead of silently allowing it.
        legacy_decision = body.get("decision")
        if legacy_decision not in ("Allow", "Deny"):
            return _failure_result(status, "Rust Policy response is missing allowed", body, **timing)
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
                **timing,
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
        **timing,
    )


def _request(
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout_ms: int | None = None,
) -> PolicyProxyResult:
    total_started_ns = time.perf_counter_ns()
    url = f"{rust_gateway_url()}{path}"
    timeout = (timeout_ms if timeout_ms is not None else _timeout_ms()) / 1000.0
    correlation_id = transport.new_correlation_id()
    req_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-DeepSeek-Request-ID": correlation_id,
    }
    del headers  # Policy preparation is credential-free; caller headers never cross this boundary.
    serialization_started_ns = time.perf_counter_ns()
    data = json.dumps(payload).encode("utf-8")
    serialization_us = max(0, (time.perf_counter_ns() - serialization_started_ns) // 1000)
    request = urllib.request.Request(url, data=data, method="POST", headers=req_headers)
    transport_started_ns = time.perf_counter_ns()

    def timing(response: Any = None, response_bytes: int = 0) -> dict[str, Any]:
        observed_transport_us = getattr(response, "transport_us", None)
        if not isinstance(observed_transport_us, int):
            observed_transport_us = max(0, (time.perf_counter_ns() - transport_started_ns) // 1000)
        rust_processing_us: int | None = None
        raw_rust_us = transport.response_header(response, "X-DeepSeek-Rust-Processing-Us") if response is not None else None
        if raw_rust_us is not None:
            try:
                rust_processing_us = max(0, int(raw_rust_us))
            except ValueError:
                rust_processing_us = None
        return {
            "serialization_us": serialization_us,
            "transport_us": observed_transport_us,
            "rust_processing_us": rust_processing_us,
            "total_us": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
            "request_bytes": len(data),
            "response_bytes": response_bytes,
            "correlation_id": correlation_id,
            "connection_reused": getattr(response, "connection_reused", None),
            "connection_count": getattr(response, "connection_count", None),
        }

    try:
        with transport.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return _parse_response(response.status, raw, **timing(response, len(raw)))
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            body = raw.decode("utf-8")
        except Exception:
            raw = b""
            body = str(exc)
        return _failure_result(exc.code, str(body), body, **timing(exc, len(raw)))
    except Exception as exc:
        return _failure_result(0, str(exc), str(exc), **timing())


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
