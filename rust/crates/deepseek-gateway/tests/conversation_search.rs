//! `POST /api/conversations/search` through the production router.
//!
//! The body is the client's conversation list. The snippets below are the
//! oracle's character windows for an ASCII query, not a second implementation.

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "conversation-search-token";

struct Harness {
    _static_root: tempfile::TempDir,
    app: axum::Router,
}

fn app() -> Harness {
    unsafe { std::env::set_var("AUTH_TOKEN", TEST_TOKEN) };
    let static_root = tempfile::tempdir().expect("static");
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let app = create_production_app(static_root.path()).expect("app");
    Harness {
        _static_root: static_root,
        app,
    }
}

async fn post(app: &axum::Router, body: &[u8], auth: bool) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method("POST")
        .uri("/api/conversations/search")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::CONTENT_LENGTH, body.len().to_string());
    if auth {
        request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    }
    let response = app
        .clone()
        .oneshot(request.body(Body::from(body.to_vec())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn search_returns_the_oracle_window_and_rejects_a_non_list() {
    let harness = app();
    let app = &harness.app;
    let payload = json!({
        "query": " Needle ",
        "conversations": [
            "skip-me",
            {
                "id": "c1",
                "title": "Hello World",
                "updatedAt": 10,
                "favorite": true,
                "tags": [" Alpha ", ""],
                "messages": [
                    {"id": "m1", "role": "user", "content": "find the needle here", "reasoning": ""}
                ]
            },
            {"id": "c2", "title": "Other", "messages": []}
        ]
    });
    let body = serde_json::to_vec(&payload).unwrap();
    let (status, found) = post(app, &body, true).await;
    assert_eq!(status, StatusCode::OK, "{found}");
    assert_eq!(
        found,
        json!({
            "results": [{
                "id": "c1",
                "title": "Hello World",
                "updatedAt": 10,
                "favorite": true,
                "tags": ["Alpha"],
                "matches": [{
                    "kind": "message",
                    "messageId": "m1",
                    "snippet": "user find the needle here"
                }]
            }]
        })
    );

    let (status, empty) = post(app, br#"{"query":"","conversations":"nope"}"#, true).await;
    assert_eq!(status, StatusCode::OK, "{empty}");
    assert_eq!(empty, json!({"results": []}));

    let (status, rejected) = post(app, br#"{"query":"needle","conversations":{}}"#, true).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{rejected}");
    assert_eq!(
        rejected,
        json!({"error": "conversations must be a list", "code": "invalid_payload"})
    );

    let (status, _) = post(app, br#"{"query":"needle","conversations":[]}"#, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}
