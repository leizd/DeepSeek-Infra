//! The launch URL establishes the same authenticated browser session as the legacy server.
use axum::{
    body::Body,
    http::{Request, StatusCode, header},
};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

#[tokio::test]
async fn launch_token_bootstraps_browser_and_desktop_sessions() {
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir(root.path().join("ui")).unwrap();
    std::fs::write(
        root.path().join("ui/index.html"),
        "<main>native browser auth</main>",
    )
    .unwrap();
    unsafe {
        std::env::remove_var("AUTH_DISABLED");
        std::env::set_var("AUTH_TOKEN", "native-auth-fixture");
        std::env::set_var("DEEPSEEK_INFRA_ROOT", root.path());
    }
    let app = create_production_app(root.path()).unwrap();
    let cookie =
        "auth_token=native-auth-fixture; HttpOnly; Max-Age=2592000; Path=/; SameSite=Strict";
    for (uri, status, location, has_cookie) in [
        (
            "/?token=native-auth-fixture&share=ignored",
            StatusCode::FOUND,
            Some("/"),
            true,
        ),
        (
            "/ui?token=native-auth-fixture",
            StatusCode::FOUND,
            Some("/ui/"),
            true,
        ),
        (
            "/ui/?token=native-auth-fixture",
            StatusCode::FOUND,
            Some("/ui/"),
            true,
        ),
        (
            "/?token=native-auth-fixture&desktop=true",
            StatusCode::OK,
            None,
            true,
        ),
        (
            "/?token=native-auth-fixture&desktop=off",
            StatusCode::FOUND,
            Some("/"),
            true,
        ),
        (
            "/?token=wrong&token=native-auth-fixture",
            StatusCode::FOUND,
            Some("/"),
            true,
        ),
        (
            "/?token=native-auth-fixture&token=wrong",
            StatusCode::UNAUTHORIZED,
            None,
            false,
        ),
        ("/?token=wrong", StatusCode::UNAUTHORIZED, None, false),
        ("/?token=", StatusCode::OK, None, false),
        (
            "/ui/index.html?token=native-auth-fixture",
            StatusCode::OK,
            None,
            false,
        ),
    ] {
        let response = app
            .clone()
            .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), status, "{uri}");
        if has_cookie || status == StatusCode::UNAUTHORIZED {
            assert_eq!(response.headers()["referrer-policy"], "no-referrer");
            assert_eq!(response.headers()["x-content-type-options"], "nosniff");
            assert_eq!(
                response.headers()["cache-control"],
                if status == StatusCode::OK {
                    "no-store"
                } else {
                    "no-cache"
                }
            );
        }
        assert_eq!(
            response
                .headers()
                .get(header::LOCATION)
                .map(|value| value.to_str().unwrap()),
            location,
            "{uri}"
        );
        assert_eq!(
            response
                .headers()
                .get(header::SET_COOKIE)
                .map(|value| value.to_str().unwrap()),
            has_cookie.then_some(cookie),
            "{uri}"
        );
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        if status == StatusCode::UNAUTHORIZED {
            let body: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(body, json!({"error":"Auth required","code":"unauthorized"}));
        }
    }
    for cookie_value in [
        None,
        Some("auth_token=wrong"),
        Some("auth_token=native-auth-fixture"),
    ] {
        let mut request = Request::builder()
            .method("POST")
            .uri("/api/skills?token=native-auth-fixture")
            .header("Content-Type", "application/json");
        if let Some(cookie) = cookie_value {
            request = request.header(header::COOKIE, cookie);
        }
        let response = app
            .clone()
            .oneshot(request.body(Body::from("{\"action\":\"list\"}")).unwrap())
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            if cookie_value == Some("auth_token=native-auth-fixture") {
                StatusCode::OK
            } else {
                StatusCode::UNAUTHORIZED
            }
        );
    }
    unsafe {
        std::env::set_var("AUTH_TOKEN", "a;b c");
    }
    let quoted_app = create_production_app(root.path()).unwrap();
    let response = quoted_app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/?token=a%3Bb+c")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FOUND);
    let quoted_cookie = response.headers()[header::SET_COOKIE].to_str().unwrap();
    assert_eq!(
        quoted_cookie,
        "auth_token=\"a\\073b c\"; HttpOnly; Max-Age=2592000; Path=/; SameSite=Strict"
    );
    let response = quoted_app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/skills")
                .header(header::COOKIE, quoted_cookie.split_once("; ").unwrap().0)
                .header("Content-Type", "application/json")
                .body(Body::from("{\"action\":\"list\"}"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}
