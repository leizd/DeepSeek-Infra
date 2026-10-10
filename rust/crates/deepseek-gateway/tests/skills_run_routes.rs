//! Offline execution must produce real run, project, trace and artifact records.
use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use deepseek_gateway::create_production_app;
use deepseek_policy::skills::registry::Registry;
use serde_json::{Value, json};
use tower::ServiceExt;

async fn post(app: &axum::Router, path: &str, payload: Value) -> (StatusCode, Value) {
    post_json(app, path, &payload.to_string()).await
}
async fn post_json(app: &axum::Router, path: &str, payload: &str) -> (StatusCode, Value) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer native-run-test")
                .body(Body::from(payload.to_owned()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&body).unwrap())
}

#[tokio::test]
async fn offline_runs_persist_actual_artifacts_and_survive_restart() {
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(root.path().join("skills/builtin")).unwrap();
    std::fs::create_dir_all(root.path().join("ui")).unwrap();
    std::fs::write(root.path().join("ui/index.html"), "<main>test</main>").unwrap();
    let config = json!({"skillId":"native-run","name":"Native run","description":"Isolated fixture","version":"1.0",
        "systemPrompt":"Explain topic","inputSchema":{"type":"object","required":["topic"]},"outputSchema":{"type":"object","required":["content"]},
        "allowedTools":[],"memoryPolicy":{"scope":"none"},"artifactPolicy":{"types":["md"],"autoSave":true},"projectBinding":{"enabled":true}});
    std::fs::write(
        root.path().join("skills/builtin/native-run.json"),
        config.to_string(),
    )
    .unwrap();
    unsafe {
        std::env::set_var("DEEPSEEK_INFRA_ROOT", root.path());
        std::env::set_var("AUTH_TOKEN", "native-run-test");
        std::env::set_var("DEEPSEEK_RUNTIME_MODE", "python");
        std::env::remove_var("TRACE_ENABLED");
    }
    let app = create_production_app(root.path()).unwrap();
    let payload = json!({"action":"run","id":"native-run","offline":true,"input":{"topic":"Native persistence"}});
    let (status, body) = post(&app, "/api/skills", payload.clone()).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert!(!root.path().join(".traces").exists());
    assert!(!root.path().join(".generated").exists());
    unsafe {
        std::env::set_var("DEEPSEEK_RUNTIME_MODE", "python_disabled");
    }
    let registry = Registry::from_env();
    let project =
        deepseek_policy::projects::create_project("Native run project", root.path(), &registry)
            .unwrap();
    let payload = format!(
        r#"{{"action":"run","id":"native-run","offline":true,"projectId":{},"input":{{"topic":"Native persistence","apiKey":"fixture-secret"}}}}"#,
        project["id"]
    );
    let (status, result) = post_json(&app, "/api/skills", &payload).await;
    assert_eq!(status, StatusCode::OK, "{result}");
    assert_eq!(
        result["analytics"]["inputSummary"],
        "topic=Native persistence, apiKey=[redacted]"
    );
    assert_eq!(result["output"]["mode"], "offline");
    assert!(
        result["output"]["content"]
            .as_str()
            .unwrap()
            .contains("Native persistence")
    );
    assert_eq!(result["savedItems"].as_array().unwrap().len(), 1);
    assert_eq!(result["artifacts"][0]["type"], "md");
    let file_id = result["artifacts"][0]["fileId"].as_str().unwrap();
    let file =
        deepseek_policy::generated_files::resolve_generated_file(root.path(), file_id).unwrap();
    assert!(
        std::fs::read_to_string(file)
            .unwrap()
            .contains("Native persistence")
    );
    let connection =
        rusqlite::Connection::open(root.path().join(".traces/traces.sqlite3")).unwrap();
    let trace = result["traceId"].as_str().unwrap();
    let completed: String = connection
        .query_row(
            "SELECT status FROM trace_runs WHERE trace_id = ?1",
            [trace],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(completed, "completed");
    let project_id = project["id"].as_str().unwrap();
    let stored =
        deepseek_policy::projects::require_project(project_id, root.path(), &registry).unwrap();
    assert_eq!(stored["skillRuns"][0]["skillRunId"], result["skillRunId"]);
    assert_eq!(stored["artifacts"][0]["fileId"], file_id);
    let restarted = create_production_app(root.path()).unwrap();
    let (status, persisted) = post(
        &restarted,
        "/api/skills",
        json!({"action":"get_run","runId":result["skillRunId"]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{persisted}");
    assert_eq!(persisted["skillRun"]["status"], "completed");
    let (status, preview) = post(
        &restarted,
        "/api/skills/native-run/run",
        json!({"offline":true,"persist":false,"inputs":{"topic":"Transient"}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{preview}");
    assert_eq!(preview["artifacts"], json!([]));
    assert!(!preview["traceId"].as_str().unwrap().is_empty());
    std::fs::write(
        root.path().join(".workspace-restore-fence.json"),
        "{\"restoreId\":\"isolated-restore\"}",
    )
    .unwrap();
    let (status, body) = post(
        &restarted,
        "/api/skills/native-run/run",
        json!({"offline":true,"persist":false,"input":{"topic":"Denied"}}),
    )
    .await;
    assert_eq!(status, StatusCode::LOCKED, "{body}");
    let count: i64 = connection
        .query_row("SELECT COUNT(*) FROM trace_runs", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 2);
}
