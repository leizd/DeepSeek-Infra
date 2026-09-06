use axum::{Json, Router, http::StatusCode, routing::get};
use deepseek_gateway::create_app;
use serde_json::json;
use tower::ServiceExt;

#[tokio::test]
async fn proxy_api_to_go_forwarding_and_unreachable_behavior() {
    let mock_app = Router::new()
        .route(
            "/api/test-go",
            get(|| async {
                Json(json!({
                    "ok": true,
                    "service": "deepseekd-mock",
                    "authority": "go"
                }))
            }),
        )
        .route(
            "/internal/test-internal",
            get(|| async {
                Json(json!({
                    "ok": true,
                    "service": "deepseekd-internal",
                    "authority": "go"
                }))
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mock_addr = listener.local_addr().unwrap();
    let server_handle = tokio::spawn(async move {
        let _ = axum::serve(listener, mock_app).await;
    });

    let prev = std::env::var("GO_CONTROL_ADDR").ok();
    unsafe {
        std::env::set_var("GO_CONTROL_ADDR", format!("http://{mock_addr}"));
    }

    let app = create_app();
    let req = axum::http::Request::builder()
        .method("GET")
        .uri("/api/test-go")
        .body(axum::body::Body::empty())
        .unwrap();
    let res = app.clone().oneshot(req).await.unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    let body_bytes = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    let val: serde_json::Value = serde_json::from_slice(&body_bytes).unwrap();
    assert_eq!(val["ok"], true);
    assert_eq!(val["authority"], "go");

    // Private Go handlers are never forwarded from the public edge.
    let internal_req = axum::http::Request::builder()
        .method("GET")
        .uri("/internal/test-internal")
        .body(axum::body::Body::empty())
        .unwrap();
    let internal_res = app.clone().oneshot(internal_req).await.unwrap();
    assert_eq!(internal_res.status(), StatusCode::NOT_FOUND);

    unsafe {
        std::env::set_var("GO_CONTROL_ADDR", "http://127.0.0.1:1");
    }
    let unreach_req = axum::http::Request::builder()
        .method("GET")
        .uri("/api/fail")
        .body(axum::body::Body::empty())
        .unwrap();
    let unreach_res = app.oneshot(unreach_req).await.unwrap();
    assert_eq!(unreach_res.status(), StatusCode::SERVICE_UNAVAILABLE);
    let unreach_bytes = axum::body::to_bytes(unreach_res.into_body(), usize::MAX)
        .await
        .unwrap();
    let unreach_val: serde_json::Value = serde_json::from_slice(&unreach_bytes).unwrap();
    assert_eq!(unreach_val["error"]["code"], "GO_CONTROL_UNREACHABLE");

    unsafe {
        match prev {
            Some(val) => std::env::set_var("GO_CONTROL_ADDR", val),
            None => std::env::remove_var("GO_CONTROL_ADDR"),
        }
    }
    server_handle.abort();
}
