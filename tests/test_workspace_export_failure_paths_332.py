from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import exports


def _zip() -> tuple[io.BytesIO, zipfile.ZipFile]:
    output = io.BytesIO()
    return output, zipfile.ZipFile(output, "w")


def test_export_resolution_searches_global_and_projects(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exports.legacy_projects, "list_projects", lambda: [{"id": "proj_1234"}, {"id": ""}])
    monkeypatch.setattr(
        exports,
        "_load_exports",
        lambda project_id: [{"exportId": "export_1234", "projectId": project_id, "createdAt": "2026-01-01"}] if project_id == "proj_1234" else [],
    )
    assert exports.resolve_export("export_1234")["projectId"] == "proj_1234"
    assert exports.list_exports() == [{"exportId": "export_1234", "projectId": "proj_1234", "createdAt": "2026-01-01"}]
    with pytest.raises(AppError):
        exports.resolve_export("export_missing")


def test_export_path_and_markdown_tolerate_partial_records(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exports, "_export_dir", lambda _project_id: tmp_settings / "exports")
    assert exports.export_path({"path": "result.json", "projectId": "proj_1234"}) == (tmp_settings / "exports" / "result.json").resolve()
    absolute = tmp_settings / "absolute.zip"
    assert exports.export_path({"path": str(absolute)}) == absolute.resolve()

    bundle = {
        "metadata": "bad",
        "conversations": [None, {"title": "Auth api_key=secretvalue", "id": "conv"}],
        "savedItems": [{"title": "Saved", "type": "trace", "savedId": "s1", "tags": ["a"], "sourceRef": {"token": "x"}, "content": ""}],
        "artifacts": [],
        "media": [],
    }
    markdown = exports.project_markdown(bundle)
    assert markdown.startswith("# Project")
    assert "secretvalue" not in markdown
    conversation = exports.conversation_markdown(
        {
            "title": "Conversation",
            "id": "c1",
            "sourceRef": {"api_key": "secret"},
            "messages": [None, {"role": "", "content": "", "sourceRef": {"password": "secret"}}],
        }
    )
    assert "_No content_" in conversation
    assert "secret" not in conversation
    assert exports.saved_items_markdown([]) == ""
    assert exports.artifacts_markdown([]) == ""
    assert exports.media_markdown([]) == ""


def test_zip_project_files_skips_invalid_missing_and_uses_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(file_id: str, *, project_id: str) -> dict[str, object]:
        if file_id == "missing":
            raise AppError("missing")
        if file_id == "preview":
            return {"name": "preview", "preview": "api_key=secretvalue"}
        return {"name": "chunks", "chunks": [None, {"text": "safe"}, {"text": "Bearer abcdefghijkl"}]}

    monkeypatch.setattr(exports, "load_cached_file", load)
    output, archive = _zip()
    with archive:
        exports.write_project_files_to_zip(archive, "proj_1234", {"files": "bad"})
        exports.write_project_files_to_zip(
            archive,
            "proj_1234",
            {"files": [None, {}, {"fileId": "missing"}, {"fileId": "preview"}, {"fileId": "chunks"}]},
        )
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as result:
        combined = "\n".join(result.read(name).decode() for name in result.namelist())
    assert "secretvalue" not in combined
    assert "abcdefghijkl" not in combined
    assert "safe" in combined


def test_zip_artifacts_media_and_traces_handle_half_failed_records(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = tmp_settings / "artifact.txt"
    text.write_text("api_key=secretvalue", encoding="utf-8")
    binary = tmp_settings / "image.bin"
    binary.write_bytes(b"\x00\x01")
    missing = tmp_settings / "missing.bin"

    def artifact_path(item: dict[str, object]) -> Path:
        if item.get("artifactId") == "bad":
            raise AppError("bad path")
        return Path(str(item.get("path") or missing))

    monkeypatch.setattr(exports.artifact_store, "artifact_path", artifact_path)
    monkeypatch.setattr(exports.artifact_store, "artifact_filename", lambda item: str(item.get("name") or "file.bin"))
    monkeypatch.setattr(exports.artifact_store, "TEXT_PREVIEW_TYPES", {"txt"})
    monkeypatch.setattr(exports.media_store, "redacted_media_payload", lambda item: {"mediaId": item.get("mediaId")})
    monkeypatch.setattr(exports.media_store, "list_segments", lambda media_id: [{"mediaId": media_id}])

    def media_path(item: dict[str, object]) -> Path:
        if item.get("mediaId") == "bad":
            raise AppError("bad media")
        return Path(str(item.get("path") or missing))

    monkeypatch.setattr(exports.media_store, "media_file_path", media_path)
    output, archive = _zip()
    with archive:
        exports.write_artifacts_to_zip(
            archive,
            [None, {"artifactId": "bad"}, {"path": str(missing)}, {"path": str(text), "type": "txt", "name": "safe.txt"}, {"path": str(binary), "type": "bin", "name": "raw.bin"}],
        )
        exports.write_media_to_zip(
            archive,
            [None, {}, {"mediaId": "bad"}, {"mediaId": "missing", "path": str(missing)}, {"mediaId": "text", "path": str(text), "title": "note"}, {"mediaId": "binary", "path": str(binary), "title": "image"}],
        )
        exports.write_trace_items_to_zip(archive, [None, {"type": "webpage"}, {"type": "trace", "savedId": "trace_1", "content": "api_key=secretvalue"}])
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as result:
        names = result.namelist()
        combined = b"\n".join(result.read(name) for name in names)
    assert "artifacts/safe.txt" in names
    assert "artifacts/raw.bin" in names
    assert "media/source/note.txt" in names
    assert "media/source/image.bin" in names
    assert "traces/trace-trace_1.json" in names
    assert b"secretvalue" not in combined


def test_export_store_includes_and_git_fallback(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exports, "_export_dir", lambda project_id: tmp_settings / (project_id or "global"))
    store = exports._export_store_path("proj_1234")
    store.parent.mkdir()
    store.write_text(json.dumps({"exports": [None, {"exportId": "e1"}]}), encoding="utf-8")
    assert exports._load_exports("proj_1234") == [{"exportId": "e1"}]
    exports._record_export("proj_1234", {"exportId": "e2"})
    assert [item["exportId"] for item in exports._load_exports("proj_1234")] == ["e1", "e2"]
    assert exports._export_includes("", "project") == {}
    monkeypatch.setattr(exports, "project_bundle", lambda _project_id: (_ for _ in ()).throw(OSError("corrupt")))
    assert exports._export_includes("proj_1234", "project") == {"projectId": "proj_1234"}

    monkeypatch.setattr(exports.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "fatal"))
    assert exports.git_short_sha() == "unknown"
    metadata = exports.evidence_metadata("3.3.2", status="ready", checks={"coverage": "pass"})
    assert metadata["commit"] == "unknown"
    assert metadata["status"] == "ready"
