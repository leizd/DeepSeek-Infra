//! `GET /api/workspace/disaster-recovery/drills/{restore_id}` through the production router.
//!
//! The handler reads `.restore-staging/{id}` and does not create that directory.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-drill-route-token";
const DRILL: &str = "/api/workspace/disaster-recovery/drills";

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
}
impl EnvGuard {
    fn set(pairs: &[(&'static str, String)]) -> Self {
        let saved = pairs
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var(name).ok();
                unsafe { std::env::set_var(name, value) }
                (*name, previous)
            })
            .collect();
        Self { saved }
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

struct Fixture {
    root: tempfile::TempDir,
    _static_root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("DEEPSEEK_RUNTIME_MODE", String::new()),
            ("GO_CONTROL_ADDR", String::new()),
            ("DEEPSEEK_GO_CONTROL_URL", String::new()),
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn staging(&self) -> PathBuf {
        self.root.path().join(".restore-staging")
    }

    async fn request(&self, method: &str, uri: &str, auth: bool) -> (StatusCode, Value) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body)
    }
}

fn write_json(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, body).unwrap();
}

fn snapshot(dir: &Path) -> Vec<(String, Vec<u8>)> {
    if !dir.is_dir() {
        return Vec::new();
    }
    let mut rows = Vec::new();
    let mut entries: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap())
        .collect();
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if path.is_file() {
            rows.push((name, std::fs::read(&path).unwrap()));
        } else {
            rows.push((format!("{name}/"), Vec::new()));
        }
    }
    rows
}

#[tokio::test]
async fn recovery_drill_get_reads_an_existing_session_without_creating_one() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let staging = fixture.staging();

    let (status, unauthenticated) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), false)
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert!(!staging.exists());

    for id in ["nope", "restore_", "restore_ab-c", "restore_ab_c"] {
        let (status, rejected) = fixture.request("GET", &format!("{DRILL}/{id}"), true).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{id} {rejected}");
        assert_eq!(
            rejected,
            json!({"error": "Invalid restore id", "code": "invalid_payload"})
        );
        assert!(!staging.exists());
    }

    let (status, missing) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing}");
    assert_eq!(
        missing,
        json!({"error": "Remote restore session not found", "code": "not_found"})
    );
    assert!(!staging.exists());

    std::fs::write(&staging, b"not-a-dir").unwrap();
    let (status, blocked) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{blocked}");
    assert_eq!(std::fs::read(&staging).unwrap(), b"not-a-dir");
    std::fs::remove_file(&staging).unwrap();

    let session = staging.join("restore_abc");
    std::fs::create_dir_all(&session).unwrap();
    let (status, empty) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{empty}");
    assert_eq!(
        empty,
        json!({"error": "Recovery Drill result not found", "code": "not_found"})
    );
    assert!(snapshot(&session).is_empty());

    write_json(
        &session.join("drill-running.json"),
        r#"{"restoreId":"restore_abc","result":"running","startedAt":"2026-10-03T00:00:00Z"}"#,
    );
    let before_claim = snapshot(&session);
    let (status, claim) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{claim}");
    assert_eq!(claim["result"], "running");
    assert_eq!(claim["restoreId"], "restore_abc");
    assert_eq!(claim["startedAt"], "2026-10-03T00:00:00Z");
    assert_eq!(snapshot(&session), before_claim);

    write_json(
        &session.join("drill-result.json"),
        r#"{"restoreId":"restore_abc","result":"success","note":"keep-me"}"#,
    );
    let before_result = snapshot(&session);
    let (status, result) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{result}");
    assert_eq!(result["result"], "success");
    assert_eq!(result["note"], "keep-me");
    assert_eq!(snapshot(&session), before_result);

    write_json(&session.join("drill-result.json"), "[1]");
    let (status, array) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{array}");
    assert_eq!(
        array,
        json!({"error": "Recovery Drill metadata is unavailable", "code": "invalid_payload"})
    );

    write_json(&session.join("drill-result.json"), "{not-json");
    let (status, broken) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{broken}");
    assert_eq!(
        broken,
        json!({"error": "Recovery Drill metadata is unavailable", "code": "invalid_payload"})
    );

    std::fs::write(session.join("drill-result.json"), [0xFF, 0xFE]).unwrap();
    let (status, invalid) = fixture
        .request("GET", &format!("{DRILL}/restore_abc"), true)
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{invalid}");
    assert_eq!(
        invalid,
        json!({"error": "Server error", "code": "internal"})
    );
    assert_eq!(
        std::fs::read(session.join("drill-result.json")).unwrap(),
        vec![0xFF, 0xFE]
    );

    let unicode = staging.join("restore_é");
    write_json(
        &unicode.join("drill-result.json"),
        r#"{"restoreId":"restore_é","result":"success"}"#,
    );
    let (status, accented) = fixture
        .request("GET", &format!("{DRILL}/restore_%C3%A9"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{accented}");
    assert_eq!(accented["restoreId"], "restore_é");
    assert_eq!(accented["result"], "success");

    let (status, forwarded) = fixture.request("POST", &format!("{DRILL}/run"), true).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{forwarded}");
    assert_eq!(forwarded["error"]["code"], "GO_CONTROL_PROXY_NOT_READY");
    assert!(session.join("drill-running.json").is_file());
}
