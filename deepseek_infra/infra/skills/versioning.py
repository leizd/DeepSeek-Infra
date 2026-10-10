"""Local Skill and Skill Pack lifecycle helpers.

Versioning is intentionally local-first: snapshots live under the runtime
``.skills`` directory, do not download remote content, and never bypass the
existing Skill / Pack validators.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.skills import eval as skill_eval
from deepseek_infra.infra.skills.pack import pack_skill_ids, validate_pack_config
from deepseek_infra.infra.skills.schema import normalize_skill_id, validate_skill_config

SKILL_REVISION_SCHEMA = "skill-revision.v1"
PACK_REVISION_SCHEMA = "skill-pack-revision.v1"
VERSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
VERSIONED_SKILL_FIELDS = (
    "systemPrompt",
    "inputSchema",
    "outputSchema",
    "allowedTools",
    "memoryPolicy",
    "artifactPolicy",
    "projectBinding",
)
VERSIONED_PACK_FIELDS = ("name", "description", "version", "author", "skills")


def snapshot_skill(skill: dict[str, Any], *, change_summary: str = "", event: str = "save") -> dict[str, Any]:
    """Persist a custom Skill revision and return its metadata."""
    from deepseek_infra.infra.skills import registry

    config = _skill_for_snapshot(skill)
    skill_id = normalize_skill_id(config.get("skillId"))
    metadata = _skill_metadata(config, change_summary=change_summary, event=event)
    payload = {"schemaVersion": SKILL_REVISION_SCHEMA, "metadata": metadata, "skill": config}
    target = skill_history_dir(skill_id) / f"{_safe_version(metadata['version'])}--{metadata['revisionId']}.json"
    with registry.skill_store_scope():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**metadata, "path": str(target.relative_to(registry.SKILLS_DIR))}


def list_skill_versions(skill_id: str) -> list[dict[str, Any]]:
    """List historical revisions plus the current registry version."""
    normalized = normalize_skill_id(skill_id)
    versions = [_public_revision(item) for item in _load_skill_snapshots(normalized)]
    try:
        from deepseek_infra.infra.skills import registry

        current = registry.get_skill(normalized, include_disabled=True)
        current_meta = _skill_metadata(_skill_for_snapshot(current), change_summary="Current registry state", event="current")
        current_meta["current"] = True
        versions.append(current_meta)
    except AppError:
        pass
    return _dedupe_revision_metadata(versions)


def diff_skill_versions(skill_id: str, from_version: str, to_version: str) -> dict[str, Any]:
    before = _resolve_skill_revision(skill_id, from_version)
    after = _resolve_skill_revision(skill_id, to_version)
    before_skill = before["skill"]
    after_skill = after["skill"]
    fields = [_field_diff(field, before_skill.get(field), after_skill.get(field)) for field in VERSIONED_SKILL_FIELDS]
    before_tools = [str(item) for item in before_skill.get("allowedTools") or []]
    after_tools = [str(item) for item in after_skill.get("allowedTools") or []]
    return {
        "ok": True,
        "kind": "skill",
        "skillId": normalize_skill_id(skill_id),
        "from": _public_revision(before),
        "to": _public_revision(after),
        "fields": fields,
        "toolGrantDiff": _list_diff(before_tools, after_tools),
        "evalScoreDiff": _score_diff("skill", normalize_skill_id(skill_id)),
        "changed": any(item["changed"] for item in fields),
    }


def migration_plan(skill_id: str, from_version: str, to_version: str) -> dict[str, Any]:
    before = _resolve_skill_revision(skill_id, from_version)
    after = _resolve_skill_revision(skill_id, to_version)
    before_schema = _dict(before["skill"].get("inputSchema"))
    after_schema = _dict(after["skill"].get("inputSchema"))
    before_props = _dict(before_schema.get("properties"))
    after_props = _dict(after_schema.get("properties"))
    before_required = set(_strings(before_schema.get("required")))
    after_required = set(_strings(after_schema.get("required")))
    removed = sorted(set(before_props) - set(after_props))
    added = sorted(set(after_props) - set(before_props))
    changes: list[dict[str, Any]] = []
    safe = True

    renamed_from: set[str] = set()
    renamed_to: set[str] = set()
    for old in removed:
        match = _rename_candidate(old, before_props[old], added, after_props, renamed_to)
        if not match:
            continue
        renamed_from.add(old)
        renamed_to.add(match)
        changes.append({"type": "inputFieldRenamed", "from": old, "to": match, "safe": True})

    for field in removed:
        if field in renamed_from:
            continue
        changes.append({"type": "inputFieldRemoved", "field": field, "safe": field not in before_required})
        if field in before_required:
            safe = False

    for field in added:
        if field in renamed_to:
            continue
        prop = _dict(after_props.get(field))
        default = prop.get("default")
        is_required = field in after_required
        change = {"type": "inputFieldAdded", "field": field, "required": is_required, "safe": not is_required or default is not None}
        if default is not None:
            change["default"] = default
        changes.append(change)
        if is_required and default is None:
            safe = False

    for field in sorted(after_required - before_required - renamed_to):
        if field in added:
            continue
        prop = _dict(after_props.get(field))
        change = {"type": "requiredFieldAdded", "field": field, "safe": prop.get("default") is not None}
        if prop.get("default") is not None:
            change["default"] = prop.get("default")
        changes.append(change)
        if prop.get("default") is None:
            safe = False

    targets = _migration_targets(normalize_skill_id(skill_id))
    return {
        "ok": True,
        "skillId": normalize_skill_id(skill_id),
        "fromVersion": str(before["metadata"].get("version") or from_version),
        "toVersion": str(after["metadata"].get("version") or to_version),
        "safe": safe,
        "changes": changes,
        "migrationTargets": targets,
        "summary": _migration_summary(changes, targets, safe=safe),
    }


def rollback_skill(skill_id: str, version: str, *, change_summary: str = "") -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    normalized = normalize_skill_id(skill_id)
    if registry.is_builtin_skill(normalized):
        raise AppError("Built-in Skills cannot be rolled back; clone them as custom Skills first", code=ErrorCode.FORBIDDEN, status=403)
    target = _resolve_skill_revision(normalized, version)
    current = registry.get_skill(normalized, include_disabled=True)
    snapshot_skill(current, change_summary=f"Rollback checkpoint before {version}", event="rollback_checkpoint")
    restored = _skill_for_snapshot(target["skill"])
    restored["updatedAt"] = utc_now_iso()
    registry.write_custom_skill(restored)
    metadata = snapshot_skill(restored, change_summary=change_summary or f"Rolled back to {version}", event="rollback")
    return {
        "ok": True,
        "skill": registry.public_skill(restored),
        "rolledBackTo": target["metadata"],
        "revision": metadata,
    }


def snapshot_pack(pack: dict[str, Any], *, change_summary: str = "", event: str = "save") -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    config = _pack_for_snapshot(pack)
    pack_id = str(config.get("packId") or "")
    metadata = _pack_metadata(config, change_summary=change_summary, event=event)
    payload = {"schemaVersion": PACK_REVISION_SCHEMA, "metadata": metadata, "pack": config}
    target = pack_history_dir(pack_id) / f"{_safe_version(metadata['version'])}--{metadata['revisionId']}.json"
    with registry.skill_store_scope():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**metadata, "path": str(target.relative_to(registry.SKILLS_DIR))}


def list_pack_versions(pack_id: str) -> list[dict[str, Any]]:
    from deepseek_infra.infra.skills.pack import normalize_pack_id

    normalized = normalize_pack_id(pack_id)
    versions = [_public_revision(item) for item in _load_pack_snapshots(normalized)]
    try:
        from deepseek_infra.infra.skills import registry

        current = registry.get_pack(normalized)
        current_meta = _pack_metadata(_pack_for_snapshot(current), change_summary="Current registry state", event="current")
        current_meta["current"] = True
        versions.append(current_meta)
    except AppError:
        pass
    return _dedupe_revision_metadata(versions)


def diff_pack_versions(pack_id: str, from_version: str, to_version: str) -> dict[str, Any]:
    before = _resolve_pack_revision(pack_id, from_version)
    after = _resolve_pack_revision(pack_id, to_version)
    before_pack = before["pack"]
    after_pack = after["pack"]
    fields = [_field_diff(field, before_pack.get(field), after_pack.get(field)) for field in VERSIONED_PACK_FIELDS]
    return {
        "ok": True,
        "kind": "pack",
        "packId": str(after_pack.get("packId") or pack_id),
        "from": _public_revision(before),
        "to": _public_revision(after),
        "fields": fields,
        "skillDiff": _list_diff(pack_skill_ids(before_pack), pack_skill_ids(after_pack)),
        "toolGrantDiff": _list_diff(_pack_tool_ids(before_pack), _pack_tool_ids(after_pack)),
        "evalScoreDiff": _score_diff("pack", str(after_pack.get("packId") or pack_id)),
        "changed": any(item["changed"] for item in fields),
    }


def rollback_pack(pack_id: str, version: str, *, project_id: str = "", change_summary: str = "") -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    target = _resolve_pack_revision(pack_id, version)
    normalized = str(target["pack"].get("packId") or pack_id)
    if registry.get_pack(normalized).get("builtin"):
        raise AppError("Built-in Skill Packs are read-only; export and import as a custom Pack to edit", code=ErrorCode.FORBIDDEN, status=403)
    current = registry.get_pack(normalized)
    snapshot_pack(current, change_summary=f"Pack rollback checkpoint before {version}", event="rollback_checkpoint")
    restored = _pack_for_snapshot(target["pack"])
    restored["updatedAt"] = utc_now_iso()
    registry.write_custom_pack(restored)
    revision = snapshot_pack(restored, change_summary=change_summary or f"Rolled back Pack to {version}", event="rollback")
    binding: dict[str, Any] = {}
    if project_id:
        binding = projects.enable_pack_for_project(project_id, normalized, version=str(restored.get("version") or version))
    return {"ok": True, "pack": registry.public_pack(restored, builtin=False), "rolledBackTo": target["metadata"], "revision": revision, "projectBinding": binding}


def upgrade_pack(pack_id: str, version: str = "", *, project_id: str = "", baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    from deepseek_infra.infra.skills import registry

    pack = registry.get_pack(pack_id)
    target_version = str(version or pack.get("version") or "")
    if target_version in {"current", "latest"}:
        target_version = str(pack.get("version") or "")
    gate = eval_aware_upgrade_gate(kind="pack", item_id=str(pack.get("packId") or pack_id), baseline=baseline)
    applied_pack = pack
    if target_version and target_version != str(pack.get("version") or ""):
        target = _resolve_pack_revision(pack_id, target_version)
        if pack.get("builtin"):
            raise AppError("Built-in Skill Packs are read-only; export and import as a custom Pack to upgrade locally", code=ErrorCode.FORBIDDEN, status=403)
        applied_pack = _pack_for_snapshot(target["pack"])
        applied_pack["updatedAt"] = utc_now_iso()
        registry.write_custom_pack(applied_pack)
        snapshot_pack(applied_pack, change_summary=f"Upgraded Pack to {target_version}", event="upgrade")
    binding: dict[str, Any] = {}
    if project_id:
        binding = projects.enable_pack_for_project(project_id, str(applied_pack.get("packId") or pack_id), version=str(applied_pack.get("version") or target_version))
    return {"ok": True, "pack": registry.get_pack(str(applied_pack.get("packId") or pack_id)), "targetVersion": target_version, "evalAwareUpgradeGate": gate, "projectBinding": binding}


def eval_aware_upgrade_gate(*, kind: str, item_id: str, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    scope = "pack" if kind == "pack" else "skill"
    if scope == "pack":
        report = skill_eval.build_skill_eval_report(version="upgrade-gate", scope=scope, pack_id=item_id, baseline=baseline)
    else:
        report = skill_eval.build_skill_eval_report(version="upgrade-gate", scope=scope, skill_id=item_id, baseline=baseline)
    regression = _dict(report.get("regression"))
    summary = _dict(report.get("summary"))
    risk = "review" if int(regression.get("regressionCount") or 0) else "low"
    return {
        "status": "PASS" if report.get("status") == "PASS" else "REVIEW",
        "risk": risk,
        "overallScore": summary.get("overallScore"),
        "passRate": summary.get("passRate"),
        "regressionCount": regression.get("regressionCount", 0),
        "newFailures": regression.get("newFailures", []),
        "scoreDrops": regression.get("scoreDrops", []),
        "recommendation": "safe to upgrade" if risk == "low" else "review before install",
    }


def skill_history_dir(skill_id: str) -> Path:
    from deepseek_infra.infra.skills import registry

    return registry.SKILLS_DIR / "history" / normalize_skill_id(skill_id)


def pack_history_dir(pack_id: str) -> Path:
    from deepseek_infra.infra.skills import registry
    from deepseek_infra.infra.skills.pack import normalize_pack_id

    return registry.SKILLS_DIR / "history" / "packs" / normalize_pack_id(pack_id)


def _skill_for_snapshot(skill: dict[str, Any]) -> dict[str, Any]:
    raw = {key: copy.deepcopy(value) for key, value in skill.items() if key not in {"builtin", "disabled"}}
    return validate_skill_config(raw)


def _pack_for_snapshot(pack: dict[str, Any]) -> dict[str, Any]:
    raw = {key: copy.deepcopy(value) for key, value in pack.items() if key not in {"builtin"}}
    return validate_pack_config(raw)


def _skill_metadata(config: dict[str, Any], *, change_summary: str, event: str) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "skillId": str(config.get("skillId") or ""),
        "version": str(config.get("version") or ""),
        "revisionId": _revision_id(config, event),
        "createdAt": now,
        "changeSummary": str(change_summary or event or "Skill saved")[:400],
        "event": event,
        "schemaHash": _hash_json({"inputSchema": config.get("inputSchema"), "outputSchema": config.get("outputSchema")}),
        "promptHash": _hash_json(config.get("systemPrompt")),
        "toolGrantHash": _hash_json(config.get("allowedTools") or []),
    }


def _pack_metadata(config: dict[str, Any], *, change_summary: str, event: str) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "packId": str(config.get("packId") or ""),
        "version": str(config.get("version") or ""),
        "revisionId": _revision_id(config, event),
        "createdAt": now,
        "changeSummary": str(change_summary or event or "Skill Pack saved")[:400],
        "event": event,
        "packHash": _hash_json(config),
        "skillIdsHash": _hash_json(pack_skill_ids(config)),
        "toolGrantHash": _hash_json(_pack_tool_ids(config)),
    }


def _revision_id(config: dict[str, Any], event: str) -> str:
    compact_time = re.sub(r"[^0-9]", "", utc_now_iso())[:14]
    digest = _hash_json({"event": event, "config": config})[:10]
    return f"rev_{compact_time}_{digest}"


def _hash_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_version(value: str) -> str:
    text = str(value or "0.0.0").strip()
    if VERSION_RE.fullmatch(text):
        return text
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)[:80] or "0.0.0"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_skill_snapshots(skill_id: str) -> list[dict[str, Any]]:
    directory = skill_history_dir(skill_id)
    if not directory.exists():
        return []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if not data or data.get("schemaVersion") != SKILL_REVISION_SCHEMA:
            continue
        if not isinstance(data.get("metadata"), dict) or not isinstance(data.get("skill"), dict):
            continue
        data["path"] = str(path)
        snapshots.append(data)
    return sorted(snapshots, key=lambda item: str(_dict(item.get("metadata")).get("createdAt") or ""))


def _load_pack_snapshots(pack_id: str) -> list[dict[str, Any]]:
    directory = pack_history_dir(pack_id)
    if not directory.exists():
        return []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if not data or data.get("schemaVersion") != PACK_REVISION_SCHEMA:
            continue
        if not isinstance(data.get("metadata"), dict) or not isinstance(data.get("pack"), dict):
            continue
        data["path"] = str(path)
        snapshots.append(data)
    return sorted(snapshots, key=lambda item: str(_dict(item.get("metadata")).get("createdAt") or ""))


def _resolve_skill_revision(skill_id: str, version_or_revision: str) -> dict[str, Any]:
    normalized = normalize_skill_id(skill_id)
    wanted = str(version_or_revision or "").strip()
    snapshots = _load_skill_snapshots(normalized)
    matches = [item for item in snapshots if _revision_matches(item, wanted)]
    if matches:
        return matches[-1]
    if wanted in {"", "current", "latest"}:
        from deepseek_infra.infra.skills import registry

        current = _skill_for_snapshot(registry.get_skill(normalized, include_disabled=True))
        return {"schemaVersion": SKILL_REVISION_SCHEMA, "metadata": _skill_metadata(current, change_summary="Current registry state", event="current"), "skill": current}
    raise AppError("Skill version not found", code=ErrorCode.NOT_FOUND, status=404)


def _resolve_pack_revision(pack_id: str, version_or_revision: str) -> dict[str, Any]:
    from deepseek_infra.infra.skills.pack import normalize_pack_id

    normalized = normalize_pack_id(pack_id)
    wanted = str(version_or_revision or "").strip()
    snapshots = _load_pack_snapshots(normalized)
    matches = [item for item in snapshots if _revision_matches(item, wanted)]
    if matches:
        return matches[-1]
    if wanted in {"", "current", "latest"}:
        from deepseek_infra.infra.skills import registry

        current = _pack_for_snapshot(registry.get_pack(normalized))
        return {"schemaVersion": PACK_REVISION_SCHEMA, "metadata": _pack_metadata(current, change_summary="Current registry state", event="current"), "pack": current}
    raise AppError("Skill Pack version not found", code=ErrorCode.NOT_FOUND, status=404)


def _revision_matches(revision: dict[str, Any], wanted: str) -> bool:
    meta = _dict(revision.get("metadata"))
    return wanted in {str(meta.get("version") or ""), str(meta.get("revisionId") or "")}


def _public_revision(revision: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(_dict(revision.get("metadata")) or revision)
    if "path" in revision:
        metadata["path"] = str(revision["path"])
    if "current" in revision:
        metadata["current"] = bool(revision["current"])
    return metadata


def _dedupe_revision_metadata(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = f"{item.get('revisionId')}::{item.get('event')}"
        seen[key] = item
    return sorted(seen.values(), key=lambda item: str(item.get("createdAt") or ""))


def _field_diff(field: str, before: Any, after: Any) -> dict[str, Any]:
    return {
        "field": field,
        "changed": before != after,
        "before": before,
        "after": after,
        "beforeHash": _hash_json(before),
        "afterHash": _hash_json(after),
    }


def _list_diff(before: list[str], after: list[str]) -> dict[str, list[str]]:
    before_set = set(before)
    after_set = set(after)
    return {"added": sorted(after_set - before_set), "removed": sorted(before_set - after_set), "unchanged": sorted(before_set & after_set)}


def _pack_tool_ids(pack: dict[str, Any]) -> list[str]:
    tools: list[str] = []
    for entry in pack.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        for tool in entry.get("allowedTools") or []:
            text = str(tool or "")
            if text and text not in tools:
                tools.append(text)
    return sorted(tools)


def _rename_candidate(old: str, old_prop: Any, added: list[str], after_props: dict[str, Any], used: set[str]) -> str:
    old_type = _dict(old_prop).get("type")
    for candidate in added:
        if candidate in used:
            continue
        if _dict(after_props.get(candidate)).get("type") == old_type:
            return candidate
    return ""


def _migration_targets(skill_id: str) -> dict[str, int]:
    project_binding_count = 0
    skill_run_count = 0
    saved_metadata_count = 0
    for project in projects.list_projects():
        exported = projects.export_project(str(project.get("id") or ""))
        binding = _dict(exported.get("skills"))
        if skill_id in _strings(binding.get("enabledSkills")):
            project_binding_count += 1
        skill_run_count += sum(1 for run in exported.get("skillRuns") or [] if isinstance(run, dict) and run.get("skillId") == skill_id)
        saved_metadata_count += sum(1 for item in exported.get("savedItems") or [] if isinstance(item, dict) and _dict(item.get("source")).get("skillId") == skill_id)
    return {"projectBindings": project_binding_count, "skillRuns": skill_run_count, "savedMetadata": saved_metadata_count}


def _migration_summary(changes: list[dict[str, Any]], targets: dict[str, int], *, safe: bool) -> str:
    status = "safe" if safe else "requires review"
    return (
        f"{len(changes)} schema changes, {status}; targets: "
        f"{targets.get('projectBindings', 0)} project bindings, "
        f"{targets.get('skillRuns', 0)} skill runs, "
        f"{targets.get('savedMetadata', 0)} saved metadata records."
    )


def _score_diff(kind: str, item_id: str) -> dict[str, Any]:
    try:
        gate = eval_aware_upgrade_gate(kind=kind, item_id=item_id)
    except Exception:
        return {"before": None, "after": None, "delta": None, "status": "unavailable"}
    return {"before": None, "after": gate.get("overallScore"), "delta": None, "status": gate.get("status")}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "")]
