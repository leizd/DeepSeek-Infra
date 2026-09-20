//! Public A2A SSE contract, driven through the real gateway router.
use axum::{body::Body, http::Request};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tower::ServiceExt;

static TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

async fn request(path: &str, message: Value) -> axum::response::Response {
    deepseek_gateway::create_app()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(message.to_string()))
                .unwrap(),
        )
        .await
        .unwrap()
}

fn events(bytes: &[u8]) -> Vec<Value> {
    std::str::from_utf8(bytes)
        .unwrap()
        .split("\n\n")
        .filter(|frame| !frame.is_empty())
        .map(|frame| serde_json::from_str(frame.strip_prefix("data: ").unwrap()).unwrap())
        .collect()
}

async fn collect(response: axum::response::Response) -> Vec<Value> {
    let bytes = tokio::time::timeout(
        Duration::from_secs(5),
        axum::body::to_bytes(response.into_body(), 100_000),
    )
    .await
    .expect("stream must terminate")
    .unwrap();
    events(&bytes)
}

fn message(method: &str) -> Value {
    json!({"jsonrpc":"2.0","id":"turn-1","method":method,
        "params":{"message":{"parts":[{"kind":"text","text":"你好"}]}}})
}

#[tokio::test]
async fn native_mode_and_partial_control_config_never_fall_back_to_local_tasks() {
    let _guard = TEST_LOCK.lock().await;
    deepseek_gateway::a2a_hub::reset_a2a_for_tests();
    let _env = EnvRestore::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_A2A_CONTROL_URL", ""),
        ("DEEPSEEK_A2A_TLS_CA", ""),
        ("DEEPSEEK_A2A_TLS_SERVER_NAME", ""),
        ("DEEPSEEK_A2A_TLS_CERT", ""),
        ("DEEPSEEK_A2A_TLS_KEY", ""),
    ]);
    let response = request("/a2a", message("message/send")).await;
    assert_eq!(response.status(), 503);
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    assert!(
        String::from_utf8(bytes.to_vec())
            .unwrap()
            .contains("NATIVE_A2A_CONTROL_NOT_CONFIGURED")
    );
    let _partial = EnvRestore::set(&[("DEEPSEEK_A2A_CONTROL_URL", "http://127.0.0.1:1")]);
    let frames = collect(request("/a2a", message("message/stream")).await).await;
    assert_eq!(frames.len(), 1);
    assert_eq!(frames[0]["error"]["code"], -32603);
    assert!(
        frames[0]["error"]["message"]
            .as_str()
            .unwrap()
            .contains("mTLS")
    );
    let local = deepseek_gateway::a2a_hub::handle_a2a_message(
        &json!({"jsonrpc":"2.0","method":"tasks/list","id":1}),
        "",
        "http://localhost",
    );
    assert_eq!(local["result"]["tasks"], json!([]));
}

#[tokio::test]
async fn resubscribe_errors_are_sse_json_rpc_errors() {
    let _guard = TEST_LOCK.lock().await;
    let response = deepseek_gateway::create_app()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/a2a")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"jsonrpc":"2.0","id":"resume","method":"tasks/resubscribe"}).to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers()["content-type"],
        "text/event-stream; charset=utf-8"
    );
    assert_eq!(response.headers()["cache-control"], "no-cache");
    assert_eq!(response.headers()["x-accel-buffering"], "no");
    let bytes = axum::body::to_bytes(response.into_body(), 1024)
        .await
        .unwrap();
    let text = std::str::from_utf8(&bytes).unwrap();
    let event: Value = serde_json::from_str(text.strip_prefix("data: ").unwrap().trim()).unwrap();
    assert_eq!(event["id"], "resume");
    assert_eq!(event["error"]["code"], -32602);
    assert_eq!(event["error"]["message"], "id is required");
}

#[tokio::test]
async fn stream_emits_artifacts_then_terminal_and_resumes_after_cursor() {
    let _guard = TEST_LOCK.lock().await;
    deepseek_gateway::a2a_hub::reset_a2a_for_tests();
    deepseek_gateway::a2a_hub::set_task_runner(Arc::new(|agent, text| {
        assert_eq!(agent, "reasoner");
        Ok(format!("answer:{text}"))
    }));
    let frames = collect(request("/a2a/agents/reasoner", message("message/stream")).await).await;
    assert!(frames.iter().all(|frame| frame["id"] == "turn-1"));
    assert_eq!(frames[0]["result"]["kind"], "task");
    let task_id = frames[0]["result"]["id"].as_str().unwrap();
    let chunks: Vec<_> = frames
        .iter()
        .filter(|frame| frame["result"]["kind"] == "artifact-update")
        .map(|frame| &frame["result"])
        .collect();
    assert_eq!(chunks.len(), 2);
    assert_eq!(chunks[0]["chunkIndex"], 0);
    assert_eq!(chunks[0]["artifact"]["name"], "progress");
    assert_eq!(chunks[1]["chunkIndex"], 1);
    assert_eq!(chunks[1]["artifact"]["parts"][0]["text"], "answer:你好");
    assert_eq!(chunks[1]["final"], true);
    let terminal = &frames.last().unwrap()["result"];
    assert_eq!(terminal["kind"], "status-update");
    assert_eq!(terminal["status"]["state"], "completed");
    assert_eq!(terminal["final"], true);

    for (cursor, expected_chunks) in [(json!(0), 1), (json!("1"), 0), (json!("bad"), 2)] {
        let resumed = collect(
            request(
                "/a2a",
                json!({"jsonrpc":"2.0","id":"resume",
            "method":"tasks/resubscribe","params":{"id":task_id,"afterChunkIndex":cursor}}),
            )
            .await,
        )
        .await;
        assert_eq!(resumed[0]["result"]["kind"], "task");
        assert_eq!(
            resumed
                .iter()
                .filter(|frame| frame["result"]["kind"] == "artifact-update")
                .count(),
            expected_chunks
        );
        assert_eq!(
            resumed.last().unwrap()["result"]["status"]["state"],
            "completed"
        );
    }
}

#[tokio::test]
async fn disconnect_does_not_cancel_task_and_cancel_discards_late_answer() {
    use futures_util::StreamExt;
    let _guard = TEST_LOCK.lock().await;
    deepseek_gateway::a2a_hub::reset_a2a_for_tests();
    let (release, receiver) = std::sync::mpsc::channel();
    let receiver = std::sync::Mutex::new(receiver);
    let started = Arc::new(tokio::sync::Notify::new());
    let worker_started = started.clone();
    deepseek_gateway::a2a_hub::set_task_runner(Arc::new(move |_, _| {
        worker_started.notify_one();
        receiver
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        Ok("late answer must not escape".to_string())
    }));
    let response = request("/a2a", message("message/stream")).await;
    let mut stream = response.into_body().into_data_stream();
    let first = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let initial = events(&first);
    let task_id = initial[0]["result"]["id"].as_str().unwrap();
    tokio::time::timeout(Duration::from_secs(2), started.notified())
        .await
        .unwrap();
    drop(stream);
    let query =
        |method: &str| json!({"jsonrpc":"2.0","id":7,"method":method,"params":{"id":task_id}});
    let snapshot =
        deepseek_gateway::a2a_hub::handle_a2a_message(&query("tasks/get"), "orchestrator", "");
    assert!(matches!(
        snapshot["result"]["status"]["state"].as_str(),
        Some("submitted" | "working")
    ));
    let canceled =
        deepseek_gateway::a2a_hub::handle_a2a_message(&query("tasks/cancel"), "orchestrator", "");
    assert_eq!(canceled["result"]["status"]["state"], "canceling");
    release.send(()).unwrap();
    let resumed = collect(request("/a2a", query("tasks/resubscribe")).await).await;
    assert_eq!(
        resumed.last().unwrap()["result"]["status"]["state"],
        "canceled"
    );
    assert!(
        !resumed
            .iter()
            .any(|frame| frame["result"]["artifact"]["name"] == "answer")
    );
    let snapshot =
        deepseek_gateway::a2a_hub::handle_a2a_message(&query("tasks/get"), "orchestrator", "");
    assert_eq!(snapshot["result"]["artifacts"], json!([]));
}

struct EnvRestore(Vec<(&'static str, Option<std::ffi::OsString>)>);

impl EnvRestore {
    // Tests in this binary serialize all environment mutations with TEST_LOCK.
    fn set(values: &[(&'static str, &str)]) -> Self {
        let previous = values
            .iter()
            .map(|(key, value)| {
                let previous = std::env::var_os(key);
                unsafe {
                    std::env::set_var(key, value);
                }
                (*key, previous)
            })
            .collect();
        Self(previous)
    }
}

impl Drop for EnvRestore {
    fn drop(&mut self) {
        for (key, previous) in &self.0 {
            unsafe {
                match previous {
                    Some(value) => std::env::set_var(key, value),
                    None => std::env::remove_var(key),
                }
            }
        }
    }
}

#[tokio::test]
async fn real_http_stream_runs_native_upstream_and_enforces_auth_and_disable() {
    use axum::{Json, Router, routing::post};
    use deepseek_policy::entropy::{Entropy, SystemEntropy};
    use futures_util::StreamExt;
    let _guard = TEST_LOCK.lock().await;
    deepseek_gateway::a2a_hub::reset_a2a_for_tests();
    let release = Arc::new(tokio::sync::Notify::new());
    let captured = Arc::new(std::sync::Mutex::new(None));
    let upstream_release = release.clone();
    let upstream_captured = captured.clone();
    let upstream = Router::new().route(
        "/chat/completions",
        post(move |Json(body): Json<Value>| {
            let release = upstream_release.clone();
            let captured = upstream_captured.clone();
            async move {
                *captured.lock().unwrap() = Some(body);
                release.notified().await;
                Json(
                    json!({"id":"local-answer","model":"deepseek-v4-pro","choices":[{
                "message":{"role":"assistant","content":"原生回答"},"finish_reason":"stop"}]}),
                )
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream_url = format!("http://{}/chat/completions", listener.local_addr().unwrap());
    let upstream_server = tokio::spawn(async move {
        axum::serve(listener, upstream).await.unwrap();
    });
    let credential = SystemEntropy.new_id().unwrap();
    let _env = EnvRestore::set(&[
        ("DEEPSEEK_API_URL", &upstream_url),
        ("DEEPSEEK_API_KEY", &credential),
        ("AUTH_TOKEN", &credential),
        ("AUTH_DISABLED", "false"),
        ("A2A_ENABLED", "true"),
    ]);
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir(root.path().join("ui")).unwrap();
    std::fs::write(
        root.path().join("ui/index.html"),
        "<!doctype html><title>test</title>",
    )
    .unwrap();
    let app = deepseek_gateway::create_production_app(root.path()).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/a2a/agents/reasoner",
        listener.local_addr().unwrap()
    );
    let gateway_server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();
    let body = message("message/stream").to_string();
    assert_eq!(
        client
            .post(&url)
            .body(body.clone())
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    let response = client
        .post(&url)
        .bearer_auth(&credential)
        .body(body.clone())
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers()["content-type"],
        "text/event-stream; charset=utf-8"
    );
    let mut stream = response.bytes_stream();
    let first = tokio::time::timeout(Duration::from_secs(2), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let mut bytes = first.to_vec();
    assert_eq!(
        events(&bytes)[0]["result"]["kind"],
        "task",
        "snapshot must arrive before upstream completes"
    );
    release.notify_one();
    while let Some(chunk) = stream.next().await {
        bytes.extend_from_slice(&chunk.unwrap());
    }
    let frames = events(&bytes);
    assert_eq!(
        frames.last().unwrap()["result"]["status"]["state"],
        "completed"
    );
    assert!(
        frames
            .iter()
            .any(|frame| frame["result"]["artifact"]["parts"][0]["text"] == "原生回答")
    );
    let upstream_body = captured.lock().unwrap().clone().unwrap();
    assert_eq!(upstream_body["messages"][1]["content"], "你好");
    assert!(
        upstream_body.get("tools").is_none(),
        "reasoner has no tools"
    );

    let _disabled = EnvRestore::set(&[("A2A_ENABLED", "false")]);
    assert_eq!(
        client
            .post(&url)
            .bearer_auth(&credential)
            .body(body)
            .send()
            .await
            .unwrap()
            .status(),
        403
    );
    gateway_server.abort();
    upstream_server.abort();
    let _ = gateway_server.await;
    let _ = upstream_server.await;
}

#[tokio::test]
async fn execution_failure_is_a_terminal_status_and_invalid_requests_close() {
    let _guard = TEST_LOCK.lock().await;
    deepseek_gateway::a2a_hub::reset_a2a_for_tests();
    deepseek_gateway::a2a_hub::set_task_runner(Arc::new(|_, _| Err("upstream unavailable".into())));
    let failed = collect(request("/a2a", message("message/stream")).await).await;
    let terminal = &failed.last().unwrap()["result"];
    assert_eq!(terminal["status"]["state"], "failed");
    assert_eq!(
        terminal["status"]["message"]["parts"][0]["text"],
        "upstream unavailable"
    );
    assert_eq!(terminal["final"], true);
    for (method, params, code) in [
        ("message/stream", json!({}), -32602),
        ("tasks/resubscribe", json!({"id":"missing"}), -32001),
        ("tasks/resubscribe", json!({"id":42}), -32001),
        ("tasks/resubscribe", json!({"id":0}), -32602),
    ] {
        let frames = collect(
            request(
                "/a2a",
                json!({"jsonrpc":"2.0","id":9,"method":method,"params":params}),
            )
            .await,
        )
        .await;
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0]["error"]["code"], code);
    }
}
