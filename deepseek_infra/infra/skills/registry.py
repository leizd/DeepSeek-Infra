"""Skill registry: built-in Skill pack plus local custom Skill storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import BUILTIN_PACKS_DIR, BUILTIN_SKILLS_DIR, SKILLS_DIR
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.skills.pack import (
    PackSchemaError,
    embedded_skill_configs,
    normalize_pack_id,
    pack_skill_ids,
    tool_permission_summary,
    validate_pack_config,
)
from deepseek_infra.infra.skills.schema import SkillSchemaError, normalize_skill_id, validate_skill_config


def list_skills(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    skills: dict[str, dict[str, Any]] = {}
    disabled = disabled_skill_ids()
    for skill in load_builtin_skills():
        skill["builtin"] = True
        skill["disabled"] = skill["skillId"] in disabled
        skills[skill["skillId"]] = skill
    for skill in load_custom_skills():
        skill["builtin"] = False
        skill["disabled"] = bool(skill.get("disabled")) or skill["skillId"] in disabled
        skills[skill["skillId"]] = skill
    result = [public_skill(skill) for skill in skills.values() if include_disabled or not skill.get("disabled")]
    return sorted(result, key=lambda item: (bool(item.get("builtin")) is False, str(item.get("name") or "")))


def list_builtin_skills(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    disabled = disabled_skill_ids()
    result = []
    for skill in load_builtin_skills():
        skill["builtin"] = True
        skill["disabled"] = skill["skillId"] in disabled
        if include_disabled or not skill["disabled"]:
            result.append(public_skill(skill))
    return sorted(result, key=lambda item: str(item.get("skillId") or ""))


def get_skill(skill_id: str, *, include_disabled: bool = False) -> dict[str, Any]:
    normalized = normalize_skill_id(skill_id)
    for skill in list_skills(include_disabled=True):
        if skill["skillId"] == normalized:
            if skill.get("disabled") and not include_disabled:
                raise AppError("Skill is disabled", code=ErrorCode.FORBIDDEN, status=403)
            return skill
    raise AppError("Skill not found", code=ErrorCode.NOT_FOUND, status=404)


def create_custom_skill(config: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    skill = normalize_config_for_storage(config)
    skill_id = skill["skillId"]
    if is_builtin_skill(skill_id):
        raise AppError("Cannot overwrite a built-in Skill", code=ErrorCode.FORBIDDEN, status=403)
    path = custom_skill_path(skill_id)
    if path.exists() and not overwrite:
        raise AppError("Skill already exists", code=ErrorCode.INVALID_PAYLOAD, status=409)
    now = utc_now_iso()
    existing = _read_json(path) if path.exists() else {}
    if isinstance(existing, dict) and existing.get("createdAt"):
        skill["createdAt"] = str(existing["createdAt"])
    else:
        skill["createdAt"] = now
    skill["updatedAt"] = now
    skill["builtin"] = False
    write_custom_skill(skill)
    return public_skill(skill)


def update_skill(skill_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_skill_id(skill_id)
    path = custom_skill_path(normalized)
    if not path.exists():
        if is_builtin_skill(normalized):
            raise AppError("Built-in Skills are read-only; export and import as a custom Skill to edit", code=ErrorCode.FORBIDDEN, status=403)
        raise AppError("Skill not found", code=ErrorCode.NOT_FOUND, status=404)
    current = _read_json(path)
    if not isinstance(current, dict):
        raise AppError("Skill file is corrupt", code=ErrorCode.INVALID_PAYLOAD)
    merged = {**current, **(patch if isinstance(patch, dict) else {})}
    if str(merged.get("skillId") or "") != normalized:
        raise AppError("skillId cannot be changed", code=ErrorCode.INVALID_PAYLOAD)
    skill = normalize_config_for_storage(merged)
    skill["createdAt"] = str(current.get("createdAt") or utc_now_iso())
    skill["updatedAt"] = utc_now_iso()
    skill["builtin"] = False
    write_custom_skill(skill)
    return public_skill(skill)


def set_skill_disabled(skill_id: str, disabled: bool = True) -> dict[str, Any]:
    normalized = normalize_skill_id(skill_id)
    get_skill(normalized, include_disabled=True)
    disabled_ids = set(disabled_skill_ids())
    if disabled:
        disabled_ids.add(normalized)
    else:
        disabled_ids.discard(normalized)
    write_disabled_skill_ids(sorted(disabled_ids))
    return get_skill(normalized, include_disabled=True)


def delete_skill(skill_id: str) -> dict[str, Any]:
    normalized = normalize_skill_id(skill_id)
    path = custom_skill_path(normalized)
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            raise AppError(f"Cannot delete Skill: {exc}", code=ErrorCode.INTERNAL, status=500) from exc
        disabled_ids = [item for item in disabled_skill_ids() if item != normalized]
        write_disabled_skill_ids(disabled_ids)
        return {"ok": True, "deleted": normalized, "disabled": False}
    if is_builtin_skill(normalized):
        set_skill_disabled(normalized, True)
        return {"ok": True, "deleted": "", "disabled": True, "skillId": normalized}
    raise AppError("Skill not found", code=ErrorCode.NOT_FOUND, status=404)


def export_skill_config(skill_id: str) -> dict[str, Any]:
    skill = get_skill(skill_id, include_disabled=True)
    return {key: value for key, value in skill.items() if key not in {"builtin", "createdAt", "updatedAt"}}


def export_skill_file(skill_id: str, path: Path) -> dict[str, Any]:
    payload = export_skill_config(skill_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(target), "skill": payload}


def import_skill_config(config: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    return create_custom_skill(config, overwrite=overwrite)


def import_skill_file(path: Path, *, overwrite: bool = False) -> dict[str, Any]:
    data = _read_json(Path(path))
    if not isinstance(data, dict):
        raise AppError("Skill import file must contain a JSON object", code=ErrorCode.INVALID_PAYLOAD)
    return import_skill_config(data, overwrite=overwrite)


def load_builtin_skills() -> list[dict[str, Any]]:
    return [_load_skill_file(path, builtin=True) for path in sorted(BUILTIN_SKILLS_DIR.glob("*.json")) if path.is_file()]


def load_custom_skills() -> list[dict[str, Any]]:
    directory = custom_skills_dir()
    if not directory.exists():
        return []
    skills = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            skills.append(_load_skill_file(path, builtin=False))
        except AppError:
            continue
    return skills


def public_skill(skill: dict[str, Any]) -> dict[str, Any]:
    data = dict(skill)
    data["builtin"] = bool(skill.get("builtin"))
    data["disabled"] = bool(skill.get("disabled"))
    return data


def is_builtin_skill(skill_id: str) -> bool:
    normalized = normalize_skill_id(skill_id)
    return any(skill["skillId"] == normalized for skill in load_builtin_skills())


def normalize_config_for_storage(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_skill_config(config)
    except SkillSchemaError as exc:
        raise AppError(str(exc), code=ErrorCode.INVALID_PAYLOAD) from exc


def custom_skills_dir() -> Path:
    return SKILLS_DIR / "custom"


def custom_skill_path(skill_id: str) -> Path:
    return custom_skills_dir() / f"{normalize_skill_id(skill_id)}.json"


def disabled_skills_path() -> Path:
    return SKILLS_DIR / "disabled.json"


def disabled_skill_ids() -> list[str]:
    data = _read_json(disabled_skills_path())
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        try:
            result.append(normalize_skill_id(item))
        except SkillSchemaError:
            continue
    return sorted(set(result))


def write_disabled_skill_ids(skill_ids: list[str]) -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    disabled_skills_path().write_text(json.dumps(sorted(set(skill_ids)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_custom_skill(skill: dict[str, Any]) -> None:
    custom_skills_dir().mkdir(parents=True, exist_ok=True)
    path = custom_skill_path(str(skill.get("skillId") or ""))
    path.write_text(json.dumps(skill, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_skill_file(path: Path, *, builtin: bool) -> dict[str, Any]:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise AppError(f"Invalid Skill file: {path}", code=ErrorCode.INVALID_PAYLOAD)
    skill = normalize_config_for_storage(data)
    skill["builtin"] = builtin
    for key in ("createdAt", "updatedAt"):
        if data.get(key):
            skill[key] = str(data[key])
    return skill


def _read_json(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- Skill Packs --------------------------------------------------------------


def list_packs(*, include_builtin: bool = True) -> list[dict[str, Any]]:
    packs: dict[str, dict[str, Any]] = {}
    if include_builtin:
        for pack in load_builtin_packs():
            pack["builtin"] = True
            packs[pack["packId"]] = pack
    for pack in load_custom_packs():
        pack["builtin"] = False
        packs[pack["packId"]] = pack
    return sorted(packs.values(), key=lambda item: (bool(item.get("builtin")) is False, str(item.get("name") or "")))


def list_builtin_packs() -> list[dict[str, Any]]:
    return [public_pack(pack, builtin=True) for pack in load_builtin_packs()]


def get_pack(pack_id: str) -> dict[str, Any]:
    normalized = normalize_pack_id(pack_id)
    for pack in list_packs():
        if pack["packId"] == normalized:
            return pack
    raise AppError("Skill Pack not found", code=ErrorCode.NOT_FOUND, status=404)


def export_pack(pack_id: str) -> dict[str, Any]:
    """Return a self-contained pack manifest with all skill configs embedded."""
    pack = get_pack(pack_id)
    resolved = _resolve_pack_skills(pack)
    payload = {key: value for key, value in pack.items() if key not in {"builtin", "createdAt", "updatedAt"}}
    payload["skills"] = [_public_skill_for_pack(skill) for skill in resolved]
    return payload


def validate_pack_manifest(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_pack_config(config)
    except PackSchemaError as exc:
        raise AppError(str(exc), code=ErrorCode.INVALID_PAYLOAD) from exc


def import_pack(config: dict[str, Any], *, overwrite: bool = False, on_conflict: str = "error") -> dict[str, Any]:
    """Validate and install a Skill Pack locally.

    Embedded Skill configs are written as custom Skills (with conflict handling).
    References are validated against existing Skills. Returns an import summary
    with the tool permission diff and conflict handling report.
    """
    pack = validate_pack_manifest(config)
    pack_id = pack["packId"]
    on_conflict = str(on_conflict or "error").strip().lower()
    if on_conflict not in {"error", "overwrite", "skip"}:
        raise AppError("onConflict must be one of error, overwrite, skip", code=ErrorCode.INVALID_PAYLOAD)

    conflicts = _pack_skill_conflicts(pack)
    if conflicts and not overwrite and on_conflict == "error":
        raise AppError(
            f"Skill Pack import would overwrite existing Skills: {', '.join(conflicts)}; set overwrite=true or onConflict=overwrite",
            code=ErrorCode.INVALID_PAYLOAD,
            status=409,
        )

    installed_skills: list[dict[str, Any]] = []
    skipped: list[str] = []
    for skill in embedded_skill_configs(pack):
        skill_id = str(skill.get("skillId") or "")
        existing = _safe_get_skill(skill_id)
        if existing is not None and not overwrite and on_conflict != "overwrite":
            if on_conflict == "skip":
                skipped.append(skill_id)
                continue
        created = create_custom_skill(skill, overwrite=overwrite or on_conflict == "overwrite")
        installed_skills.append(created)

    unresolved = _unresolved_references(pack)
    manifest = _pack_manifest_for_storage(pack)
    write_custom_pack(manifest)

    summary = {
        "ok": True,
        "packId": pack_id,
        "name": pack.get("name") or "",
        "installedSkills": [str(skill.get("skillId") or "") for skill in installed_skills],
        "skippedSkills": skipped,
        "conflicts": conflicts,
        "unresolvedReferences": unresolved,
        "toolPermissions": tool_permission_summary(pack),
        "pack": public_pack(manifest, builtin=False),
    }
    return summary


def delete_pack(pack_id: str) -> dict[str, Any]:
    normalized = normalize_pack_id(pack_id)
    path = custom_pack_path(normalized)
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            raise AppError(f"Cannot delete Skill Pack: {exc}", code=ErrorCode.INTERNAL, status=500) from exc
        return {"ok": True, "deleted": normalized}
    if any(pack["packId"] == normalized for pack in load_builtin_packs()):
        raise AppError("Built-in Skill Packs are read-only; export and re-import as a custom Pack to edit", code=ErrorCode.FORBIDDEN, status=403)
    raise AppError("Skill Pack not found", code=ErrorCode.NOT_FOUND, status=404)


def load_builtin_packs() -> list[dict[str, Any]]:
    return [_load_pack_file(path, builtin=True) for path in sorted(BUILTIN_PACKS_DIR.glob("*.json")) if path.is_file()]


def load_custom_packs() -> list[dict[str, Any]]:
    directory = custom_packs_dir()
    if not directory.exists():
        return []
    packs = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            packs.append(_load_pack_file(path, builtin=False))
        except AppError:
            continue
    return packs


def public_pack(pack: dict[str, Any], *, builtin: bool) -> dict[str, Any]:
    data = dict(pack)
    data["builtin"] = bool(builtin)
    data["skills"] = [
        {"skillId": str(entry.get("skillId") or ""), "name": str(entry.get("name") or ""), "embedded": not _is_ref(entry)}
        for entry in (pack.get("skills") or [])
        if isinstance(entry, dict)
    ]
    return data


def custom_packs_dir() -> Path:
    return SKILLS_DIR / "packs"


def custom_pack_path(pack_id: str) -> Path:
    return custom_packs_dir() / f"{normalize_pack_id(pack_id)}.json"


def write_custom_pack(pack: dict[str, Any]) -> None:
    custom_packs_dir().mkdir(parents=True, exist_ok=True)
    path = custom_pack_path(str(pack.get("packId") or ""))
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pack_manifest_for_storage(pack: dict[str, Any]) -> dict[str, Any]:
    manifest = {key: value for key, value in pack.items() if key != "skills"}
    manifest["skills"] = [{"skillId": skill_id} for skill_id in pack_skill_ids(pack)]
    manifest["createdAt"] = utc_now_iso()
    manifest["updatedAt"] = utc_now_iso()
    return manifest


def _resolve_pack_skills(pack: dict[str, Any]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for entry in pack.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        if not _is_ref(entry):
            resolved.append(entry)
            continue
        skill = _safe_get_skill(str(entry.get("skillId") or ""))
        if skill is None:
            raise AppError(
                f"Skill Pack references unknown skillId: {entry.get('skillId')}",
                code=ErrorCode.NOT_FOUND,
                status=404,
            )
        resolved.append(skill)
    return resolved


def _pack_skill_conflicts(pack: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for skill in embedded_skill_configs(pack):
        skill_id = str(skill.get("skillId") or "")
        if skill_id and _safe_get_skill(skill_id) is not None:
            conflicts.append(skill_id)
    return conflicts


def _unresolved_references(pack: dict[str, Any]) -> list[str]:
    unresolved: list[str] = []
    for entry in pack.get("skills") or []:
        if not isinstance(entry, dict) or not _is_ref(entry):
            continue
        skill_id = str(entry.get("skillId") or "")
        if skill_id and _safe_get_skill(skill_id) is None:
            unresolved.append(skill_id)
    return unresolved


def _safe_get_skill(skill_id: str) -> dict[str, Any] | None:
    try:
        return get_skill(skill_id, include_disabled=True)
    except AppError:
        return None


def _is_ref(entry: Any) -> bool:
    from deepseek_infra.infra.skills.pack import is_reference_entry

    return is_reference_entry(entry)


def _public_skill_for_pack(skill: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in skill.items() if key not in {"builtin", "disabled", "createdAt", "updatedAt"}}


def _load_pack_file(path: Path, *, builtin: bool) -> dict[str, Any]:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise AppError(f"Invalid Skill Pack file: {path}", code=ErrorCode.INVALID_PAYLOAD)
    try:
        pack = validate_pack_config(data)
    except PackSchemaError as exc:
        raise AppError(str(exc), code=ErrorCode.INVALID_PAYLOAD) from exc
    pack["builtin"] = builtin
    for key in ("createdAt", "updatedAt"):
        if data.get(key):
            pack[key] = str(data[key])
    return pack
