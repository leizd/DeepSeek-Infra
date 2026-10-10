from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.automation import registry as automation_registry
from deepseek_infra.infra.automation import runner as automation_runner
from deepseek_infra.infra.media import library as media_library
from deepseek_infra.infra.memory import store as memory_store
from deepseek_infra.infra.workspace import artifacts, exports, home, projects, provenance, saved_items


def test_workspace_home_and_provenance_graph_link_ga_objects(tmp_settings: Path) -> None:
    project = projects.create_project("GA Runtime", description="Personal AI Runtime smoke project")
    project_id = str(project["projectId"])
    projects.upsert_project_conversation(
        project_id,
        {
            "conversationId": "conv-ga",
            "title": "GA planning",
            "messages": [{"id": "msg-ga", "role": "user", "content": "Summarize the launch evidence."}],
        },
    )
    memory = memory_store.add_memory(
        "GA project should keep release evidence local.",
        scope="project",
        project_id=project_id,
        memory_type="summary",
        source={"kind": "chat", "refId": "msg-ga", "messageId": "msg-ga"},
    )
    media = media_library.register_media(
        project_id=project_id,
        media_type="webpage",
        title="Browser Snapshot",
        source={"kind": "browser", "refId": "browser-ga", "browserSessionId": "browser-ga"},
        metadata={"sourceUrl": "https://example.com"},
        status="ready",
    )
    saved = saved_items.create_saved_item(
        project_id,
        item_type="webpage",
        title="Snapshot note",
        content="Useful launch note.",
        source_ref={"messageId": "msg-ga", "mediaId": media["mediaId"]},
    )
    artifact_path = tmp_settings / ".generated" / "ga-report.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("# GA Report\n", encoding="utf-8")
    artifact = artifacts.register_artifact(
        project_id,
        artifact_type="markdown",
        title="GA Report",
        path=str(artifact_path),
        source={"skillRunId": "skill-run-ga", "mediaId": media["mediaId"]},
    )
    automation = automation_registry.create_automation(
        {
            "name": "GA Snapshot Saver",
            "projectId": project_id,
            "trigger": {"type": "manual"},
            "condition": {"type": "always"},
            "action": {"type": "save_item", "title": "Automation note", "content": "Automation completed."},
            "policy": {"maxRunsPerDay": 5},
        }
    )
    run = automation_runner.run_once(str(automation["automationId"]), force=True)
    export = exports.export_project(project_id, export_format="zip")["export"]

    overview = home.workspace_home(limit=10)
    assert {module["label"] for module in overview["modules"]} >= {"Projects", "Memory", "Skills", "Media", "Browser", "Automations", "Artifacts", "Saved Items", "Exports", "Settings"}
    assert overview["counts"]["projects"] == 1
    assert overview["counts"]["memories"] >= 2
    assert any(item["mediaId"] == media["mediaId"] for item in overview["recent"]["media"])
    assert any(item["exportId"] == export["exportId"] for item in overview["recent"]["exports"])

    graph = provenance.project_provenance(project_id)
    node_ids = {node["id"] for node in graph["nodes"]}
    edge_keys = {(edge["source"], edge["target"], edge["relation"]) for edge in graph["edges"]}

    assert f"project:{project_id}" in node_ids
    assert f"memory:{memory['memoryId']}" in node_ids
    assert f"media:{media['mediaId']}" in node_ids
    assert f"saved_item:{saved['savedId']}" in node_ids
    assert f"artifact:{artifact['artifactId']}" in node_ids
    assert f"automation_run:{run['runId']}" in node_ids
    assert f"export:{export['exportId']}" in node_ids
    assert (f"media:{media['mediaId']}", f"saved_item:{saved['savedId']}", "sourced_from") in edge_keys
    assert any(edge[0] == f"automation_run:{run['runId']}" and edge[2] == "produced" for edge in edge_keys)
    assert any(edge[0] == f"export:{export['exportId']}" and edge[2] == "includes" for edge in edge_keys)
