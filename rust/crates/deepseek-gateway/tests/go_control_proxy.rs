use axum::{
    Json, Router,
    body::Bytes,
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use deepseek_gateway::create_app;
use serde_json::json;
use tower::ServiceExt;

#[tokio::test]
async fn proxy_api_to_go_forwarding_and_unreachable_behavior() {
    let mock_app = Router::new()
        .route(
            "/api/media",
            post(|headers: HeaderMap, body: Bytes| async move {
                if headers.contains_key("X-Test-Slow-Upload") {
                    tokio::time::sleep(std::time::Duration::from_secs(11)).await;
                }
                Json(
                    json!({"length":headers.get("content-length").and_then(|v|v.to_str().ok()),
                "transferEncoding":headers.get("transfer-encoding").and_then(|v|v.to_str().ok()),
                "bytes":body.to_vec()}),
                )
            }),
        )
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

    // Actual loopback framing only; successful media ingestion is qualified by
    // the real native-launcher/original-ASGI workload, not this mock endpoint.
    for (has_length, slow) in [(true, false), (false, false), (true, true)] {
        let mut builder = axum::http::Request::builder()
            .method("POST")
            .uri("/api/media")
            .header(
                "Content-Type",
                "multipart/form-data; boundary=transport-fixture",
            );
        if has_length {
            builder = builder.header("Content-Length", "3");
        }
        if slow {
            builder = builder.header("X-Test-Slow-Upload", "1");
        }
        let response = app
            .clone()
            .oneshot(builder.body(axum::body::Body::from("abc")).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap();
        let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(value["bytes"], json!([97, 98, 99]));
        if has_length {
            assert_eq!(value["length"], "3");
        } else {
            assert!(value["length"].is_null());
            assert_eq!(value["transferEncoding"], "chunked");
        }
    }

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
