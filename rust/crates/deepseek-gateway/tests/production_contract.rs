//! Production entry contract. These tests call `create_production_app`, the
//! router `deepseek-gateway` serves, and they read the packaging inputs the
//! release image is built from.
use std::path::PathBuf;

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::Value;
use tower::ServiceExt;

const TOKEN: &str = "production-contract-token";

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn production_app() -> axum::Router {
    let root = repo_root().join("static");
    create_production_app(&root).expect("production app loads the Vite build")
}

async fn send(
    app: axum::Router,
    method: &str,
    path: &str,
    body: Option<&str>,
    token: bool,
) -> (StatusCode, Value) {
    let mut builder = Request::builder().method(method).uri(path);
    if token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {TOKEN}"));
    }
    if body.is_some() {
        builder = builder.header(header::CONTENT_TYPE, "application/json");
    }
    let request = builder
        .body(Body::from(body.unwrap_or("").to_string()))
        .unwrap();
    let response = app.oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let parsed = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, parsed)
}

#[tokio::test]
async fn production_listener_serves_health_config_and_mcp() {
    // Rust 2024 makes process-global environment mutation unsafe.
    unsafe {
        std::env::set_var("AUTH_TOKEN", TOKEN);
        std::env::set_var("DEEPSEEK_API_KEY", "sk-production-contract");
        std::env::set_var("OCR_ENABLED", "1");
        std::env::set_var("OCR_MODE", "quality");
    }
    let app = production_app();

    let (status, health) = send(app.clone(), "GET", "/healthz", None, false).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(health["status"], "ok");
    assert_eq!(health["runtime"], "local");
    assert_eq!(health["provider"], "deepseek");
    assert!(health["version"].as_str().unwrap_or("").contains('.'));
    assert_eq!(health["ok"], true);

    let (ready_status, ready) = send(app.clone(), "GET", "/readyz", None, false).await;
    assert_eq!(ready_status, StatusCode::OK);
    assert_eq!(ready["status"], "ready");
    assert_eq!(ready["checks"]["model_provider"], "configured");
    assert_eq!(ready["version"], health["version"]);

    let (config_status, config) = send(app.clone(), "GET", "/api/config", None, true).await;
    assert_eq!(config_status, StatusCode::OK, "{config}");
    for field in [
        "version",
        "hasServerKey",
        "hasSearch",
        "defaultModel",
        "models",
        "modelRoutes",
        "uploadLimits",
    ] {
        assert!(config.get(field).is_some(), "missing {field}: {config}");
    }
    assert_eq!(config["hasServerKey"], true);
    assert_eq!(config["ocr"]["enabled"], true);
    assert_eq!(config["ocr"]["mode"], "quality");
    assert_eq!(config["ocr"]["localOnly"], false);
    assert_eq!(config["models"][0], "deepseek-v4-pro");
    assert_eq!(config["models"][1], "deepseek-v4-flash");
    assert_eq!(config["modelRoutes"]["fast"], "deepseek-v4-flash");
    assert_eq!(config["modelRoutes"]["expert"], "deepseek-v4-pro");
    assert_eq!(config["uploadLimits"]["fileMaxBytes"], 200_000_000);
    assert_eq!(config["uploadLimits"]["requestMaxBytes"], 220_000_000);
    assert_eq!(config["uploadLimits"]["maxFiles"], 20);
    assert_eq!(config["version"], health["version"]);

    let (again_status, again) = send(app.clone(), "GET", "/api/config", None, true).await;
    assert_eq!(again_status, StatusCode::OK);
    assert_eq!(again, config);

    let mcp_body = r#"{"jsonrpc":"2.0","id":"init-1","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}}}"#;
    let (mcp_status, mcp) = send(app, "POST", "/mcp", Some(mcp_body), true).await;
    assert_eq!(mcp_status, StatusCode::OK, "{mcp}");
    assert_eq!(mcp["jsonrpc"], "2.0");
    assert_eq!(mcp["id"], "init-1");
    assert!(
        mcp.get("result").is_some(),
        "mcp initialize had no result: {mcp}"
    );
    let encoded = serde_json::to_string(&mcp).unwrap();
    assert!(!encoded.to_ascii_lowercase().contains("traceback"));
}

#[test]
fn production_packaging_inputs_do_not_launch_python_or_node_mcp() {
    let root = repo_root();
    let dockerfile = std::fs::read_to_string(root.join("Dockerfile")).unwrap();
    let lowered = dockerfile.to_ascii_lowercase();
    assert!(!lowered.contains("from python"));
    assert!(!lowered.contains("pip install"));
    // DEEPSEEK_INFRA_* is the native data-root prefix, not the Python package.
    assert!(!lowered.contains("deepseek_infra/"));
    assert!(!lowered.contains("python -m deepseek_infra"));
    assert!(!lowered.contains("stateless-mcp"));
    assert!(lowered.contains("deepseek-gateway"));
    assert!(lowered.contains("deepseekd"));
    assert!(lowered.contains("deepseek-worker"));
    assert!(lowered.contains("deepseek-launch"));
    assert!(!lowered.contains("cmd [\"python\""));

    let compose = std::fs::read_to_string(root.join("docker-compose.yml")).unwrap();
    assert!(!compose.to_ascii_lowercase().contains("python"));
    assert!(compose.contains("deepseek-infra"));

    let native = std::fs::read_to_string(root.join("docker-compose.native.yml")).unwrap();
    // This exact mode mechanically denies the legacy writer. It is not an
    // interpreter invocation; every other mention still fails the guard.
    assert!(
        native
            .lines()
            .any(|line| line.trim() == "DEEPSEEK_RUNTIME_MODE: python_disabled")
    );
    assert!(
        !native
            .lines()
            .filter(|line| line.trim() != "DEEPSEEK_RUNTIME_MODE: python_disabled")
            .any(|line| line.to_ascii_lowercase().contains("python"))
    );
    assert!(native.contains("DEEPSEEKD_MODE: authoritative"));
    assert!(native.contains("deepseek-edge:"));
    assert!(native.contains("deepseekd:"));
    assert!(native.contains("deepseek-worker:"));

    let android = std::fs::read_to_string(root.join("android/app/build.gradle")).unwrap();
    assert!(!android.to_ascii_lowercase().contains("chaquopy"));
    assert!(!android.contains("syncPythonSources"));
    let activity = std::fs::read_to_string(
        root.join("android/app/src/main/java/com/deepseek/mobile/MainActivity.java"),
    )
    .unwrap();
    assert!(!activity.contains("com.chaquo.python"));
    assert!(!activity.contains("startPythonServer"));
    assert!(activity.contains("startNativeServer"));

    let entry = std::fs::read_to_string(root.join("packaging/native/entrypoint.sh")).unwrap();
    assert!(
        entry
            .lines()
            .any(|line| line.trim() == "exec deepseek-launch --server")
    );
    let supervisor = std::fs::read_to_string(root.join("go/internal/launch/plan.go")).unwrap();
    for child in ["deepseek-gateway", "deepseekd", "deepseek-worker"] {
        assert!(supervisor.contains(child));
    }
    assert!(!entry.lines().any(|line| {
        let trimmed = line.trim();
        trimmed.starts_with("python")
            || trimmed.starts_with("python3")
            || trimmed.contains(" node ")
    }));
}
