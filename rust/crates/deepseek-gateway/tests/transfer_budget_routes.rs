//! `GET /api/workspace/backup-transfer-budget` through the production router (S1).
//!
//! The oracle seeds durable `qos_transfers` rows — including an expired lease and
//! a completed background transfer — through the real Python control store, then
//! serves the route with `TestClient`. Both sides read the same workspace root.
//!
//! Two behaviours are pinned by the comparison:
//!
//! * the counters and the `rebalanceBacklogBytes` sum (`max(0, estimated -
//!   transferred)` over classes 5/6 only);
//! * the original `list_qos_transfers` **deletes** expired leases before
//!   answering. The native read filters them instead, so the bodies match while
//!   the store is unchanged — asserted directly by counting rows afterwards.
//!
//! The original's in-memory `TransferBudgetManager` half is documented as the
//! slice's narrowing: it exists only inside the transferring Python process and
//! a separate native process cannot observe it, so it is not simulated.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::Value;
use tower::ServiceExt;

const TEST_TOKEN: &str = "transfer-budget-route-token";
const BUDGET_URI: &str = "/api/workspace/backup-transfer-budget";

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
            ("AUTH_DISABLED", String::new()),
            ("AUTH_ALLOWED_HOSTS", String::new()),
            ("HOST", String::new()),
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

    fn path(&self) -> &Path {
        self.root.path()
    }

    async fn request(
        &self,
        method: &str,
        token: bool,
    ) -> (StatusCode, Vec<u8>, Option<String>, Option<String>) {
        let mut request = Request::builder()
            .method(method)
            .uri(BUDGET_URI)
            .header(header::HOST, "127.0.0.1");
        if token {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let header_value = |name: axum::http::HeaderName| {
            response
                .headers()
                .get(name)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string)
        };
        let allow = header_value(header::ALLOW);
        let acam = header_value(header::ACCESS_CONTROL_ALLOW_METHODS);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, bytes.to_vec(), allow, acam)
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn spawn_oracle(root: &Path, script: &str) -> std::process::Child {
    Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .env("PYTHONPATH", ".")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python")
}

fn write_and_wait(mut child: std::process::Child, payload: &str) -> Vec<u8> {
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(payload.as_bytes())
        .expect("write oracle input");
    let output = child.wait_with_output().expect("oracle");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

const ORACLE_SCRIPT: &str = r#"
import json, sys, time
from fastapi.testclient import TestClient
from deepseek_infra.infra.workspace import backup_control
from deepseek_infra.web.server import create_app

data = json.load(sys.stdin)

# id, class, estimated, transferred, lease offset (seconds)
ROWS = [
    ("t-p0",      0,       1000,        100,  3600),
    ("t-repair",  2,        500,        500,  3600),
    ("t-repair2", 3,        700,        200,  3600),
    ("t-bg",      5,        900,        100,  3600),
    ("t-bg2",     6,        400,        200,  3600),
    ("t-bg-done", 5,         10,         10,  3600),
    ("t-other",   9,        100,          0,  3600),
    ("t-expired", 0,       1000,          0, -120),
]

def seed():
    now = time.time()
    with backup_control._connect() as conn:
        for tid, cls, estimated, transferred, lease in ROWS:
            conn.execute(
                "INSERT OR REPLACE INTO qos_transfers(transfer_id, owner_instance_id,"
                " traffic_class, source_target_id, dest_target_id, estimated_bytes,"
                " bytes_transferred, lease_until, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, "inst-1", cls, None, None, estimated, transferred, now + lease,
                 "2026-11-09T00:00:00Z"),
            )

def rows_present():
    with backup_control._connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM qos_transfers").fetchone()[0])

seed()
before = rows_present()
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
results = []
for case in data["cases"]:
    headers = {"Host": "127.0.0.1"}
    if case["token"]:
        headers["Authorization"] = "Bearer " + data["token"]
    try:
        response = client.request(case["method"], "/api/workspace/backup-transfer-budget", headers=headers)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text,
                        "allow": response.headers.get("allow", ""),
                        "acam": response.headers.get("access-control-allow-methods", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__,
                        "allow": "", "acam": ""})
after = rows_present()
client.close()
print(json.dumps({"before": before, "after": after, "results": results}))
"#;

struct Case {
    name: &'static str,
    method: &'static str,
    token: bool,
}

/// `GET`/`HEAD` are the route; `POST` is the catch-all 404; other verbs are 405.
const CASES: &[Case] = &[
    Case {
        name: "get",
        method: "GET",
        token: true,
    },
    Case {
        name: "put-method-not-allowed",
        method: "PUT",
        token: true,
    },
    Case {
        name: "post-catch-all",
        method: "POST",
        token: true,
    },
    Case {
        name: "delete-method-not-allowed",
        method: "DELETE",
        token: true,
    },
    Case {
        name: "options-global",
        method: "OPTIONS",
        token: true,
    },
    Case {
        name: "no-token",
        method: "GET",
        token: false,
    },
];

fn body_text(bytes: &[u8]) -> String {
    String::from_utf8(bytes.to_vec()).unwrap_or_default()
}

#[tokio::test]
async fn transfer_budget_matches_the_oracle_without_deleting_expired_leases() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.path().to_path_buf();

    let child = spawn_oracle(&root, ORACLE_SCRIPT);
    let payload = serde_json::json!({
        "token": TEST_TOKEN,
        "cases": CASES.iter().map(|case| serde_json::json!({
            "name": case.name,
            "method": case.method,
            "token": case.token,
        })).collect::<Vec<_>>(),
    });
    let output = write_and_wait(child, &payload.to_string());
    let oracle: Value = serde_json::from_slice(&output).expect("oracle json");
    let results = oracle["results"].as_array().expect("oracle results");

    // The original deletes the expired lease while answering; the native read
    // must leave every row in place. Check both halves of that: `after` below the
    // oracle's `before` proves the original really does delete, and the native
    // count afterwards must still be the naively seeded size.
    assert_eq!(oracle["before"].as_u64(), Some(8), "oracle seed");
    assert_eq!(
        oracle["after"].as_u64(),
        Some(7),
        "the original no longer deletes the expired lease; the parity premise changed"
    );

    for (case, want) in CASES.iter().zip(results.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        let (status, body, allow, acam) = fixture.request(case.method, case.token).await;
        assert_eq!(
            status.as_u16(),
            want["status"].as_u64().expect("oracle status") as u16,
            "{}: {}",
            case.name,
            body_text(&body)
        );
        assert_eq!(
            body_text(&body),
            want["body"].as_str().unwrap_or_default(),
            "{}: body",
            case.name
        );
        assert_eq!(
            allow.as_deref().unwrap_or(""),
            want["allow"].as_str().unwrap_or(""),
            "{}: allow",
            case.name
        );
        if case.method == "OPTIONS" {
            assert_eq!(
                acam.as_deref().unwrap_or(""),
                want["acam"].as_str().unwrap_or(""),
                "{}: access-control-allow-methods",
                case.name
            );
        }
    }

    // HEAD is native-only: the original reaches the static catch-all for HEAD.
    let (status, body, _, _) = fixture.request("HEAD", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.is_empty());

    // Re-seed so the native count is meaningful after the oracle's deletion, then
    // read natively and confirm the read did not delete the expired row.
    let child = spawn_oracle(&root, SEED_SCRIPT);
    write_and_wait(child, "{}");
    let (status, body, _, _) = fixture.request("GET", true).await;
    assert_eq!(status, StatusCode::OK, "{}", body_text(&body));
    assert!(
        body_text(&body).contains(r#""activeTransfersTotal":7"#),
        "the expired lease should be filtered out of the count: {}",
        body_text(&body)
    );
    let child = spawn_oracle(&root, COUNT_SCRIPT);
    let output = write_and_wait(child, "{}");
    let counted: Value = serde_json::from_slice(&output).expect("count json");
    assert_eq!(
        counted["rows"].as_u64(),
        Some(8),
        "the native read deleted an expired lease from the control store"
    );
}

/// Seeds the same eight rows the oracle uses, for the non-deletion assertion.
const SEED_SCRIPT: &str = r#"
import json, time
from deepseek_infra.infra.workspace import backup_control
ROWS = [
    ("t-p0",      0,       1000,        100,  3600),
    ("t-repair",  2,        500,        500,  3600),
    ("t-repair2", 3,        700,        200,  3600),
    ("t-bg",      5,        900,        100,  3600),
    ("t-bg2",     6,        400,        200,  3600),
    ("t-bg-done", 5,         10,         10,  3600),
    ("t-other",   9,        100,          0,  3600),
    ("t-expired", 0,       1000,          0, -120),
]
now = time.time()
with backup_control._connect() as conn:
    for tid, cls, estimated, transferred, lease in ROWS:
        conn.execute(
            "INSERT OR REPLACE INTO qos_transfers(transfer_id, owner_instance_id, traffic_class,"
            " source_target_id, dest_target_id, estimated_bytes, bytes_transferred,"
            " lease_until, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, "inst-1", cls, None, None, estimated, transferred, now + lease,
             "2026-11-09T00:00:00Z"),
        )
print(json.dumps({"seeded": len(ROWS)}))
"#;

const COUNT_SCRIPT: &str = r#"
import json
from deepseek_infra.infra.workspace import backup_control
with backup_control._connect() as conn:
    rows = int(conn.execute("SELECT COUNT(*) FROM qos_transfers").fetchone()[0])
print(json.dumps({"rows": rows}))
"#;

/// A workspace that has never recorded a transfer answers without creating the
/// control store, and gets the same body as an initialized store with no rows.
#[tokio::test]
async fn transfer_budget_answers_an_unused_workspace_without_creating_the_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.path().to_path_buf();
    assert!(!root.join(".backup-control").exists());

    let (status, body, _, _) = fixture.request("GET", true).await;
    assert_eq!(status, StatusCode::OK, "{}", body_text(&body));
    assert_eq!(
        body_text(&body),
        concat!(
            r#"{"activeTransfersTotal":0,"activeRecoveryTransfers":0,"activeRepairTransfers":0,"#,
            r#""rebalanceBacklogBytes":0,"backgroundThrottleActive":false,"#,
            r#""globalBytesPerSecond":209715200,"globalBandwidthBytesPerSec":209715200,"#,
            r#""reservedRecoveryBytesPerSecond":104857600,"reservedDrBandwidthBytesPerSec":104857600,"#,
            r#""repairReservedBytesPerSecond":104857600}"#
        )
    );
    assert!(
        !root.join(".backup-control").exists(),
        "the read-only route created the control store"
    );
}
