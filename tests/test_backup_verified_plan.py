from __future__ import annotations

import hashlib
import json
from pathlib import Path

from deepseek_infra.infra.workspace import backup_projection, backup_verified_plan


def _session(base: Path) -> dict[str, object]:
    metadata = base / "metadata-0"
    metadata.mkdir(parents=True)
    (metadata / "manifest.json").write_text('{"backupId":"backup-plan"}', encoding="utf-8")
    return {
        "restoreId": "restore-plan",
        "selectionDigest": "a" * 64,
        "chain": [
            {
                "backupId": "backup-plan",
                "objectSetDigest": "b" * 64,
                "controlObjectDigest": "c" * 64,
                "control": {"objectDigest": "c" * 64},
                "requiredComponents": [
                    {
                        "componentId": "p0001",
                        "objectDigest": "d" * 64,
                        "expectedBytes": 123,
                        "plaintextSize": 100,
                        "plaintextSha256": "e" * 64,
                        "priority": 2,
                    }
                ],
            }
        ],
    }


def _projection() -> backup_projection.ProjectionPlan:
    selection = backup_projection.RestoreSelection(("projects",), ("p1",))
    return backup_projection.ProjectionPlan(
        selection=selection,
        selection_digest="a" * 64,
        output_files=frozenset({"payload/projects/p1/a.bin"}),
        support_files=frozenset({"payload/projects/p1/base.bin"}),
        needed_full_entries=frozenset({"payload/projects/p1/base.bin"}),
        needed_packs=frozenset({"payload/packs/0000.pack"}),
        needed_blobs=frozenset({"blob-1"}),
        needed_standalone=frozenset(),
        needed_after_layer=(frozenset({"payload/projects/p1/base.bin"}),),
        produced_by_layer=(frozenset(),),
        requires_frontend_apply=False,
        requires_external_mcp=False,
        report={"selectionDigest": "a" * 64, "networkSelective": True},
    )


def test_verified_plan_round_trips_and_contains_no_secret(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session["secret"] = "must-never-be-persisted"
    projection = _projection()

    path = backup_verified_plan.write_verified_plan(tmp_path, session, projection, projection.report)
    loaded = backup_verified_plan.load_verified_plan(tmp_path, session)

    assert loaded is not None
    loaded_projection, report = loaded
    assert loaded_projection == projection
    assert report == projection.report
    raw = path.read_text(encoding="utf-8")
    assert "must-never-be-persisted" not in raw
    assert json.loads(raw)["body"]["plannerSchemaVersion"] == 1


def test_verified_plan_rejects_body_and_metadata_tampering(tmp_path: Path) -> None:
    session = _session(tmp_path)
    path = backup_verified_plan.write_verified_plan(tmp_path, session, _projection(), {"networkSelective": True})
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"]["report"]["networkSelective"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert backup_verified_plan.load_verified_plan(tmp_path, session) is None

    backup_verified_plan.write_verified_plan(tmp_path, session, _projection(), {"networkSelective": True})
    (tmp_path / "metadata-0" / "manifest.json").write_text("tampered", encoding="utf-8")
    assert backup_verified_plan.load_verified_plan(tmp_path, session) is None


def test_verified_plan_rejects_selection_chain_control_or_closure_changes(tmp_path: Path) -> None:
    for mutation in ("selection", "chain", "control", "closure"):
        case = tmp_path / mutation
        session = _session(case)
        backup_verified_plan.write_verified_plan(case, session, _projection(), {"networkSelective": True})
        chain = session["chain"]
        assert isinstance(chain, list)
        member = chain[0]
        assert isinstance(member, dict)
        if mutation == "selection":
            session["selectionDigest"] = "f" * 64
        elif mutation == "chain":
            member["objectSetDigest"] = "f" * 64
        elif mutation == "control":
            member["controlObjectDigest"] = "f" * 64
        else:
            required = member["requiredComponents"]
            assert isinstance(required, list) and isinstance(required[0], dict)
            required[0]["expectedBytes"] = 124

        assert backup_verified_plan.load_verified_plan(case, session) is None


def test_verified_plan_rejects_internally_inconsistent_selection_digest(tmp_path: Path) -> None:
    session = _session(tmp_path)
    path = backup_verified_plan.write_verified_plan(tmp_path, session, _projection(), _projection().report)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"]["projection"]["selectionDigest"] = "f" * 64
    body_bytes = json.dumps(payload["body"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["bodySha256"] = hashlib.sha256(body_bytes).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert backup_verified_plan.load_verified_plan(tmp_path, session) is None


def test_verified_plan_supports_full_restore_without_projection(tmp_path: Path) -> None:
    session = _session(tmp_path)
    path = backup_verified_plan.write_verified_plan(tmp_path, session, None, {"requiredComponents": 1})

    loaded = backup_verified_plan.load_verified_plan(tmp_path, session)

    assert path.is_file()
    assert loaded == (None, {"requiredComponents": 1})
