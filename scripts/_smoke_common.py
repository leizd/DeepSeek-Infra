#!/usr/bin/env python3
"""Small helpers shared by protocol compatibility smoke scripts."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


class SmokeFailure(RuntimeError):
    """A smoke check failed in a way that should fail the script."""


@dataclass(slots=True)
class StepResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_token(explicit: str = "") -> str:
    """Resolve the local auth token from CLI, env, or known token files."""
    if explicit.strip():
        return explicit.strip()
    for env_name in ("DEEPSEEK_INFRA_TOKEN", "AUTH_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    roots = [Path.cwd(), REPO_ROOT]
    configured_root = os.environ.get("DEEPSEEK_INFRA_ROOT", "").strip()
    if configured_root:
        roots.insert(0, Path(configured_root))
    seen: set[Path] = set()
    for root in roots:
        try:
            candidate = root.resolve() / ".auth-token"
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return ""


def service_base_from_endpoint(url: str, suffix: str) -> str:
    """Return the service root when a user passes either the root or a protocol endpoint."""
    parsed = urllib.parse.urlsplit(url.rstrip("/"))
    suffix = suffix.strip("/")
    path = parsed.path.rstrip("/")
    if suffix and path.endswith("/" + suffix):
        path = path[: -len(suffix) - 1] or ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def bearer_headers(token: str = "", *, accept: str = "application/json") -> dict[str, str]:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(
    method: str,
    url: str,
    *,
    token: str = "",
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = bearer_headers(token)
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method.upper()} {url} returned HTTP {exc.code}: {_short(body)}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SmokeFailure(f"{method.upper()} {url} failed: {exc}") from exc
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"{method.upper()} {url} returned non-JSON: {_short(raw.decode('utf-8', errors='replace'))}") from exc
    if not isinstance(parsed, dict):
        raise SmokeFailure(f"{method.upper()} {url} returned JSON {type(parsed).__name__}, expected object")
    return parsed


def jsonrpc(method: str, params: dict[str, Any] | None = None, message_id: int | str = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def rpc_result(response: dict[str, Any], method: str) -> dict[str, Any]:
    error = response.get("error")
    if isinstance(error, dict):
        raise SmokeFailure(f"{method} returned JSON-RPC error {error.get('code')}: {error.get('message')}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise SmokeFailure(f"{method} returned no object result")
    return result


def print_step(step: StepResult, *, as_json: bool = False) -> None:
    if as_json:
        return
    marker = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(step.status, step.status.upper())
    print(f"[{marker}] {step.name}: {step.detail}")


def finish(steps: list[StepResult], *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({"ok": not any(step.status == "fail" for step in steps), "steps": [step.to_dict() for step in steps]}, ensure_ascii=False, indent=2))
    failed = [step for step in steps if step.status == "fail"]
    if failed and not as_json:
        print("\nTroubleshooting hints:", file=sys.stderr)
        print("- Confirm the local server is running, for example: AUTH_DISABLED=1 python app.py", file=sys.stderr)
        print("- If auth is enabled, pass --token or set DEEPSEEK_INFRA_TOKEN/AUTH_TOKEN.", file=sys.stderr)
        print("- Check /healthz first, then retry the protocol-specific endpoint.", file=sys.stderr)
    return 1 if failed else 0


def _short(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
