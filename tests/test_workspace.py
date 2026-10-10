from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.data import projects as legacy_projects
from deepseek_infra.infra.workspace import artifacts, exports, projects, saved_items


def test_workspace_project_saved_items_artifacts_and_zip_export(tmp_settings: Path) -> None:
    project = projects.create_project("Workspace 408", description="Study plan")
    renamed = projects.rename_project(project["projectId"], "Workspace Core")
    legacy_projects.add_project_files(
        project["projectId"],
        [{"filename": "notes.txt", "content_type": "text/plain", "data": b"OS notes\nDEEPSEEK_API_KEY=sk-secretsecret"}],
    )

    saved = saved_items.create_saved_item(
        project["projectId"],
        item_type="chat_snippet",
        title="Scheduler answer",
        content="Use round-robin. Authorization: Bearer verysecrettoken",
        source_ref={"conversationId": "conv-1", "messageId": "msg-1"},
        tags=["408", "OS"],
        purpose="export_fragment",
    )

    generated = tmp_settings / ".generated"
    generated.mkdir()
    artifact_path = generated / "summary.md"
    artifact_path.write_text("# Summary\napi_key=sk-artifactsecret", encoding="utf-8")
    artifact = artifacts.register_artifact(
        project["projectId"],
        artifact_type="markdown",
        title="Review Summary",
        path=str(artifact_path),
        source={"conversationId": "conv-1", "messageId": "msg-2"},
    )
    second_path = generated / "summary-v2.md"
    second_path.write_text("# Summary v2", encoding="utf-8")
    artifact_v2 = artifacts.add_artifact_version(project["projectId"], artifact["artifactId"], path=str(second_path))

    conversation = projects.upsert_project_conversation(
        project["projectId"],
        {
            "id": "conv-1",
            "title": "408 review",
            "messages": [
                {"id": "m1", "role": "user", "content": "Explain schedulers"},
                {"id": "m2", "role": "assistant", "content": "Artifact: /api/workspace/artifacts/link"},
            ],
        },
    )
    preview = artifacts.preview_artifact(artifact["artifactId"], project_id=project["projectId"])
    exported = exports.export_project(project["projectId"], export_format="zip")["export"]

    assert renamed["name"] == "Workspace Core"
    assert saved["purpose"] == "export_fragment"
    assert artifact_v2["version"] == 2
    assert preview["previewAvailable"] is True
    assert "sk-artifactsecret" not in preview["content"]
    assert conversation["conversationId"] == "conv-1"

    zip_path = Path(exported["path"])
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
        combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in names)

    assert metadata["projectId"] == project["projectId"]
    assert "saved-items/saved-items.json" in names
    assert any(name.startswith("conversations/conversation-conv-1") for name in names)
    assert any(name.startswith("artifacts/Review-Summary") for name in names)
    assert any(name.startswith("files/source-files/") for name in names)
    assert "sk-secretsecret" not in combined
    assert "verysecrettoken" not in combined
    assert "sk-artifactsecret" not in combined


def test_workspace_delete_project_removes_only_project_runtime_data(tmp_settings: Path) -> None:
    project = projects.create_project("Disposable")
    global_file = tmp_settings / ".generated" / "global.md"
    global_file.parent.mkdir()
    global_file.write_text("keep me", encoding="utf-8")

    assert projects.delete_project(project["projectId"]) == 1

    assert not (tmp_settings / ".projects" / project["projectId"]).exists()
    assert global_file.exists()


def test_saved_items_filter_update_delete_and_limits(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Saved Items")
    first = saved_items.create_saved_item(
        project["projectId"],
        item_type="assistant_answer",
        title="Answer",
        content="content",
        source_ref={"conversationId": "conv-1"},
        tags=["408", "OS"],
        purpose="memory_candidate",
    )
    saved_items.create_saved_item(
        project["projectId"],
        item_type="webpage",
        title="Page",
        content="web content",
        tags=["web"],
    )

    assert [item["savedId"] for item in saved_items.list_saved_items(project["projectId"], item_type="assistant_answer")] == [first["savedId"]]
    assert [item["savedId"] for item in saved_items.list_saved_items(project["projectId"], tags=["408", "os"])] == [first["savedId"]]

    updated = saved_items.update_saved_item(
        project["projectId"],
        first["savedId"],
        {
            "title": "Updated",
            "content": "new content",
            "tags": ["408", "Scheduler"],
            "purpose": "export_fragment",
            "sourceRef": {"messageId": "m2"},
        },
    )
    assert updated["title"] == "Updated"
    assert updated["purpose"] == "export_fragment"
    assert updated["sourceRef"]["messageId"] == "m2"
    assert saved_items.require_saved_item(project["projectId"], first["savedId"])["content"] == "new content"

    assert saved_items.delete_saved_item(project["projectId"], "save_missing") == 0
    assert saved_items.delete_saved_item(project["projectId"], first["savedId"]) == 1
    with pytest.raises(AppError):
        saved_items.require_saved_item(project["projectId"], first["savedId"])
    with pytest.raises(AppError):
        saved_items.create_saved_item(project["projectId"], item_type="unknown", title="bad", content="bad")

    monkeypatch.setattr(saved_items, "MAX_SAVED_ITEMS", 0)
    with pytest.raises(AppError):
        saved_items.create_saved_item(project["projectId"], item_type="webpage", title="limit", content="limit")


def test_artifact_hub_preview_update_delete_and_errors(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = projects.create_project("Artifacts")
    generated = tmp_settings / ".generated"
    generated.mkdir()
    binary_path = generated / "report.pdf"
    binary_path.write_bytes(b"%PDF-1.4\nbinary")
    artifact = artifacts.register_artifact(project["projectId"], artifact_type="pdf", title="Report", path=str(binary_path))

    assert artifacts.preview_artifact(artifact["artifactId"], project_id=project["projectId"])["previewAvailable"] is False
    assert artifacts.require_artifact(artifact["artifactId"])["projectId"] == project["projectId"]
    renamed = artifacts.rename_artifact(project["projectId"], artifact["artifactId"], "Renamed Report")
    assert renamed["title"] == "Renamed Report"
    updated = artifacts.update_artifact(project["projectId"], artifact["artifactId"], {"source": {"messageId": "m3"}})
    assert updated["source"]["messageId"] == "m3"

    with pytest.raises(AppError):
        artifacts.add_artifact_version(project["projectId"], "art_missing", path=str(binary_path))
    with pytest.raises(AppError):
        artifacts.update_artifact(project["projectId"], "art_missing", {"title": "Missing"})
    with pytest.raises(AppError):
        artifacts.require_artifact("art_missing")

    missing_file = generated / "missing.md"
    missing_file.write_text("exists briefly", encoding="utf-8")
    missing = artifacts.register_artifact(project["projectId"], artifact_type="markdown", title="Missing", path=str(missing_file))
    missing_file.unlink()
    with pytest.raises(AppError):
        artifacts.preview_artifact(missing["artifactId"], project_id=project["projectId"])

    assert artifacts.delete_artifact(project["projectId"], "art_missing") == 0
    assert artifacts.delete_artifact(project["projectId"], artifact["artifactId"]) == 1

    monkeypatch.setattr(artifacts, "MAX_ARTIFACTS", 0)
    with pytest.raises(AppError):
        artifacts.register_artifact(project["projectId"], artifact_type="markdown", title="limit", path=str(binary_path))


def test_workspace_export_dispatcher_formats_and_redaction(tmp_settings: Path) -> None:
    project = projects.create_project("Exports", description="Export all the things")
    saved = saved_items.create_saved_item(
        project["projectId"],
        item_type="trace",
        title="Trace result",
        content="Authorization: Bearer secret-token-value",
        source_ref={"traceId": "trace-1"},
        tags=["trace"],
    )
    generated = tmp_settings / ".generated"
    generated.mkdir()
    artifact_path = generated / "table.csv"
    artifact_path.write_text("name,token\nalice,api_key=sk-secretvalue\n", encoding="utf-8")
    artifact = artifacts.register_artifact(
        project["projectId"],
        artifact_type="csv",
        title="Table",
        path=str(artifact_path),
        source={"conversationId": "conv-export"},
    )
    conversation = projects.upsert_project_conversation(
        project["projectId"],
        {
            "conversationId": "conv-export",
            "title": "Export conversation",
            "sourceRef": {"artifactId": artifact["artifactId"]},
            "messages": [{"id": "m1", "role": "assistant", "content": "See artifact", "sourceRef": {"savedId": saved["savedId"]}}],
        },
    )

    project_json = exports.create_export({"kind": "project", "projectId": project["projectId"], "format": "json"})["export"]
    project_html = exports.export_project(project["projectId"], export_format="html")["export"]
    project_md = exports.export_project(project["projectId"], export_format="markdown")["export"]
    assert Path(project_json["path"]).suffix == ".json"
    assert Path(project_html["path"]).suffix == ".html"
    assert Path(project_md["path"]).read_text(encoding="utf-8").startswith("# Exports")

    conversation_json = exports.create_export({"kind": "conversation", "projectId": project["projectId"], "format": "json", "conversation": conversation})["export"]
    conversation_html = exports.export_conversation(conversation, project_id=project["projectId"], export_format="html")["export"]
    conversation_zip = exports.export_conversation(conversation, project_id=project["projectId"], export_format="zip")["export"]
    assert json.loads(Path(conversation_json["path"]).read_text(encoding="utf-8"))["conversation"]["conversationId"] == "conv-export"
    assert Path(conversation_html["path"]).suffix == ".html"
    with zipfile.ZipFile(Path(conversation_zip["path"])) as archive:
        assert "metadata.json" in archive.namelist()

    saved_zip = exports.create_export({"kind": "saved-items", "projectId": project["projectId"], "format": "zip", "savedIds": [saved["savedId"]]})["export"]
    saved_html = exports.export_saved_items(project["projectId"], export_format="html")["export"]
    saved_md = exports.export_saved_items(project["projectId"], export_format="markdown")["export"]
    saved_json = exports.export_saved_items(project["projectId"], export_format="json")["export"]
    assert "secret-token-value" not in Path(saved_md["path"]).read_text(encoding="utf-8")
    assert Path(saved_html["path"]).suffix == ".html"
    assert json.loads(Path(saved_json["path"]).read_text(encoding="utf-8"))["items"]
    with zipfile.ZipFile(Path(saved_zip["path"])) as archive:
        assert "saved-items/saved-items.md" in archive.namelist()

    artifacts_json = exports.create_export({"kind": "artifacts", "projectId": project["projectId"], "format": "json", "artifactIds": [artifact["artifactId"]]})["export"]
    artifacts_html = exports.export_artifacts(project["projectId"], export_format="html")["export"]
    artifacts_md = exports.export_artifacts(project["projectId"], export_format="markdown")["export"]
    artifacts_zip = exports.export_artifacts(project["projectId"], export_format="zip")["export"]
    assert json.loads(Path(artifacts_json["path"]).read_text(encoding="utf-8"))["artifacts"][0]["artifactId"] == artifact["artifactId"]
    assert Path(artifacts_html["path"]).suffix == ".html"
    assert "Table" in Path(artifacts_md["path"]).read_text(encoding="utf-8")
    with zipfile.ZipFile(Path(artifacts_zip["path"])) as archive:
        combined = "\n".join(archive.read(name).decode("utf-8", errors="ignore") for name in archive.namelist())
    assert "sk-secretvalue" not in combined

    evidence_zip = exports.create_export({"kind": "evidence", "projectId": project["projectId"], "format": "zip", "traces": [{"token": "abc"}]})["export"]
    evidence_html = exports.export_evidence({"evals": [{"status": "PASS"}]}, project_id=project["projectId"], export_format="html")["export"]
    evidence_md = exports.export_evidence({"sourceRef": {"messageId": "m1"}}, project_id=project["projectId"], export_format="markdown")["export"]
    evidence_json = exports.export_evidence({"traces": [{"apiKey": "sk-secret"}]}, project_id=project["projectId"], export_format="json")["export"]
    with zipfile.ZipFile(Path(evidence_zip["path"])) as archive:
        assert {"metadata.json", "traces/traces.json", "evals/evals.json"}.issubset(set(archive.namelist()))
    assert Path(evidence_html["path"]).suffix == ".html"
    assert Path(evidence_md["path"]).suffix == ".md"
    assert "sk-secret" not in Path(evidence_json["path"]).read_text(encoding="utf-8")

    with pytest.raises(AppError):
        exports.create_export({"kind": "unknown", "projectId": project["projectId"]})
