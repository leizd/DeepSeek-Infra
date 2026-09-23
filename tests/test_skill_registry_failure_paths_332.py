from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.skills import registry


def _skill(skill_id: str = "skill_registry_332") -> dict[str, Any]:
    return {
        "skillId": skill_id,
        "name": "Registry 332",
        "description": "Failure-path registry fixture.",
        "version": "1.0.0",
        "systemPrompt": "Return JSON.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        "allowedTools": [],
        "memoryPolicy": {"scope": "none", "read": False, "write": False},
        "artifactPolicy": {"autoSave": False, "types": []},
        "projectBinding": {"enabled": False},
        "exampleInputs": [{}],
    }


def test_disabled_duplicate_and_builtin_create_guards(tmp_settings: Path) -> None:
    created = registry.create_custom_skill(_skill())
    registry.set_skill_disabled(created["skillId"], True)
    with pytest.raises(AppError, match="disabled"):
        registry.get_skill(created["skillId"])
    with pytest.raises(AppError, match="already exists"):
        registry.create_custom_skill(_skill())
    builtin = registry.export_skill_config("skill_code_review")
    with pytest.raises(AppError, match="built-in"):
        registry.create_custom_skill(builtin, overwrite=True)


def test_update_missing_builtin_corrupt_and_changed_id(tmp_settings: Path) -> None:
    with pytest.raises(AppError, match="read-only"):
        registry.update_skill("skill_code_review", {})
    with pytest.raises(AppError, match="not found"):
        registry.update_skill("skill_missing_332", {})

    path = registry.custom_skill_path("skill_corrupt_332")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(AppError, match="corrupt"):
        registry.update_skill("skill_corrupt_332", {})

    created = registry.create_custom_skill(_skill("skill_change_id_332"))
    with pytest.raises(AppError, match="cannot be changed"):
        registry.update_skill(created["skillId"], {"skillId": "skill_other_332"})


def test_delete_permission_failure_builtin_disable_and_missing(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = registry.create_custom_skill(_skill("skill_delete_332"))
    target = registry.custom_skill_path(created["skillId"])
    original_unlink = Path.unlink

    def denied(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            raise PermissionError("locked")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(AppError, match="Cannot delete"):
        registry.delete_skill(created["skillId"])
    monkeypatch.setattr(Path, "unlink", original_unlink)
    result = registry.delete_skill("skill_code_review")
    assert result["disabled"] is True
    with pytest.raises(AppError, match="not found"):
        registry.delete_skill("skill_missing_delete_332")


def test_export_import_and_corrupt_custom_files(tmp_settings: Path) -> None:
    created = registry.create_custom_skill(_skill("skill_export_332"))
    exported = registry.export_skill_file(created["skillId"], tmp_settings / "nested" / "skill.json")
    assert Path(exported["path"]).is_file()
    imported = registry.import_skill_config(_skill("skill_import_config_332"))
    assert imported["skillId"] == "skill_import_config_332"

    bad = tmp_settings / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(AppError, match="JSON object"):
        registry.import_skill_file(bad)
    corrupt = registry.custom_skills_dir() / "skill_corrupt_file_332.json"
    corrupt.write_text("{", encoding="utf-8")
    assert all(item["skillId"] != "skill_corrupt_file_332" for item in registry.load_custom_skills())
    assert registry._read_json(corrupt) is None
    with pytest.raises(AppError, match="Invalid Skill file"):
        registry._load_skill_file(corrupt, builtin=False)


def test_disabled_ids_skip_invalid_values(tmp_settings: Path) -> None:
    registry.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    registry.disabled_skills_path().write_text(json.dumps(["skill_code_review", "bad id", "skill_code_review"]), encoding="utf-8")
    assert registry.disabled_skill_ids() == ["skill_code_review"]


def test_pack_missing_invalid_delete_and_corrupt_load(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="Pack not found"):
        registry.get_pack("pack_missing_332")
    with pytest.raises(AppError):
        registry.validate_pack_manifest({})

    pack = registry.import_pack(
        {"packId": "pack_delete_332", "name": "Delete", "description": "delete", "version": "1.0.0", "skills": [_skill("skill_pack_delete_332")]},
        overwrite=True,
    )
    target = registry.custom_pack_path(pack["packId"])
    original_unlink = Path.unlink

    def denied(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            raise PermissionError("locked")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(AppError, match="Cannot delete"):
        registry.delete_pack(pack["packId"])
    monkeypatch.setattr(Path, "unlink", original_unlink)
    with pytest.raises(AppError, match="read-only"):
        registry.delete_pack("pack_code")
    with pytest.raises(AppError, match="not found"):
        registry.delete_pack("pack_missing_delete_332")

    corrupt = registry.custom_packs_dir() / "pack_corrupt_332.json"
    corrupt.write_text("[]", encoding="utf-8")
    assert all(item["packId"] != "pack_corrupt_332" for item in registry.load_custom_packs())
    with pytest.raises(AppError, match="Invalid Skill Pack"):
        registry._load_pack_file(corrupt, builtin=False)


def test_pack_resolution_embedded_reference_and_unknown(tmp_settings: Path) -> None:
    embedded = _skill("skill_embedded_332")
    created = registry.create_custom_skill(_skill("skill_reference_332"))
    resolved = registry._resolve_pack_skills({"skills": [None, embedded, {"skillId": created["skillId"]}]})
    assert [item["skillId"] for item in resolved] == ["skill_embedded_332", "skill_reference_332"]
    with pytest.raises(AppError, match="unknown skillId"):
        registry._resolve_pack_skills({"skills": [{"skillId": "skill_unknown_332"}]})
    assert registry._unresolved_references({"skills": [None, embedded, {"skillId": "skill_unknown_332"}]}) == ["skill_unknown_332"]


def test_every_skill_registry_write_path_is_denied_once_python_is_de_authorized(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills_store` is a declared domain (python -> rust, 4.9.4), so the handover is mechanical.

    One scope covers every write into the capability registry —
    `registry.skill_store_scope()`, taken by `write_disabled_skill_ids`, `write_custom_skill`,
    `write_custom_pack`, `delete_skill`, `delete_pack`, `versioning.snapshot_skill`,
    `versioning.snapshot_pack`, `security._append_review` and `security._write_trust_store`. It sits
    at the store rather than at each call site because a custom Skill write alone reaches three of
    those sinks (the record, its history revision, its security review) and a gate at the route would
    have missed the two that the route never names.

    The run log, the catalog and the eval-case file share `.skills/` and are deliberately **not**
    gated: Rust's `Registry` writes none of them, so denying them would take away a store nobody has
    taken over — the other half of ADR-0049's "a cutover gate that has not passed leaves the prior
    owner authoritative; it does not permit dual writers".

    That sentence cuts both ways, so the default mode leaving every path working is asserted here
    too: a gate that fired early would be a regression, not caution.
    """
    from deepseek_infra.infra.native_runtime.authority import (
        PythonWriterMechanicallyDeniedError,
        RUST_DATA_DOMAINS,
    )
    from deepseek_infra.infra.skills import catalog, security

    assert "skills_store" in RUST_DATA_DOMAINS

    created = registry.create_custom_skill(_skill("skill_handover_332"))
    skill_id = created["skillId"]
    stores = {
        "the custom record": registry.custom_skill_path(skill_id),
        "a history revision": registry.SKILLS_DIR / "history" / skill_id,
        "the review log": registry.SKILLS_DIR / "security" / "reviews.jsonl",
        "the disabled list": registry.disabled_skills_path(),
        "the trust store": registry.SKILLS_DIR / "security" / "trust-store.json",
    }
    registry.set_skill_disabled(skill_id, True)
    security.trust_skill(skill_id)
    for label, path in stores.items():
        assert path.exists(), label
    catalog_manifest = registry.SKILLS_DIR / "catalog" / "catalog.json"
    catalog.catalog_refresh()
    assert catalog_manifest.is_file()
    written = registry.custom_skill_path(skill_id).read_text(encoding="utf-8")

    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    for write in (
        lambda: registry.create_custom_skill(_skill("skill_handover_denied_332")),
        lambda: registry.set_skill_disabled(skill_id, False),
        lambda: registry.delete_skill(skill_id),
        lambda: security.trust_skill(skill_id),
    ):
        with pytest.raises(PythonWriterMechanicallyDeniedError, match="Domain 'skills_store' write mutation is mechanically denied"):
            write()
    assert registry.custom_skill_path(skill_id).read_text(encoding="utf-8") == written
    assert not registry.custom_skill_path("skill_handover_denied_332").exists()
    assert catalog.catalog_refresh()["ok"] is True
