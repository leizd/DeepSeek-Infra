//! Local qualification through the real Go daemon, typed gRPC and Rust public HTTP.
//! The provider is a recorded, isolated HTTP/SSE fixture; this is not remote-provider evidence.
use std::process::Stdio;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use deepseek_gateway::create_production_app;
use deepseek_policy::multi_agent as policy;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpListener;

const CONTROL_BEARER: &str = "isolated-agent-control-test-bearer-123456";
const USER_BEARER: &str = "isolated-agent-public-test-bearer";

#[derive(Clone)]
struct Provider {
    scenario: String,
    bodies: Arc<Mutex<Vec<Value>>>,
    coder_calls: Arc<AtomicUsize>,
    searches: Arc<AtomicUsize>,
}

async fn provider(State(state): State<Provider>, Json(body): Json<Value>) -> Response {
    state.bodies.lock().unwrap().push(body.clone());
    let messages = body["messages"].as_array().unwrap();
    let contains = |needle: &str| {
        messages.iter().any(|m| {
            m["content"]
                .as_str()
                .is_some_and(|text| text.contains(needle))
        })
    };
    let (text, role) = if contains(policy::PLANNER_SYSTEM) {
        let mut agents = if state.scenario == "planner-prefix" {
            (0..65)
                .map(|index| json!({"id":format!("unknown-{index}"),"task":"ignored"}))
                .collect::<Vec<_>>()
        } else if state.scenario == "planner-duplicate" {
            let mut duplicates = vec![json!({"id":"coder","task":"Analyze code"})];
            duplicates.extend((0..65).map(|_| json!({"id":"coder","task":"ignored duplicate"})));
            duplicates
        } else {
            Vec::new()
        };
        agents.extend([
            json!({"id":"coder","task":"Analyze code"}),
            json!({"id":"critic","task":"Review risk","depends_on":["coder"]}),
        ]);
        (json!({"agents":agents}).to_string(), "planner")
    } else if contains(policy::SYNTHESIZER_SYSTEM) {
        ("最终综合已完成。".into(), "synthesizer")
    } else if contains("你本轮扮演：反驳审查 Agent") {
        let target = if state.scenario == "revision" {
            "coder"
        } else {
            "无"
        };
        (
            format!("## 摘要\n已审查\n## 风险/不确定\n修订建议：{target}\n## 完整分析\nReviewed"),
            "critic",
        )
    } else {
        let attempt = state.coder_calls.fetch_add(1, Ordering::SeqCst);
        if state.scenario == "retry" && attempt == 0 {
            return (
                StatusCode::BAD_GATEWAY,
                "isolated transient provider failure",
            )
                .into_response();
        }
        if state.scenario == "tool-denial" && attempt == 0 {
            let frame = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"forbidden-call","type":"function","function":{"name":"web_search","arguments":"{\"query\":\"not permitted for coder\"}"}}]},"finish_reason":"tool_calls"}]}).to_string();
            return (
                [("content-type", "text/event-stream")],
                format!("data: {frame}\n\ndata: [DONE]\n\n"),
            )
                .into_response();
        }
        (
            format!(
                "## 摘要\n代码分析 {attempt}\n## 关键事实\n事实\n## 风险/不确定\n风险\n## 完整分析\n详细分析"
            ),
            "coder",
        )
    };
    let usage = json!({"prompt_tokens":12,"completion_tokens":8,"total_tokens":20,"prompt_cache_hit_tokens":9,"prompt_cache_miss_tokens":3});
    let frame = json!({"model":"deepseek-v4-flash","choices":[{"delta":{"content":text},"finish_reason":"stop"}],"usage":usage});
    let _ = role;
    (
        [("content-type", "text/event-stream")],
        format!("data: {frame}\n\ndata: [DONE]\n\n"),
    )
        .into_response()
}

async fn search(State(state): State<Provider>) -> Json<Value> {
    state.searches.fetch_add(1, Ordering::SeqCst);
    Json(json!({"results":[]}))
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let scenario = std::env::args().nth(1).expect("scenario is required");
    let executable = std::env::var("DEEPSEEK_AGENT_GO_BINARY").expect("real Go daemon is required");
    let root = tempfile::tempdir().unwrap();
    let static_root = root.path().join("static");
    std::fs::create_dir_all(static_root.join("ui")).unwrap();
    std::fs::write(
        static_root.join("ui/index.html"),
        "<main>isolated native qualification</main>",
    )
    .unwrap();
    let mut go = tokio::process::Command::new(executable);
    go.env(
        "DEEPSEEKD_MODE",
        if scenario == "shadow" {
            "shadow"
        } else {
            "authoritative"
        },
    )
    .env(
        "DEEPSEEKD_PRODUCTION_STORE",
        if scenario == "shadow" {
            String::new()
        } else {
            root.path()
                .join("go-control")
                .to_string_lossy()
                .into_owned()
        },
    )
    .env("DEEPSEEKD_SHADOW_STORE", "")
    .env("DEEPSEEKD_LISTEN", "127.0.0.1:0")
    .env("DEEPSEEKD_INTERNAL_BEARER", CONTROL_BEARER)
    .env("DEEPSEEKD_CONTROL_AUTHORITY", "1")
    .env("DEEPSEEKD_ENVIRONMENT", "isolated-qualification")
    .env(
        "MULTI_AGENT_TOKEN_BUDGET",
        if scenario == "budget" {
            "20"
        } else if scenario == "unlimited" {
            "0"
        } else {
            "2000000"
        },
    )
    .env("MULTI_AGENT_TIMEOUT_SECONDS", "10")
    .stdout(Stdio::piped())
    .stderr(Stdio::inherit())
    .kill_on_drop(true);
    let mut child = go.spawn().expect("start the real Go daemon");
    let mut output = BufReader::new(child.stdout.take().unwrap()).lines();
    let address = tokio::time::timeout(Duration::from_secs(10), async {
        while let Some(line) = output.next_line().await.unwrap() {
            if let Some(rest) = line.strip_prefix("deepseekd listening on ") {
                return rest.split_whitespace().next().unwrap().to_string();
            }
        }
        panic!("Go daemon exited before listening");
    })
    .await
    .expect("Go daemon becomes ready");
    let state = Provider {
        scenario: scenario.clone(),
        bodies: Arc::default(),
        coder_calls: Arc::default(),
        searches: Arc::default(),
    };
    let upstream = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream_address = upstream.local_addr().unwrap();
    // Environment is set before constructing the sole product router in this isolated process.
    for (name, value) in [
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", USER_BEARER.into()),
        ("DEEPSEEK_API_KEY", "isolated-fixture-provider-key".into()),
        (
            "DEEPSEEK_API_URL",
            format!("http://{upstream_address}/chat/completions"),
        ),
        ("TAVILY_API_KEY", "isolated-fixture-search-key".into()),
        (
            "TAVILY_API_URL",
            format!("http://{upstream_address}/search"),
        ),
        ("GO_CONTROL_ADDR", format!("http://{address}")),
        (
            "DEEPSEEK_INTERNAL_BEARER",
            if scenario == "bad-bearer" {
                "incorrect-private-control-bearer-123456".into()
            } else {
                CONTROL_BEARER.into()
            },
        ),
        ("TOOL_POLICY_ENABLED", "false".into()),
    ] {
        unsafe {
            std::env::set_var(name, value);
        }
    }
    let app = create_production_app(&static_root).unwrap();
    let provider_app = axum::Router::new()
        .route("/chat/completions", post(provider))
        .route("/search", post(search))
        .with_state(state.clone());
    let upstream_task = tokio::spawn(async move {
        axum::serve(upstream, provider_app).await.unwrap();
    });
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let gateway_address = listener.local_addr().unwrap();
    let gateway_task = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let response = reqwest::Client::new().post(format!("http://{gateway_address}/api/chat")).bearer_auth(USER_BEARER)
        .header("content-type", "application/json")
        .body(json!({"model":"deepseek-v4-flash","agentMode":true,"stream":true,"memoryEnabled":false,"searchEnabled":false,"messages":[{"role":"user","content":"Explain the implementation"}]}).to_string())
        .send().await.unwrap();
    let status = response.status();
    let body = response.text().await.unwrap();
    let calls = state.bodies.lock().unwrap().clone();
    if matches!(scenario.as_str(), "shadow" | "bad-bearer") {
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
        assert!(body.contains("NATIVE_AGENT_CONTROL_UNAVAILABLE"));
        assert!(calls.is_empty());
    } else {
        assert_eq!(status, StatusCode::OK, "{body}");
        let events: Vec<Value> = body
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert!(!events.iter().any(|e| e["type"] == "error"), "{body}");
        let done = events.last().unwrap();
        assert_eq!(done["type"], "done");
        // The reference stops before the next tier; only executed outputs count.
        assert_eq!(
            done["diagnostics"]["agentCount"],
            if scenario == "budget" { 1 } else { 2 }
        );
        assert_eq!(done["diagnostics"]["agentCache"]["hitRate"], 75.0);
        let expected_tokens = if scenario == "budget" {
            40
        } else if scenario == "revision" {
            80
        } else {
            60
        };
        assert_eq!(done["diagnostics"]["agentTokenBudgetUsed"], expected_tokens);
        assert!(
            events
                .iter()
                .any(|e| e["type"] == "content" && e["text"] == "最终综合已完成。")
        );
        if scenario == "budget" {
            // Token accounting is post-hoc; stop the next tier after coder consumes 20.
            assert_eq!(calls.len(), 3);
            assert_eq!(state.coder_calls.load(Ordering::SeqCst), 1);
            assert_eq!(done["diagnostics"]["agentTokenBudgetLimit"], 20);
            assert_eq!(
                done["diagnostics"]["agentOutputs"]
                    .as_array()
                    .unwrap()
                    .len(),
                1
            );
            assert_eq!(done["diagnostics"]["agentOutputs"][0]["id"], "coder");
            assert!(
                events
                    .iter()
                    .any(|event| event["type"] == "agent_note" && event["phase"] == "leader")
            );
        } else {
            if scenario == "unlimited" {
                assert_eq!(done["diagnostics"]["agentTokenBudgetLimit"], 0);
            }
            assert_eq!(
                done["diagnostics"]["agentOutputs"]
                    .as_array()
                    .unwrap()
                    .len(),
                2
            );
            let expected = if matches!(scenario.as_str(), "revision" | "retry" | "tool-denial") {
                2
            } else {
                1
            };
            assert_eq!(state.coder_calls.load(Ordering::SeqCst), expected, "{body}");
        }
        if scenario == "retry" {
            assert!(
                events
                    .iter()
                    .any(|e| e["type"] == "agent_reset" && e["reason"] == "stream_retry")
            );
        }
        if scenario == "revision" {
            assert!(
                events
                    .iter()
                    .any(|e| e["type"] == "agent_reset" && e["reason"] == "critic_revision")
            );
        }
        if scenario == "tool-denial" {
            assert_eq!(state.searches.load(Ordering::SeqCst), 0);
            assert!(
                calls
                    .iter()
                    .any(|call| call["messages"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|m| m["role"] == "tool"
                            && m["content"]
                                .as_str()
                                .is_some_and(|t| t.to_lowercase().contains("denied")))),
                "{body}"
            );
        }
        let main: String = events
            .iter()
            .filter(|e| e["type"] == "content")
            .filter_map(|e| e["text"].as_str())
            .collect();
        assert_eq!(main, "最终综合已完成。");
    }
    child.kill().await.unwrap();
    child.wait().await.unwrap();
    gateway_task.abort();
    upstream_task.abort();
    println!(
        "{}",
        json!({"scenario":scenario,"status":"PASS","publicHttpStatus":status.as_u16(),"providerRequests":calls.len(),"coderRequests":state.coder_calls.load(Ordering::SeqCst),"searchRequests":state.searches.load(Ordering::SeqCst),"realGoDaemon":true,"remoteProviderQualified":false,"durableAgentRunsQualified":false})
    );
}
