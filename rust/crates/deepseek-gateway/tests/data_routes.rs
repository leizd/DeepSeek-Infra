//! Real HTTP coverage for the native data-plane routes.
//!
//! Every case drives the **production** router (`create_production_app`), so what
//! is measured is the route the browser reaches — registration order ahead of the
//! Go `/api/*` catch-all, the auth layer, and the JSON envelope included — rather
//! than a handler called directly.
//!
//! Two properties matter here and neither is provable from a unit test:
//!
//! 1. **The store is really written, through the fence.** A `create` must land in
//!    `.reminders/reminders.json` under the bound root *and* advance
//!    `.workspace-generation`, because the write goes through the mutation gate.
//!    A route that reported success without storing anything is exactly the
//!    failure this asserts against.
//! 2. **The ownership gate is what decides, and it is symmetric.** While
//!    `reminders_store` is Python's, a mutating action is refused and the file is
//!    left byte-identical; once `DEEPSEEK_RUNTIME_MODE=python_disabled` de-authorises
//!    Python, the *same* request stores the reminder for real. One environment
//!    variable decides which side writes — never both.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

/// `DEEPSEEK_INFRA_ROOT` and `DEEPSEEK_RUNTIME_MODE` are process-wide, so the
/// cases that set them must not overlap. A blocking guard cannot be held across
/// `.await` (and clippy's `await_holding_lock` is right to object), so this is the
/// same spin flag `chat_execution.rs` uses, released by `EnvGuard::drop`.
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
    root: tempfile::TempDir,
}

impl EnvGuard {
    /// Bind a fresh workspace root, plus whatever else the case needs.
    fn set(pairs: &[(&'static str, &str)]) -> Self {
        let root = tempfile::tempdir().expect("a temp workspace root");
        let root_path = root.path().to_str().expect("utf-8 temp path").to_string();
        let mut saved = Vec::new();
        for (name, value) in std::iter::once(("DEEPSEEK_INFRA_ROOT", root_path.as_str()))
            .chain(pairs.iter().copied())
        {
            saved.push((name, std::env::var(name).ok()));
            unsafe {
                std::env::set_var(name, value);
            }
        }
        Self { saved, root }
    }

    fn path(&self) -> &std::path::Path {
        self.root.path()
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

/// The token every case authenticates with. `ProductionAuth::from_env` reads
/// `AUTH_TOKEN`, and an empty configured token never matches — so without this the
/// production layer answers `401` before the route is reached.
const TEST_TOKEN: &str = "unit-data-routes-token";

/// The frontend's own client shape: JSON body with a bearer token.
///
/// `create_production_app` refuses to start without a real `static/ui/index.html`,
/// so the fixture writes one. That is deliberate: this suite measures the
/// *production* router, and stubbing the static layer out would mean measuring a
/// different router than the one the browser reaches.
async fn post(uri: &str, body: Value) -> (StatusCode, Value) {
    post_with_token(uri, body, Some(TEST_TOKEN)).await
}

async fn post_with_token(uri: &str, body: Value, token: Option<&str>) -> (StatusCode, Value) {
    let static_root = tempfile::tempdir().expect("a static root");
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(
        static_root.path().join("ui/index.html"),
        "<!doctype html><main>native ui</main>",
    )
    .unwrap();
    let app = create_production_app(static_root.path()).expect("production app");
    let mut builder = Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    let request = builder.body(Body::from(body.to_string())).unwrap();
    let response = app.oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn read_store(root: &std::path::Path) -> String {
    std::fs::read_to_string(root.join(".reminders/reminders.json")).unwrap_or_default()
}

/// The gate's refusal must not be reachable through the Go proxy catch-all
/// either: a 409 from this route is not a 501 from the proxy.
#[tokio::test]
async fn a_mutating_action_is_refused_while_python_owns_the_store() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post(
        "/api/reminders",
        json!({"action": "create", "title": "t", "content": "c", "dueAt": "2026-01-01T00:00:00Z"}),
    )
    .await;

    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    assert_eq!(body["code"], "NATIVE_REMINDERS_WRITE_NOT_OWNED");
    // Refused means *nothing written*, not "written and reported as refused".
    assert!(
        !_guard.path().join(".reminders/reminders.json").exists(),
        "a refused create must leave no store behind"
    );
}

/// `list` is a read, so it is served for real even while Python owns the writes —
/// the frontend needs it to render, and a refusal here would be a capability
/// regression rather than a correctness guard.
#[tokio::test]
async fn list_is_served_and_reads_the_bound_root() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    // Seed the store the way the Python oracle lays it out.
    let dir = _guard.path().join(".reminders");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("reminders.json"),
        r#"[{"id":"abc","title":"喝水","content":"","dueAt":"2026-01-01T00:00:00+00:00","createdAt":1,"notified":false}]"#,
    )
    .unwrap();

    let (status, body) = post("/api/reminders", json!({"action": "list"})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let reminders = body["reminders"].as_array().expect("a reminders list");
    assert_eq!(reminders.len(), 1);
    assert_eq!(reminders[0]["title"], "喝水");
    assert_eq!(reminders[0]["id"], "abc");
}

/// These routes live behind the same production auth as every other `/api/*`
/// path — they are not a new unauthenticated surface. `auth::requires_auth` keys
/// on the `/api/` prefix, so this pins that the prefix rule still covers them
/// after being merged ahead of the Go catch-all.
#[tokio::test]
async fn the_routes_require_production_auth() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post_with_token("/api/reminders", json!({"action": "list"}), None).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "body: {body}");
    assert_eq!(body["error"]["code"], "UNAUTHORIZED");

    let (status, _) = post_with_token(
        "/api/reminders",
        json!({"action": "list"}),
        Some("the-wrong-token"),
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
}

/// An unknown action is the oracle's `400 invalid_payload`, and an empty body is
/// the oracle's `list` default rather than an error.
#[tokio::test]
async fn unknown_action_and_empty_body_match_the_oracle() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post("/api/reminders", json!({"action": "explode"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    assert_eq!(body["error"], "Unsupported reminder action");
    assert_eq!(body["code"], "invalid_payload");

    // `read_json_body` yields `{}`, which takes the `list` default.
    let (status, body) = post("/api/reminders", json!({})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["reminders"], json!([]));
}

/// The other half of the flip: the *same* request, one mode different, stores the
/// reminder for real — and the write is visible on disk and fenced.
#[tokio::test]
async fn a_mutating_action_writes_once_python_is_de_authorised() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post(
        "/api/reminders",
        json!({"action": "create", "title": "喝水", "content": "多喝水", "dueAt": "2026-01-01T00:00:00Z"}),
    )
    .await;

    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    let reminder = &body["reminder"];
    assert_eq!(reminder["title"], "喝水");
    assert_eq!(reminder["content"], "多喝水");
    // `parse_due_at` renders UTC isoformat, so `Z` becomes `+00:00`.
    assert_eq!(reminder["dueAt"], "2026-01-01T00:00:00+00:00");
    assert_eq!(reminder["notified"], false);
    // `secrets.token_hex(8)` is 16 lowercase hex characters.
    let id = reminder["id"].as_str().expect("an id");
    assert_eq!(id.len(), 16, "id: {id}");
    assert!(
        id.chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    );

    // The stored file is the evidence, not the response body.
    let stored = read_store(_guard.path());
    assert!(stored.contains("喝水"), "store: {stored}");
    assert!(stored.contains(id), "store: {stored}");

    // And the write went through the mutation fence.
    let generation = std::fs::read_to_string(_guard.path().join(".workspace-generation"))
        .expect("the fence generation file");
    assert_eq!(generation.trim(), "2", "one scope bumps twice");

    // A list read now sees it, which is the user-visible round trip.
    let (status, listed) = post("/api/reminders", json!({"action": "list"})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(listed["reminders"].as_array().unwrap().len(), 1);
    assert_eq!(listed["reminders"][0]["id"], id);
}

/// Delete is the second write path, and it must be refused and then work on the
/// same terms as create.
#[tokio::test]
async fn delete_is_refused_then_works_after_the_flip() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".reminders");
    std::fs::create_dir_all(&dir).unwrap();
    let seeded = r#"[{"id":"abc","title":"喝水","content":"","dueAt":"2026-01-01T00:00:00+00:00","createdAt":1,"notified":false}]"#;
    std::fs::write(dir.join("reminders.json"), seeded).unwrap();

    let (status, body) = post("/api/reminders", json!({"action": "delete", "id": "abc"})).await;
    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    assert_eq!(body["code"], "NATIVE_REMINDERS_WRITE_NOT_OWNED");
    // Byte-identical: a refused delete must not have rewritten the file.
    assert_eq!(read_store(_guard.path()), seeded);

    unsafe {
        std::env::set_var("DEEPSEEK_RUNTIME_MODE", "python_disabled");
    }
    let (status, body) = post("/api/reminders", json!({"action": "delete", "id": "abc"})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["deleted"], 1);
    assert_eq!(read_store(_guard.path()), "[]");
}

/// `POST /api/reminders/due` reads like a query and writes: the oracle marks each
/// newly-due entry `notified` and rewrites the file. So it is gated, and after the
/// flip it really marks the entry.
#[tokio::test]
async fn the_due_poll_is_gated_because_it_marks_notified() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".reminders");
    std::fs::create_dir_all(&dir).unwrap();
    let seeded = r#"[{"id":"abc","title":"喝水","content":"","dueAt":"2000-01-01T00:00:00+00:00","createdAt":1,"notified":false}]"#;
    std::fs::write(dir.join("reminders.json"), seeded).unwrap();

    let (status, body) = post("/api/reminders/due", json!({})).await;
    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    assert_eq!(body["code"], "NATIVE_REMINDERS_WRITE_NOT_OWNED");
    assert_eq!(read_store(_guard.path()), seeded);

    unsafe {
        std::env::set_var("DEEPSEEK_RUNTIME_MODE", "python_disabled");
    }
    let (status, body) = post("/api/reminders/due", json!({})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let due = body["reminders"].as_array().expect("a due list");
    assert_eq!(due.len(), 1);
    assert_eq!(due[0]["id"], "abc");
    assert_eq!(due[0]["notified"], true);
    assert!(due[0]["notifiedAt"].is_i64());

    // The marking is persisted, not just reported.
    let stored = read_store(_guard.path());
    assert!(stored.contains("\"notified\": true"), "store: {stored}");
}

/// A second poll must not re-report an entry it already marked, which is the
/// property the delivery loop depends on.
#[tokio::test]
async fn a_second_due_poll_reports_nothing() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".reminders");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("reminders.json"),
        r#"[{"id":"abc","title":"喝水","content":"","dueAt":"2000-01-01T00:00:00+00:00","createdAt":1,"notified":false}]"#,
    )
    .unwrap();

    let (_, first) = post("/api/reminders/due", json!({})).await;
    assert_eq!(first["reminders"].as_array().unwrap().len(), 1);

    let (status, second) = post("/api/reminders/due", json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(second["reminders"], json!([]));
}

/// A missing `dueAt` is the oracle's `400 invalid_payload` with its own message,
/// and it must not have written a partial store.
#[tokio::test]
async fn a_create_without_due_at_is_the_oracles_400() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post("/api/reminders", json!({"action": "create", "title": "t"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    assert_eq!(body["error"], "Reminder dueAt is required");
    assert_eq!(body["code"], "invalid_payload");
    assert!(!_guard.path().join(".reminders/reminders.json").exists());
}

// --- memory ------------------------------------------------------------------------
//
// `deepseek_infra/infra/memory/` projects the *same* `.memory/memories.json` the chat
// turn writes, so it is one store with one gate. These cases pin the gate, the flip,
// and the two oracle behaviours that are easy to get wrong: the 409 conflict path and
// the bare `int()` on `limit`.

async fn get(uri: &str) -> (StatusCode, Value) {
    request("GET", uri, None).await
}

#[tokio::test]
async fn workspace_projects_read_real_metadata_without_creating_runtime_state() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN)]);
    let directory = guard.path().join(".projects/proj-read");
    std::fs::create_dir_all(&directory).unwrap();
    std::fs::write(
        directory.join("project.json"),
        json!({
            "id": "proj-read", "name": "Reading", "description": " detail ",
            "createdAt": 1700000000000_i64, "updatedAt": 1700000001000_i64,
            "conversations": [{"id": "conv-1", "createdAtMs": 1700000000000_i64,
                "messages": [{"role": "user", "content": "Hello"}]}]
        })
        .to_string(),
    )
    .unwrap();
    let (status, body) = get("/api/workspace/projects/proj-read").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["project"]["description"], "detail");
    assert_eq!(body["project"]["stats"]["conversations"], 1);
    assert_eq!(
        body["project"]["conversations"][0]["messages"][0]["content"],
        "Hello"
    );
    assert!(!guard.path().join(".workspace-generation").exists());
    assert!(!guard.path().join(".workspace-mutation.lock").exists());
}

async fn request(method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
    let static_root = tempfile::tempdir().expect("a static root");
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(
        static_root.path().join("ui/index.html"),
        "<!doctype html><main>native ui</main>",
    )
    .unwrap();
    let app = create_production_app(static_root.path()).expect("production app");
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let request = match body {
        Some(value) => builder
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(value.to_string()))
            .unwrap(),
        None => {
            builder = builder.header(header::CONTENT_TYPE, "application/json");
            builder.body(Body::empty()).unwrap()
        }
    };
    let response = app.oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn read_memory_store(root: &std::path::Path) -> String {
    std::fs::read_to_string(root.join(".memory/memories.json")).unwrap_or_default()
}

fn seed_project_oracle(root: &std::path::Path) -> Value {
    let cases: Vec<Value> = serde_json::from_str(include_str!(
        "../../deepseek-policy/tests/fixtures/workspace_projects_oracle.json"
    ))
    .unwrap();
    let case = &cases[0];
    assert_eq!(case["name"], "full_children");
    for (relative, value) in case["files"].as_object().unwrap() {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, value.to_string()).unwrap();
    }
    case.clone()
}

#[tokio::test]
async fn native_project_metadata_mutations_are_refused_and_leave_no_store() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);
    // The exact sequence the write cutover will have to satisfy, refused for now:
    // the project store has no native writer until the ownership decision lands,
    // so create/rename/save-conversation must all refuse instead of persisting.
    let (status, body) = post(
        "/api/workspace/projects",
        json!({"name": " Native ", "description": " first "}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{body}");
    assert_eq!(body["code"], "NATIVE_PROJECTS_MUTATIONS_NOT_READY");

    let uri = "/api/workspace/projects/proj-read";
    let (status, body) = request(
        "PATCH",
        uri,
        Some(json!({"name": "Renamed", "description": "second"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{body}");
    assert_eq!(body["code"], "NATIVE_PROJECTS_MUTATIONS_NOT_READY");

    let (status, body) = post(
        &format!("{uri}/conversations"),
        json!({"id": "conv-save", "title": "Remember", "messages": [{"role": "user", "content": "stored"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{body}");
    assert_eq!(body["code"], "NATIVE_PROJECTS_MUTATIONS_NOT_READY");

    // A refusal that had already written something would be worse than the refusal.
    assert!(!guard.path().join(".projects").exists());
    assert!(!guard.path().join(".workspace-generation").exists());
    assert_eq!(std::fs::read_dir(guard.path()).unwrap().count(), 0);
}

#[tokio::test]
async fn project_http_envelopes_and_filters_match_the_storage_oracle() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);
    let case = seed_project_oracle(guard.path());
    let expected = &case["expected"];
    for (uri, body) in [
        (
            "/api/workspace/projects",
            json!({"ok": true, "projects": expected["list"]["ok"]}),
        ),
        (
            "/api/workspace/projects/proj-read",
            json!({"ok": true, "project": expected["get"]["ok"]}),
        ),
        (
            "/api/workspace/projects/proj-read/conversations",
            json!({"ok": true, "conversations": expected["conversations"]["ok"]}),
        ),
        (
            "/api/workspace/projects/proj-read/saved-items",
            json!({"ok": true, "savedItems": expected["saved"]["ok"]}),
        ),
        (
            "/api/workspace/projects/proj-read/saved-items?type=chat_snippet&tags=A",
            json!({"ok": true, "savedItems": expected["saved_filtered"]["ok"]}),
        ),
        (
            "/api/workspace/projects/proj-read/artifacts",
            json!({"ok": true, "artifacts": expected["artifacts"]["ok"]}),
        ),
    ] {
        let (status, actual) = get(uri).await;
        assert_eq!(status, StatusCode::OK, "{uri}: {actual}");
        assert_eq!(actual, body, "{uri}");
    }
    for action in [Value::Null, json!(false), json!(0), json!(" LIST ")] {
        let (status, body) = post("/api/projects", json!({"action": action})).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"projects": expected["legacy_list"]["ok"]}));
    }
    let (status, body) = post(
        "/api/projects",
        json!({"action": "get", "id": false, "projectId": "proj-read"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body, json!({"ok": true, "project": expected["get"]["ok"]}));
    for (relative, value) in case["files"].as_object().unwrap() {
        assert_eq!(
            std::fs::read_to_string(guard.path().join(relative)).unwrap(),
            value.to_string()
        );
    }
    assert!(!guard.path().join(".workspace-generation").exists());
    assert!(!guard.path().join(".workspace-mutation.lock").exists());
}

#[tokio::test]
async fn project_errors_distinguish_bad_ids_missing_projects_and_bad_child_data() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN)]);
    seed_project_oracle(guard.path());
    for (uri, expected_status, code) in [
        (
            "/api/workspace/projects/bad",
            StatusCode::BAD_REQUEST,
            "invalid_payload",
        ),
        (
            "/api/workspace/projects/proj-missing",
            StatusCode::NOT_FOUND,
            "not_found",
        ),
        (
            "/api/workspace/projects/proj-read/saved-items?type=invalid",
            StatusCode::BAD_REQUEST,
            "invalid_payload",
        ),
    ] {
        let (status, body) = get(uri).await;
        assert_eq!(status, expected_status, "{uri}: {body}");
        assert_eq!(body["code"], code);
    }
    std::fs::write(
        guard.path().join(".projects/proj-read/saved-items.json"),
        r#"{"items":[{"id":"s","type":"invalid"}]}"#,
    )
    .unwrap();
    let (status, body) = get("/api/workspace/projects/proj-read/saved-items").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"], "Unsupported saved item type");
    let (status, body) = get("/api/workspace/projects/proj-read").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["project"]["stats"]["savedItems"], 0);
    let (status, body) = post("/api/projects", json!({"action": "unsupported"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"], "Unsupported project action");
}

#[tokio::test]
async fn project_mutations_stay_closed_in_every_runtime_mode_without_disk_effects() {
    let _env_lock = EnvLock::acquire();
    for mode in [
        "python_authoritative",
        "go_authoritative",
        "python_disabled",
    ] {
        let guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN), ("DEEPSEEK_RUNTIME_MODE", mode)]);
        for action in ["create", "rename", "delete"] {
            let (status, body) = post(
                "/api/projects",
                json!({"action": action, "id": "proj-read", "name": "test"}),
            )
            .await;
            assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "{mode}: {body}");
            assert_eq!(body["code"], "NATIVE_PROJECTS_MUTATIONS_NOT_READY");
        }
        for (method, uri) in [
            ("POST", "/api/workspace/projects"),
            ("PATCH", "/api/workspace/projects/proj-read"),
            ("DELETE", "/api/workspace/projects/proj-read"),
            ("POST", "/api/workspace/projects/proj-read/conversations"),
            ("POST", "/api/workspace/projects/proj-read/saved-items"),
            ("POST", "/api/workspace/projects/proj-read/artifacts"),
        ] {
            let (status, body) = request(method, uri, Some(json!({}))).await;
            assert_eq!(
                status,
                StatusCode::NOT_IMPLEMENTED,
                "{method} {uri}: {body}"
            );
        }
        assert_eq!(std::fs::read_dir(guard.path()).unwrap().count(), 0);
    }
}

#[tokio::test]
async fn project_auth_and_request_limits_are_enforced_by_the_production_router() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN)]);
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let app = create_production_app(static_root.path()).unwrap();
    for uri in [
        "/api/workspace/projects",
        "/api/workspace/projects/proj-read",
        "/api/workspace/projects/proj-read/conversations",
        "/api/workspace/projects/proj-read/saved-items",
        "/api/workspace/projects/proj-read/artifacts",
    ] {
        let response = app
            .clone()
            .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{uri}");
    }
    let (status, _) = post_with_token("/api/projects", json!({}), None).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    for (body, status, code, error_prefix) in [
        (
            String::new(),
            StatusCode::BAD_REQUEST,
            "invalid_payload",
            "Request body is empty",
        ),
        (
            "[]".into(),
            StatusCode::BAD_REQUEST,
            "invalid_payload",
            "Request body must be a JSON object",
        ),
        (
            "{".into(),
            StatusCode::BAD_REQUEST,
            "invalid_payload",
            "Invalid JSON:",
        ),
        (
            " ".repeat(2_000_001),
            StatusCode::PAYLOAD_TOO_LARGE,
            "upload_too_large",
            "Request body is too large",
        ),
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/projects")
                    .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), status);
        let body: Value = serde_json::from_slice(
            &axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(body["code"], code);
        assert!(
            body["error"].as_str().unwrap().starts_with(error_prefix),
            "{body}"
        );
    }
    assert_eq!(std::fs::read_dir(guard.path()).unwrap().count(), 0);
}

/// The list projection is served while Python owns the writes, and it reads the
/// bound root through the v3.0 shape.
#[tokio::test]
async fn memory_list_is_served_with_the_v3_shape() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".memory");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("memories.json"),
        r#"[{"id":"m1","content":"likes dark mode","category":"preference","scope":"global","createdAt":"2020-01-01T00:00:00+00:00"}]"#,
    )
    .unwrap();

    let (status, body) = get("/api/memory").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let memories = body["memories"].as_array().expect("a memory list");
    assert_eq!(memories.len(), 1);
    // The v3.0 shape *and* the legacy aliases the frontend still reads.
    assert_eq!(memories[0]["memoryId"], "m1");
    assert_eq!(memories[0]["id"], "m1");
    assert_eq!(memories[0]["type"], "preference");
    assert_eq!(memories[0]["category"], "preference");
    assert_eq!(memories[0]["legacyScope"], "global");
    assert_eq!(memories[0]["pinned"], false);
}

/// Every memory mutation is refused while Python owns the store, and refused means
/// **nothing written**.
#[tokio::test]
async fn memory_mutations_are_refused_while_python_owns_the_store() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    for body in [
        json!({"action": "add", "content": "a new fact"}),
        json!({"action": "clear"}),
        json!({"action": "delete", "query": "fact"}),
        json!({"action": "deletebyid", "id": "m1"}),
    ] {
        let (status, response) = post("/api/memory", body.clone()).await;
        assert_eq!(status, StatusCode::CONFLICT, "body {body}: {response}");
        assert_eq!(response["code"], "NATIVE_MEMORY_WRITE_NOT_OWNED");
    }

    // The verb routes are gated on the same terms.
    let (status, response) = request("DELETE", "/api/memory/m1", None).await;
    assert_eq!(status, StatusCode::CONFLICT, "body: {response}");
    assert_eq!(response["code"], "NATIVE_MEMORY_WRITE_NOT_OWNED");

    let (status, response) =
        request("PATCH", "/api/memory/m1", Some(json!({"content": "x"}))).await;
    assert_eq!(status, StatusCode::CONFLICT, "body: {response}");
    assert_eq!(response["code"], "NATIVE_MEMORY_WRITE_NOT_OWNED");

    assert!(
        !_guard.path().join(".memory/memories.json").exists(),
        "a refused mutation must leave no store behind"
    );
}

/// The flip: the same request, one mode different, writes for real — and the write is
/// fenced and visible on disk.
#[tokio::test]
async fn memory_add_writes_once_python_is_de_authorised() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post(
        "/api/memory",
        json!({"action": "add", "content": "likes dark mode", "category": "preference"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    let memory = &body["memory"];
    assert_eq!(memory["content"], "likes dark mode");
    assert_eq!(memory["type"], "preference");
    assert_eq!(memory["scope"], "global");
    let id = memory["memoryId"].as_str().expect("a memoryId");
    assert_eq!(id.len(), 20, "a content-addressed fingerprint: {id}");

    let stored = read_memory_store(_guard.path());
    assert!(stored.contains("likes dark mode"), "store: {stored}");
    let generation = std::fs::read_to_string(_guard.path().join(".workspace-generation"))
        .expect("the fence generation file");
    // **Four**, not two: the oracle's `add_memory` saves twice — `upsert_memory`
    // persists, then `save_memories(_merge_item(item))` persists the public fields it
    // just added — and each save is one fenced scope bumping the counter twice.
    // Measured against the oracle, which reports the same `4` for one `add_memory`.
    assert_eq!(generation.trim(), "4", "two saves, two bumps each");

    // The round trip through the list projection.
    let (status, listed) = get("/api/memory").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(listed["memories"].as_array().unwrap().len(), 1);
    assert_eq!(listed["memories"][0]["memoryId"], id);
}

/// A conflicting add is a **409 listing the conflicts**, and it is checked before the
/// gate — reporting conflicts is not a write.
///
/// The content must **differ** from the stored row: the oracle skips a candidate whose
/// normalised content equals the incoming content (`memory.py:302`), so an identical
/// add is not a conflict at all — it is the update path. Measured, not assumed.
#[tokio::test]
async fn memory_add_reports_conflicts_before_the_gate() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".memory");
    std::fs::create_dir_all(&dir).unwrap();
    // Same category and scope, same conflict domain (`preference:theme`), different
    // content — so it conflicts.
    std::fs::write(
        dir.join("memories.json"),
        r#"[{"id":"m1","content":"likes dark mode","category":"preference","scope":"global","createdAt":"2020-01-01T00:00:00+00:00"}]"#,
    )
    .unwrap();

    let (status, body) = post(
        "/api/memory",
        json!({"action": "add", "content": "prefers light mode instead", "category": "preference"}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    assert_eq!(body["code"], "memory_conflict");
    assert_eq!(body["error"], "Memory conflicts with an existing item");
    let conflicts = body["conflicts"].as_array().expect("the conflicts list");
    assert_eq!(conflicts.len(), 1, "the conflicts must be listed: {body}");
    assert_eq!(conflicts[0]["id"], "m1");
    assert_eq!(conflicts[0]["reason"], "same_memory_domain");
    assert_eq!(conflicts[0]["scope"], "global");
}

/// `replaceIds` suppresses the conflicts it names, so the add proceeds — to the gate.
#[tokio::test]
async fn memory_replace_ids_clear_the_conflict() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".memory");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("memories.json"),
        r#"[{"id":"m1","content":"likes dark mode","category":"preference","scope":"global","createdAt":"2020-01-01T00:00:00+00:00"}]"#,
    )
    .unwrap();

    let (status, body) = post(
        "/api/memory",
        json!({
            "action": "add",
            "content": "prefers light mode instead",
            "category": "preference",
            "replaceIds": ["m1"],
        }),
    )
    .await;
    // No conflict now, so the request reaches the ownership gate instead.
    assert_eq!(status, StatusCode::CONFLICT, "body: {body}");
    assert_eq!(body["code"], "NATIVE_MEMORY_WRITE_NOT_OWNED");
}

/// `limit` goes through a **bare** `int()` in the oracle, so a non-numeric value is a
/// 400 rather than a silent fallback — and `search` is a read, so it is served.
#[tokio::test]
async fn memory_search_serves_and_rejects_a_non_numeric_limit() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".memory");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("memories.json"),
        r#"[{"id":"m1","content":"rust ownership notes","category":"fact","scope":"global","createdAt":"2020-01-01T00:00:00+00:00"}]"#,
    )
    .unwrap();

    let (status, body) = get("/api/memory/search?q=rust%20ownership&limit=10").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["memories"].as_array().unwrap().len(), 1);

    let (status, body) = get("/api/memory/search?q=rust&limit=abc").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    assert_eq!(body["code"], "invalid_payload");
}

/// The conflict *probe* is a read, so it answers even while Python owns the writes.
#[tokio::test]
async fn memory_conflict_probe_is_a_read() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post(
        "/api/memory/conflicts",
        json!({"content": "anything at all", "category": "fact"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["conflicts"], json!([]));
}

/// An unknown action is the oracle's 400, and an empty body takes the `add` default
/// (which is refused for the empty content it then carries, not for the action).
#[tokio::test]
async fn memory_unknown_action_is_the_oracles_400() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);

    let (status, body) = post("/api/memory", json!({"action": "explode"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    assert_eq!(body["error"], "Unsupported memory action");
    assert_eq!(body["code"], "invalid_payload");
}

/// Delete-by-id works after the flip and is a no-op the second time.
#[tokio::test]
async fn memory_delete_by_id_after_the_flip() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".memory");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("memories.json"),
        r#"[{"id":"m1","content":"a fact","category":"fact","scope":"global","createdAt":"2020-01-01T00:00:00+00:00"}]"#,
    )
    .unwrap();

    let (status, body) = request("DELETE", "/api/memory/m1", None).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["deleted"], 1);
    assert_eq!(read_memory_store(_guard.path()), "[]");

    let (status, body) = request("DELETE", "/api/memory/m1", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["deleted"], 0);
}

/// `PATCH` applies the oracle's field-wise patch after the flip, including the
/// `type`/`category` pair that is easy to write only half of.
#[tokio::test]
async fn memory_edit_after_the_flip() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("AUTH_TOKEN", TEST_TOKEN),
    ]);
    let dir = _guard.path().join(".memory");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("memories.json"),
        r#"[{"id":"m1","content":"a fact","category":"fact","scope":"global","createdAt":"2020-01-01T00:00:00+00:00"}]"#,
    )
    .unwrap();

    let (status, body) = request(
        "PATCH",
        "/api/memory/m1",
        Some(json!({"content": "an instruction", "type": "instruction", "pinned": true})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["memory"]["content"], "an instruction");
    assert_eq!(body["memory"]["type"], "instruction");
    // `edit_memory` writes both from the *public* type; measured against the oracle.
    assert_eq!(body["memory"]["category"], "instruction");
    assert_eq!(body["memory"]["pinned"], true);

    let (status, body) = request(
        "PATCH",
        "/api/memory/missing",
        Some(json!({"content": "x"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "body: {body}");
    assert_eq!(body["code"], "not_found");
}
