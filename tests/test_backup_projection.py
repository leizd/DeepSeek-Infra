"""Offline contracts for the projected-recovery projected restore planner."""

from __future__ import annotations

import hashlib

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_incremental
from deepseek_infra.infra.workspace.backup_projection import (
    ChainPackage,
    RestoreSelection,
    contributor_granularity,
    normalize_selection,
    plan_projection,
    restore_projection_capabilities,
    selection_digest,
    validate_selection,
)


def _rec(contributor: str, path: str, size: int, sha: str) -> backup_incremental.FileRecord:
    return backup_incremental.FileRecord(contributor, path, size, sha)


def _sha(seed: int) -> str:
    return hashlib.sha256(f"seed-{seed}".encode("utf-8")).hexdigest()


def _baseline() -> tuple[list[backup_incremental.FileRecord], ChainPackage]:
    files = [
        _rec("projects", "payload/projects/p1/cache.bin", 100, _sha(1)),
        _rec("projects", "payload/projects/p2/main.bin", 50, _sha(2)),
        _rec("memory", "payload/memory/memories.json", 10, _sha(3)),
    ]
    package = ChainPackage(
        snapshot_kind="full",
        files=tuple(files),
        root_digest=backup_incremental.snapshot_root(files),
        contributor_ids=frozenset({"projects", "memory"}),
    )
    return files, package


def test_contributor_granularity() -> None:
    assert contributor_granularity("projects") == "project"
    assert contributor_granularity("memory") == "contributor"
    assert contributor_granularity("frontend") == "contributor"
    assert contributor_granularity("stateless-mcp") == "contributor"
    caps = restore_projection_capabilities()
    assert caps["projects"]["granularity"] == "project"
    assert caps["memory"]["granularity"] == "contributor"


def test_capabilities_expose_restore_projection() -> None:
    from deepseek_infra.infra.workspace import backups

    projection = backups.capabilities()["restoreProjection"]
    assert projection["projects"]["granularity"] == "project"
    assert projection["memory"]["granularity"] == "contributor"


def test_normalize_selection_sorts_and_dedups() -> None:
    selection = normalize_selection({"contributors": ["memory", "projects", "projects"], "projectIds": ["p2", "p1"]})
    assert selection is not None
    assert selection.contributors == ("memory", "projects")
    assert selection.project_ids == ("p1", "p2")


def test_normalize_selection_rejects_empty_and_bad_shapes() -> None:
    assert normalize_selection(None) is None
    with pytest.raises(AppError):
        normalize_selection({"contributors": []})
    with pytest.raises(AppError):
        normalize_selection({"projectIds": ["p1"]})
    with pytest.raises(AppError):
        normalize_selection({"contributors": ["projects"], "projectIds": "p1"})


def test_selection_digest_is_canonical_and_distinct() -> None:
    a = RestoreSelection(contributors=("projects", "memory"), project_ids=("p1", "p2"))
    b = RestoreSelection(contributors=("memory", "projects"), project_ids=("p2", "p1"))
    c = RestoreSelection(contributors=("projects", "memory"), project_ids=("p1",))
    assert selection_digest(a) == selection_digest(b)
    assert len(selection_digest(a)) == 64
    assert selection_digest(a) != selection_digest(c)


def test_validate_selection_unknown_contributor_and_project() -> None:
    _, baseline = _baseline()
    errors = validate_selection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [baseline])
    assert errors == []
    unknown = validate_selection(RestoreSelection(contributors=("nope",)), [baseline])
    assert any("unsupported contributors" in error for error in unknown)
    missing_project = validate_selection(RestoreSelection(contributors=("projects",), project_ids=("missing",)), [baseline])
    assert any("unknown project ids" in error for error in missing_project)
    no_projects = validate_selection(RestoreSelection(contributors=("memory",), project_ids=("p1",)), [baseline])
    assert any("require the projects contributor" in error for error in no_projects)
    assert validate_selection(RestoreSelection(contributors=(),), [baseline])


def test_plan_projection_accepts_project_created_after_full_baseline() -> None:
    f0 = [_rec("projects", "payload/projects/p1/base.bin", 8, _sha(5))]
    created_sha = _sha(6)
    f1 = [*f0, _rec("projects", "payload/projects/p2/created.bin", 24, created_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p2/created.bin":
            put["storage"] = "whole"
            put["payloadRef"] = {"kind": "standalone", "path": "payload/files/created.bin"}
    baseline = ChainPackage(
        snapshot_kind="full",
        files=tuple(f0),
        root_digest=backup_incremental.snapshot_root(f0),
        contributor_ids=frozenset({"projects"}),
    )
    incremental = ChainPackage(
        snapshot_kind="incremental",
        files=tuple(f1),
        operations=ops,
        root_digest=backup_incremental.snapshot_root(f1),
        contributor_ids=frozenset({"projects"}),
    )

    plan = plan_projection(
        RestoreSelection(contributors=("projects",), project_ids=("p2",)),
        [baseline, incremental],
        ciphertext_download_bytes=0,
    )

    assert plan.output_files == frozenset({"payload/projects/p2/created.bin"})
    assert plan.needed_standalone == frozenset({"payload/files/created.bin"})


def test_validate_selection_frontend_and_mcp_are_conditional() -> None:
    files, baseline = _baseline()
    plain = ChainPackage(
        snapshot_kind="full",
        files=tuple(files),
        root_digest=backup_incremental.snapshot_root(files),
        contributor_ids=frozenset({"projects", "memory"}),
    )
    assert any("unsupported" in e for e in validate_selection(RestoreSelection(contributors=("frontend",)), [plain]))
    assert any("unsupported" in e for e in validate_selection(RestoreSelection(contributors=("stateless-mcp",)), [plain]))
    with_frontend = ChainPackage(
        snapshot_kind="full",
        files=(*files, _rec("frontend", "frontend/sealed-state.age", 5, _sha(4))),
        root_digest=backup_incremental.snapshot_root(files),
        contributor_ids=frozenset({"projects", "memory"}),
        frontend=True,
        external_mcp=True,
    )
    assert validate_selection(RestoreSelection(contributors=("frontend", "stateless-mcp")), [with_frontend]) == []


def test_plan_projection_project_and_contributor_scope() -> None:
    _, baseline = _baseline()
    plan = plan_projection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [baseline], ciphertext_download_bytes=1000)
    assert plan.output_files == frozenset({"payload/projects/p1/cache.bin"})
    assert plan.support_files == frozenset()
    assert plan.report["bytes"]["selectedLogicalBytes"] == 100
    assert plan.report["bytes"]["dependencyBytes"] == 0

    whole_projects = plan_projection(RestoreSelection(contributors=("projects",)), [baseline], ciphertext_download_bytes=0)
    assert whole_projects.output_files == frozenset({"payload/projects/p1/cache.bin", "payload/projects/p2/main.bin"})

    with_memory = plan_projection(
        RestoreSelection(contributors=("projects", "memory"), project_ids=("p1",)),
        [baseline],
        ciphertext_download_bytes=0,
    )
    assert "payload/memory/memories.json" in with_memory.output_files


def _cross_file_parent_range_chain() -> tuple[ChainPackage, ChainPackage, dict[str, object]]:
    f0 = [
        _rec("projects", "payload/projects/p2/source.bin", 50, _sha(10)),
        _rec("projects", "payload/projects/p1/keep.bin", 10, _sha(11)),
    ]
    restored_sha = _sha(12)
    f1 = [*f0, _rec("projects", "payload/projects/p1/restored.bin", 40, restored_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/restored.bin":
            put["storage"] = "cdc"
            put["chunks"] = [
                {
                    "source": "parent-range",
                    "parentPath": "payload/projects/p2/source.bin",
                    "offset": 0,
                    "length": 40,
                    "sha256": restored_sha,
                }
            ]
            put.pop("payloadRef", None)
    baseline = ChainPackage(
        snapshot_kind="full",
        files=tuple(f0),
        root_digest=backup_incremental.snapshot_root(f0),
        contributor_ids=frozenset({"projects"}),
    )
    incremental = ChainPackage(
        snapshot_kind="incremental",
        files=tuple(f1),
        operations=ops,
        root_digest=backup_incremental.snapshot_root(f1),
    )
    return baseline, incremental, ops


def test_projection_cross_file_parent_range_closure() -> None:
    baseline, incremental, _ = _cross_file_parent_range_chain()
    plan = plan_projection(
        RestoreSelection(contributors=("projects",), project_ids=("p1",)),
        [baseline, incremental],
        ciphertext_download_bytes=2048,
    )
    assert plan.output_files == frozenset({"payload/projects/p1/keep.bin", "payload/projects/p1/restored.bin"})
    assert plan.support_files == frozenset({"payload/projects/p2/source.bin"})
    assert "payload/projects/p2/source.bin" not in plan.output_files
    assert plan.needed_full_entries == frozenset({"payload/projects/p1/keep.bin", "payload/projects/p2/source.bin"})
    assert plan.produced_by_layer[1] == frozenset({"payload/projects/p1/restored.bin"})
    assert plan.report["dependencies"]["supportFiles"] == 1
    assert plan.report["dependencies"]["parentRanges"] == 1
    assert plan.report["bytes"]["selectedLogicalBytes"] == 50
    assert plan.report["bytes"]["dependencyBytes"] == 50


def test_projection_verifies_full_logical_chain() -> None:
    baseline, incremental, ops = _cross_file_parent_range_chain()
    selection = RestoreSelection(contributors=("projects",), project_ids=("p1",))
    plan_projection(selection, [baseline, incremental], ciphertext_download_bytes=0)
    tampered_root = dict(ops)
    tampered_root["rootDigest"] = "0" * 64
    bad_incremental = ChainPackage(
        snapshot_kind="incremental",
        files=incremental.files,
        operations=tampered_root,
        root_digest=incremental.root_digest,
    )
    with pytest.raises(AppError, match="Merkle root mismatch"):
        plan_projection(selection, [baseline, bad_incremental], ciphertext_download_bytes=0)
    tampered_parent = dict(ops)
    tampered_parent["parentRootDigest"] = "0" * 64
    bad_parent = ChainPackage(
        snapshot_kind="incremental",
        files=incremental.files,
        operations=tampered_parent,
        root_digest=incremental.root_digest,
    )
    with pytest.raises(AppError, match="parent root mismatch"):
        plan_projection(selection, [baseline, bad_parent], ciphertext_download_bytes=0)


def test_projection_needed_packs_and_blobs() -> None:
    f0 = [_rec("projects", "payload/projects/p1/a.bin", 8, _sha(20))]
    blob_sha = _sha(21)
    pack_index = {"schemaVersion": 1, "packs": [{"path": "payload/packs/0000.pack", "size": 32, "sha256": _sha(22)}], "entries": {"blob_000000": {"pack": "payload/packs/0000.pack", "offset": 0, "length": 24, "sha256": blob_sha}}}
    f1 = [*f0, _rec("projects", "payload/projects/p1/b.bin", 24, blob_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/b.bin":
            put["storage"] = "whole"
            put["payloadRef"] = {"kind": "pack-range", "blobId": "blob_000000"}
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops, root_digest=backup_incremental.snapshot_root(f1), pack_index=pack_index)
    plan = plan_projection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [baseline, incremental], ciphertext_download_bytes=0)
    assert plan.needed_packs == frozenset({"payload/packs/0000.pack"})
    assert plan.needed_blobs == frozenset({"blob_000000"})
    assert plan.needed_standalone == frozenset()


def test_projection_needed_standalone() -> None:
    f0 = [_rec("projects", "payload/projects/p1/a.bin", 8, _sha(30))]
    blob_sha = _sha(31)
    f1 = [*f0, _rec("projects", "payload/projects/p1/big.bin", 20 * 1024 * 1024, blob_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/big.bin":
            put["storage"] = "whole"
            put["payloadRef"] = {"kind": "standalone", "path": "payload/files/000000"}
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops, root_digest=backup_incremental.snapshot_root(f1))
    plan = plan_projection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [baseline, incremental], ciphertext_download_bytes=0)
    assert plan.needed_standalone == frozenset({"payload/files/000000"})
    assert plan.needed_packs == frozenset()


def test_projection_network_report_is_honest() -> None:
    _, baseline = _baseline()
    plan = plan_projection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [baseline], ciphertext_download_bytes=123456)
    assert plan.report["networkSelective"] is False
    assert plan.report["networkSelectivityReason"] == "whole-age-object"
    assert plan.report["bytes"]["ciphertextDownloadBytes"] == 123456
    assert plan.report["selectionDigest"] == selection_digest(plan.selection)


def test_projection_derives_frontend_and_mcp_requirements() -> None:
    files = [
        _rec("projects", "payload/projects/p1/a.bin", 8, _sha(40)),
        _rec("frontend", "frontend/sealed-state.age", 5, _sha(41)),
    ]
    baseline = ChainPackage(
        snapshot_kind="full",
        files=tuple(files),
        root_digest=backup_incremental.snapshot_root(files),
        contributor_ids=frozenset({"projects"}),
        frontend=True,
        external_mcp=True,
    )
    plan = plan_projection(
        RestoreSelection(contributors=("projects", "frontend"), project_ids=("p1",)),
        [baseline],
        ciphertext_download_bytes=0,
    )
    assert plan.requires_frontend_apply is True
    assert plan.requires_external_mcp is False
    assert "frontend/sealed-state.age" in plan.output_files
    without_frontend = plan_projection(
        RestoreSelection(contributors=("projects",), project_ids=("p1",)),
        [baseline],
        ciphertext_download_bytes=0,
    )
    assert without_frontend.requires_frontend_apply is False


def test_projection_empty_match_raises() -> None:
    files = [_rec("projects", "payload/projects/p1/a.bin", 8, _sha(45))]
    baseline = ChainPackage(
        snapshot_kind="full",
        files=tuple(files),
        root_digest=backup_incremental.snapshot_root(files),
        contributor_ids=frozenset({"projects"}),
        external_mcp=True,
    )
    with pytest.raises(AppError, match="matches no restorable files"):
        plan_projection(RestoreSelection(contributors=("stateless-mcp",)), [baseline], ciphertext_download_bytes=0)
    with pytest.raises(AppError, match="selection is empty"):
        plan_projection(RestoreSelection(contributors=()), [baseline], ciphertext_download_bytes=0)


def test_projection_deleted_selected_file_raises() -> None:
    gone_sha = _sha(50)
    f0 = [_rec("projects", "payload/projects/p1/gone.bin", 8, gone_sha)]
    f1: list[backup_incremental.FileRecord] = []
    ops_delete = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    restored_sha = _sha(51)
    f2 = [_rec("projects", "payload/projects/p1/restored.bin", 8, restored_sha)]
    ops_restore = backup_incremental.diff_trees(f1, f2, successful_contributors={"projects"})
    for put in ops_restore["put"]:
        if put["path"] == "payload/projects/p1/restored.bin":
            put["storage"] = "cdc"
            put["chunks"] = [{"source": "parent-range", "parentPath": "payload/projects/p1/gone.bin", "offset": 0, "length": 8, "sha256": restored_sha}]
            put.pop("payloadRef", None)
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental1 = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops_delete, root_digest=backup_incremental.snapshot_root(f1))
    incremental2 = ChainPackage(snapshot_kind="incremental", files=tuple(f2), operations=ops_restore, root_digest=backup_incremental.snapshot_root(f2))
    with pytest.raises(AppError, match="deleted by a later snapshot"):
        plan_projection(
            RestoreSelection(contributors=("projects",), project_ids=("p1",)),
            [baseline, incremental1, incremental2],
            ciphertext_download_bytes=0,
        )


def test_projection_baseline_missing_support_raises() -> None:
    f0 = [_rec("projects", "payload/projects/p1/keep.bin", 8, _sha(60))]
    restored_sha = _sha(61)
    f1 = [*f0, _rec("projects", "payload/projects/p1/restored.bin", 8, restored_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/restored.bin":
            put["storage"] = "cdc"
            put["chunks"] = [{"source": "parent-range", "parentPath": "payload/projects/p2/missing.bin", "offset": 0, "length": 8, "sha256": restored_sha}]
            put.pop("payloadRef", None)
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops, root_digest=backup_incremental.snapshot_root(f1))
    with pytest.raises(AppError, match="not in the snapshot"):
        plan_projection(
            RestoreSelection(contributors=("projects",), project_ids=("p1",)),
            [baseline, incremental],
            ciphertext_download_bytes=0,
        )


def test_projection_cdc_same_file_parent_closure() -> None:
    parent_sha = _sha(70)
    f0 = [_rec("projects", "payload/projects/p1/big.bin", 100, parent_sha)]
    final_sha = _sha(71)
    f1 = [*f0, _rec("projects", "payload/projects/p1/big.bin", 120, final_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/big.bin":
            put["storage"] = "cdc"
            put["chunks"] = [
                {"source": "parent", "parentOrdinal": 0, "length": 100, "sha256": parent_sha},
                {"source": "payload", "payloadRef": {"kind": "pack-range", "blobId": "blob_000001"}, "length": 20, "sha256": _sha(72)},
            ]
            put.pop("payloadRef", None)
    pack_index = {"schemaVersion": 1, "packs": [{"path": "payload/packs/0000.pack", "size": 24, "sha256": _sha(73)}], "entries": {"blob_000001": {"pack": "payload/packs/0000.pack", "offset": 0, "length": 20, "sha256": _sha(72)}}}
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops, root_digest=backup_incremental.snapshot_root(f1), pack_index=pack_index)
    plan = plan_projection(
        RestoreSelection(contributors=("projects",), project_ids=("p1",)),
        [baseline, incremental],
        ciphertext_download_bytes=0,
    )
    assert "payload/projects/p1/big.bin" in plan.needed_after_layer[0]
    assert "payload/projects/p1/big.bin" in plan.produced_by_layer[1]
    assert plan.needed_blobs == frozenset({"blob_000001"})


def test_normalize_selection_rejects_non_object_and_all_blank() -> None:
    with pytest.raises(AppError, match="must be an object"):
        normalize_selection("projects")
    with pytest.raises(AppError, match="at least one contributor"):
        normalize_selection({"contributors": [""]})


def test_plan_projection_rejects_empty_chain_and_bad_baseline_root() -> None:
    from deepseek_infra.infra.workspace.backup_projection import _chain_roots

    with pytest.raises(AppError, match="non-empty chain"):
        _chain_roots([])
    bad_baseline = ChainPackage(
        snapshot_kind="full",
        files=(_rec("projects", "payload/projects/p1/a.bin", 4, _sha(80)),),
        root_digest="0" * 64,
        contributor_ids=frozenset({"projects"}),
    )
    with pytest.raises(AppError, match="Full baseline Merkle root mismatch"):
        plan_projection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [bad_baseline], ciphertext_download_bytes=0)


def test_plan_projection_rejects_incremental_without_operations() -> None:
    baseline = ChainPackage(
        snapshot_kind="full",
        files=(_rec("projects", "payload/projects/p1/a.bin", 4, _sha(81)),),
        root_digest=backup_incremental.snapshot_root([_rec("projects", "payload/projects/p1/a.bin", 4, _sha(81))]),
        contributor_ids=frozenset({"projects"}),
    )
    incremental = ChainPackage(snapshot_kind="incremental", files=(_rec("projects", "payload/projects/p1/b.bin", 4, _sha(82)),), operations=None)
    with pytest.raises(AppError, match="has no operations"):
        plan_projection(
            RestoreSelection(contributors=("projects",), project_ids=("p1",)),
            [baseline, incremental],
            ciphertext_download_bytes=0,
        )


def test_projection_parent_file_closure_and_migration_skip() -> None:
    parent_sha = _sha(83)
    base_sha = _sha(88)
    f0 = [
        _rec("projects", "payload/projects/p2/source.bin", 4, parent_sha),
        _rec("projects", "payload/projects/p1/base.bin", 4, base_sha),
    ]
    final_sha = _sha(84)
    f1 = [*f0, _rec("projects", "payload/projects/p1/copy.bin", 4, final_sha)]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/copy.bin":
            put["storage"] = "parent-file"
            put["parentPath"] = "payload/projects/p2/source.bin"
            put.pop("payloadRef", None)
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops, root_digest=backup_incremental.snapshot_root(f1))
    plan = plan_projection(
        RestoreSelection(contributors=("projects",), project_ids=("p1",)),
        [baseline, incremental],
        ciphertext_download_bytes=0,
    )
    assert "payload/projects/p1/copy.bin" in plan.output_files
    assert "payload/projects/p2/source.bin" in plan.support_files


def test_projection_selectable_skips_migration_files() -> None:
    from deepseek_infra.infra.workspace.backup_projection import _selectable_files

    files = [
        _rec("projects", "payload/projects/p1/a.bin", 4, _sha(85)),
        _rec("migration", "migration/source-schemas.json", 4, _sha(86)),
    ]
    output = _selectable_files(files, RestoreSelection(contributors=("projects", "migration"), project_ids=("p1",)))
    assert output == {"payload/projects/p1/a.bin"}


def test_layer_needed_payloads_boundaries() -> None:
    from deepseek_infra.infra.workspace.backup_projection import layer_needed_payloads

    baseline = ChainPackage(
        snapshot_kind="full",
        files=(_rec("projects", "payload/projects/p1/a.bin", 4, _sha(87)),),
        root_digest=backup_incremental.snapshot_root([_rec("projects", "payload/projects/p1/a.bin", 4, _sha(87))]),
        contributor_ids=frozenset({"projects"}),
    )
    assert layer_needed_payloads([baseline], [set()], 0) == (set(), set(), set())
    assert layer_needed_payloads([baseline], [set()], 1) == (set(), set(), set())


def test_projection_private_edge_branches() -> None:
    from deepseek_infra.infra.workspace.backup_projection import (
        _count_parent_ranges,
        _put_parent_dependencies,
        _under_selected_project,
        layer_needed_payloads,
    )

    # parent-file with no parentPath contributes no dependency.
    assert _put_parent_dependencies({"storage": "parent-file"}) == set()
    # CDC chunks that are not dicts or carry empty sources are skipped.
    assert _put_parent_dependencies({"storage": "cdc", "path": "p", "chunks": [{"source": "parent"}]}) == {"p"}
    assert _put_parent_dependencies({"storage": "cdc", "chunks": [{"source": "parent"}]}) == set()
    assert _put_parent_dependencies({"storage": "cdc", "path": "p", "chunks": [{"source": "parent-range"}]}) == set()
    assert _put_parent_dependencies({"storage": "cdc", "path": "p", "chunks": ["junk", {"source": "parent-range", "parentPath": "q"}]}) == {"q"}
    # whole storage has no parent dependencies.
    assert _put_parent_dependencies({"storage": "whole", "payloadRef": "x"}) == set()
    # Projects paths with too few parts are never selected by project id.
    assert _under_selected_project("payload/projects", frozenset({"p1"})) is False

    blob_sha = _sha(90)
    f0 = [_rec("projects", "payload/projects/p1/a.bin", 4, _sha(91))]
    f1 = [
        _rec("projects", "payload/projects/p1/a.bin", 4, _sha(91)),
        _rec("projects", "payload/projects/p1/cdc.bin", 4, blob_sha),
        _rec("projects", "payload/projects/p1/whole.bin", 4, _sha(92)),
    ]
    ops = backup_incremental.diff_trees(f0, f1, successful_contributors={"projects"})
    for put in ops["put"]:
        if put["path"] == "payload/projects/p1/cdc.bin":
            put["storage"] = "cdc"
            put["chunks"] = [
                {"source": "payload", "payloadRef": {"kind": "standalone", "path": "payload/files/000000"}, "length": 4, "sha256": blob_sha},
                {"source": "payload", "payloadRef": {"kind": "pack-range", "blobId": "blob_000000"}, "length": 4, "sha256": blob_sha},
            ]
            put.pop("payloadRef", None)
        elif put["path"] == "payload/projects/p1/whole.bin":
            put["storage"] = "whole"
            put["payloadRef"] = {"kind": "pack-range", "blobId": "blob_missing"}
    pack_index = {"schemaVersion": 1, "packs": [{"path": "payload/packs/0000.pack", "size": 8, "sha256": _sha(93)}], "entries": {"blob_000000": {"pack": "payload/packs/0000.pack", "offset": 0, "length": 4, "sha256": blob_sha}}}
    baseline = ChainPackage(snapshot_kind="full", files=tuple(f0), root_digest=backup_incremental.snapshot_root(f0), contributor_ids=frozenset({"projects"}))
    incremental = ChainPackage(snapshot_kind="incremental", files=tuple(f1), operations=ops, root_digest=backup_incremental.snapshot_root(f1), pack_index=pack_index)
    produced = [set(), {"payload/projects/p1/cdc.bin", "payload/projects/p1/whole.bin"}]
    packs, blobs, standalone = layer_needed_payloads([baseline, incremental], produced, 1)
    assert blobs == {"blob_000000", "blob_missing"}
    assert standalone == {"payload/files/000000"}
    assert packs == {"payload/packs/0000.pack"}
    # A produced set that omits the put path contributes nothing.
    assert layer_needed_payloads([baseline, incremental], [set(), set()], 1) == (set(), set(), set())
    # _count_parent_ranges skips non-produced and non-cdc entries.
    assert _count_parent_ranges([baseline, incremental], produced) == 0


def test_projection_plan_with_selection_digest_override() -> None:
    _, baseline = _baseline()
    plan = plan_projection(RestoreSelection(contributors=("projects",), project_ids=("p1",)), [baseline], ciphertext_download_bytes=0)
    overridden = plan.with_selection_digest("0" * 64)
    assert overridden.selection_digest == "0" * 64
    assert overridden.report["selectionDigest"] == "0" * 64
    assert plan.selection_digest != overridden.selection_digest
