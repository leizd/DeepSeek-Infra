"""Strongly bound, reusable Projection plans for object-set recovery."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_projection

PLANNER_SCHEMA_VERSION = 1
PLAN_NAME = "verified-plan.json"


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _metadata_digest(path: Path) -> str:
    records: list[dict[str, Any]] = []
    if not path.is_dir():
        return ""
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = file_path.relative_to(path).as_posix()
        hasher = hashlib.sha256()
        size = 0
        try:
            with file_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    hasher.update(chunk)
        except OSError:
            return ""
        records.append({"path": relative, "sha256": hasher.hexdigest(), "size": size})
    return hashlib.sha256(_stable_json(records)).hexdigest() if records else ""


def _chain(session: dict[str, Any]) -> list[dict[str, Any]]:
    raw = session.get("chain")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _bindings(base: Path, session: dict[str, Any]) -> dict[str, Any]:
    chain = _chain(session)
    object_sets = [str(member.get("objectSetDigest") or "") for member in chain]
    controls = [str(member.get("controlObjectDigest") or (member.get("control") or {}).get("objectDigest") or "") for member in chain]
    required: list[dict[str, Any]] = []
    for member in chain:
        for component in member.get("requiredComponents") or []:
            if not isinstance(component, dict):
                continue
            raw_priority = component.get("priority")
            required.append(
                {
                    "componentId": str(component.get("componentId") or ""),
                    "expectedBytes": int(component.get("expectedBytes") or 0),
                    "objectDigest": str(component.get("objectDigest") or ""),
                    "plaintextSha256": str(component.get("plaintextSha256") or ""),
                    "plaintextSize": int(component.get("plaintextSize") or 0),
                    "priority": raw_priority if isinstance(raw_priority, int) else 2,
                }
            )
    chain_digest = hashlib.sha256(_stable_json(object_sets)).hexdigest()
    return {
        "chainDigest": chain_digest,
        "controlDigests": controls,
        "metadataDigests": [_metadata_digest(base / f"metadata-{index}") for index in range(len(chain))],
        "objectSetDigests": object_sets,
        "requiredComponents": required,
        "selectionDigest": str(session.get("selectionDigest") or ""),
    }


def _serialize_projection(plan: backup_projection.ProjectionPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "neededAfterLayer": [sorted(level) for level in plan.needed_after_layer],
        "neededBlobs": sorted(plan.needed_blobs),
        "neededFullEntries": sorted(plan.needed_full_entries),
        "neededPacks": sorted(plan.needed_packs),
        "neededStandalone": sorted(plan.needed_standalone),
        "outputFiles": sorted(plan.output_files),
        "producedByLayer": [sorted(level) for level in plan.produced_by_layer],
        "requiresExternalMcp": plan.requires_external_mcp,
        "requiresFrontendApply": plan.requires_frontend_apply,
        "selection": plan.selection.canonical(),
        "selectionDigest": plan.selection_digest,
        "supportFiles": sorted(plan.support_files),
    }


def _deserialize_projection(payload: Any, report: dict[str, Any]) -> backup_projection.ProjectionPlan | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("projection must be an object")
    selection = backup_projection.normalize_selection(payload.get("selection"))
    if selection is None:
        raise ValueError("projection selection is missing")

    def _strings(name: str) -> frozenset[str]:
        raw = payload.get(name)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError(f"projection {name} is invalid")
        return frozenset(raw)

    def _levels(name: str) -> tuple[frozenset[str], ...]:
        raw = payload.get(name)
        if not isinstance(raw, list) or any(not isinstance(level, list) for level in raw):
            raise ValueError(f"projection {name} is invalid")
        result: list[frozenset[str]] = []
        for level in raw:
            if any(not isinstance(item, str) for item in level):
                raise ValueError(f"projection {name} is invalid")
            result.append(frozenset(level))
        return tuple(result)

    return backup_projection.ProjectionPlan(
        selection=selection,
        selection_digest=str(payload.get("selectionDigest") or ""),
        output_files=_strings("outputFiles"),
        support_files=_strings("supportFiles"),
        needed_full_entries=_strings("neededFullEntries"),
        needed_packs=_strings("neededPacks"),
        needed_blobs=_strings("neededBlobs"),
        needed_standalone=_strings("neededStandalone"),
        needed_after_layer=_levels("neededAfterLayer"),
        produced_by_layer=_levels("producedByLayer"),
        requires_frontend_apply=bool(payload.get("requiresFrontendApply")),
        requires_external_mcp=bool(payload.get("requiresExternalMcp")),
        report=report,
    )


def write_verified_plan(
    base: Path,
    session: dict[str, Any],
    projection: backup_projection.ProjectionPlan | None,
    report: dict[str, Any],
) -> Path:
    body = {
        "bindings": _bindings(base, session),
        "plannerSchemaVersion": PLANNER_SCHEMA_VERSION,
        "projection": _serialize_projection(projection),
        "report": report,
    }
    payload = {"body": body, "bodySha256": hashlib.sha256(_stable_json(body)).hexdigest()}
    path = base / PLAN_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def load_verified_plan(
    base: Path,
    session: dict[str, Any],
) -> tuple[backup_projection.ProjectionPlan | None, dict[str, Any]] | None:
    try:
        payload = json.loads((base / PLAN_NAME).read_text(encoding="utf-8"))
        body = payload.get("body") if isinstance(payload, dict) else None
        body_digest = payload.get("bodySha256") if isinstance(payload, dict) else None
        if not isinstance(body, dict) or body_digest != hashlib.sha256(_stable_json(body)).hexdigest():
            return None
        current_bindings = _bindings(base, session)
        metadata_digests = current_bindings.get("metadataDigests")
        if (
            body.get("plannerSchemaVersion") != PLANNER_SCHEMA_VERSION
            or body.get("bindings") != current_bindings
            or not isinstance(metadata_digests, list)
            or any(not digest for digest in metadata_digests)
        ):
            return None
        report = body.get("report")
        if not isinstance(report, dict):
            return None
        projection = _deserialize_projection(body.get("projection"), report)
        selection_digest = str(current_bindings.get("selectionDigest") or "")
        if projection is not None and (
            projection.selection_digest != selection_digest
            or str(report.get("selectionDigest") or "") != selection_digest
        ):
            return None
        return projection, report
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
