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

/// The taint block is a diagnostics read, but it is still behind the production auth
/// layer: it names the sensitive tool set and the exfiltration tables, which is
/// reconnaissance an unauthenticated caller must not get.
#[tokio::test]
async fn the_taint_route_is_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN)]);
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let app = create_production_app(static_root.path()).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .uri("/api/taint")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
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

// --- diagnostics status routes -----------------------------------------------------
//
// These are the read-only blocks the frontend's config/diagnostics screens read. They
// used to fall through to the Go `/api/*` catch-all, which answers 503 when no Go
// control plane is configured — so "the route exists but nothing serves it" was the
// state being measured, not a working endpoint.

/// `GET /api/taint` serves the ported `taint_status` block.
///
/// The block itself is byte-identical to the oracle's (`context_taint` is a complete
/// port, pinned by the context-taint probe); what this case adds is that the *route*
/// serves it, honours the environment, and is not the proxy's refusal.
#[tokio::test]
async fn the_taint_status_route_serves_the_ported_block() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("TAINT_ENABLED", "0"),
        ("TAINT_ESCALATE_CONFIRM", "0"),
        ("TAINT_MAX_SEGMENTS", "999"),
    ]);

    let (status, body) = get("/api/taint").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    let taint = &body["contextTaint"];
    // The two flags this case set, and the default of the one it did not.
    assert_eq!(taint["enabled"], false);
    assert_eq!(taint["escalateConfirm"], false);
    assert_eq!(taint["hardenSearchContext"], true);
    // The tables the block reports are the ported ones, not constants invented here.
    assert_eq!(taint["exfiltrationPatterns"], 3);
    assert!(taint["toolDirectivePatterns"].as_u64().unwrap_or(0) > 0);
    assert!(
        taint["sensitiveToolNames"]
            .as_array()
            .is_some_and(|names| !names.is_empty()),
        "the block must name the sensitive tools: {taint}"
    );
    assert_eq!(
        taint["trustLevels"],
        json!(["trusted", "untrusted"]),
        "body: {body}"
    );
    assert!(
        taint["sources"]
            .as_array()
            .is_some_and(|sources| sources.len() == 10),
        "body: {body}"
    );
}

/// A `TAINT_MAX_SEGMENTS` outside the oracle's range is clamped, not refused, and the
/// clamp is the oracle's `(4, 200)` — not a bound chosen here.
#[tokio::test]
async fn the_taint_segment_cap_is_clamped_like_the_oracle() {
    let _env_lock = EnvLock::acquire();
    let _guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN), ("TAINT_MAX_SEGMENTS", "999")]);
    let (status, body) = get("/api/taint").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    // The block does not report the cap, so the clamp is proved through the policy
    // object the route builds rather than through the JSON.
    let settings = deepseek_policy::context_taint::ContextTaintSettings::from_env();
    assert_eq!(settings.max_segments, 200);
    assert!(settings.enabled, "an unset flag keeps the oracle's default");
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
async fn native_project_metadata_mutations_persist_only_after_python_is_disabled() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);
    let (status, body) = post(
        "/api/workspace/projects",
        json!({"name": " Native ", "description": " first "}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["project"]["name"], "Native");
    assert_eq!(body["project"]["description"], "first");
    let project_id = body["project"]["id"].as_str().unwrap();
    assert!(project_id.starts_with("proj-"));
    let record = guard
        .path()
        .join(".projects")
        .join(project_id)
        .join("project.json");
    let stored: Value = serde_json::from_str(&std::fs::read_to_string(&record).unwrap()).unwrap();
    assert_eq!(stored["name"], "Native");
    assert_eq!(stored["description"], "first");

    let uri = format!("/api/workspace/projects/{project_id}");
    let (status, body) = request(
        "PATCH",
        &uri,
        Some(json!({"name": "Renamed", "description": "second"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["project"]["name"], "Renamed");
    assert_eq!(body["project"]["description"], "second");

    let (status, body) = post(
        &format!("{uri}/conversations"),
        json!({"id": "conv-save", "title": "Remember", "messages": [{"role": "user", "content": "stored"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["conversation"]["conversationId"], "conv-save");
    assert_eq!(body["conversation"]["title"], "Remember");
    let (status, listed) = get(&format!("{uri}/conversations")).await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed["conversations"][0]["conversationId"], "conv-save");

    let (status, deleted) = request("DELETE", &uri, None).await;
    assert_eq!(status, StatusCode::OK, "{deleted}");
    assert_eq!(deleted["deleted"], 1);
    assert!(!record.exists());
    assert!(!guard.path().join(".workspace-generation").exists());
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
async fn project_mutations_stay_closed_until_python_is_deauthorised() {
    let _env_lock = EnvLock::acquire();
    for mode in ["python_authoritative", "go_authoritative", ""] {
        let guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN), ("DEEPSEEK_RUNTIME_MODE", mode)]);
        for action in ["create", "rename", "delete"] {
            let (status, body) = post(
                "/api/projects",
                json!({"action": action, "id": "proj-read", "name": "test"}),
            )
            .await;
            assert_eq!(status, StatusCode::CONFLICT, "{mode}: {body}");
            assert_eq!(body["code"], "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED");
        }
        for (method, uri) in [
            ("POST", "/api/workspace/projects"),
            ("PATCH", "/api/workspace/projects/proj-read"),
            ("DELETE", "/api/workspace/projects/proj-read"),
            ("POST", "/api/workspace/projects/proj-read/conversations"),
            ("POST", "/api/workspace/projects/proj-read/saved-items"),
            (
                "PATCH",
                "/api/workspace/projects/proj-read/saved-items/save_keep",
            ),
            (
                "DELETE",
                "/api/workspace/projects/proj-read/saved-items/save_keep",
            ),
            ("POST", "/api/workspace/projects/proj-read/artifacts"),
            (
                "PATCH",
                "/api/workspace/projects/proj-read/artifacts/art_keep",
            ),
            (
                "DELETE",
                "/api/workspace/projects/proj-read/artifacts/art_keep",
            ),
        ] {
            let (status, body) = request(method, uri, Some(json!({}))).await;
            assert_eq!(
                status,
                StatusCode::CONFLICT,
                "{mode} {method} {uri}: {body}"
            );
            assert_eq!(body["code"], "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED");
        }
        assert_eq!(std::fs::read_dir(guard.path()).unwrap().count(), 0);
    }
}

#[tokio::test]
async fn project_artifact_writes_match_python_and_refuse_without_a_store_change() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);
    let project_dir = guard.path().join(".projects/proj_ok");
    std::fs::create_dir_all(&project_dir).unwrap();
    let project_bytes =
        b"{\"id\":\"proj_ok\",\"name\":\"Demo\",\"createdAt\":10,\"updatedAt\":10}\n";
    std::fs::write(project_dir.join("project.json"), project_bytes).unwrap();
    let artifact_bytes = serde_json::to_vec_pretty(&json!({
        "artifacts": [{
            "artifactId": "art_keep",
            "projectId": "proj_ok",
            "type": "txt",
            "title": "Original",
            "path": "notes/old.txt",
            "source": {},
            "version": 1,
            "versions": [{
                "version": 1,
                "path": "notes/old.txt",
                "createdAt": "2026-01-01T00:00:00Z",
                "createdAtMs": 10
            }],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "createdAtMs": 10,
            "updatedAtMs": 10
        }]
    }))
    .unwrap();
    let artifact_path = project_dir.join("artifacts.json");
    let project_path = project_dir.join("project.json");
    std::fs::write(&artifact_path, &artifact_bytes).unwrap();

    let (status, body) = post(
        "/api/workspace/projects/no/artifacts",
        json!({"path": "notes/kept.md"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Invalid project id");
    let (status, body) = post(
        "/api/workspace/projects/proj_missing/artifacts",
        json!({"path": "notes/kept.md", "title": "Nope"}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"], "Project not found");
    assert!(!guard.path().join(".projects/proj_missing").exists());
    let (status, body) = post("/api/workspace/projects/proj_ok/artifacts", json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Artifact path is required");
    let (status, body) = post(
        "/api/workspace/projects/proj_ok/artifacts",
        json!({"path": "../escape.txt"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Artifact path must not escape the workspace");
    let (status, body) = post(
        "/api/workspace/projects/proj_ok/artifacts",
        json!({"path": "notes/file.bin"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Unsupported artifact type");
    let (status, body) = post("/api/workspace/projects/proj_ok/artifacts", json!([])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Request body must be a JSON object");
    let (status, body) = request("POST", "/api/workspace/projects/proj_ok/artifacts", None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Request body is empty");
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/artifacts/x",
        Some(json!({"title": "Nope"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Invalid artifact id");
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/artifacts/art_gone",
        Some(json!({"title": "Nope"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"], "Artifact not found");
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/artifacts/art_keep",
        Some(json!({"path": "../nope.md"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Artifact path must not escape the workspace");
    let (status, body) = request(
        "DELETE",
        "/api/workspace/projects/proj_ok/artifacts/art_gone",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["deleted"], 0);
    assert_eq!(std::fs::read(&artifact_path).unwrap(), artifact_bytes);
    assert_eq!(std::fs::read(&project_path).unwrap(), project_bytes);
    assert!(!guard.path().join(".workspace-generation").exists());

    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let app = create_production_app(static_root.path()).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/workspace/projects/proj_ok/artifacts/art_keep")
                .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(
        response
            .headers()
            .get(header::ALLOW)
            .and_then(|value| value.to_str().ok()),
        Some("PATCH, DELETE")
    );
    let detail: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(detail, json!({"detail": "Method Not Allowed"}));

    let copy = tempfile::tempdir().unwrap();
    copy_project_tree(guard.path(), copy.path());
    let oracle = artifact_oracle(copy.path());
    let (status, created) = post(
        "/api/workspace/projects/proj_ok/artifacts",
        json!({"title": "Kept", "path": "notes/kept.md", "source": {"kind": "note"}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{created}");
    assert_eq!(
        strip_artifact(&created["artifact"]),
        strip_artifact(&oracle["created"])
    );
    assert!(
        created["artifact"]["artifactId"]
            .as_str()
            .unwrap()
            .starts_with("art_")
    );
    assert!(
        created["artifact"]["downloadUrl"]
            .as_str()
            .unwrap()
            .contains("projectId=proj_ok")
    );
    assert!(project_updated_at(guard.path()) > 10);
    let (status, listed) = get("/api/workspace/projects/proj_ok/artifacts").await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed["artifacts"].as_array().unwrap().len(), 2);

    let (status, updated) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/artifacts/art_keep",
        Some(json!({"title": "Renamed", "source": {"kind": "edit"}, "ignored": 1})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{updated}");
    assert_eq!(
        strip_artifact(&updated["artifact"]),
        strip_artifact(&oracle["updated"])
    );
    assert_eq!(updated["artifact"]["version"], 1);
    assert_eq!(updated["artifact"]["path"], "notes/old.txt");
    let (status, versioned) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/artifacts/art_keep",
        Some(json!({"path": "notes/v2.md", "source": {"kind": "v2"}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{versioned}");
    assert_eq!(
        strip_artifact(&versioned["artifact"]),
        strip_artifact(&oracle["versioned"])
    );
    assert_eq!(versioned["artifact"]["version"], 2);
    assert_eq!(versioned["artifact"]["path"], "notes/v2.md");
    let (status, deleted) = request(
        "DELETE",
        "/api/workspace/projects/proj_ok/artifacts/art_keep",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{deleted}");
    assert_eq!(deleted["deleted"], 1);
    assert_eq!(deleted["deleted"], oracle["deleted"]);
    let (status, missing) = request(
        "DELETE",
        "/api/workspace/projects/proj_ok/artifacts/art_gone",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{missing}");
    assert_eq!(missing["deleted"], 0);
    let (status, listed) = get("/api/workspace/projects/proj_ok/artifacts").await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed["artifacts"].as_array().unwrap().len(), 1);
    assert_eq!(listed["artifacts"][0]["path"], "notes/kept.md");
    assert_eq!(listed["artifacts"][0]["type"], "markdown");
    assert_eq!(listed["artifacts"][0]["title"], "Kept");
    assert_eq!(listed["artifacts"][0]["source"]["kind"], "note");
    assert!(guard.path().join(".workspace-generation").exists());

    let full_dir = guard.path().join(".projects/proj_full");
    std::fs::create_dir_all(&full_dir).unwrap();
    std::fs::write(
        full_dir.join("project.json"),
        b"{\"id\":\"proj_full\",\"name\":\"Full\",\"createdAt\":10,\"updatedAt\":10}\n",
    )
    .unwrap();
    let mut crowded = Vec::new();
    for index in 0..=500 {
        crowded.push(json!({
            "artifactId": format!("a{index:04}"),
            "projectId": "proj_full",
            "type": "txt",
            "title": if index == 0 { "hidden" } else { "t" },
            "path": format!("notes/a{index:04}.txt"),
            "source": {},
            "version": 1,
            "versions": [{
                "version": 1,
                "path": format!("notes/a{index:04}.txt"),
                "createdAt": "2026-01-01T00:00:00Z",
                "createdAtMs": index
            }],
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "createdAtMs": index,
            "updatedAtMs": index
        }));
    }
    let crowded_bytes = serde_json::to_vec(&json!({"artifacts": crowded})).unwrap();
    let crowded_path = full_dir.join("artifacts.json");
    std::fs::write(&crowded_path, &crowded_bytes).unwrap();
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_full/artifacts/a0000",
        Some(json!({"title": "Still hidden"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(std::fs::read(&crowded_path).unwrap(), crowded_bytes);
    let (status, body) = post(
        "/api/workspace/projects/proj_full/artifacts",
        json!({"path": "notes/overflow.txt", "title": "overflow"}),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{body}");
    assert_eq!(body["error"], "Too many artifacts");
    assert_eq!(body["code"], "upload_too_large");
    assert_eq!(std::fs::read(&crowded_path).unwrap(), crowded_bytes);
    let (status, body) = request(
        "DELETE",
        "/api/workspace/projects/proj_full/artifacts/a0001",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["deleted"], 1);
    let rewritten = String::from_utf8(std::fs::read(&crowded_path).unwrap()).unwrap();
    assert!(!rewritten.contains("a0000"));
    assert!(!rewritten.contains("a0001"));
    assert!(rewritten.contains("a0500"));
}

fn strip_artifact(item: &Value) -> Value {
    let mut item = item.clone();
    let Some(object) = item.as_object_mut() else {
        return item;
    };
    object.remove("artifactId");
    object.remove("createdAt");
    object.remove("updatedAt");
    object.remove("createdAtMs");
    object.remove("updatedAtMs");
    object.remove("downloadUrl");
    if let Some(versions) = object.get_mut("versions").and_then(Value::as_array_mut) {
        for version in versions {
            if let Some(fields) = version.as_object_mut() {
                fields.remove("createdAt");
                fields.remove("createdAtMs");
            }
        }
    }
    item
}

fn artifact_oracle(root: &std::path::Path) -> Value {
    let repo = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    let script = r#"
import json
from deepseek_infra.infra.workspace.artifacts import add_artifact_version, delete_artifact, register_artifact, update_artifact
created = register_artifact(
    "proj_ok",
    artifact_type="",
    title="Kept",
    path="notes/kept.md",
    source={"kind": "note"},
)
updated = update_artifact("proj_ok", "art_keep", {"title": "Renamed", "source": {"kind": "edit"}, "ignored": 1})
versioned = add_artifact_version("proj_ok", "art_keep", path="notes/v2.md", source={"kind": "v2"})
deleted = delete_artifact("proj_ok", "art_keep")
missing = delete_artifact("proj_ok", "art_gone")
print(json.dumps({"created": created, "updated": updated, "versioned": versioned, "deleted": deleted, "missing": missing}))
"#;
    let output = std::process::Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo)
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env_remove("DEEPSEEK_RUNTIME_MODE")
        .output()
        .expect("python artifact oracle");
    assert!(
        output.status.success(),
        "artifact oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("artifact oracle json")
}

#[tokio::test]
async fn project_saved_item_writes_match_python_and_refuse_without_a_store_change() {
    let _env_lock = EnvLock::acquire();
    let guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);
    let project_dir = guard.path().join(".projects/proj_ok");
    std::fs::create_dir_all(&project_dir).unwrap();
    let project_bytes =
        b"{\"id\":\"proj_ok\",\"name\":\"Demo\",\"createdAt\":10,\"updatedAt\":10}\n";
    std::fs::write(project_dir.join("project.json"), project_bytes).unwrap();
    let saved_bytes = serde_json::to_vec_pretty(&json!({
        "items": [{
            "savedId": "save_keep",
            "projectId": "proj_ok",
            "type": "chat_snippet",
            "title": "Original",
            "content": "body",
            "sourceRef": {},
            "tags": [],
            "purpose": "reference",
            "createdAt": "2026-01-01T00:00:00Z",
            "createdAtMs": 10
        }]
    }))
    .unwrap();
    std::fs::write(project_dir.join("saved-items.json"), &saved_bytes).unwrap();
    let saved_path = project_dir.join("saved-items.json");
    let project_path = project_dir.join("project.json");

    let (status, body) = post(
        "/api/workspace/projects/no/saved-items",
        json!({"type": "chat_snippet"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Invalid project id");
    let (status, body) = post(
        "/api/workspace/projects/proj_missing/saved-items",
        json!({"type": "chat_snippet", "title": "Nope"}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"], "Project not found");
    assert!(!guard.path().join(".projects/proj_missing").exists());
    let (status, body) = post("/api/workspace/projects/proj_ok/saved-items", json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Unsupported saved item type");
    let (status, body) = post("/api/workspace/projects/proj_ok/saved-items", json!([])).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Request body must be a JSON object");
    let (status, body) = request("POST", "/api/workspace/projects/proj_ok/saved-items", None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Request body is empty");
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/saved-items/x",
        Some(json!({"title": "Nope"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"], "Invalid saved item id");
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/saved-items/save_gone",
        Some(json!({"title": "Nope"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"], "Saved item not found");
    let (status, body) = request(
        "DELETE",
        "/api/workspace/projects/proj_ok/saved-items/save_gone",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["deleted"], 0);
    assert_eq!(std::fs::read(&saved_path).unwrap(), saved_bytes);
    assert_eq!(std::fs::read(&project_path).unwrap(), project_bytes);
    assert!(!guard.path().join(".workspace-generation").exists());

    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let app = create_production_app(static_root.path()).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("GET")
                .uri("/api/workspace/projects/proj_ok/saved-items/save_keep")
                .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(
        response
            .headers()
            .get(header::ALLOW)
            .and_then(|value| value.to_str().ok()),
        Some("PATCH, DELETE")
    );
    let detail: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(detail, json!({"detail": "Method Not Allowed"}));
    assert_eq!(std::fs::read(&saved_path).unwrap(), saved_bytes);

    let copy = tempfile::tempdir().unwrap();
    copy_project_tree(guard.path(), copy.path());
    let oracle = saved_item_oracle(copy.path());
    let create_body = json!({
        "type": "assistant_answer",
        "title": "Kept",
        "content": "hello",
        "sourceRef": {"kind": "note"},
        "tags": ["Alpha", "alpha"],
        "purpose": "memory_candidate"
    });
    let (status, created) = post("/api/workspace/projects/proj_ok/saved-items", create_body).await;
    assert_eq!(status, StatusCode::OK, "{created}");
    assert_eq!(
        strip_saved(&created["savedItem"]),
        strip_saved(&oracle["created"])
    );
    assert!(
        created["savedItem"]["savedId"]
            .as_str()
            .unwrap()
            .starts_with("save_")
    );
    assert!(project_updated_at(guard.path()) > 10);
    let (status, listed) = get("/api/workspace/projects/proj_ok/saved-items").await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed["savedItems"].as_array().unwrap().len(), 2);

    let (status, updated) = request(
        "PATCH",
        "/api/workspace/projects/proj_ok/saved-items/save_keep",
        Some(json!({"title": "Renamed", "purpose": "nope", "ignored": 1})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{updated}");
    assert_eq!(updated["savedItem"], oracle["updated"]);
    let (status, deleted) = request(
        "DELETE",
        "/api/workspace/projects/proj_ok/saved-items/save_keep",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{deleted}");
    assert_eq!(deleted["deleted"], oracle["deleted"]);
    assert_eq!(deleted["deleted"], 1);
    let (status, missing) = request(
        "DELETE",
        "/api/workspace/projects/proj_ok/saved-items/save_gone",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{missing}");
    assert_eq!(missing["deleted"], 0);
    assert_eq!(missing["deleted"], oracle["missing"]);
    let (status, listed) = get("/api/workspace/projects/proj_ok/saved-items").await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed["savedItems"].as_array().unwrap().len(), 1);
    assert_eq!(listed["savedItems"][0]["content"], "hello");
    assert_eq!(listed["savedItems"][0]["tags"], json!(["Alpha"]));
    assert_eq!(listed["savedItems"][0]["purpose"], "memory_candidate");
    assert_eq!(listed["savedItems"][0]["type"], "assistant_answer");
    assert!(guard.path().join(".workspace-generation").exists());

    let full_dir = guard.path().join(".projects/proj_full");
    std::fs::create_dir_all(&full_dir).unwrap();
    std::fs::write(
        full_dir.join("project.json"),
        b"{\"id\":\"proj_full\",\"name\":\"Full\",\"createdAt\":10,\"updatedAt\":10}\n",
    )
    .unwrap();
    let mut crowded = Vec::new();
    for index in 0..=1000 {
        crowded.push(json!({
            "savedId": format!("s{index:04}"),
            "projectId": "proj_full",
            "type": "chat_snippet",
            "title": if index == 0 { "hidden" } else { "t" },
            "content": "",
            "sourceRef": {},
            "tags": [],
            "purpose": "reference",
            "createdAt": "2026-01-01T00:00:00Z",
            "createdAtMs": index
        }));
    }
    let crowded_bytes = serde_json::to_vec(&json!({"items": crowded})).unwrap();
    let crowded_path = full_dir.join("saved-items.json");
    std::fs::write(&crowded_path, &crowded_bytes).unwrap();
    let (status, body) = request(
        "PATCH",
        "/api/workspace/projects/proj_full/saved-items/s0000",
        Some(json!({"title": "Still hidden"})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(std::fs::read(&crowded_path).unwrap(), crowded_bytes);
    let (status, body) = post(
        "/api/workspace/projects/proj_full/saved-items",
        json!({"type": "chat_snippet", "title": "overflow"}),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{body}");
    assert_eq!(body["error"], "Too many saved items");
    assert_eq!(body["code"], "upload_too_large");
    assert_eq!(std::fs::read(&crowded_path).unwrap(), crowded_bytes);
    let (status, body) = request(
        "DELETE",
        "/api/workspace/projects/proj_full/saved-items/s0001",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["deleted"], 1);
    let rewritten = String::from_utf8(std::fs::read(&crowded_path).unwrap()).unwrap();
    assert!(!rewritten.contains("s0000"));
    assert!(!rewritten.contains("s0001"));
    assert!(rewritten.contains("s1000"));
}

fn strip_saved(item: &Value) -> Value {
    let mut item = item.clone();
    if let Some(object) = item.as_object_mut() {
        object.remove("savedId");
        object.remove("createdAt");
        object.remove("createdAtMs");
    }
    item
}

fn project_updated_at(root: &std::path::Path) -> i64 {
    let raw = std::fs::read_to_string(root.join(".projects/proj_ok/project.json")).unwrap();
    let value: Value = serde_json::from_str(&raw).unwrap();
    value["updatedAt"].as_i64().unwrap_or(0)
}

fn copy_project_tree(from: &std::path::Path, to: &std::path::Path) {
    let source = from.join(".projects");
    let dest = to.join(".projects");
    copy_tree(&source, &dest);
}

fn copy_tree(from: &std::path::Path, to: &std::path::Path) {
    std::fs::create_dir_all(to).unwrap();
    for entry in std::fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let dest = to.join(entry.file_name());
        if entry.path().is_dir() {
            copy_tree(&entry.path(), &dest);
        } else {
            std::fs::copy(entry.path(), dest).unwrap();
        }
    }
}

fn saved_item_oracle(root: &std::path::Path) -> Value {
    let repo = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    let script = r#"
import json
from deepseek_infra.infra.workspace.saved_items import create_saved_item, delete_saved_item, update_saved_item
created = create_saved_item(
    "proj_ok",
    item_type="assistant_answer",
    title="Kept",
    content="hello",
    source_ref={"kind": "note"},
    tags=["Alpha", "alpha"],
    purpose="memory_candidate",
)
updated = update_saved_item("proj_ok", "save_keep", {"title": "Renamed", "purpose": "nope", "ignored": 1})
deleted = delete_saved_item("proj_ok", "save_keep")
missing = delete_saved_item("proj_ok", "save_gone")
print(json.dumps({"created": created, "updated": updated, "deleted": deleted, "missing": missing}))
"#;
    let output = std::process::Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo)
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env_remove("DEEPSEEK_RUNTIME_MODE")
        .output()
        .expect("python saved-item oracle");
    assert!(
        output.status.success(),
        "saved-item oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("saved-item oracle json")
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
        "/api/workspace/projects/proj-read/saved-items/save_keep",
        "/api/workspace/projects/proj-read/artifacts",
        "/api/workspace/projects/proj-read/artifacts/art_keep",
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

/// `GET /api/workspace/home` is the production router, compared with a live
/// `workspace_home.workspace_home` on the same root. The route is a read: it is
/// served while Python still owns the runtime, and an empty root stays empty.
#[tokio::test]
async fn workspace_home_matches_python_and_leaves_missing_stores_absent() {
    let _env_lock = EnvLock::acquire();
    {
        let guard = EnvGuard::set(&[
            ("AUTH_TOKEN", TEST_TOKEN),
            ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ]);
        let (status, _headers, body, _) = home_exchange("GET", "/api/workspace/home", None).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{body}");
        assert_eq!(body["error"]["code"], "UNAUTHORIZED");
        assert_eq!(body["error"]["message"], "Auth required");
        assert_home_stores_absent(guard.path());

        let (status, _, body, _) =
            home_exchange("GET", "/api/workspace/home?limit=nope", Some(TEST_TOKEN)).await;
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{body}");
        assert_eq!(body["error"], "Server error");
        assert_eq!(body["code"], "internal");
        assert_ne!(body["code"], "NATIVE_PROJECTS_MUTATIONS_NOT_READY");
        assert_home_stores_absent(guard.path());

        let (status, headers, body, _) =
            home_exchange("POST", "/api/workspace/home", Some(TEST_TOKEN)).await;
        assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{body}");
        assert_eq!(body["detail"], "Method Not Allowed");
        assert_eq!(
            headers
                .get(header::ALLOW)
                .and_then(|value| value.to_str().ok()),
            Some("GET, HEAD")
        );
        assert_home_stores_absent(guard.path());

        let (status, _, body, nbytes) =
            home_exchange("HEAD", "/api/workspace/home", Some(TEST_TOKEN)).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(nbytes, 0);
        assert_home_stores_absent(guard.path());

        let (status, _, body, _) =
            home_exchange("GET", "/api/workspace/home", Some(TEST_TOKEN)).await;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(body["code"], Value::Null);
        assert_home_stores_absent(guard.path());
        let oracle = home_oracle(guard.path(), None);
        assert_eq!(body, oracle);
        assert_eq!(body["counts"]["projects"], 0);
        assert_eq!(body["counts"]["automationRuns"], 0);
        assert_eq!(body["status"]["evidence"]["present"], false);
        assert_eq!(body["status"]["evidence"]["status"], "missing");
    }

    let guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN),
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
    ]);
    seed_home_workspace(guard.path());
    let saved_before =
        std::fs::read(guard.path().join(".projects/proj_bad/saved-items.json")).unwrap();
    let broken_export_before =
        std::fs::read(guard.path().join(".projects/proj_old/exports/exports.json")).unwrap();

    let (status, _, body, _) =
        home_exchange("GET", "/api/workspace/home?limit=1", Some(TEST_TOKEN)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, home_oracle(guard.path(), Some("1")));
    let history: Value = serde_json::from_str(
        &std::fs::read_to_string(guard.path().join(".automation/history.json")).unwrap(),
    )
    .unwrap();
    let raw_runs = history["runs"].as_array().unwrap().len();
    assert!(raw_runs > body["counts"]["automationRuns"].as_u64().unwrap() as usize);
    assert_eq!(body["counts"]["automationRuns"], 1);
    assert_eq!(body["counts"]["automations"], 2);
    assert_eq!(body["recent"]["automations"].as_array().unwrap().len(), 1);
    assert!(
        body["counts"]["savedItems"].as_u64().unwrap()
            > body["recent"]["savedItems"].as_array().unwrap().len() as u64
    );
    assert!(body["counts"]["artifacts"].as_u64().unwrap() > 1);
    assert_eq!(body["recent"]["projects"].as_array().unwrap().len(), 1);
    assert_eq!(body["counts"]["projects"], 3);
    assert_eq!(body["status"]["doctor"], "ok");
    assert_eq!(body["status"]["runtime"], "local");
    assert_eq!(body["status"]["evidence"]["present"], true);
    assert_eq!(body["status"]["evidence"]["status"], "present");
    assert!(
        body["status"]["evidence"]["path"]
            .as_str()
            .unwrap()
            .ends_with("/docs/evidence/ga-v4.8.0.json"),
        "{}",
        body["status"]["evidence"]["path"]
    );
    assert!(
        !body["status"]["evidence"]["path"]
            .as_str()
            .unwrap()
            .contains('\\')
    );
    assert_eq!(body["modules"].as_array().unwrap().len(), 10);
    assert_eq!(
        std::fs::read(guard.path().join(".projects/proj_bad/saved-items.json")).unwrap(),
        saved_before
    );
    assert_eq!(
        std::fs::read(guard.path().join(".projects/proj_old/exports/exports.json")).unwrap(),
        broken_export_before
    );
    assert!(!guard.path().join(".projects/proj_bad/exports").exists());
    assert!(!guard.path().join(".workspace-generation").exists());

    for (raw, label) in [
        ("", "empty"),
        ("0", "zero"),
        ("-3", "negative"),
        ("100", "high"),
    ] {
        let uri = if raw.is_empty() {
            "/api/workspace/home?limit=".to_string()
        } else {
            format!("/api/workspace/home?limit={raw}")
        };
        let (status, _, clamped, _) = home_exchange("GET", &uri, Some(TEST_TOKEN)).await;
        assert_eq!(status, StatusCode::OK, "{label}: {clamped}");
        assert_eq!(clamped, home_oracle(guard.path(), Some(raw)), "{label}");
    }
    let wide = home_oracle(guard.path(), Some("100"));
    assert_eq!(wide["counts"]["automationRuns"], 3);
    assert_eq!(wide["counts"]["automations"], 2);
}

fn assert_home_stores_absent(root: &std::path::Path) {
    for name in [
        ".projects",
        ".automation",
        ".media",
        ".memory",
        ".generated",
        "docs",
    ] {
        assert!(!root.join(name).exists(), "{name} must stay absent");
    }
}

fn seed_home_workspace(root: &std::path::Path) {
    write_json(
        &root.join(".projects/proj_new/project.json"),
        &json!({
            "id": "proj_new",
            "name": "Newer",
            "description": " first ",
            "createdAt": 1000,
            "updatedAt": 3000,
            "skillRuns": [
                {"skillRunId": "skillrun_new", "skillId": "demo_skill", "status": "completed", "projectId": "proj_new", "startedAt": "2026-06-02T00:00:00Z"},
                {"skillRunId": "skillrun_old", "skillId": "demo_skill", "status": "completed", "projectId": "proj_new", "startedAt": "2026-06-01T00:00:00Z"}
            ]
        }),
    );
    write_json(
        &root.join(".projects/proj_old/project.json"),
        &json!({"id": "proj_old", "name": "Older", "createdAt": 1000, "updatedAt": 2000}),
    );
    write_json(
        &root.join(".projects/proj_bad/project.json"),
        &json!({"id": "proj_bad", "name": "Broken items", "createdAt": 1000, "updatedAt": 1000}),
    );
    write_json(
        &root.join(".projects/proj_new/saved-items.json"),
        &json!({"items": [
            {"savedId": "save_new", "projectId": "proj_new", "type": "chat_snippet", "title": "New", "content": "n", "createdAt": "2026-08-02T00:00:00Z", "createdAtMs": 3000},
            {"savedId": "save_mid", "projectId": "proj_new", "type": "chat_snippet", "title": "Mid", "content": "m", "createdAt": "2026-08-01T00:00:00Z", "createdAtMs": 2000}
        ]}),
    );
    std::fs::write(
        root.join(".projects/proj_old/saved-items.json"),
        b"not-json",
    )
    .unwrap();
    write_json(
        &root.join(".projects/proj_bad/saved-items.json"),
        &json!({"items": [
            {"savedId": "save_bad", "projectId": "proj_bad", "type": "not-a-saved-type", "title": "Bad", "content": "b", "createdAt": "2026-08-03T00:00:00Z", "createdAtMs": 4000}
        ]}),
    );
    write_json(
        &root.join(".projects/proj_new/artifacts.json"),
        &json!({"artifacts": [
            {"artifactId": "art_new", "projectId": "proj_new", "type": "markdown", "title": "New note", "path": "notes/new.md", "source": {"kind": "note"}, "version": 1, "createdAt": "2026-05-02T00:00:00Z", "updatedAt": "2026-05-02T00:00:00Z", "createdAtMs": 3000, "updatedAtMs": 3000},
            {"artifactId": "art_mid", "projectId": "proj_new", "type": "markdown", "title": "Mid note", "path": "notes/mid.md", "source": {"kind": "note"}, "version": 1, "createdAt": "2026-05-01T00:00:00Z", "updatedAt": "2026-05-01T00:00:00Z", "createdAtMs": 2000, "updatedAtMs": 2000}
        ]}),
    );
    write_json(
        &root.join(".projects/proj_bad/artifacts.json"),
        &json!({"artifacts": [
            {"artifactId": "art_side", "projectId": "proj_bad", "type": "markdown", "title": "Side", "path": "notes/side.md", "source": {"kind": "note"}, "version": 1, "createdAt": "2026-04-01T00:00:00Z", "updatedAt": "2026-04-01T00:00:00Z", "createdAtMs": 1000, "updatedAtMs": 1000}
        ]}),
    );
    write_json(
        &root.join(".automation/automations.json"),
        &json!({"automations": [
            {"automationId": "auto_home", "projectId": "proj_new", "name": "Home", "enabled": true, "trigger": {"type": "manual"}, "condition": {"type": "always"}, "action": {"type": "save_item"}, "createdAt": "2026-01-01T00:00:00Z", "createdAtMs": 1000, "updatedAtMs": 2000},
            {"automationId": "auto_other", "projectId": "proj_new", "name": "Other", "enabled": true, "trigger": {"type": "manual"}, "condition": {"type": "always"}, "action": {"type": "save_item"}, "createdAt": "2026-01-02T00:00:00Z", "createdAtMs": 1000, "updatedAtMs": 1000}
        ]}),
    );
    write_json(
        &root.join(".automation/history.json"),
        &json!({"runs": [
            run_record("run_new", 3000),
            run_record("run_mid", 2000),
            run_record("run_old", 1000)
        ]}),
    );
    write_json(
        &root.join(".media/library.json"),
        &json!({"schemaVersion": "media-library.v1", "media": [
            {"mediaId": "media_new", "projectId": "", "type": "image", "title": "New clip", "mimeType": "image/png", "path": "objects/media_new/clip.png", "source": {"kind": "upload"}, "status": "ready", "createdAt": "2026-03-02T00:00:00Z", "updatedAt": "2026-03-02T00:00:00Z", "metadata": {"note": "kept"}},
            {"mediaId": "media_old", "projectId": "", "type": "image", "title": "Old clip", "mimeType": "image/png", "path": "objects/media_old/clip.png", "source": {"kind": "upload"}, "status": "ready", "createdAt": "2026-03-01T00:00:00Z", "updatedAt": "2026-03-01T00:00:00Z", "metadata": {}},
            {"mediaId": "media_bad", "projectId": "", "type": "nope", "title": "Bad", "mimeType": "application/octet-stream", "path": "objects/media_bad/clip.bin", "status": "ready", "createdAt": "2026-03-03T00:00:00Z", "updatedAt": "2026-03-03T00:00:00Z"}
        ]}),
    );
    write_json(
        &root.join(".generated/workspace-exports/exports.json"),
        &json!({"exports": [{"exportId": "exp_global", "projectId": "", "createdAt": "2026-02-01T00:00:00Z"}]}),
    );
    write_json(
        &root.join(".projects/proj_new/exports/exports.json"),
        &json!({"exports": [
            {"exportId": "exp_new", "projectId": "proj_new", "createdAt": "2026-02-03T00:00:00Z"},
            {"exportId": "exp_mid", "projectId": "proj_new", "createdAt": "2026-02-02T00:00:00Z"}
        ]}),
    );
    std::fs::create_dir_all(root.join(".projects/proj_old/exports")).unwrap();
    std::fs::write(
        root.join(".projects/proj_old/exports/exports.json"),
        b"not-json",
    )
    .unwrap();
    write_json(
        &root.join(".memory/memories.json"),
        &json!([
            {"id": "mem_new", "memoryId": "mem_new", "content": "newer fact", "category": "fact", "type": "fact", "scope": "global", "pinned": false, "createdAt": "2026-07-02T00:00:00Z", "updatedAt": "2026-07-02T00:00:00Z", "source": {"kind": "manual", "refId": "mem_new"}, "confidence": 1},
            {"id": "mem_old", "memoryId": "mem_old", "content": "older fact", "category": "fact", "type": "fact", "scope": "global", "pinned": false, "createdAt": "2026-07-01T00:00:00Z", "updatedAt": "2026-07-01T00:00:00Z", "source": {"kind": "manual", "refId": "mem_old"}, "confidence": 1}
        ]),
    );
    write_json(
        &root.join("docs/evidence/ga-v4.8.0.json"),
        &json!({"ok": true}),
    );
}

fn run_record(run_id: &str, started_at_ms: i64) -> Value {
    json!({
        "runId": run_id,
        "automationId": "auto_home",
        "projectId": "proj_new",
        "status": "success",
        "startedAt": format!("2026-01-01T00:00:{started_at_ms:02}Z"),
        "finishedAt": format!("2026-01-01T00:00:{started_at_ms:02}Z"),
        "startedAtMs": started_at_ms,
        "finishedAtMs": started_at_ms,
        "durationMs": 0,
        "trigger": {"type": "manual"},
        "traceId": "",
        "attempts": 1
    })
}

fn write_json(path: &std::path::Path, value: &Value) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

async fn home_exchange(
    method: &str,
    uri: &str,
    token: Option<&str>,
) -> (StatusCode, axum::http::HeaderMap, Value, usize) {
    let static_root = tempfile::tempdir().expect("a static root");
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(
        static_root.path().join("ui/index.html"),
        "<!doctype html><main>native ui</main>",
    )
    .unwrap();
    let app = create_production_app(static_root.path()).expect("production app");
    let mut builder = Request::builder().method(method).uri(uri);
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    let response = app
        .oneshot(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let nbytes = bytes.len();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, headers, body, nbytes)
}

fn home_oracle(root: &std::path::Path, raw_limit: Option<&str>) -> Value {
    let repo = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    let script = r#"
import json, os
missing = os.environ.get("HOME_LIMIT_MISSING") == "1"
raw = os.environ.get("HOME_LIMIT_RAW", "")
limit = 8 if missing or raw == "" else int(raw)
from deepseek_infra.infra.workspace.home import workspace_home
print(json.dumps(workspace_home(limit=limit)))
"#;
    let mut command = std::process::Command::new("python");
    command
        .arg("-c")
        .arg(script)
        .current_dir(&repo)
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env_remove("DEEPSEEK_RUNTIME_MODE");
    match raw_limit {
        None => {
            command.env("HOME_LIMIT_MISSING", "1");
            command.env_remove("HOME_LIMIT_RAW");
        }
        Some(raw) => {
            command.env_remove("HOME_LIMIT_MISSING");
            command.env("HOME_LIMIT_RAW", raw);
        }
    }
    let output = command.output().expect("python home oracle");
    assert!(
        output.status.success(),
        "home oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).expect("oracle utf-8");
    let line = stdout
        .lines()
        .rev()
        .find(|line| !line.is_empty())
        .unwrap_or("");
    serde_json::from_str(line).expect("home oracle json")
}
