//! `GET /api/scheduler` through the production router.
//!
//! The body is `scheduler_status()` plus `dead_letters(limit)`. A missing
//! `scheduler.sqlite3` is not created. An existing file is not rewritten.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "scheduler-route-token";

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
            ("SCHEDULER_ENABLED", "yes".to_string()),
            ("SCHEDULER_MAX_CONCURRENCY", "32".to_string()),
            ("SCHEDULER_MAX_QUEUE_DEPTH", "7".to_string()),
            ("SCHEDULER_RATE_PER_SECOND", "0".to_string()),
            ("SCHEDULER_RATE_BURST", "0".to_string()),
            ("SCHEDULER_DLQ_ENABLED", "true".to_string()),
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    async fn request(
        &self,
        method: &str,
        uri: &str,
        auth: bool,
    ) -> (StatusCode, Value, Vec<u8>, Option<String>) {
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
        let allow = response
            .headers()
            .get(header::ALLOW)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body, bytes.to_vec(), allow)
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn tree(root: &Path) -> Vec<String> {
    let mut found = Vec::new();
    walk(root, root, &mut found);
    found.sort();
    found
}

fn walk(root: &Path, dir: &Path, found: &mut Vec<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        found.push(relative);
        if path.is_dir() {
            walk(root, &path, found);
        }
    }
}

fn set_scheduler(pairs: &[(&str, &str)]) {
    for (name, value) in pairs {
        unsafe { std::env::set_var(name, value) }
    }
}

fn oracle(root: &Path, limits: &Value) -> Value {
    let script = r#"
import json, sys
from deepseek_infra.infra.gateway.scheduler import dead_letters, scheduler_status

def route_limit(raw):
    if raw is None:
        raw = "50"
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 50

limits = json.loads(sys.stdin.read())
out = []
for raw in limits:
    out.append({
        "status": 200,
        "body": {
            "ok": True,
            "scheduler": scheduler_status(),
            "deadLetters": dead_letters(route_limit(raw)),
        },
    })
json.dump(out, sys.stdout)
"#;
    let mut child = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python");
    let stdin = child.stdin.take().expect("stdin");
    serde_json::to_writer(stdin, limits).expect("write plan");
    let output = child.wait_with_output().expect("python");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn db_path(root: &Path) -> PathBuf {
    root.join(".scheduler").join("scheduler.sqlite3")
}

fn write_letters(path: &Path) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    if path.exists() {
        std::fs::remove_file(path).unwrap();
    }
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE scheduler_dead_letters (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            key TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );",
    )
    .unwrap();
    for index in 0..12 {
        let reason = match index % 3 {
            0 => "retry_exhausted",
            1 => "other",
            _ => "zeta",
        };
        let created = if index == 11 {
            3011.5
        } else {
            1000.0 + index as f64
        };
        conn.execute(
            "INSERT INTO scheduler_dead_letters
             (id, kind, key, reason, attempts, priority, created_at, detail)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                format!("id-{index:02}"),
                if index % 2 == 0 {
                    "deepseek_json"
                } else {
                    "request"
                },
                if index == 0 {
                    String::new()
                } else {
                    format!("k{index}")
                },
                reason,
                index,
                index * 2,
                created,
                "hidden-detail",
            ],
        )
        .unwrap();
    }
}

fn write_empty_schema(path: &Path) {
    if path.is_dir() {
        std::fs::remove_dir_all(path).unwrap();
    } else if path.exists() {
        std::fs::remove_file(path).unwrap();
    }
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE scheduler_dead_letters (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            key TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );",
    )
    .unwrap();
}

fn write_empty_db(path: &Path) {
    if path.is_dir() {
        std::fs::remove_dir_all(path).unwrap();
    } else if path.exists() {
        std::fs::remove_file(path).unwrap();
    }
    drop(Connection::open(path).unwrap());
}

const LIMITS: &[(&str, Option<&str>)] = &[
    ("/api/scheduler", None),
    ("/api/scheduler?limit=1", Some("1")),
    ("/api/scheduler?limit=0", Some("0")),
    ("/api/scheduler?limit=-3", Some("-3")),
    ("/api/scheduler?limit=abc", Some("abc")),
    ("/api/scheduler?limit=1.5", Some("1.5")),
    ("/api/scheduler?limit=1001", Some("1001")),
    ("/api/scheduler?limit=1_0", Some("1_0")),
    ("/api/scheduler?limit=%20%207%20", Some("  7 ")),
    ("/api/scheduler?limit=1+2", Some("1 2")),
    ("/api/scheduler?limit=", Some("")),
    ("/api/scheduler?limit=007", Some("007")),
    ("/api/scheduler?limit=%2B8", Some("+8")),
    ("/api/scheduler?limit=2&limit=9", Some("2")),
    ("/api/scheduler?Limit=3", None),
];

async fn collect(fixture: &Fixture) -> Vec<(u16, Value)> {
    let mut http = Vec::new();
    for (uri, _) in LIMITS {
        let (status, body, _, _) = fixture.request("GET", uri, true).await;
        http.push((status.as_u16(), body));
    }
    http
}

fn assert_oracle(root: &Path, http: &[(u16, Value)]) {
    let payload = Value::Array(
        LIMITS
            .iter()
            .map(|(_, raw)| match raw {
                Some(value) => Value::String((*value).to_string()),
                None => Value::Null,
            })
            .collect(),
    );
    let expected = oracle(root, &payload);
    let expected = expected.as_array().expect("oracle list");
    assert_eq!(expected.len(), http.len());
    for (index, (status, body)) in http.iter().enumerate() {
        assert_eq!(
            *status,
            expected[index]["status"].as_u64().unwrap() as u16,
            "case {index}"
        );
        assert_eq!(body, &expected[index]["body"], "case {index}");
    }
}

#[tokio::test]
async fn scheduler_matches_the_oracle_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();

    let (status, unauthenticated, _, _) = fixture.request("GET", "/api/scheduler", false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    assert_eq!(
        tree(root),
        Vec::<String>::new(),
        "unauthenticated request created a store"
    );

    let missing = collect(&fixture).await;
    let (status, denied, _, allow) = fixture.request("POST", "/api/scheduler", true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{denied}");
    assert_eq!(denied, json!({"detail": "Method Not Allowed"}));
    assert_eq!(allow.as_deref(), Some("GET, HEAD"));
    let (status, head, head_bytes, _) = fixture.request("HEAD", "/api/scheduler", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");
    assert_eq!(
        tree(root),
        Vec::<String>::new(),
        "missing scheduler db was created"
    );
    assert_oracle(root, &missing);
    assert_eq!(
        tree(root),
        Vec::<String>::new(),
        "oracle created the scheduler store"
    );

    let database = db_path(root);
    write_letters(&database);
    let before = std::fs::read(&database).unwrap();
    let with_rows = collect(&fixture).await;
    assert_eq!(
        std::fs::read(&database).unwrap(),
        before,
        "reading the dlq rewrote scheduler.sqlite3"
    );
    assert_eq!(
        tree(root),
        vec![
            ".scheduler".to_string(),
            ".scheduler/scheduler.sqlite3".to_string()
        ],
        "read created a journal or wal"
    );
    let (status, head, head_bytes, _) = fixture
        .request("HEAD", "/api/scheduler?limit=1", true)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");
    assert_eq!(
        std::fs::read(&database).unwrap(),
        before,
        "head rewrote scheduler.sqlite3"
    );
    assert_oracle(root, &with_rows);

    set_scheduler(&[
        ("SCHEDULER_ENABLED", "off"),
        ("SCHEDULER_MAX_CONCURRENCY", "0"),
        ("SCHEDULER_MAX_QUEUE_DEPTH", "999999"),
        ("SCHEDULER_RATE_PER_SECOND", "999999.9"),
        ("SCHEDULER_RATE_BURST", "5"),
        ("SCHEDULER_DLQ_ENABLED", "0"),
    ]);
    let clamped = collect(&fixture).await;
    assert_eq!(
        std::fs::read(&database).unwrap(),
        before,
        "clamped read rewrote scheduler.sqlite3"
    );
    assert_oracle(root, &clamped);

    set_scheduler(&[
        ("SCHEDULER_ENABLED", "   "),
        ("SCHEDULER_MAX_CONCURRENCY", "abc"),
        ("SCHEDULER_MAX_QUEUE_DEPTH", "  4 "),
        ("SCHEDULER_RATE_PER_SECOND", ""),
        ("SCHEDULER_RATE_BURST", "  0 "),
        ("SCHEDULER_DLQ_ENABLED", "   yes  "),
    ]);
    let defaults = collect(&fixture).await;
    assert_oracle(root, &defaults);

    write_empty_schema(&database);
    let empty_schema = collect(&fixture).await;
    assert!(database.is_file());
    assert_oracle(root, &empty_schema);

    write_empty_db(&database);
    let no_table = collect(&fixture).await;
    assert!(no_table.iter().all(|(status, _)| *status == 200));
    assert_oracle(root, &no_table);

    std::fs::write(&database, b"not-a-db").unwrap();
    let corrupt = collect(&fixture).await;
    assert_eq!(std::fs::read(&database).unwrap(), b"not-a-db");
    assert_oracle(root, &corrupt);

    std::fs::remove_file(&database).unwrap();
    std::fs::create_dir(&database).unwrap();
    let directory = collect(&fixture).await;
    assert!(database.is_dir());
    assert_oracle(root, &directory);
    assert!(
        database.is_dir(),
        "the route replaced the directory database"
    );
    let scheduler_dir = root.join(".scheduler");
    assert_eq!(
        tree(&scheduler_dir),
        vec!["scheduler.sqlite3".to_string()],
        "the route created a file beside the directory database"
    );
}
