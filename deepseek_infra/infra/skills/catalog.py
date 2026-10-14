"""Local Skill Catalog / Marketplace-lite helpers.

The catalog is intentionally local-only: it indexes Skills and Skill Packs that
already exist on disk, enriches them with security and eval metadata, and
coordinates project installation. It does not download third-party content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import APP_VERSION
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.skills import registry, security
from deepseek_infra.infra.skills.pack import tool_permission_summary

CATALOG_SCHEMA = "skill-catalog.v1"
REPO_ROOT = Path(__file__).resolve().parents[3]

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Study", ("study", "tutor", "exam", "course", "learn", "paper", "reader", "document")),
    ("Research", ("research", "brief", "source", "reading")),
    ("Code", ("code", "review", "python", "engineering", "readme")),
    ("Office", ("ppt", "slide", "office", "document", "report")),
    ("Writing", ("write", "writer", "paper", "summary", "markdown")),
    ("Data", ("data", "chart", "table", "analysis")),
    ("Automation", ("automation", "workflow", "scheduler")),
)


def catalog_dir() -> Path:
    return registry.SKILLS_DIR / "catalog"


def catalog_manifest_path() -> Path:
    return catalog_dir() / "catalog.json"


def catalog_manifest() -> dict[str, Any]:
    items = catalog_list()
    return {
        "catalogVersion": "1.0.0",
        "schemaVersion": CATALOG_SCHEMA,
        "version": APP_VERSION,
        "generatedAt": utc_now_iso(),
        "source": "local",
        "network": False,
        "summary": _catalog_summary(items),
        "items": items,
    }


def catalog_refresh() -> dict[str, Any]:
    with registry.skill_store_scope():
        manifest = catalog_manifest()
        catalog_dir().mkdir(parents=True, exist_ok=True)
        catalog_manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"ok": True, "path": str(catalog_manifest_path()), "manifest": manifest}


def catalog_export() -> dict[str, Any]:
    return {"ok": True, "catalog": catalog_manifest()}


def catalog_list() -> list[dict[str, Any]]:
    eval_scores = _eval_scores()
    install_counts = _install_counts()
    items: list[dict[str, Any]] = []
    for skill in registry.list_skills(include_disabled=True):
        items.append(_skill_item(skill, eval_scores=eval_scores, install_counts=install_counts))
    for pack in registry.list_packs(include_builtin=True):
        items.append(_pack_item(pack, eval_scores=eval_scores, install_counts=install_counts))
    return sorted(items, key=lambda item: (str(item.get("kind") or ""), str(item.get("category") or ""), str(item.get("name") or "")))


def catalog_get(item_id: str) -> dict[str, Any]:
    normalized = str(item_id or "").strip()
    if not normalized:
        raise AppError("itemId is required", code=ErrorCode.INVALID_PAYLOAD)
    for item in catalog_list():
        if item.get("itemId") == normalized:
            return item
    raise AppError("Catalog item not found", code=ErrorCode.NOT_FOUND, status=404)


def catalog_search(query: str = "", *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_query = str(query or "").strip().lower()
    active_filters = filters if isinstance(filters, dict) else {}
    items = []
    for item in catalog_list():
        if normalized_query and normalized_query not in _search_blob(item):
            continue
        if not _matches_filters(item, active_filters):
            continue
        items.append(item)
    return {"ok": True, "query": query, "filters": active_filters, "items": items, "summary": _catalog_summary(items)}


def catalog_install(item_id: str, *, project_id: str = "", security_approved: bool = False, dry_run: bool = False) -> dict[str, Any]:
    item = catalog_get(item_id)
    project_id = str(project_id or "").strip()
    if not project_id:
        raise AppError("projectId is required", code=ErrorCode.INVALID_PAYLOAD)
    projects.require_project(project_id)
    preview = install_preview(item, project_id=project_id)
    if dry_run:
        return {"ok": True, "dryRun": True, "item": item, "installPreview": preview}
    _enforce_install_security(preview, approved=security_approved)
    if item["kind"] == "pack":
        binding = projects.enable_pack_for_project(project_id, str(item["packId"]), version=str(item.get("version") or ""))
    else:
        binding = _install_skill(project_id, str(item["skillId"]))
    return {"ok": True, "dryRun": False, "item": item, "installPreview": preview, "projectId": project_id, "skills": binding}


def catalog_uninstall(item_id: str, *, project_id: str = "") -> dict[str, Any]:
    item = catalog_get(item_id)
    project_id = str(project_id or "").strip()
    if not project_id:
        raise AppError("projectId is required", code=ErrorCode.INVALID_PAYLOAD)
    projects.require_project(project_id)
    if item["kind"] == "pack":
        binding = _uninstall_pack(project_id, str(item["packId"]))
    else:
        binding = _uninstall_skill(project_id, str(item["skillId"]))
    return {"ok": True, "itemId": item["itemId"], "projectId": project_id, "skills": binding}


def install_preview(item: dict[str, Any], *, project_id: str) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    binding = projects.project_skill_binding(project_id)
    enabled_skills = set(str(skill) for skill in binding.get("enabledSkills") or [])
    enabled_packs = set(str(pack) for pack in binding.get("enabledPacks") or [])
    included_skills = [str(skill) for skill in item.get("includedSkills") or []]
    security_review = _dict_value(item.get("securityReview"))
    review_status = str(security_review.get("reviewStatus") or item.get("trustLevel") or "needs-review")
    return {
        "itemId": item.get("itemId"),
        "kind": kind,
        "projectId": project_id,
        "includedSkills": included_skills,
        "newSkills": [skill for skill in included_skills if skill not in enabled_skills],
        "alreadyEnabledSkills": [skill for skill in included_skills if skill in enabled_skills],
        "willEnablePack": kind == "pack" and str(item.get("packId") or "") not in enabled_packs,
        "willModifyProjectBinding": True,
        "requiresSecurityApproval": _requires_install_approval(review_status),
        "blocked": review_status == "blocked",
        "reviewStatus": review_status,
        "trustLevel": str(item.get("trustLevel") or review_status),
        "riskScore": int(item.get("riskScore") or 0),
        "evalScore": float(item.get("evalScore") or 0.0),
        "signed": bool(item.get("signed")),
        "requiredTools": item.get("requiredTools") if isinstance(item.get("requiredTools"), list) else [],
        "toolPermissionSummary": item.get("toolPermissionSummary") if isinstance(item.get("toolPermissionSummary"), list) else [],
        "securityReview": security_review,
        "projectChanges": {
            "enabledPacksBefore": sorted(enabled_packs),
            "enabledSkillsBefore": sorted(enabled_skills),
        },
    }


def _skill_item(skill: dict[str, Any], *, eval_scores: dict[str, dict[str, float]], install_counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    review = security.review_skill(skill=skill, persist=False)
    manifest = _dict_value(review.get("manifest"))
    required_tools = _string_list(skill.get("allowedTools"))
    category = _category_for(skill, required_tools)
    return {
        "itemId": str(skill.get("skillId") or ""),
        "kind": "skill",
        "skillId": str(skill.get("skillId") or ""),
        "packId": "",
        "name": str(skill.get("name") or skill.get("skillId") or ""),
        "description": str(skill.get("description") or ""),
        "category": category,
        "tags": _tags_for(category, required_tools, skill),
        "author": "builtin" if skill.get("builtin") else "local",
        "version": str(skill.get("version") or ""),
        "trustLevel": str(review.get("reviewStatus") or "needs-review"),
        "riskScore": int(review.get("riskScore") or 0),
        "evalScore": eval_scores.get("skills", {}).get(str(skill.get("skillId") or ""), 0.0),
        "installCount": install_counts.get("skills", {}).get(str(skill.get("skillId") or ""), 0),
        "lastUpdated": str(skill.get("updatedAt") or skill.get("createdAt") or ""),
        "includedSkills": [str(skill.get("skillId") or "")],
        "requiredTools": required_tools,
        "artifactTypes": _string_list(_dict_value(skill.get("artifactPolicy")).get("types")),
        "difficulty": _difficulty_for(review),
        "useCases": _use_cases_for(category, skill),
        "recommendedProjects": _recommended_projects_for(category),
        "builtin": bool(skill.get("builtin")),
        "disabled": bool(skill.get("disabled")),
        "source": "builtin" if skill.get("builtin") else "custom",
        "signed": bool(manifest.get("signed")),
        "contentHash": str(manifest.get("contentHash") or ""),
        "schemaHash": str(manifest.get("schemaHash") or ""),
        "promptHash": str(manifest.get("promptHash") or ""),
        "toolGrantHash": str(manifest.get("toolGrantHash") or ""),
        "securityReview": review,
        "toolPermissionSummary": [
            {
                "skillId": str(skill.get("skillId") or ""),
                "embedded": True,
                "allowedTools": [
                    {
                        "tool": str(tool.get("tool") or ""),
                        "risk": str(tool.get("risk") or ""),
                        "requiresApproval": bool(tool.get("requiresApproval")),
                    }
                    for tool in review.get("allowedToolsRisk") or []
                    if isinstance(tool, dict)
                ],
            }
        ],
    }


def _pack_item(pack: dict[str, Any], *, eval_scores: dict[str, dict[str, float]], install_counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    exported = registry.export_pack(str(pack.get("packId") or ""))
    exported["builtin"] = bool(pack.get("builtin"))
    exported["createdAt"] = pack.get("createdAt")
    exported["updatedAt"] = pack.get("updatedAt")
    review = security.review_pack(pack=exported, persist=False)
    manifest = _dict_value(review.get("manifest"))
    included_skills = [str(skill.get("skillId") or "") for skill in exported.get("skills") or [] if isinstance(skill, dict)]
    required_tools = sorted({tool for skill in exported.get("skills") or [] if isinstance(skill, dict) for tool in _string_list(skill.get("allowedTools"))})
    category = _category_for({**pack, "skills": included_skills}, required_tools)
    return {
        "itemId": str(pack.get("packId") or ""),
        "kind": "pack",
        "skillId": "",
        "packId": str(pack.get("packId") or ""),
        "name": str(pack.get("name") or pack.get("packId") or ""),
        "description": str(pack.get("description") or ""),
        "category": category,
        "tags": _tags_for(category, required_tools, pack),
        "author": str(pack.get("author") or ("builtin" if pack.get("builtin") else "local")),
        "version": str(pack.get("version") or ""),
        "trustLevel": str(review.get("reviewStatus") or "needs-review"),
        "riskScore": int(review.get("riskScore") or 0),
        "evalScore": eval_scores.get("packs", {}).get(str(pack.get("packId") or ""), 0.0),
        "installCount": install_counts.get("packs", {}).get(str(pack.get("packId") or ""), 0),
        "lastUpdated": str(pack.get("updatedAt") or pack.get("createdAt") or ""),
        "includedSkills": included_skills,
        "requiredTools": required_tools,
        "artifactTypes": sorted({artifact for skill in exported.get("skills") or [] if isinstance(skill, dict) for artifact in _artifact_types(skill)}),
        "difficulty": _difficulty_for(review),
        "useCases": _use_cases_for(category, pack),
        "recommendedProjects": _recommended_projects_for(category),
        "builtin": bool(pack.get("builtin")),
        "disabled": False,
        "source": "builtin" if pack.get("builtin") else "custom",
        "signed": bool(manifest.get("signed")),
        "contentHash": str(manifest.get("contentHash") or ""),
        "schemaHash": str(manifest.get("schemaHash") or ""),
        "promptHash": str(manifest.get("promptHash") or ""),
        "toolGrantHash": str(manifest.get("toolGrantHash") or ""),
        "securityReview": review,
        "toolPermissionSummary": tool_permission_summary(exported),
    }


def _install_skill(project_id: str, skill_id: str) -> dict[str, Any]:
    registry.get_skill(skill_id, include_disabled=True)
    binding = projects.project_skill_binding(project_id)
    enabled = [str(skill) for skill in binding.get("enabledSkills") or []]
    if skill_id not in enabled:
        enabled.append(skill_id)
    default = str(binding.get("defaultSkill") or "") or skill_id
    return projects.set_project_skill_binding(
        project_id,
        enabled,
        default_skill=default,
        enabled_packs=[str(pack) for pack in binding.get("enabledPacks") or []],
        enabled_pack_versions=[item for item in binding.get("enabledPackVersions") or [] if isinstance(item, dict)],
    )


def _uninstall_skill(project_id: str, skill_id: str) -> dict[str, Any]:
    binding = projects.project_skill_binding(project_id)
    enabled = [str(skill) for skill in binding.get("enabledSkills") or [] if str(skill) != skill_id]
    default = str(binding.get("defaultSkill") or "")
    if default == skill_id:
        default = enabled[0] if enabled else ""
    return projects.set_project_skill_binding(
        project_id,
        enabled,
        default_skill=default,
        enabled_packs=[str(pack) for pack in binding.get("enabledPacks") or []],
        enabled_pack_versions=[item for item in binding.get("enabledPackVersions") or [] if isinstance(item, dict)],
    )


def _uninstall_pack(project_id: str, pack_id: str) -> dict[str, Any]:
    pack = registry.export_pack(pack_id)
    removing = {str(skill.get("skillId") or "") for skill in pack.get("skills") or [] if isinstance(skill, dict)}
    binding = projects.project_skill_binding(project_id)
    remaining_packs = [str(pack_value) for pack_value in binding.get("enabledPacks") or [] if str(pack_value) != pack_id]
    remaining_pack_versions = [
        item for item in binding.get("enabledPackVersions") or [] if isinstance(item, dict) and str(item.get("packId") or "") != pack_id
    ]
    protected: set[str] = set()
    for remaining_pack in remaining_packs:
        try:
            exported = registry.export_pack(remaining_pack)
        except AppError:
            continue
        protected.update(str(skill.get("skillId") or "") for skill in exported.get("skills") or [] if isinstance(skill, dict))
    enabled = [str(skill) for skill in binding.get("enabledSkills") or [] if str(skill) not in removing or str(skill) in protected]
    default = str(binding.get("defaultSkill") or "")
    if default not in enabled:
        default = enabled[0] if enabled else ""
    return projects.set_project_skill_binding(
        project_id,
        enabled,
        default_skill=default,
        enabled_packs=remaining_packs,
        enabled_pack_versions=remaining_pack_versions,
    )


def _enforce_install_security(preview: dict[str, Any], *, approved: bool) -> None:
    if preview.get("blocked"):
        raise AppError("Catalog item is blocked by local security review", code=ErrorCode.FORBIDDEN, status=403)
    if preview.get("requiresSecurityApproval") and not approved:
        raise AppError("Catalog item requires securityApproved=true before install", code=ErrorCode.FORBIDDEN, status=403)


def _requires_install_approval(review_status: str) -> bool:
    return review_status in {"high-risk", "blocked"}


def _catalog_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "itemCount": len(items),
        "skillCount": sum(1 for item in items if item.get("kind") == "skill"),
        "packCount": sum(1 for item in items if item.get("kind") == "pack"),
        "trusted": sum(1 for item in items if item.get("trustLevel") == "trusted"),
        "needsReview": sum(1 for item in items if item.get("trustLevel") == "needs-review"),
        "highRisk": sum(1 for item in items if item.get("trustLevel") == "high-risk"),
        "blocked": sum(1 for item in items if item.get("trustLevel") == "blocked"),
        "averageEvalScore": round(sum(float(item.get("evalScore") or 0.0) for item in items) / max(1, len(items)), 2),
        "localOnly": True,
    }


def _matches_filters(item: dict[str, Any], filters: dict[str, Any]) -> bool:
    if filters.get("kind") and str(item.get("kind") or "") != str(filters.get("kind")):
        return False
    if filters.get("category") and str(item.get("category") or "").lower() != str(filters.get("category")).lower():
        return False
    if filters.get("trustLevel") and str(item.get("trustLevel") or "") != str(filters.get("trustLevel")):
        return False
    if filters.get("trusted") is True and item.get("trustLevel") != "trusted":
        return False
    if filters.get("offline") is True and "web_search" in set(item.get("requiredTools") or []):
        return False
    if _filter_number(filters.get("maxRiskScore")) is not None and int(item.get("riskScore") or 0) > int(_filter_number(filters.get("maxRiskScore")) or 0):
        return False
    if _filter_number(filters.get("minEvalScore")) is not None and float(item.get("evalScore") or 0.0) < float(_filter_number(filters.get("minEvalScore")) or 0.0):
        return False
    tool = str(filters.get("tool") or "").strip()
    if tool and tool not in set(item.get("requiredTools") or []):
        return False
    return True


def _filter_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _search_blob(item: dict[str, Any]) -> str:
    fields = [
        item.get("itemId"),
        item.get("name"),
        item.get("description"),
        item.get("category"),
        " ".join(str(tag) for tag in item.get("tags") or []),
        " ".join(str(tool) for tool in item.get("requiredTools") or []),
        " ".join(str(skill) for skill in item.get("includedSkills") or []),
    ]
    return " ".join(str(field or "").lower() for field in fields)


def _eval_scores() -> dict[str, dict[str, float]]:
    report = _load_eval_report()
    skills: dict[str, float] = {}
    packs: dict[str, float] = {}
    for item in report.get("skillResults") or []:
        if isinstance(item, dict):
            skills[str(item.get("skillId") or "")] = float(item.get("overallScore") or 0.0)
    for item in report.get("packResults") or []:
        if isinstance(item, dict):
            packs[str(item.get("packId") or "")] = float(item.get("overallScore") or 0.0)
    return {"skills": skills, "packs": packs}


def _load_eval_report() -> dict[str, Any]:
    candidates = [
        REPO_ROOT / "evals" / "reports" / f"skills-v{APP_VERSION}.json",
        REPO_ROOT / "evals" / "reports" / "skill-latest.json",
        REPO_ROOT / "evals" / "reports" / "latest.json",
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _install_counts() -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {"skills": {}, "packs": {}}
    try:
        project_rows = projects.list_projects()
    except AppError:
        return counts
    for project in project_rows:
        binding = project.get("skills") if isinstance(project, dict) else {}
        if not isinstance(binding, dict):
            continue
        for skill_id in binding.get("enabledSkills") or []:
            key = str(skill_id or "")
            counts["skills"][key] = counts["skills"].get(key, 0) + 1
        for pack_id in binding.get("enabledPacks") or []:
            key = str(pack_id or "")
            counts["packs"][key] = counts["packs"].get(key, 0) + 1
    return counts


def _category_for(item: dict[str, Any], tools: list[str]) -> str:
    text = " ".join(
        [
            str(item.get("skillId") or item.get("packId") or ""),
            str(item.get("name") or ""),
            str(item.get("description") or ""),
            " ".join(str(skill) for skill in item.get("skills") or []),
            " ".join(tools),
        ]
    ).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return category
    return "General"


def _tags_for(category: str, tools: list[str], item: dict[str, Any]) -> list[str]:
    tags = [category.lower(), "local"]
    if item.get("builtin"):
        tags.append("builtin")
    else:
        tags.append("custom")
    for tool in tools:
        if tool in {"web_search", "fetch_url", "compare_search_results"}:
            tags.append("network")
        if tool in {"create_document", "create_pptx", "create_mindmap"}:
            tags.append("artifact")
        if tool in {"search_files", "read_file_chunk", "list_project_files"}:
            tags.append("filesystem")
    return sorted(set(tags))


def _difficulty_for(review: dict[str, Any]) -> str:
    risk_score = int(review.get("riskScore") or 0)
    if risk_score >= 70:
        return "advanced"
    if risk_score >= 25:
        return "intermediate"
    return "beginner"


def _use_cases_for(category: str, item: dict[str, Any]) -> list[str]:
    name = str(item.get("name") or item.get("skillId") or item.get("packId") or "Skill")
    defaults = {
        "Study": ["exam prep", "worked explanation", "revision notes"],
        "Research": ["topic brief", "source synthesis", "markdown report"],
        "Code": ["code review", "README support", "engineering notes"],
        "Office": ["slides", "documents", "project export"],
        "Writing": ["paper draft", "summary", "reference outline"],
        "Data": ["analysis notes", "chart planning", "data summary"],
        "Automation": ["repeatable workflow", "local task helper", "runtime prep"],
    }
    return defaults.get(category, ["local workspace task", name])


def _recommended_projects_for(category: str) -> list[str]:
    defaults = {
        "Study": ["exam-prep", "course-notes"],
        "Research": ["research-library", "briefing"],
        "Code": ["repo-review", "engineering"],
        "Office": ["presentation", "reporting"],
        "Writing": ["paper-draft", "article"],
        "Data": ["analysis", "dashboard"],
        "Automation": ["operations", "runtime"],
    }
    return defaults.get(category, ["workspace"])


def _artifact_types(skill: dict[str, Any]) -> list[str]:
    policy = _dict_value(skill.get("artifactPolicy"))
    return _string_list(policy.get("types"))


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result
