//! `GET /api/mcp/external/tools` integration tests with mock Streamable HTTP MCP servers.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{HeaderMap, HeaderValue, Request, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "external-mcp-test-token";
static ENV_BUSY: AtomicBool = AtomicBool::new(false);

struct EnvLock;
impl EnvLock {
    fn acquire() -> Self {
        while ENV_BUSY
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            std::thread::yield_now();
        }
        Self
    }
}
impl Drop for EnvLock {
    fn drop(&mut self) {
        ENV_BUSY.store(false, Ordering::Release);
    }
}

struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}
impl EnvGuard {
    fn set(pairs: &[(&'static str, String)]) -> Self {
        let saved = pairs
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var(name).ok();
                unsafe { std::env::set_var(name, value) };
                (*name, previous)
            })
            .collect();
        Self { saved }
    }
}
impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
    }
}

fn build_app(static_root: &std::path::Path) -> axum::Router {
    let ui = static_root.join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    create_production_app(static_root).unwrap()
}

async fn call_tools(app: &axum::Router, method: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri("/api/mcp/external/tools")
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = app.clone().oneshot(req).await.unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 65_536).await.unwrap();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, body)
}

async fn mock_mcp_sse_handler(_headers: HeaderMap, body: Bytes) -> Response {
    let payload: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let method = payload.get("method").and_then(Value::as_str).unwrap_or("");
    let id = payload.get("id").cloned().unwrap_or(Value::Null);
    if method == "notifications/initialized" {
        return StatusCode::ACCEPTED.into_response();
    }
    let rpc = match method {
        "initialize" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "mock-sse", "version": "1.0"}
            }
        }),
        "tools/list" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "tools": [
                    {
                        "name": "read.file",
                        "description": "Read a file",
                        "annotations": {"openWorldHint": false},
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}}
                        }
                    },
                    {
                        "name": "read/file",
                        "description": "Delete remote token",
                        "annotations": {"destructiveHint": true, "openWorldHint": true},
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "token": {"type": "string"}
                            }
                        }
                    }
                ]
            }
        }),
        _ => json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32601, "message": "unknown"}}),
    };
    let sse = format!(
        "event: message\ndata: {}\n\n",
        serde_json::to_string(&rpc).unwrap()
    );
    let mut response = Response::new(Body::from(sse));
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream"),
    );
    response
        .headers_mut()
        .insert("mcp-session-id", HeaderValue::from_static("sess-sse-123"));
    response
}

use axum::body::Bytes;

#[tokio::test]
async fn external_mcp_tools_disabled_mode_answers_without_network() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("MCP_CLIENT_ENABLED", "0".to_string()),
        (
            "MCP_CLIENT_SERVERS",
            json!([{"name": "unused", "url": "http://127.0.0.1:9/mcp", "timeoutSeconds": 2}])
                .to_string(),
        ),
    ]);
    let app = build_app(static_root.path());

    let (status, body) = call_tools(&app, "GET").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["ok"], true);
    assert_eq!(body["tools"], json!([]));
    assert_eq!(body["servers"].as_array().unwrap().len(), 1);
    assert_eq!(body["servers"][0]["status"], "unknown");
    assert_eq!(body["servers"][0]["timeoutSeconds"], 2);

    let (post_status, _) = call_tools(&app, "POST").await;
    assert_eq!(post_status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn external_mcp_tools_connects_over_sse_and_infers_profiles_with_collision_suffix() {
    let _lock = EnvLock::acquire();
    let mock_app = Router::new().route("/mcp", post(mock_mcp_sse_handler));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mock_addr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = axum::serve(listener, mock_app).await;
    });

    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    let servers_json = json!([
        {"name": "Alpha", "url": format!("http://{mock_addr}/mcp"), "timeoutSeconds": 5}
    ])
    .to_string();
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("MCP_CLIENT_ENABLED", "1".to_string()),
        ("MCP_CLIENT_SERVERS", servers_json),
        ("MCP_CLIENT_MAX_RETRIES", "0".to_string()),
    ]);
    let app = build_app(static_root.path());

    let (status, body) = call_tools(&app, "GET").await;
    server_handle.abort();

    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["ok"], true);
    assert_eq!(body["servers"][0]["name"], "Alpha");
    assert_eq!(body["servers"][0]["status"], "ok");
    assert_eq!(body["servers"][0]["available"], true);

    let tools = body["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 2);
    assert_eq!(tools[0]["bridgedName"], "mcp__alpha__read_file");
    assert_eq!(tools[0]["risk"], "medium");
    assert_eq!(tools[0]["filesystem"], true);
    assert_eq!(tools[0]["network"], false);
    assert_eq!(tools[0]["requiresApproval"], false);

    let second_bridged = tools[1]["bridgedName"].as_str().unwrap();
    assert!(second_bridged.starts_with("mcp__alpha__read_file__"));
    assert_eq!(second_bridged.len(), "mcp__alpha__read_file__".len() + 6);
    assert_eq!(tools[1]["risk"], "critical");
    assert_eq!(tools[1]["network"], true);
    assert_eq!(tools[1]["requiresApproval"], true);
}
