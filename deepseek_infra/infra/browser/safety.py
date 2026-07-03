"""Browser action safety policy and audit logging."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from deepseek_infra.core import config
from deepseek_infra.infra.workspace.schema import redact_value, utc_now

ALLOW = "allow"
DENY = "deny"
NEEDS_CONFIRMATION = "needs_confirmation"

READ_ACTIONS = {"open_url", "read_page", "screenshot", "scroll", "extract_links", "extract_dom", "save_snapshot", "close_session"}
WRITE_ACTIONS = {"click", "type_text", "select", "download"}
SUPPORTED_ACTIONS = READ_ACTIONS | WRITE_ACTIONS
_audit_lock = threading.Lock()
_PRIVATE_HOST_SUFFIXES = (".local", ".localhost", ".internal")
_HIGH_RISK_TEXT_RE = re.compile(r"(?i)\b(submit|delete|remove|purchase|buy|pay|checkout|confirm|authorize|transfer|sign\s*in|login)\b")
_PASSWORD_RE = re.compile(r"(?i)(password|passwd|pwd|type=['\"]password['\"]|\[type=['\"]password['\"]\])")


@dataclass(frozen=True, slots=True)
class BrowserSafetyDecision:
    action: str
    verdict: str
    risk: str
    reasons: tuple[str, ...] = ()
    suggestion: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.verdict == NEEDS_CONFIRMATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "verdict": self.verdict,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "suggestion": self.suggestion,
        }


def evaluate_action(payload: dict[str, Any]) -> BrowserSafetyDecision:
    action = normalize_action(payload.get("action"))
    if action not in SUPPORTED_ACTIONS:
        return BrowserSafetyDecision(action or "unknown", DENY, "high", ("unknown_action",), "Use a registered browser action.")
    if not config.BROWSER_CONTROL_ENABLED:
        return BrowserSafetyDecision(
            action,
            DENY,
            "high",
            ("browser_control_disabled",),
            "Set BROWSER_CONTROL_ENABLED=1 before exposing browser control.",
        )

    reasons: list[str] = []
    risk = "low"
    url = str(payload.get("url") or payload.get("downloadUrl") or payload.get("currentUrl") or "").strip()
    if action == "open_url" and not url:
        return BrowserSafetyDecision(action, DENY, "medium", ("missing_url",), "Provide a URL to open.")
    if url:
        safe, why = evaluate_url_safety(url)
        if not safe:
            return BrowserSafetyDecision(action, DENY, "critical", (f"unsafe_url:{why}",), "Use a public http(s) URL or an approved fixture file.")

    selector = str(payload.get("selector") or "")
    reason = str(payload.get("reason") or "")
    if action in WRITE_ACTIONS:
        risk = "medium"
        if config.BROWSER_REQUIRE_CONFIRM:
            reasons.append("write_action_requires_confirmation")
    if action == "type_text" and (_PASSWORD_RE.search(selector) or str(payload.get("fieldType") or "").lower() == "password"):
        risk = "high"
        reasons.append("password_field_requires_confirmation")
    if action == "click" and (_HIGH_RISK_TEXT_RE.search(selector) or _HIGH_RISK_TEXT_RE.search(reason)):
        risk = "high"
        reasons.append("high_risk_click_requires_confirmation")
    if action == "download" and download_looks_executable(payload):
        risk = "high"
        reasons.append("executable_download_requires_confirmation")
    if bool(payload.get("requiresConfirmation")):
        reasons.append("caller_requested_confirmation")

    confirmed = bool(payload.get("confirmed"))
    if reasons and not confirmed:
        return BrowserSafetyDecision(action, NEEDS_CONFIRMATION, risk, tuple(sorted(set(reasons))), "Ask the user to confirm this browser action.")
    if reasons and confirmed:
        reasons.append("confirmed")
    return BrowserSafetyDecision(action, ALLOW, risk, tuple(sorted(set(reasons))))


def normalize_action(value: Any) -> str:
    return str(value or "").strip().lower()


def evaluate_url_safety(url: str) -> tuple[bool, str]:
    raw = str(url or "").strip()
    if not raw:
        return False, "empty url"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False, "invalid url"
    if parsed.scheme == "file":
        return evaluate_file_url_safety(parsed.path)
    if parsed.scheme not in {"http", "https"}:
        return False, f"scheme not allowed: {parsed.scheme or '(none)'}"
    if parsed.username or parsed.password:
        return False, "url credentials are not allowed"
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return False, "missing host"
    if config.BROWSER_ALLOW_PRIVATE_HOSTS:
        return True, ""
    if host == "localhost" or host.endswith(_PRIVATE_HOST_SUFFIXES):
        return False, "private host is not allowed"
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        return True, ""
    if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False, f"private or local ip is not allowed: {ip}"
    return True, ""


def evaluate_file_url_safety(path_value: str) -> tuple[bool, str]:
    try:
        import urllib.request

        path = Path(urllib.request.url2pathname(path_value)).resolve()
    except (OSError, ValueError):
        return False, "invalid file url"
    root = Path(__file__).resolve().parents[3]
    allowed_roots = [root / "tests" / "fixtures" / "browser", root / "evals" / "golden" / "browser"]
    for allowed in allowed_roots:
        try:
            path.relative_to(allowed.resolve())
            return True, ""
        except ValueError:
            continue
    return False, "file url is only allowed for browser fixture directories"


def download_looks_executable(payload: dict[str, Any]) -> bool:
    from deepseek_infra.infra.browser.downloads import is_executable_filename

    candidates = (
        str(payload.get("filename") or ""),
        str(payload.get("url") or ""),
        str(payload.get("downloadUrl") or ""),
        str(payload.get("selector") or ""),
    )
    return any(is_executable_filename(value) for value in candidates)


def audit_decision(
    decision: BrowserSafetyDecision,
    payload: dict[str, Any],
    *,
    session_id: str = "",
    project_id: str = "",
    request_id: str = "",
    outcome: str = "",
    media_ids: list[str] | None = None,
) -> None:
    entry = {
        "ts": utc_now(),
        "sessionId": session_id or str(payload.get("sessionId") or ""),
        "requestId": request_id or str(payload.get("requestId") or ""),
        "projectId": project_id or str(payload.get("projectId") or ""),
        "action": decision.action,
        "verdict": decision.verdict,
        "risk": decision.risk,
        "riskLevel": decision.risk,
        "reasons": list(decision.reasons),
        "outcome": outcome,
        "argsHash": normalized_args_hash(payload),
        "url": redact_value(str(payload.get("url") or payload.get("downloadUrl") or "")),
        "selector": str(payload.get("selector") or "")[:300],
        "mediaIds": media_ids or [],
        "taint": "untrusted_browser",
    }
    try:
        with _audit_lock:
            config.BROWSER_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            with config.BROWSER_AUDIT_LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def normalized_args_hash(payload: dict[str, Any]) -> str:
    redacted = redact_value(payload)
    try:
        canonical = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = "{}"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()[:16]
