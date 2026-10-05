//! `GET /api/workspace/disaster-recovery/replication` through the production router.
//!
//! The handler reads `.backup-replication/*.json` and does not create that directory.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-replication-route-token";

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
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn replication_dir(&self) -> PathBuf {
        self.root.path().join(".backup-replication")
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

fn write_job(dir: &Path, name: &str, body: &str) {
    std::fs::write(dir.join(name), body).unwrap();
}

fn seed(dir: &Path) {
    std::fs::create_dir_all(dir).unwrap();
    std::fs::create_dir_all(dir.join("holds")).unwrap();
    write_job(
        dir,
        "job-m.json",
        r#"{"jobId":"job-m","policyId":"pol-m","backupId":"bk-m","phase":"copying","note":"keep-me"}"#,
    );
    write_job(
        dir,
        "job-a.json",
        r#"{"jobId":"job-a","policyId":"pol-a","backupId":"bk-a","phase":"done"}"#,
    );
    write_job(dir, "cursors.json", r#"{"cursor":"abc"}"#);
    write_job(dir, "bad.json", "{not-json");
    write_job(
        dir,
        ".hidden.json",
        r#"{"jobId":"hidden","policyId":"pol-m"}"#,
    );
    write_job(dir, "array.json", "[1]");
    write_job(dir, "empty-job.json", r#"{"jobId":""}"#);
    write_job(
        dir,
        "null-job.json",
        r#"{"jobId":null,"policyId":"pol-null","backupId":"bk-null"}"#,
    );
    write_job(dir, "false-job.json", r#"{"jobId":false,"policyId":false}"#);
    write_job(dir, "zero-job.json", r#"{"jobId":0,"policyId":0}"#);
    write_job(
        dir,
        "num-job.json",
        r#"{"jobId":"num","policyId":12,"backupId":34}"#,
    );
    write_job(dir, "space-job.json", r#"{"jobId":" ","policyId":" "}"#);
    write_job(dir, "Upper.JSON", r#"{"jobId":"upper","policyId":"pol-m"}"#);
    write_job(&dir.join("holds"), "nested.json", r#"{"jobId":"nested"}"#);
    std::fs::create_dir(dir.join("dir.json")).unwrap();
}

fn tree_snapshot(dir: &Path) -> Vec<(String, Vec<u8>)> {
    fn walk(dir: &Path, prefix: &str, out: &mut Vec<(String, Vec<u8>)>) {
        let mut entries: Vec<_> = std::fs::read_dir(dir)
            .unwrap()
            .map(|entry| entry.unwrap())
            .collect();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            let rel = if prefix.is_empty() {
                name
            } else {
                format!("{prefix}/{}", entry.file_name().to_string_lossy())
            };
            let path = entry.path();
            if path.is_dir() {
                out.push((format!("{rel}/"), Vec::new()));
                walk(&path, &rel, out);
            } else {
                out.push((rel, std::fs::read(&path).unwrap()));
            }
        }
    }
    let mut out = Vec::new();
    if dir.is_dir() {
        walk(dir, "", &mut out);
    }
    out
}

fn marker(job: &Value) -> String {
    match &job["jobId"] {
        Value::Null => "null".to_string(),
        Value::Bool(false) => "false".to_string(),
        Value::Bool(true) => "true".to_string(),
        Value::Number(number) => number.to_string(),
        Value::String(text) if text == " " => "space".to_string(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn markers(body: &Value) -> Vec<String> {
    body["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .map(marker)
        .collect()
}

fn full_order() -> Vec<&'static str> {
    let order = vec!["0", "space", "num", "null", "job-m", "job-a", "false"];
    #[cfg(windows)]
    let order = {
        let mut order = order;
        order.insert(1, "upper");
        order
    };
    order
}

const LIST: &str = "/api/workspace/disaster-recovery/replication";

#[tokio::test]
async fn replication_list_reads_job_files_without_creating_the_directory() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let dir = fixture.replication_dir();

    let (status, posted) = fixture.request("POST", LIST, true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{posted}");
    assert!(!dir.exists());

    std::fs::write(&dir, b"not-a-dir").unwrap();
    let (status, blocked) = fixture.request("GET", LIST, true).await;
    assert_eq!(status, StatusCode::OK, "{blocked}");
    assert_eq!(blocked, json!({"jobs": []}));
    assert_eq!(std::fs::read(&dir).unwrap(), b"not-a-dir");
    std::fs::remove_file(&dir).unwrap();

    let (status, empty) = fixture.request("GET", LIST, true).await;
    assert_eq!(status, StatusCode::OK, "{empty}");
    assert_eq!(empty, json!({"jobs": []}));
    assert!(!dir.exists());

    seed(&dir);
    let before = tree_snapshot(&dir);

    let (status, listed) = fixture.request("GET", LIST, true).await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(markers(&listed), full_order(), "{listed}");
    let job_m = listed["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|job| job["jobId"] == "job-m")
        .unwrap();
    assert_eq!(job_m["note"], "keep-me");
    assert_eq!(job_m["phase"], "copying");
    assert_eq!(job_m["policyId"], "pol-m");
    assert_eq!(job_m["backupId"], "bk-m");
    let null_job = listed["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|job| job["jobId"].is_null())
        .unwrap();
    assert!(null_job["jobId"].is_null());
    let false_job = listed["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|job| job["jobId"] == false)
        .unwrap();
    assert_eq!(false_job["jobId"], false);
    let zero_job = listed["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|job| marker(job) == "0")
        .unwrap();
    assert_eq!(zero_job["jobId"], 0);
    let num_job = listed["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|job| job["jobId"] == "num")
        .unwrap();
    assert_eq!(num_job["policyId"], 12);
    assert_eq!(num_job["backupId"], 34);

    let (status, by_policy) = fixture
        .request("GET", &format!("{LIST}?policyId=pol-m"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{by_policy}");
    let policy_markers = vec!["job-m"];
    #[cfg(windows)]
    let policy_markers = {
        let mut policy_markers = policy_markers;
        policy_markers.insert(0, "upper");
        policy_markers
    };
    assert_eq!(markers(&by_policy), policy_markers, "{by_policy}");

    let (status, by_backup) = fixture
        .request("GET", &format!("{LIST}?backupId=bk-a"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{by_backup}");
    assert_eq!(markers(&by_backup), vec!["job-a"]);

    let (status, both) = fixture
        .request("GET", &format!("{LIST}?policyId=pol-a&backupId=bk-m"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{both}");
    assert_eq!(markers(&both), Vec::<String>::new());

    let (status, matched) = fixture
        .request("GET", &format!("{LIST}?policyId=12&backupId=34"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{matched}");
    assert_eq!(markers(&matched), vec!["num"]);
    assert_eq!(matched["jobs"][0]["policyId"], 12);

    let (status, numeric_zero) = fixture
        .request("GET", &format!("{LIST}?policyId=0"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{numeric_zero}");
    assert_eq!(markers(&numeric_zero), Vec::<String>::new());

    let (status, false_filter) = fixture
        .request("GET", &format!("{LIST}?policyId=False"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{false_filter}");
    assert_eq!(markers(&false_filter), Vec::<String>::new());

    let (status, blank) = fixture
        .request("GET", &format!("{LIST}?policyId="), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{blank}");
    assert_eq!(markers(&blank), full_order());

    let (status, space) = fixture
        .request("GET", &format!("{LIST}?policyId=%20"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{space}");
    assert_eq!(markers(&space), vec!["space"]);

    let (status, phase) = fixture
        .request("GET", &format!("{LIST}?phase=done"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{phase}");
    assert_eq!(markers(&phase), full_order());

    assert_eq!(tree_snapshot(&dir), before);

    let (status, unauthenticated) = fixture.request("GET", LIST, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");

    std::fs::write(dir.join("zz-bad.json"), [0xFF, 0xFE]).unwrap();
    let (status, invalid) = fixture.request("GET", LIST, true).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{invalid}");
    assert_eq!(
        invalid,
        json!({"error": "Server error", "code": "internal"})
    );
    let mut after = tree_snapshot(&dir);
    assert!(
        after
            .iter()
            .any(|(name, bytes)| { name == "zz-bad.json" && bytes.as_slice() == [0xFF, 0xFE] })
    );
    after.retain(|(name, _)| name != "zz-bad.json");
    assert_eq!(after, before);
}

#[tokio::test]
async fn replication_list_stops_at_one_hundred_jobs_in_descending_name_order() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let dir = fixture.replication_dir();

    let (status, empty) = fixture.request("GET", LIST, true).await;
    assert_eq!(status, StatusCode::OK, "{empty}");
    assert_eq!(empty, json!({"jobs": []}));
    assert!(!dir.exists());

    std::fs::create_dir_all(&dir).unwrap();
    for index in 0..=100 {
        write_job(
            &dir,
            &format!("job-{index:03}.json"),
            &format!(r#"{{"jobId":"job-{index:03}","policyId":"p"}}"#),
        );
    }
    write_job(&dir, "aaa.json", r#"{"jobId":"aaa","policyId":"p"}"#);
    let before = tree_snapshot(&dir);

    let (status, listed) = fixture.request("GET", LIST, true).await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    let jobs = listed["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 100);
    assert_eq!(jobs[0]["jobId"], "job-100");
    assert_eq!(jobs[99]["jobId"], "job-001");
    assert!(
        jobs.iter()
            .all(|job| job["jobId"] != "job-000" && job["jobId"] != "aaa")
    );
    assert_eq!(tree_snapshot(&dir), before);
}
