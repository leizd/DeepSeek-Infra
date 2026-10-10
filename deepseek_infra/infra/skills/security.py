"""Local Skill / Pack security review, trust store, and signing prep helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.skills.pack import is_reference_entry, pack_skill_ids
from deepseek_infra.infra.skills.schema import normalize_skill_id, validate_skill_config
from deepseek_infra.infra.tool_runtime.tool_policy import RISK_ORDER, tool_metadata

SECURITY_REVIEW_SCHEMA = "skill-security-review.v1"
SECURITY_MANIFEST_SCHEMA = "skill-security-manifest.v1"
TRUST_STORE_SCHEMA = "skill-trust-store.v1"

SEVERITY_SCORE = {"low": 5, "medium": 15, "high": 35, "critical": 60}
STATUS_ORDER = {"trusted": 0, "local-custom": 1, "needs-review": 2, "high-risk": 3, "blocked": 4}

SUSPICIOUS_PATTERNS: tuple[dict[str, str], ...] = (
    {
        "type": "prompt_injection",
        "severity": "high",
        "pattern": r"ignore\s+(all\s+)?previous\s+instructions|disregard\s+(all\s+)?previous\s+instructions|system\s+override",
        "suggestion": "Remove instructions that try to override upstream system or developer guidance.",
    },
    {
        "type": "secret_exfiltration",
        "severity": "high",
        "pattern": r"exfiltrate|send\s+secrets?|steal\s+secrets?|leak\s+secrets?|upload\s+.*(?:secret|token|api[_ -]?key)",
        "suggestion": "Remove instructions that request exposing credentials, tokens, or private files.",
    },
    {
        "type": "secret_file_access",
        "severity": "high",
        "pattern": r"(?:read|cat|open)\s+(?:~[/\\]\.ssh|\.env|id_rsa|id_ed25519|credentials|secrets?)",
        "suggestion": "Do not instruct Skills to read credential stores or secret-bearing files.",
    },
    {
        "type": "network_exfiltration",
        "severity": "medium",
        "pattern": r"curl\s+https?://|wget\s+https?://|post\s+to\s+https?://|send\s+to\s+https?://",
        "suggestion": "Keep network usage explicit and bounded to the Skill allowedTools policy.",
    },
    {
        "type": "hidden_tool_instruction",
        "severity": "medium",
        "pattern": r"hidden\s+tool|covert\s+tool|do\s+not\s+tell\s+the\s+user|secretly\s+(?:call|use)",
        "suggestion": "Remove hidden tool-use instructions; tool grants must be visible in allowedTools.",
    },
)

BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")
SECRET_TERMS_RE = re.compile(r"(secret|api[_ -]?key|token|password|ssh|\.env|exfiltrate)", re.IGNORECASE)


def security_dir() -> Path:
    from deepseek_infra.infra.skills import registry

    return registry.SKILLS_DIR / "security"


def trust_store_path() -> Path:
    return security_dir() / "trust-store.json"


def reviews_path() -> Path:
    return security_dir() / "reviews.jsonl"


def review_skill(skill_id: str = "", *, skill: dict[str, Any] | None = None, persist: bool = True) -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    current = registry.get_skill(skill_id, include_disabled=True) if skill is None else _normalize_skill(skill)
    skill_id = normalize_skill_id(current.get("skillId"))
    builtin = bool(current.get("builtin"))
    trust = _trust_entry("skill", skill_id)
    manifest = skill_manifest(current)
    findings = _scan_skill_fields(current)
    tool_review = tool_grant_review(current.get("allowedTools") or [], baseline_tools=trust.get("allowedTools") if isinstance(trust.get("allowedTools"), list) else [])
    findings.extend(tool_review["findings"])
    findings.extend(_tamper_findings(trust, manifest))
    risk_score = _risk_score(findings, tool_review["riskScore"])
    status = _review_status(findings=findings, risk_score=risk_score, builtin=builtin, trust=trust)
    review = {
        "schemaVersion": SECURITY_REVIEW_SCHEMA,
        "reviewId": _review_id("skill", skill_id, manifest["contentHash"]),
        "kind": "skill",
        "skillId": skill_id,
        "name": str(current.get("name") or skill_id),
        "version": str(current.get("version") or ""),
        "builtin": builtin,
        "trustLevel": status,
        "reviewStatus": status,
        "riskScore": risk_score,
        "allowedToolsRisk": tool_review["tools"],
        "requiresApprovalCount": tool_review["requiresApprovalCount"],
        "capabilities": tool_review["capabilities"],
        "findings": findings,
        "manifest": manifest,
        "lastSecurityReviewAt": utc_now_iso(),
        "signed": False,
    }
    if persist:
        _append_review(review)
    return review


def review_pack(pack_id: str = "", *, pack: dict[str, Any] | None = None, persist: bool = True) -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    current = registry.get_pack(pack_id) if pack is None else dict(pack)
    pack_id = str(current.get("packId") or pack_id)
    exported = registry.export_pack(pack_id) if pack is None else _pack_with_embedded_skills(current)
    trust = _trust_entry("pack", pack_id)
    manifest = pack_manifest(exported)
    findings = _scan_text_fields(
        {
            "name": current.get("name"),
            "description": current.get("description"),
            "author": current.get("author"),
        }
    )
    skill_reviews: list[dict[str, Any]] = []
    tools: list[str] = []
    for entry in exported.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        if not is_reference_entry(entry):
            review = review_skill(skill=entry, persist=False)
        else:
            try:
                review = review_skill(str(entry.get("skillId") or ""), persist=False)
            except AppError:
                findings.append(_finding("unresolved_skill_reference", "skills", "high", f"Pack references unknown Skill {entry.get('skillId')}.", "Install or remove the unresolved Skill reference."))
                continue
        skill_reviews.append(_pack_skill_review_summary(review))
        tools.extend([str(tool) for tool in (entry.get("allowedTools") or [])])
        findings.extend(_pack_child_findings(review))
    tool_review = tool_grant_review(tools, baseline_tools=trust.get("allowedTools") if isinstance(trust.get("allowedTools"), list) else [])
    findings.extend(tool_review["findings"])
    findings.extend(_tamper_findings(trust, manifest))
    risk_score = _risk_score(findings, tool_review["riskScore"] + sum(int(item.get("riskScore") or 0) for item in skill_reviews) // 4)
    status = _review_status(findings=findings, risk_score=risk_score, builtin=bool(current.get("builtin")), trust=trust)
    review = {
        "schemaVersion": SECURITY_REVIEW_SCHEMA,
        "reviewId": _review_id("pack", pack_id, manifest["contentHash"]),
        "kind": "pack",
        "packId": pack_id,
        "name": str(current.get("name") or pack_id),
        "version": str(current.get("version") or ""),
        "builtin": bool(current.get("builtin")),
        "trustLevel": status,
        "reviewStatus": status,
        "riskScore": min(100, risk_score),
        "allowedToolsRisk": tool_review["tools"],
        "requiresApprovalCount": tool_review["requiresApprovalCount"],
        "capabilities": tool_review["capabilities"],
        "findings": findings,
        "skillReviews": skill_reviews,
        "manifest": manifest,
        "lastSecurityReviewAt": utc_now_iso(),
        "signed": False,
    }
    if persist:
        _append_review(review)
    return review


def security_summary(*, scope: str = "all") -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    skills = [review_skill(skill=skill, persist=False) for skill in registry.list_skills(include_disabled=True)] if scope in {"all", "skills"} else []
    packs = [review_pack(pack=pack, persist=False) for pack in registry.list_packs(include_builtin=True)] if scope in {"all", "packs"} else []
    statuses = Counter(str(item.get("reviewStatus") or "") for item in [*skills, *packs])
    high_risk = [item for item in [*skills, *packs] if STATUS_ORDER.get(str(item.get("reviewStatus") or ""), 0) >= STATUS_ORDER["high-risk"]]
    return {
        "ok": True,
        "scope": scope,
        "generatedAt": utc_now_iso(),
        "summary": {
            "skillCount": len(skills),
            "packCount": len(packs),
            "trusted": statuses.get("trusted", 0),
            "needsReview": statuses.get("needs-review", 0),
            "highRisk": statuses.get("high-risk", 0),
            "blocked": statuses.get("blocked", 0),
            "averageRiskScore": round(sum(int(item.get("riskScore") or 0) for item in [*skills, *packs]) / max(1, len(skills) + len(packs)), 2),
        },
        "skills": skills,
        "packs": packs,
        "highRiskItems": [{"kind": item.get("kind"), "id": item.get("skillId") or item.get("packId"), "riskScore": item.get("riskScore")} for item in high_risk],
    }


def trust_skill(skill_id: str) -> dict[str, Any]:
    review = review_skill(skill_id, persist=True)
    entry = _trust_payload(review, status="trusted")
    store = _load_trust_store()
    store.setdefault("skills", {})[normalize_skill_id(skill_id)] = entry
    _write_trust_store(store)
    return {"ok": True, "skillId": normalize_skill_id(skill_id), "trustLevel": "trusted", "review": review, "securityManifest": review["manifest"]}


def untrust_skill(skill_id: str) -> dict[str, Any]:
    normalized = normalize_skill_id(skill_id)
    store = _load_trust_store()
    removed = store.setdefault("skills", {}).pop(normalized, None)
    _write_trust_store(store)
    return {"ok": True, "skillId": normalized, "removed": bool(removed), "trustLevel": "needs-review"}


def block_skill(skill_id: str, *, reason: str = "") -> dict[str, Any]:
    review = review_skill(skill_id, persist=True)
    entry = _trust_payload(review, status="blocked")
    entry["blockedAt"] = utc_now_iso()
    entry["blockedReason"] = str(reason or "Blocked by local security review")[:500]
    store = _load_trust_store()
    store.setdefault("skills", {})[normalize_skill_id(skill_id)] = entry
    _write_trust_store(store)
    return {"ok": True, "skillId": normalize_skill_id(skill_id), "trustLevel": "blocked", "blockedReason": entry["blockedReason"], "review": review}


def trust_pack(pack_id: str) -> dict[str, Any]:
    review = review_pack(pack_id, persist=True)
    entry = _trust_payload(review, status="trusted")
    store = _load_trust_store()
    store.setdefault("packs", {})[str(pack_id)] = entry
    _write_trust_store(store)
    return {"ok": True, "packId": str(pack_id), "trustLevel": "trusted", "review": review, "securityManifest": review["manifest"]}


def security_context_for_skill(skill: dict[str, Any], *, approved: bool = False, persist_review: bool = False) -> dict[str, Any]:
    review = review_skill(skill=skill, persist=persist_review)
    approval_required = review["reviewStatus"] == "high-risk" and review.get("trustLevel") != "trusted"
    blocked_reason = ""
    if review["reviewStatus"] == "blocked":
        blocked_reason = _trust_entry("skill", str(skill.get("skillId") or "")).get("blockedReason") or "Skill is blocked by local security review"
    elif approval_required and not approved:
        blocked_reason = "High-risk Skill requires explicit securityApproved=true before running"
    return {
        "review": review,
        "blocked": bool(blocked_reason),
        "blockedReason": blocked_reason,
        "approvalRequired": approval_required,
        "approved": bool(approved),
    }


def run_security_metadata(context: dict[str, Any]) -> dict[str, Any]:
    review_value = context.get("review")
    review: dict[str, Any] = review_value if isinstance(review_value, dict) else {}
    manifest_value = review.get("manifest")
    manifest: dict[str, Any] = manifest_value if isinstance(manifest_value, dict) else {}
    return {
        "runSecurityLevel": str(review.get("reviewStatus") or "needs-review"),
        "securityReviewId": str(review.get("reviewId") or ""),
        "trustedAtRun": review.get("reviewStatus") == "trusted",
        "toolGrantHashAtRun": str(manifest.get("toolGrantHash") or ""),
        "blockedReason": str(context.get("blockedReason") or ""),
        "approvalRequired": bool(context.get("approvalRequired")),
    }


def skill_manifest(skill: dict[str, Any]) -> dict[str, Any]:
    data = _skill_manifest_payload(skill)
    return {
        "schemaVersion": SECURITY_MANIFEST_SCHEMA,
        "kind": "skill",
        "skillId": str(data.get("skillId") or ""),
        "version": str(data.get("version") or ""),
        "contentHash": _hash(data),
        "schemaHash": _hash({"inputSchema": data.get("inputSchema"), "outputSchema": data.get("outputSchema")}),
        "promptHash": _hash(data.get("systemPrompt") or ""),
        "toolGrantHash": _hash(sorted(str(tool) for tool in data.get("allowedTools") or [])),
        "packId": "",
        "reviewStatus": "",
        "signed": False,
    }


def pack_manifest(pack: dict[str, Any]) -> dict[str, Any]:
    data = _pack_manifest_payload(pack)
    tool_ids: list[str] = []
    for entry in data.get("skills") or []:
        if isinstance(entry, dict):
            tool_ids.extend(str(tool) for tool in (entry.get("allowedTools") or []))
    return {
        "schemaVersion": SECURITY_MANIFEST_SCHEMA,
        "kind": "pack",
        "packId": str(data.get("packId") or ""),
        "version": str(data.get("version") or ""),
        "contentHash": _hash(data),
        "schemaHash": _hash(pack_skill_ids(data)),
        "promptHash": _hash([entry.get("systemPrompt") for entry in data.get("skills") or [] if isinstance(entry, dict)]),
        "toolGrantHash": _hash(sorted(set(tool_ids))),
        "reviewStatus": "",
        "signed": False,
    }


def tool_grant_review(tools: Any, *, baseline_tools: Any = None) -> dict[str, Any]:
    normalized = sorted({str(tool or "").strip() for tool in (tools or []) if str(tool or "").strip()})
    baseline = sorted({str(tool or "").strip() for tool in (baseline_tools or []) if str(tool or "").strip()})
    findings: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    risk_score = 0
    requires_approval = 0
    capabilities: set[str] = set()
    for tool in normalized:
        meta = tool_metadata(tool)
        risk = _tool_risk_label(tool)
        score = _tool_risk_score(tool)
        risk_score += score
        if risk == "requires approval":
            requires_approval += 1
        if meta is not None:
            capabilities.add(str(meta.capability or "general"))
        details.append(
            {
                "tool": tool,
                "risk": risk,
                "riskScore": score,
                "network": bool(meta.network) if meta else tool.startswith("mcp__"),
                "filesystem": bool(meta.filesystem) if meta else False,
                "sensitive": bool(meta.sensitive_sink) if meta else False,
                "requiresApproval": bool(meta.requires_confirm) if meta else False,
            }
        )
        if meta is None:
            findings.append(_finding("unknown_tool", f"allowedTools.{tool}", "medium", f"Unknown tool grant: {tool}", "Remove unknown tools or register metadata before trusting this Skill."))
        elif meta.requires_confirm or RISK_ORDER.get(str(meta.risk), 0) >= RISK_ORDER["high"]:
            findings.append(_finding("high_risk_tool", f"allowedTools.{tool}", "high", f"Tool {tool} is {risk}.", "Review the tool grant and require approval before trusting the Skill."))
    added = [tool for tool in normalized if tool not in baseline]
    if baseline and added:
        risky_added = [tool for tool in added if _tool_risk_score(tool) >= 15]
        if risky_added:
            findings.append(_finding("tool_grant_expanded", "allowedTools", "medium", f"allowedTools expanded: {', '.join(risky_added)}", "Review newly granted network/filesystem/sensitive capabilities before upgrade."))
    return {
        "tools": details,
        "riskScore": min(100, risk_score),
        "requiresApprovalCount": requires_approval,
        "capabilities": sorted(capabilities),
        "toolGrantDiff": {"added": added, "removed": [tool for tool in baseline if tool not in normalized]},
        "findings": findings,
    }


def _normalize_skill(skill: dict[str, Any]) -> dict[str, Any]:
    try:
        clean = validate_skill_config(skill)
    except Exception:
        clean = dict(skill)
    for key in ("builtin", "disabled", "createdAt", "updatedAt"):
        if key in skill:
            clean[key] = skill[key]
    return clean


def _pack_with_embedded_skills(pack: dict[str, Any]) -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    result = dict(pack)
    skills = []
    for entry in pack.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        if not is_reference_entry(entry):
            skills.append(entry)
            continue
        try:
            skills.append(registry.get_skill(str(entry.get("skillId") or ""), include_disabled=True))
        except AppError:
            skills.append(entry)
    result["skills"] = skills
    return result


def _scan_skill_fields(skill: dict[str, Any]) -> list[dict[str, Any]]:
    fields = {
        "name": skill.get("name"),
        "description": skill.get("description"),
        "systemPrompt": skill.get("systemPrompt"),
    }
    fields.update(_schema_description_fields(skill.get("inputSchema"), prefix="inputSchema"))
    fields.update(_schema_description_fields(skill.get("outputSchema"), prefix="outputSchema"))
    return _scan_text_fields(fields)


def _scan_text_fields(fields: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for field, raw in fields.items():
        text = str(raw or "")
        if not text:
            continue
        for pattern in SUSPICIOUS_PATTERNS:
            if re.search(pattern["pattern"], text, re.IGNORECASE):
                findings.append(_finding(pattern["type"], field, pattern["severity"], f"Suspicious instruction in {field}.", pattern["suggestion"]))
        if _has_suspicious_base64(text):
            findings.append(_finding("encoded_suspicious_text", field, "medium", f"Base64-like encoded sensitive instruction in {field}.", "Remove encoded instructions from Skill metadata and prompts."))
    return findings


def _schema_description_fields(schema: Any, *, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(schema, dict):
        return out
    if schema.get("description"):
        out[f"{prefix}.description"] = str(schema.get("description") or "")
    props = schema.get("properties")
    if isinstance(props, dict):
        for key, value in props.items():
            if isinstance(value, dict) and value.get("description"):
                out[f"{prefix}.properties.{key}.description"] = str(value.get("description") or "")
    return out


def _has_suspicious_base64(text: str) -> bool:
    for match in BASE64_RE.findall(text):
        try:
            decoded = base64.b64decode(match + "===", validate=False).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if SECRET_TERMS_RE.search(decoded):
            return True
    return False


def _tamper_findings(trust: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if trust.get("status") != "trusted":
        return []
    trusted_hash = str(trust.get("contentHash") or "")
    if trusted_hash and trusted_hash != manifest.get("contentHash"):
        return [_finding("tamper_detected", "contentHash", "high", "Trusted Skill content hash changed since trust was granted.", "Review the diff and trust the new content only after validation.")]
    return []


def _review_status(*, findings: list[dict[str, Any]], risk_score: int, builtin: bool, trust: dict[str, Any]) -> str:
    if trust.get("status") == "blocked":
        return "blocked"
    if trust.get("status") == "trusted" and not any(item.get("type") == "tamper_detected" for item in findings):
        return "trusted"
    if builtin and not any(item.get("type") == "tamper_detected" for item in findings):
        return "trusted"
    if any(str(item.get("severity")) in {"critical", "high"} for item in findings) or risk_score >= 70:
        return "high-risk"
    if findings or risk_score >= 25:
        return "needs-review"
    return "local-custom"


def _risk_score(findings: list[dict[str, Any]], tool_score: int) -> int:
    score = int(tool_score or 0)
    for finding in findings:
        score += SEVERITY_SCORE.get(str(finding.get("severity") or "low"), 5)
    return min(100, score)


def _pack_skill_review_summary(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "skillId": review.get("skillId"),
        "reviewStatus": review.get("reviewStatus"),
        "riskScore": review.get("riskScore"),
        "findingCount": len(review.get("findings") or []),
        "toolGrantHash": (review.get("manifest") or {}).get("toolGrantHash"),
    }


def _pack_child_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for item in review.get("findings") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "")
        if severity in {"high", "critical"}:
            findings.append({**item, "field": f"skills.{review.get('skillId')}.{item.get('field')}"})
    return findings


def _finding(kind: str, field: str, severity: str, message: str, suggestion: str) -> dict[str, Any]:
    return {
        "type": kind,
        "field": field,
        "severity": severity,
        "message": message,
        "suggestion": suggestion,
    }


def _tool_risk_label(tool_name: str) -> str:
    meta = tool_metadata(tool_name)
    if meta is None:
        return "mcp" if str(tool_name).startswith("mcp__") else "unknown"
    if meta.requires_confirm:
        return "requires approval"
    if meta.network:
        return "network"
    if meta.filesystem:
        return "filesystem"
    if meta.sensitive_sink:
        return "sensitive"
    return str(meta.risk or "low")


def _tool_risk_score(tool_name: str) -> int:
    meta = tool_metadata(tool_name)
    if meta is None:
        return 18 if str(tool_name).startswith("mcp__") else 25
    score = RISK_ORDER.get(str(meta.risk), 0) * 12
    if meta.network:
        score += 18
    if meta.filesystem:
        score += 10
    if meta.sensitive_sink:
        score += 20
    if meta.requires_confirm:
        score += 20
    return min(60, score)


def _skill_manifest_payload(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        key: skill.get(key)
        for key in (
            "skillId",
            "name",
            "description",
            "version",
            "systemPrompt",
            "inputSchema",
            "outputSchema",
            "allowedTools",
            "memoryPolicy",
            "artifactPolicy",
            "projectBinding",
        )
    }


def _pack_manifest_payload(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "packId": pack.get("packId"),
        "name": pack.get("name"),
        "description": pack.get("description"),
        "version": pack.get("version"),
        "author": pack.get("author"),
        "skills": [
            _skill_manifest_payload(entry) if isinstance(entry, dict) and not is_reference_entry(entry) else {"skillId": str((entry or {}).get("skillId") or "")}
            for entry in (pack.get("skills") or [])
            if isinstance(entry, dict)
        ],
    }


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _review_id(kind: str, item_id: str, content_hash: str) -> str:
    digest = content_hash.split(":", 1)[-1][:16]
    return f"sec-{kind}-{item_id}-{digest}"[:120]


def _trust_payload(review: dict[str, Any], *, status: str) -> dict[str, Any]:
    manifest_value = review.get("manifest")
    manifest: dict[str, Any] = manifest_value if isinstance(manifest_value, dict) else {}
    return {
        "status": status,
        "reviewId": review.get("reviewId"),
        "contentHash": manifest.get("contentHash"),
        "schemaHash": manifest.get("schemaHash"),
        "promptHash": manifest.get("promptHash"),
        "toolGrantHash": manifest.get("toolGrantHash"),
        "allowedTools": [item.get("tool") for item in review.get("allowedToolsRisk") or [] if isinstance(item, dict)],
        "updatedAt": utc_now_iso(),
    }


def _load_trust_store() -> dict[str, Any]:
    try:
        data = json.loads(trust_store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schemaVersion", TRUST_STORE_SCHEMA)
    data.setdefault("skills", {})
    data.setdefault("packs", {})
    return data


def _write_trust_store(store: dict[str, Any]) -> None:
    from deepseek_infra.infra.skills import registry

    with registry.skill_store_scope():
        security_dir().mkdir(parents=True, exist_ok=True)
        store["schemaVersion"] = TRUST_STORE_SCHEMA
        store["updatedAt"] = utc_now_iso()
        trust_store_path().write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _trust_entry(kind: str, item_id: str) -> dict[str, Any]:
    store = _load_trust_store()
    bucket = "packs" if kind == "pack" else "skills"
    entry = store.get(bucket, {}).get(str(item_id))
    return dict(entry) if isinstance(entry, dict) else {}


def _append_review(review: dict[str, Any]) -> None:
    from deepseek_infra.infra.skills import registry

    with registry.skill_store_scope():
        security_dir().mkdir(parents=True, exist_ok=True)
        with reviews_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n")
