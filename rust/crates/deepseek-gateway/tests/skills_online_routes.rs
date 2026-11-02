//! Legal online Skill requests must reach a real provider socket and retain their output.
//! Python may be an offline oracle; neither this production route nor provider uses it.
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use deepseek_gateway::create_production_app;
use deepseek_policy::skills::registry::Registry;
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tower::ServiceExt;

const TOKEN: &str = "native-online-skill-auth";
const KEY: &str = "fixture-provider-key-never-persist";
const ANSWER: &str = "真实原生在线 Skill 输出，保留中文和原始内容。";

struct Environment(Vec<(&'static str, Option<String>)>);
impl Environment {
    fn set(values: &[(&'static str, String)]) -> Self {
        let mut saved = Vec::new();
        for (name, value) in values {
            saved.push((*name, std::env::var(name).ok()));
            // This integration-test binary contains one serial scenario.
            unsafe { std::env::set_var(name, value) };
        }
        Self(saved)
    }
}
impl Drop for Environment {
    fn drop(&mut self) {
        for (name, value) in self.0.iter().rev() {
            unsafe {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

#[derive(Clone, Default)]
struct Provider {
    requests: Arc<Mutex<Vec<Value>>>,
    status: Arc<AtomicU16>,
}
async fn provider(
    State(state): State<Provider>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    state.requests.lock().unwrap().push(json!({
        "authorization": headers.get("authorization").unwrap().to_str().unwrap(), "body": body,
    }));
    let status = state.status.load(Ordering::SeqCst);
    if status != 200 {
        return (
            StatusCode::from_u16(status).unwrap(),
            Json(json!({"error":{"message":format!("rejected {KEY}")}})),
        )
            .into_response();
    }
    Json(json!({"id":"actual-skill-provider", "model":body["model"],
        "choices":[{"index":0,"message":{"role":"assistant","content":ANSWER},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}))
    .into_response()
}

async fn call(
    app: &Router,
    path: &str,
    payload: Value,
    authenticated: bool,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method("POST")
        .uri(path)
        .header("content-type", "application/json")
        .header("host", "127.0.0.1:18123");
    if authenticated {
        request = request.header("authorization", format!("Bearer {TOKEN}"));
    }
    let response = tokio::time::timeout(
        Duration::from_secs(10),
        app.clone()
            .oneshot(request.body(Body::from(payload.to_string())).unwrap()),
    )
    .await
    .expect("the original Skill request must settle")
    .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn online_skill_uses_native_chat_and_persists_real_provider_output() {
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(root.path().join("skills/builtin")).unwrap();
    std::fs::create_dir_all(root.path().join("ui")).unwrap();
    std::fs::write(
        root.path().join("ui/index.html"),
        "<main>isolated Skill HTTP fixture</main>",
    )
    .unwrap();
    let config = json!({"skillId":"native-online","name":"Native online","description":"Real provider socket fixture","version":"1.0",
        "systemPrompt":"Explain the supplied topic in Chinese.","inputSchema":{"type":"object","required":["topic"]},
        "outputSchema":{"type":"object","required":["content","model","usage","diagnostics"]},
        "allowedTools":[],"memoryPolicy":{"scope":"none","read":false},
        "artifactPolicy":{"types":["md"],"autoSave":true},"projectBinding":{"enabled":true}});
    std::fs::write(
        root.path().join("skills/builtin/native-online.json"),
        config.to_string(),
    )
    .unwrap();
    let observed = Provider::default();
    observed.status.store(200, Ordering::SeqCst);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let provider_app = Router::new()
        .route("/chat/completions", post(provider))
        .with_state(observed.clone());
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let server = tokio::spawn(async move {
        axum::serve(listener, provider_app)
            .with_graceful_shutdown(async {
                let _ = stopped.await;
            })
            .await
            .unwrap();
    });
    let _environment = Environment::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TOKEN.into()),
        ("DEEPSEEK_RUNTIME_MODE", "python".into()),
        (
            "DEEPSEEK_API_URL",
            format!("http://{address}/chat/completions"),
        ),
        ("DEEPSEEK_API_KEY", String::new()),
        ("DEEPSEEK_TIMEOUT_SECONDS", "5".into()),
    ]);
    let app = create_production_app(root.path()).unwrap();
    let request = json!({"action":"run","skillId":"native-online","apiKey":KEY,"input":{"topic":"原生保留功能"}});
    let (status, _) = call(&app, "/api/skills", request.clone(), false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, body) = call(&app, "/api/skills", request.clone(), true).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert!(observed.requests.lock().unwrap().is_empty());
    assert!(!root.path().join(".traces").exists());
    assert!(!root.path().join(".generated").exists());

    unsafe { std::env::set_var("DEEPSEEK_RUNTIME_MODE", "python_disabled") };
    let registry = Registry::from_env();
    let project =
        deepseek_policy::projects::create_project("Online Skill project", root.path(), &registry)
            .unwrap();
    let mut request = request;
    request["projectId"] = project["id"].clone();
    let (status, result) = call(&app, "/api/skills", request, true).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a legal online request must succeed: {result}"
    );
    assert_eq!(result["status"], "completed");
    assert_eq!(result["output"]["content"], ANSWER);
    assert_eq!(result["output"].as_object().unwrap().len(), 4);
    assert_eq!(result["output"]["usage"]["total_tokens"], 10);
    assert!(result["output"]["diagnostics"].is_object());
    assert_eq!(result["analytics"]["offline"], false);
    assert_eq!(result["savedItems"].as_array().unwrap().len(), 1);
    let requests = observed.requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["authorization"], format!("Bearer {KEY}"));
    assert_eq!(requests[0]["body"]["model"], result["output"]["model"]);
    assert_eq!(requests[0]["body"]["stream"], false);
    assert!(
        requests[0]["body"]["messages"]
            .to_string()
            .contains("原生保留功能")
    );
    assert!(!requests[0]["body"].to_string().contains(KEY));
    let file_id = result["artifacts"][0]["fileId"].as_str().unwrap();
    let output =
        deepseek_policy::generated_files::resolve_generated_file(root.path(), file_id).unwrap();
    assert!(std::fs::read_to_string(&output).unwrap().contains(ANSWER));
    let traces = rusqlite::Connection::open(root.path().join(".traces/traces.sqlite3")).unwrap();
    let trace_status: String = traces
        .query_row(
            "SELECT status FROM trace_runs WHERE trace_id = ?1",
            [result["traceId"].as_str().unwrap()],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(trace_status, "completed");
    let restarted = create_production_app(root.path()).unwrap();
    let (status, saved) = call(
        &restarted,
        "/api/skills",
        json!({"action":"get_run","runId":result["skillRunId"]}),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{saved}");
    assert_eq!(saved["skillRun"]["status"], "completed");
    assert_eq!(
        std::fs::read_to_string(&output)
            .unwrap()
            .matches(ANSWER)
            .count(),
        1
    );

    unsafe { std::env::set_var("DEEPSEEK_API_KEY", KEY) };
    let (status, transient) = call(
        &restarted,
        "/api/skills/native-online/run",
        json!({"skillId":"the-path-wins","persist":false,"inputs":{"topic":"环境密钥与路径入口"}}),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{transient}");
    assert_eq!(transient["output"]["content"], ANSWER);
    assert_eq!(transient["artifacts"], json!([]));
    assert_eq!(transient["savedItems"], json!([]));
    assert_eq!(deepseek_policy::skills::analytics::read(&registry).len(), 1);
    assert_eq!(observed.requests.lock().unwrap().len(), 2);

    let before: i64 = traces
        .query_row("SELECT COUNT(*) FROM trace_runs", [], |row| row.get(0))
        .unwrap();
    std::fs::write(
        root.path().join(".workspace-restore-fence.json"),
        "{\"restoreId\":\"online-skill-restore\"}",
    )
    .unwrap();
    let (status, body) = call(
        &restarted,
        "/api/skills/native-online/run",
        json!({"input":{"topic":"必须拒绝"}}),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::LOCKED, "{body}");
    let after: i64 = traces
        .query_row("SELECT COUNT(*) FROM trace_runs", [], |row| row.get(0))
        .unwrap();
    assert_eq!(before, after);
    assert_eq!(observed.requests.lock().unwrap().len(), 2);
    std::fs::remove_file(root.path().join(".workspace-restore-fence.json")).unwrap();

    observed.status.store(401, Ordering::SeqCst);
    let (status, error) = call(
        &restarted,
        "/api/skills/native-online/run",
        json!({"input":{"topic":"真实拒绝"}}),
        true,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_GATEWAY, "{error}");
    assert!(!error.to_string().contains(KEY));
    assert_eq!(
        observed.requests.lock().unwrap().len(),
        3,
        "a conclusive provider rejection must not be repeated"
    );
    let runs = deepseek_policy::skills::analytics::read(&registry);
    assert_eq!(runs.len(), 2);
    assert_eq!(
        runs.iter().filter(|run| run["status"] == "failed").count(),
        1
    );
    stop.send(()).unwrap();
    server.await.unwrap();
}
