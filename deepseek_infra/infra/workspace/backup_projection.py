"""Projected restore planning (4.4.13).

A restore can be frozen into an explicit Contributor/Project selection. This
module is the pure, I/O-free planner: it validates a selection against chain
metadata, computes a canonical selection digest, runs the full logical
F0->I1->...->In chain (verifying every Merkle root), and derives the strict
``restoreOutputSet`` / ``restoreDependencySet`` split with a backward
dependency closure over cross-file ``parent-range`` references.

The metadata plane is always verified in full; only payload-byte
materialization is projected. Because this release keeps the whole-age-object
network model, every report carries ``networkSelective: false``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_incremental, backup_pack

PROJECTS_CONTRIBUTOR = "projects"
PROJECTS_PAYLOAD_PREFIX = "payload/projects/"
FRONTEND_CONTRIBUTOR = "frontend"
STATELESS_MCP_CONTRIBUTOR = "stateless-mcp"
FRONTEND_PATH_PREFIX = "frontend/"
MIGRATION_PATH_PREFIX = "migration/"

GRANULARITY_PROJECT = "project"
GRANULARITY_CONTRIBUTOR = "contributor"

NETWORK_SELECTIVITY_REASON = "whole-age-object"


def _stable_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def contributor_granularity(contributor_id: str) -> str:
    """Restore granularity a contributor can offer (``project`` or ``contributor``)."""
    return GRANULARITY_PROJECT if contributor_id == PROJECTS_CONTRIBUTOR else GRANULARITY_CONTRIBUTOR


def restore_projection_capabilities() -> dict[str, dict[str, str]]:
    """Per-contributor projection granularity for the capabilities surface."""
    known = ("projects", "project-files", "artifacts", "memory", "media", "custom-skills-and-packs", "automations", "reminders")
    optional = ("agent-checkpoints", "a2a-tasks", "traces-and-run-history")
    result: dict[str, dict[str, str]] = {}
    for contributor_id in (*known, *optional, FRONTEND_CONTRIBUTOR, STATELESS_MCP_CONTRIBUTOR):
        result[contributor_id] = {"granularity": contributor_granularity(contributor_id)}
    return result


@dataclass(frozen=True, slots=True)
class RestoreSelection:
    contributors: tuple[str, ...]
    project_ids: tuple[str, ...] = ()

    def canonical(self) -> dict[str, Any]:
        return {
            "contributors": sorted(self.contributors),
            "projectIds": sorted(self.project_ids),
        }


def normalize_selection(value: Any) -> RestoreSelection | None:
    """Coerce an API selection payload into a canonical, sorted selection."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AppError("Restore selection must be an object", code=ErrorCode.INVALID_REQUEST, status=409)
    raw_contributors = value.get("contributors")
    raw_projects = value.get("projectIds") or []
    if not isinstance(raw_contributors, list) or not raw_contributors:
        raise AppError("Restore selection requires at least one contributor", code=ErrorCode.INVALID_REQUEST, status=409)
    if not isinstance(raw_projects, list):
        raise AppError("Restore selection projectIds must be a list", code=ErrorCode.INVALID_REQUEST, status=409)
    contributors = tuple(sorted({str(item) for item in raw_contributors if str(item)}))
    project_ids = tuple(sorted({str(item) for item in raw_projects if str(item)}))
    if not contributors:
        raise AppError("Restore selection requires at least one contributor", code=ErrorCode.INVALID_REQUEST, status=409)
    return RestoreSelection(contributors=contributors, project_ids=project_ids)


def selection_digest(selection: RestoreSelection) -> str:
    """Canonical digest of the frozen selection, independent of chain content."""
    return hashlib.sha256(_stable_json(selection.canonical())).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainPackage:
    """Metadata-only view of one decrypted chain member (no payload bytes)."""

    snapshot_kind: str
    files: tuple[backup_incremental.FileRecord, ...]
    root_digest: str = ""
    operations: Mapping[str, Any] | None = None
    pack_index: Mapping[str, Any] | None = None
    frontend: bool = False
    contributor_ids: frozenset[str] = frozenset()
    external_mcp: bool = False


def _chain_roots(chain: Sequence[ChainPackage]) -> list[list[backup_incremental.FileRecord]]:
    """Apply the full logical chain and verify every available Merkle root.

    Returns the ordered effective file state after each member so the final
    member's state is the complete logical tree.
    """
    if not chain:
        raise AppError("Restore projection requires a non-empty chain", code=ErrorCode.INVALID_PAYLOAD)
    state: list[list[backup_incremental.FileRecord]] = []
    current = list(chain[0].files)
    if chain[0].root_digest and backup_incremental.snapshot_root(current) != chain[0].root_digest:
        raise AppError("Full baseline Merkle root mismatch", code=ErrorCode.INVALID_PAYLOAD)
    state.append(current)
    for index, package in enumerate(chain[1:], start=1):
        ops = package.operations
        if not isinstance(ops, dict):
            raise AppError(f"Incremental chain member {index} has no operations", code=ErrorCode.INVALID_PAYLOAD)
        parent = str(ops.get("parentRootDigest") or "")
        if parent and backup_incremental.snapshot_root(current) != parent:
            raise AppError(f"Incremental chain parent root mismatch at {index}", code=ErrorCode.INVALID_PAYLOAD)
        current = backup_incremental.apply_delta_ops(
            current,
            ops,
            successful_contributors={item.contributor_id for item in current},
        )
        root = str(ops.get("rootDigest") or package.root_digest)
        if root and backup_incremental.snapshot_root(current) != root:
            raise AppError(f"Incremental chain Merkle root mismatch at {index}", code=ErrorCode.INVALID_PAYLOAD)
        state.append(current)
    return state


def _known_contributors(chain: Sequence[ChainPackage]) -> set[str]:
    baseline = chain[0]
    known = set(baseline.contributor_ids)
    if baseline.frontend:
        known.add(FRONTEND_CONTRIBUTOR)
    if baseline.external_mcp:
        known.add(STATELESS_MCP_CONTRIBUTOR)
    return known


def _project_ids_in_baseline(chain: Sequence[ChainPackage]) -> set[str]:
    result: set[str] = set()
    for record in chain[0].files:
        path = PurePosixPath(record.logical_path)
        if len(path.parts) >= 3 and path.parts[:2] == ("payload", "projects"):
            result.add(path.parts[2])
    return result


def validate_selection(selection: RestoreSelection, chain: Sequence[ChainPackage]) -> list[str]:
    """Return human-readable errors, or an empty list when the selection is valid."""
    errors: list[str] = []
    if not selection.contributors:
        errors.append("selection is empty")
    known = _known_contributors(chain)
    unknown = sorted(set(selection.contributors) - known)
    if unknown:
        errors.append(f"selection includes unsupported contributors: {', '.join(unknown)}")
    if selection.project_ids and PROJECTS_CONTRIBUTOR not in selection.contributors:
        errors.append("selection projectIds require the projects contributor")
    if PROJECTS_CONTRIBUTOR in selection.contributors and selection.project_ids:
        available = _project_ids_in_baseline(chain)
        missing = sorted(set(selection.project_ids) - available)
        if missing:
            errors.append(f"selection includes unknown project ids: {', '.join(missing)}")
    return errors


def _under_selected_project(logical_path: str, project_ids: frozenset[str]) -> bool:
    parts = PurePosixPath(logical_path).parts
    if len(parts) < 3 or parts[:2] != ("payload", "projects"):
        return False
    return parts[2] in project_ids


def _selectable_files(final_files: list[backup_incremental.FileRecord], selection: RestoreSelection) -> set[str]:
    selected_contributors = set(selection.contributors)
    project_ids = frozenset(selection.project_ids)
    output: set[str] = set()
    for record in final_files:
        if record.contributor_id not in selected_contributors:
            continue
        if record.logical_path.startswith(MIGRATION_PATH_PREFIX):
            continue
        if record.contributor_id == PROJECTS_CONTRIBUTOR and project_ids:
            if not _under_selected_project(record.logical_path, project_ids):
                continue
        output.add(record.logical_path)
    return output


def _put_parent_dependencies(put: Mapping[str, Any]) -> set[str]:
    """Logical paths this PUT needs from the previous layer's tree."""
    deps: set[str] = set()
    storage = str(put.get("storage") or "whole")
    if storage == "parent-file":
        parent = str(put.get("parentPath") or "")
        if parent:
            deps.add(parent)
    elif storage == "cdc":
        path = str(put.get("path") or "")
        for chunk in put.get("chunks") or []:
            if not isinstance(chunk, dict):
                continue
            source = str(chunk.get("source") or "")
            if source == "parent":
                if path:
                    deps.add(path)
            elif source == "parent-range":
                parent = str(chunk.get("parentPath") or "")
                if parent:
                    deps.add(parent)
    return deps


def _closure(chain: Sequence[ChainPackage], output_set: set[str]) -> tuple[list[set[str]], list[set[str]]]:
    """Backward dependency closure over the chain.

    Returns ``(needed_after, produced_by_layer)`` where ``needed_after[k]`` is
    the set of logical paths that must exist in the tree after layer ``k`` and
    ``produced_by_layer[k]`` is the subset materialized by layer ``k``'s puts.
    """
    layer_count = len(chain)
    needed_after: list[set[str]] = [set() for _ in range(layer_count)]
    needed_after[layer_count - 1] = set(output_set)
    produced_by_layer: list[set[str]] = [set() for _ in range(layer_count)]
    for k in range(layer_count - 1, 0, -1):
        ops = chain[k].operations or {}
        puts_by_path = {str(item.get("path") or ""): item for item in ops.get("put") or [] if isinstance(item, dict)}
        deleted = {str(item.get("path") or "") for item in ops.get("delete") or [] if isinstance(item, dict)}
        next_needed: set[str] = set()
        for path in needed_after[k]:
            put = puts_by_path.get(path)
            if put is not None:
                next_needed |= _put_parent_dependencies(put)
                produced_by_layer[k].add(path)
            elif path in deleted:
                raise AppError(
                    f"Selected or required file is deleted by a later snapshot: {path}",
                    code=ErrorCode.INVALID_PAYLOAD,
                )
            else:
                next_needed.add(path)
        needed_after[k - 1] = next_needed
    baseline_paths = {record.logical_path for record in chain[0].files}
    missing = sorted(needed_after[0] - baseline_paths)
    if missing:
        raise AppError(
            f"Projection depends on baseline files that are not in the snapshot: {', '.join(missing)}",
            code=ErrorCode.INVALID_PAYLOAD,
        )
    return needed_after, produced_by_layer


def _payload_entries(
    chain: Sequence[ChainPackage],
    produced_by_layer: list[set[str]],
) -> tuple[set[str], set[str], set[str]]:
    """Resolve needed pack paths, blob ids and standalone payload paths."""
    packs: set[str] = set()
    blobs: set[str] = set()
    standalone: set[str] = set()

    def record_ref(raw: Any, index: Mapping[str, Any] | None) -> None:
        kind, locator = backup_pack.parse_payload_ref(raw)
        if kind == "pack-range":
            blobs.add(locator)
            entry = (index or {}).get("entries", {}).get(locator) if isinstance(index, dict) else None
            if isinstance(entry, dict) and str(entry.get("pack") or ""):
                packs.add(str(entry["pack"]))
        elif kind == "standalone":
            standalone.add(locator)

    for k in range(1, len(chain)):
        ops = chain[k].operations or {}
        index = chain[k].pack_index
        produced = produced_by_layer[k]
        for put in ops.get("put") or []:
            if not isinstance(put, dict) or str(put.get("path") or "") not in produced:
                continue
            storage = str(put.get("storage") or "whole")
            if storage == "cdc":
                for chunk in put.get("chunks") or []:
                    if isinstance(chunk, dict) and str(chunk.get("source") or "") == "payload":
                        record_ref(chunk.get("payloadRef"), index)
            elif storage == "whole":
                record_ref(put.get("payloadRef"), index)
    return packs, blobs, standalone


def _count_parent_ranges(chain: Sequence[ChainPackage], produced_by_layer: list[set[str]]) -> int:
    total = 0
    for k in range(1, len(chain)):
        ops = chain[k].operations or {}
        produced = produced_by_layer[k]
        for put in ops.get("put") or []:
            if not isinstance(put, dict) or str(put.get("path") or "") not in produced:
                continue
            if str(put.get("storage") or "") != "cdc":
                continue
            for chunk in put.get("chunks") or []:
                if isinstance(chunk, dict) and str(chunk.get("source") or "") == "parent-range":
                    total += 1
    return total


@dataclass(frozen=True, slots=True)
class ProjectionPlan:
    selection: RestoreSelection
    selection_digest: str
    output_files: frozenset[str]
    support_files: frozenset[str]
    needed_full_entries: frozenset[str]
    needed_packs: frozenset[str]
    needed_blobs: frozenset[str]
    needed_standalone: frozenset[str]
    needed_after_layer: tuple[frozenset[str], ...]
    produced_by_layer: tuple[frozenset[str], ...]
    requires_frontend_apply: bool
    requires_external_mcp: bool
    report: dict[str, Any]

    def with_selection_digest(self, digest: str) -> "ProjectionPlan":
        return ProjectionPlan(
            selection=self.selection,
            selection_digest=digest,
            output_files=self.output_files,
            support_files=self.support_files,
            needed_full_entries=self.needed_full_entries,
            needed_packs=self.needed_packs,
            needed_blobs=self.needed_blobs,
            needed_standalone=self.needed_standalone,
            needed_after_layer=self.needed_after_layer,
            produced_by_layer=self.produced_by_layer,
            requires_frontend_apply=self.requires_frontend_apply,
            requires_external_mcp=self.requires_external_mcp,
            report={**self.report, "selectionDigest": digest},
        )


def plan_projection(
    selection: RestoreSelection,
    chain: Sequence[ChainPackage],
    *,
    ciphertext_download_bytes: int,
    selection_digest_value: str | None = None,
) -> ProjectionPlan:
    """Plan a projected restore and emit the honest byte report.

    The full logical chain is applied and every Merkle root verified before any
    projection is computed; only payload materialization is selective.
    """
    errors = validate_selection(selection, chain)
    if errors:
        raise AppError("; ".join(errors), code=ErrorCode.INVALID_REQUEST, status=409)
    state = _chain_roots(chain)
    final_files = state[-1]
    output_set = _selectable_files(final_files, selection)
    if not output_set:
        raise AppError(
            "Restore selection matches no restorable files in this snapshot",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    needed_after, produced_by_layer = _closure(chain, output_set)
    packs, blobs, standalone = _payload_entries(chain, produced_by_layer)
    all_needed = set().union(*needed_after) if needed_after else set()
    support_files = all_needed - output_set
    final_by_path = {record.logical_path: record for record in final_files}
    selected_logical_bytes = sum(int(final_by_path[path].size) for path in output_set if path in final_by_path)
    dependency_bytes = sum(int(final_by_path[path].size) for path in support_files if path in final_by_path)
    estimated_materialized = selected_logical_bytes + dependency_bytes
    baseline_files = frozenset(needed_after[0])
    report = {
        "selectionDigest": selection_digest_value or selection_digest(selection),
        "selected": {
            "contributors": len(selection.contributors),
            "projects": len(selection.project_ids),
            "files": len(output_set),
        },
        "dependencies": {
            "supportFiles": len(support_files),
            "parentRanges": _count_parent_ranges(chain, produced_by_layer),
            "packs": len(packs),
        },
        "bytes": {
            "selectedLogicalBytes": selected_logical_bytes,
            "dependencyBytes": dependency_bytes,
            "ciphertextDownloadBytes": max(0, int(ciphertext_download_bytes)),
            "estimatedMaterializedBytes": estimated_materialized,
        },
        "networkSelective": False,
        "networkSelectivityReason": NETWORK_SELECTIVITY_REASON,
        "requiresFrontendApply": FRONTEND_CONTRIBUTOR in selection.contributors,
        "requiresExternalMcp": STATELESS_MCP_CONTRIBUTOR in selection.contributors,
    }
    return ProjectionPlan(
        selection=selection,
        selection_digest=str(report["selectionDigest"]),
        output_files=frozenset(output_set),
        support_files=frozenset(support_files),
        needed_full_entries=baseline_files,
        needed_packs=frozenset(packs),
        needed_blobs=frozenset(blobs),
        needed_standalone=frozenset(standalone),
        needed_after_layer=tuple(frozenset(level) for level in needed_after),
        produced_by_layer=tuple(frozenset(level) for level in produced_by_layer),
        requires_frontend_apply=bool(report["requiresFrontendApply"]),
        requires_external_mcp=bool(report["requiresExternalMcp"]),
        report=report,
    )
