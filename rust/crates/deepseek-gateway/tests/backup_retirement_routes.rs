//! Copy-retirement HTTP through the production router.
//!
//! The success path creates a `requested` job and reads it back. While Python is
//! still the writer, POST is 409 and does not create `.backup-retirements`. A
//! database Python already wrote is readable without a journal or schema change.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-retirement-route-token";

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
    fn new(mode: &str) -> Self {
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
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn db_path(&self) -> std::path::PathBuf {
        self.root
            .path()
            .join(".backup-retirements")
            .join("retirements.sqlite3")
    }

    async fn request(
        &self,
        method: &str,
        uri: &str,
        body: Option<&[u8]>,
        auth: bool,
        content_length: Option<usize>,
    ) -> (StatusCode, Value) {
        let bytes = body.unwrap_or(b"");
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if body.is_some() || content_length.is_some() {
            request = request.header(header::CONTENT_TYPE, "application/json");
            let length = content_length.unwrap_or(bytes.len());
            request = request.header(header::CONTENT_LENGTH, length.to_string());
        }
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::from(bytes.to_vec())).unwrap())
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
}

fn seed_legacy_database(path: &Path) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE copy_retirement_jobs (
            job_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            backup_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            bytes_reclaimed INTEGER DEFAULT 0,
            sim_metadata TEXT
        );",
    )
    .unwrap();
    conn.execute(
        "INSERT INTO copy_retirement_jobs(
            job_id, policy_id, backup_id, target_id, phase, created_at, updated_at,
            error, bytes_reclaimed, sim_metadata
        ) VALUES
            ('retire_aaaaaaaaaaaaaaaa', 'pol-old', 'bk-1', 'tgt-1', 'requested',
             '2026-10-03T00:00:01Z', '2026-10-03T00:00:01Z', NULL, 0, '{}'),
            ('retire_bbbbbbbbbbbbbbbb', 'pol-old', 'bk-2', 'tgt-1', 'requested',
             '2026-10-03T00:00:02Z', '2026-10-03T00:00:02Z', 'boom', 12, '{\"kept\":true}'),
            ('retire_cccccccccccccccc', 'pol-old', 'bk-3', 'tgt-1', 'failed',
             '2026-10-03T00:00:03Z', '2026-10-03T00:00:03Z', NULL, 0, 'not-json')",
        [],
    )
    .unwrap();
    conn.pragma_update(None, "journal_mode", "DELETE").unwrap();
    conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);").ok();
}

fn column_names(path: &Path) -> Vec<String> {
    let conn = Connection::open(path).unwrap();
    let mut statement = conn
        .prepare("PRAGMA table_info(copy_retirement_jobs)")
        .unwrap();
    let names = statement
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap();
    names.map(|name| name.unwrap()).collect()
}

fn directory_files(path: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(path)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[tokio::test]
async fn retirement_create_get_and_list_match_the_python_job_contract() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("python_disabled");

    let (status, body) = fixture
        .request(
            "POST",
            "/api/workspace/backup-retirements",
            Some(
                br#"{"policyId":"  pol-a  ","backupId":12,"targetId":"tgt-a","reason":"  keep  "}"#,
            ),
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let job_id = body["jobId"].as_str().expect("jobId").to_string();
    assert!(
        job_id.starts_with("retire_") && job_id.len() == "retire_".len() + 16,
        "{job_id}"
    );
    assert!(
        job_id[7..]
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
    );
    assert_eq!(body["policyId"], "pol-a");
    assert_eq!(body["backupId"], "12");
    assert_eq!(body["targetId"], "tgt-a");
    assert_eq!(body["phase"], "requested");
    assert_eq!(body["reason"], "  keep  ");
    assert_eq!(body["bytesReclaimed"], 0);
    assert!(body["error"].is_null());
    assert_eq!(body["simMetadata"], json!({}));
    let created_at = body["createdAt"].as_str().unwrap();
    assert_eq!(body["updatedAt"], created_at);
    assert_eq!(created_at.len(), "2026-10-03T00:00:00Z".len());
    assert!(created_at.ends_with('Z'));
    assert_eq!(&created_at[10..11], "T");

    let (status, fetched) = fixture
        .request(
            "GET",
            &format!("/api/workspace/backup-retirements/{job_id}"),
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{fetched}");
    assert_eq!(fetched, body);

    let (status, other) = fixture
        .request(
            "POST",
            "/api/workspace/backup-retirements",
            Some(br#"{"policyId":"pol-b","backupId":"bk-b","targetId":"tgt-b"}"#),
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{other}");
    assert_eq!(other["reason"], "api-retirement-request");

    let (status, listed) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements?policyId=pol-a&phase=requested",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    let jobs = listed["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0]["jobId"], job_id);

    let (status, empty) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements?policyId=missing",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{empty}");
    assert_eq!(empty["jobs"].as_array().unwrap().len(), 0);

    let (status, missing) = fixture
        .request(
            "POST",
            "/api/workspace/backup-retirements",
            Some(br#"{"policyId":"   ","backupId":"bk","targetId":"tgt"}"#),
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{missing}");
    assert_eq!(
        missing,
        json!({
            "error": "policyId, backupId, and targetId are required",
            "code": "invalid_payload"
        })
    );

    let (status, unknown) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements/retire_does_not_exist",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{unknown}");
    assert_eq!(
        unknown,
        json!({"error": "Retirement job not found", "code": "not_found"})
    );

    let (status, large) = fixture
        .request(
            "POST",
            "/api/workspace/backup-retirements",
            Some(br#"{}"#),
            true,
            Some(16_001),
        )
        .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{large}");
    assert_eq!(large["code"], "upload_too_large");

    let (status, unauthenticated) = fixture
        .request(
            "POST",
            "/api/workspace/backup-retirements",
            Some(br#"{"policyId":"pol","backupId":"bk","targetId":"tgt"}"#),
            false,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
}

#[tokio::test]
async fn retirement_write_is_refused_while_python_owns_the_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("");

    let (status, listed) = fixture
        .request("GET", "/api/workspace/backup-retirements", None, true, None)
        .await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed, json!({"jobs": []}));
    assert!(!fixture.db_path().parent().unwrap().exists());

    let (status, missing) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements/retire_absent",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing}");
    assert!(!fixture.db_path().parent().unwrap().exists());

    let (status, refused) = fixture
        .request(
            "POST",
            "/api/workspace/backup-retirements",
            Some(br#"{"policyId":"pol","backupId":"bk","targetId":"tgt"}"#),
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{refused}");
    assert_eq!(refused["code"], "NATIVE_BACKUP_RETIREMENT_WRITE_NOT_OWNED");
    assert!(!fixture.db_path().parent().unwrap().exists());
}

#[tokio::test]
async fn retirement_reads_a_python_database_without_migrating_it() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("");
    seed_legacy_database(&fixture.db_path());
    let before = directory_files(fixture.db_path().parent().unwrap());

    let (status, listed) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements?phase=requested",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    let jobs = listed["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 2);
    assert_eq!(jobs[0]["jobId"], "retire_bbbbbbbbbbbbbbbb");
    assert_eq!(jobs[0]["error"], "boom");
    assert_eq!(jobs[0]["bytesReclaimed"], 12);
    assert_eq!(jobs[0]["simMetadata"], json!({"kept": true}));
    assert!(jobs[0]["reason"].is_null());
    assert_eq!(jobs[1]["jobId"], "retire_aaaaaaaaaaaaaaaa");
    assert!(jobs[1]["error"].is_null());

    let (status, broken) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements/retire_cccccccccccccccc",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{broken}");
    assert_eq!(broken, json!({"error": "Server error", "code": "internal"}));

    assert_eq!(directory_files(fixture.db_path().parent().unwrap()), before);
    assert!(
        !column_names(&fixture.db_path())
            .iter()
            .any(|name| name == "reason")
    );
}

#[tokio::test]
async fn retirement_owner_adds_the_reason_column_when_it_opens_the_database() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("python_disabled");
    seed_legacy_database(&fixture.db_path());

    let (status, listed) = fixture
        .request(
            "GET",
            "/api/workspace/backup-retirements?phase=requested",
            None,
            true,
            None,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed["jobs"].as_array().unwrap().len(), 2);
    assert!(
        column_names(&fixture.db_path())
            .iter()
            .any(|name| name == "reason")
    );
}
