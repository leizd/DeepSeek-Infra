use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use axum::{
    Router,
    body::{Body, Bytes},
    http::{HeaderMap, Method, Request, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::any,
};
use deepseek_gateway::{create_app, create_production_app};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tower::ServiceExt;

struct Environment(Vec<(&'static str, Option<std::ffi::OsString>)>);

impl Environment {
    fn set(values: &[(&'static str, &str)]) -> Self {
        Self(
            values
                .iter()
                .map(|&(name, value)| {
                    let previous = std::env::var_os(name);
                    unsafe {
                        std::env::set_var(name, value);
                    }
                    (name, previous)
                })
                .collect(),
        )
    }
}

impl Drop for Environment {
    fn drop(&mut self) {
        for (name, previous) in &self.0 {
            unsafe {
                match previous {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

async fn request(app: &Router, path: &str) -> Response {
    app.clone()
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap()
}

#[tokio::test]
async fn public_edge_cannot_reach_private_control_routes_or_normalized_aliases() {
    let private_calls = Arc::new(AtomicUsize::new(0));
    let upstream_calls = Arc::new(AtomicUsize::new(0));
    let observed_calls = upstream_calls.clone();
    let calls = private_calls.clone();
    let upstream = Router::new()
        .route(
            "/internal/*path",
            any(move || {
                calls.fetch_add(1, Ordering::SeqCst);
                async { StatusCode::OK }
            }),
        )
        .route(
            "/api/redirect",
            any(|| async {
                (
                    StatusCode::TEMPORARY_REDIRECT,
                    [("location", "/internal/cutover/status")],
                )
            }),
        )
        .route(
            "/api/echo",
            any(
                |method: Method, headers: HeaderMap, body: Bytes| async move {
                    let mut response = axum::Json(serde_json::json!({
                "method": method.as_str(), "body": String::from_utf8(body.to_vec()).unwrap(),
                "hop": headers.contains_key("x-request-hop"),
                "proxyAuth": headers.contains_key("proxy-authorization"),
                "authorization": headers.get("authorization").unwrap().to_str().unwrap(),
                "actionId": headers.get("x-action-id").unwrap().to_str().unwrap(),
            })).into_response();
                    response
                        .headers_mut()
                        .insert("connection", "x-response-hop".parse().unwrap());
                    response
                        .headers_mut()
                        .insert("x-response-hop", "not-end-to-end".parse().unwrap());
                    response
                        .headers_mut()
                        .insert("x-response-end", "preserved".parse().unwrap());
                    response
                },
            ),
        )
        .fallback(|uri: Uri| async move { (StatusCode::OK, uri.to_string()) })
        .layer(axum::middleware::from_fn(
            move |request: axum::extract::Request, next: axum::middleware::Next| {
                let observed_calls = observed_calls.clone();
                async move {
                    observed_calls.fetch_add(1, Ordering::SeqCst);
                    next.run(request).await
                }
            },
        ));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async {
        axum::serve(listener, upstream).await.unwrap();
    });
    // This integration-test binary contains one test; no concurrent environment access.
    let _environment = Environment::set(&[
        ("GO_CONTROL_ADDR", &format!("http://{address}")),
        ("HTTP_PROXY", "http://127.0.0.1:1"),
        ("HTTPS_PROXY", "http://127.0.0.1:1"),
        ("ALL_PROXY", "http://127.0.0.1:1"),
        ("NO_PROXY", ""),
    ]);
    let app = create_app();
    let connect = app
        .clone()
        .oneshot(
            Request::builder()
                .method("CONNECT")
                .uri("/api/tunnel")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(connect.status(), StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(upstream_calls.load(Ordering::SeqCst), 0);
    let direct = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/internal/cutover/status?domain=policy")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let direct_status = direct.status();
    for path in [
        "/api/../internal/cutover/status",
        "/api/%2e%2e/internal/cutover/status",
        "/api/%252e%252e/internal/cutover/status",
        "/api/.%2E/internal/cutover/status",
        "/api/x/../../internal/cutover/status",
        "/api/%2f../internal/cutover/status",
        "/api/%5c../internal/cutover/status",
    ] {
        let _ = app
            .clone()
            .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
            .await
            .unwrap();
    }
    let public = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/policies?name=a%2Bb&limit=2")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(public.into_body(), 1024)
        .await
        .unwrap();
    // URL-standard escaping may encode an apostrophe, without changing query
    // meaning, ordering, duplicates or the literal-percent/plus distinction.
    for (path, expected) in [
        (
            "/api/policies?name=O'Reilly",
            "/api/policies?name=O%27Reilly",
        ),
        (
            "/api/policies?x=%27&x=%2527&space=+&plus=%2B",
            "/api/policies?x=%27&x=%2527&space=+&plus=%2B",
        ),
    ] {
        let response = request(&app, path).await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            axum::body::to_bytes(response.into_body(), 1024)
                .await
                .unwrap()
                .as_ref(),
            expected.as_bytes()
        );
    }
    assert_eq!(public_status(&app).await, StatusCode::TEMPORARY_REDIRECT);

    let echo = app
        .clone()
        .oneshot(
            Request::builder()
                .method("PATCH")
                .uri("/api/echo")
                .header("connection", "keep-alive, X-Request-Hop")
                .header("x-request-hop", "remove")
                .header("proxy-authorization", "remove")
                .header("authorization", "Bearer local-test-identity")
                .header("x-action-id", "proxy-action")
                .body(Body::from("opaque mutation body"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(echo.status(), StatusCode::OK);
    assert!(!echo.headers().contains_key("connection"));
    assert!(!echo.headers().contains_key("x-response-hop"));
    assert_eq!(echo.headers()["x-response-end"], "preserved");
    let echo: serde_json::Value =
        serde_json::from_slice(&axum::body::to_bytes(echo.into_body(), 4096).await.unwrap())
            .unwrap();
    assert_eq!(
        echo,
        serde_json::json!({"method":"PATCH", "body":"opaque mutation body", "hop":false,
        "proxyAuth":false, "authorization":"Bearer local-test-identity", "actionId":"proxy-action"})
    );

    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "SPA fallback").unwrap();
    let production = create_production_app(static_root.path()).unwrap();
    for app in [&app, &production] {
        for path in ["/internal", "/internal/", "/internal/shadow/snapshot"] {
            assert_eq!(request(app, path).await.status(), StatusCode::NOT_FOUND);
        }
    }
    server.abort();
    assert_eq!(
        private_calls.load(Ordering::SeqCst),
        0,
        "public traffic reached private Go handlers"
    );
    assert_eq!(direct_status, StatusCode::NOT_FOUND);
    assert_eq!(
        body.as_ref(),
        b"/api/policies?name=a%2Bb&limit=2",
        "query string changed"
    );

    // A real TCP peer returns headers then truncates its body. The proxy must
    // surface the stream error, not fabricate an empty successful body.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let _truncated_origin = Environment::set(&[("GO_CONTROL_ADDR", &format!("http://{address}"))]);
    let peer = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut buffer = [0_u8; 4096];
        let mut received = Vec::new();
        while !received.windows(4).any(|window| window == b"\r\n\r\n") {
            let count = socket.read(&mut buffer).await.unwrap();
            assert_ne!(count, 0);
            received.extend_from_slice(&buffer[..count]);
            assert!(received.len() <= 8192);
        }
        socket
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort")
            .await
            .unwrap();
        socket.shutdown().await.unwrap();
    });
    let truncated = request(&app, "/api/truncated").await;
    assert_eq!(truncated.status(), StatusCode::OK);
    assert!(
        axum::body::to_bytes(truncated.into_body(), 1024)
            .await
            .is_err()
    );
    peer.await.unwrap();
}

async fn public_status(app: &Router) -> StatusCode {
    let response = request(app, "/api/redirect").await;
    assert_eq!(response.headers()["location"], "/internal/cutover/status");
    response.status()
}
