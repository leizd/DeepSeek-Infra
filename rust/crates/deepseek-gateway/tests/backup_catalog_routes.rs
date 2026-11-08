//! `GET /api/workspace/backup-catalog` through the production router (S2).
//!
//! The Python oracle seeds the real catalog through the `backup_catalog` writers
//! and then serves the route with `TestClient`; both sides read the same
//! workspace root. Bodies are compared as **bytes** after normalising only
//! `health.evaluatedAt` (the single wall-clock field), so key order, the fold
//! order of governance events, the chain recomputation, the orphan/missing scan
//! and the health evaluation are all pinned by the oracle.
//!
//! The native side additionally proves the read-only contract: a workspace with
//! no `.backups` directory answers the empty catalog and still has no
//! `.backups` directory afterwards.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::Value;
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-catalog-route-token";

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
        uri: &str,
        token: bool,
    ) -> (StatusCode, Vec<u8>, Option<String>, Option<String>) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
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

/// Replaces the single wall-clock field so two runs compare byte-for-byte.
fn normalize(body: &str) -> String {
    let marker = "\"evaluatedAt\":\"";
    let Some(start) = body.find(marker) else {
        return body.to_string();
    };
    let from = start + marker.len();
    let Some(len) = body[from..].find('"') else {
        return body.to_string();
    };
    format!("{}<evaluatedAt>{}", &body[..from], &body[from + len..])
}

struct Case {
    name: &'static str,
    method: &'static str,
    uri: &'static str,
    token: bool,
}

const CASES: &[Case] = &[
    Case {
        name: "get",
        method: "GET",
        uri: "/api/workspace/backup-catalog",
        token: true,
    },
    Case {
        name: "get-policy",
        method: "GET",
        uri: "/api/workspace/backup-catalog?policyId=pol-a",
        token: true,
    },
    Case {
        name: "get-policy-unknown",
        method: "GET",
        uri: "/api/workspace/backup-catalog?policyId=nope",
        token: true,
    },
    Case {
        name: "get-policy-empty",
        method: "GET",
        uri: "/api/workspace/backup-catalog?policyId=",
        token: true,
    },
    Case {
        name: "get-target-explicit",
        method: "GET",
        uri: "/api/workspace/backup-catalog?targetId=managed-local",
        token: true,
    },
    Case {
        name: "get-target-empty",
        method: "GET",
        uri: "/api/workspace/backup-catalog?targetId=",
        token: true,
    },
    Case {
        name: "get-target-unknown",
        method: "GET",
        uri: "/api/workspace/backup-catalog?targetId=does-not-exist",
        token: true,
    },
    Case {
        name: "get-bare-target-name",
        method: "GET",
        uri: "/api/workspace/backup-catalog?targetId",
        token: true,
    },
    Case {
        name: "put-method-not-allowed",
        method: "PUT",
        uri: "/api/workspace/backup-catalog",
        token: true,
    },
    Case {
        name: "post-catch-all",
        method: "POST",
        uri: "/api/workspace/backup-catalog",
        token: true,
    },
    Case {
        name: "options-global",
        method: "OPTIONS",
        uri: "/api/workspace/backup-catalog",
        token: true,
    },
    Case {
        name: "no-token",
        method: "GET",
        uri: "/api/workspace/backup-catalog",
        token: false,
    },
];

/// Seeds the real catalog through the Python writers, then serves `CASES`.
fn oracle(root: &Path) -> Vec<Value> {
    let script = r#"
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from deepseek_infra.infra.workspace import backup_catalog, backup_object_set, backups
from deepseek_infra.web.server import create_app

root = backups.BACKUP_DIR
digest_a = "a" * 64
digest_b = "b" * 64
digest_c = "c" * 64
for digest in (digest_a, digest_b):
    path = root / "objects" / "sha256" / digest[:2] / (digest + ".age")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ciphertext")
orphan = root / "objects" / "sha256" / digest_c[:2] / (digest_c + ".age")
orphan.parent.mkdir(parents=True, exist_ok=True)
orphan.write_bytes(b"orphan")
(root / "backups").mkdir(parents=True, exist_ok=True)
(root / "backups" / "legacy.dsibackup").write_bytes(b"legacy")

legacy = {
    "backupId": "bk-legacy",
    "createdAt": "2026-10-01T00:00:01Z",
    "policyId": "pol-a",
    "filename": "bk-legacy.age",
    "objectDigest": digest_a,
    "ciphertextSha256": digest_a,
    "size": 10,
    "creationVerified": True,
    "targetId": "managed-local",
}
object_set = {
    "backupId": "bk-set",
    "createdAt": "2026-10-02T00:00:01Z",
    "policyId": "pol-b",
    "filename": "bk-set.age",
    "storageProtocol": "object-set-v1",
    "objects": [
        {"digest": digest_a, "size": 10, "role": "ciphertext"},
        {"digest": digest_b, "size": 20, "role": "control"},
    ],
    "objectSetDigest": "",
    "controlObjectDigest": digest_b,
    "creationVerified": False,
    "targetId": "managed-local",
}
object_set["objectSetDigest"] = backup_object_set.object_inventory_digest(object_set["objects"])
broken = {
    "backupId": "bk-broken",
    "createdAt": "2026-10-03T00:00:01Z",
    "policyId": "pol-b",
    "filename": "bk-broken.age",
    "storageProtocol": "object-set-v1",
    "objects": [{"digest": digest_c, "size": 5, "role": "ciphertext"}],
    "objectSetDigest": "f" * 64,
    "controlObjectDigest": digest_c,
    "creationVerified": False,
    "targetId": "managed-local",
}
trashed = {
    "backupId": "bk-trashed",
    "createdAt": "2026-10-04T00:00:01Z",
    "policyId": "pol-a",
    "filename": "bk-trashed.age",
    "objectDigest": digest_c,
    "ciphertextSha256": digest_c,
    "size": 4,
    "creationVerified": False,
    "targetId": "managed-local",
}
deleted = {
    "backupId": "bk-deleted",
    "createdAt": "2026-10-05T00:00:01Z",
    "policyId": "pol-a",
    "filename": "bk-deleted.age",
    "objectDigest": "d" * 64,
    "ciphertextSha256": "d" * 64,
    "size": 3,
    "creationVerified": False,
    "targetId": "managed-local",
}
for receipt in (legacy, object_set, broken, trashed, deleted):
    if receipt is broken:
        # A catalog row whose object-set commitment no longer validates: written
        # through the chain writer because `append_receipt` validates first. The
        # fold must still list it and report it as missing without any candidates.
        backup_catalog._append_entry(root, "receipt", broken)
    else:
        backup_catalog.append_receipt(root, receipt)
backup_catalog.pin_backup(root, "bk-legacy", True)
backup_catalog.record_scrub(root, "bk-legacy", ok=True, detail="ok")
backup_catalog.record_unlock_verification(root, "bk-legacy")
backup_catalog.record_scrub(root, "bk-set", ok=False, detail="bad")
backup_catalog.record_trash(root, "bk-trashed", retention_run_id="run-1", at="2026-10-06T00:00:00Z")
backup_catalog.record_delete(root, "bk-deleted", retention_run_id="run-1", at="2026-10-07T00:00:00Z")
# An overdue unlock verification, written through the chain writer so the
# timestamp is a stored value rather than a clock reading.
backup_catalog._append_entry(root, "unlock-verified", {"backupId": "bk-set", "userUnlockVerifiedAt": "2020-01-01T00:00:00Z"})
(root / "receipts").mkdir(parents=True, exist_ok=True)
(root / "receipts" / "bk-legacy.json").write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")

data = json.load(sys.stdin)
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
results = []
for case in data["cases"]:
    headers = {"Host": "127.0.0.1"}
    if case["token"]:
        headers["Authorization"] = "Bearer " + data["token"]
    try:
        response = client.request(case["method"], case["uri"], headers=headers)
        results.append({"name": case["name"], "status": response.status_code,
                        "body": response.text, "allow": response.headers.get("allow", ""),
                        "acam": response.headers.get("access-control-allow-methods", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__,
                        "allow": "", "acam": ""})
client.close()
print(json.dumps(results))
"#;
    run_oracle(root, script, CASES)
}

/// The empty-catalog body, produced in a separate root so the seeded root above
/// keeps its own content.
fn empty_catalog_oracle(root: &Path) -> String {
    let script = r#"
import json, sys
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app
data = json.load(sys.stdin)
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
response = client.get("/api/workspace/backup-catalog",
                      headers={"Host": "127.0.0.1", "Authorization": "Bearer " + data["token"]})
client.close()
print(json.dumps({"status": response.status_code, "body": response.text}))
"#;
    let child = spawn_oracle(root, script);
    let payload = serde_json::json!({"token": TEST_TOKEN});
    let output = write_and_wait(child, &payload.to_string());
    let value: Value = serde_json::from_slice(&output).expect("oracle json");
    assert_eq!(value["status"], 200, "oracle status");
    value["body"].as_str().expect("oracle body").to_string()
}

fn run_oracle(root: &Path, script: &str, cases: &[Case]) -> Vec<Value> {
    let child = spawn_oracle(root, script);
    let payload = serde_json::json!({
        "token": TEST_TOKEN,
        "cases": cases.iter().map(|case| serde_json::json!({
            "name": case.name,
            "method": case.method,
            "uri": case.uri,
            "token": case.token,
        })).collect::<Vec<_>>(),
    });
    let output = write_and_wait(child, &payload.to_string());
    serde_json::from_slice(&output).expect("oracle json")
}

/// The Python oracle runs under the same workspace root as the native fixture,
/// so both sides read the identical store.
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

fn body_text(bytes: &[u8]) -> String {
    String::from_utf8(bytes.to_vec()).unwrap_or_default()
}

#[tokio::test]
async fn backup_catalog_matches_the_oracle_and_creates_nothing() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.path().to_path_buf();
    let expected = oracle(&root);
    let baseline = tree(&root);

    for (case, want) in CASES.iter().zip(expected.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        let (status, body, allow, acam) = fixture.request(case.method, case.uri, case.token).await;
        assert_eq!(
            status.as_u16(),
            want["status"].as_u64().expect("oracle status") as u16,
            "{}: {}",
            case.name,
            body_text(&body)
        );
        assert_eq!(
            normalize(&body_text(&body)),
            normalize(want["body"].as_str().unwrap_or_default()),
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

    // HEAD is answered by the native route; the original reaches the static
    // catch-all for HEAD, so it cannot be compared in this fixture.
    let (status, body, _, _) = fixture
        .request("HEAD", "/api/workspace/backup-catalog", true)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.is_empty());

    // A corrupt catalog is the documented 409 refusal, and the empty catalog
    // body is unchanged from the original.
    let catalog = root
        .join(".backups")
        .join("catalog")
        .join("catalog-v1.jsonl");
    let original = std::fs::read_to_string(&catalog).unwrap();
    std::fs::write(&catalog, format!("{original}{{not json}}\n")).unwrap();
    let (status, body, _, _) = fixture
        .request("GET", "/api/workspace/backup-catalog", true)
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        body_text(&body),
        r#"{"error":"Backup catalog is corrupt; rebuild it from receipts","code":"invalid_request"}"#
    );
    std::fs::write(&catalog, original).unwrap();

    assert_eq!(
        tree(&root),
        baseline,
        "reading the catalog changed the tree"
    );
}

#[tokio::test]
async fn backup_catalog_of_an_unused_workspace_is_empty_and_read_only() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.path().to_path_buf();
    assert!(!root.join(".backups").exists());

    let (status, body, _, _) = fixture
        .request("GET", "/api/workspace/backup-catalog", true)
        .await;
    assert_eq!(status, StatusCode::OK, "{}", body_text(&body));
    assert!(
        !root.join(".backups").exists(),
        "the read-only route created the managed backup directory"
    );

    // The original creates the directory and answers the same empty catalog.
    let oracle_root = tempfile::tempdir().expect("oracle workspace");
    let expected = empty_catalog_oracle(oracle_root.path());
    assert_eq!(normalize(&body_text(&body)), normalize(&expected));
    assert!(
        oracle_root.path().join(".backups").is_dir(),
        "the oracle fixture did not exercise the creating branch"
    );
}

/// A *registered* target is the remaining S2 capability: the original resolves
/// filesystem markers through `verify_target_ready` and reads the S3 catalog
/// store, neither of which is declared as a Rust data domain yet. The native
/// edge refuses by name instead of answering from the wrong store, and the
/// refusal does not touch the control database.
#[tokio::test]
async fn backup_catalog_refuses_a_registered_target_without_writing_the_control_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.path().to_path_buf();

    let script = r#"
import json, os
from pathlib import Path
from deepseek_infra.infra.workspace import backup_control
root = Path(os.environ["DEEPSEEK_INFRA_ROOT"])
target = root / "external-target"
target.mkdir(parents=True, exist_ok=True)
# `upsert_target` opens and closes the control store, so the writer's `-wal` and
# `-shm` side files are gone once it returns.
record = backup_control.upsert_target({
    "targetId": "fs-1",
    "kind": "filesystem",
    "path": str(target),
    "label": "external disk",
})
print(json.dumps(record))
"#;
    let child = spawn_oracle(&root, script);
    let output = write_and_wait(child, "{}");
    assert!(!output.is_empty(), "the control store was not seeded");

    let control_dir = root.join(".backup-control");
    let control_file = control_dir.join("control.sqlite3");
    assert!(control_file.is_file(), "the control store was not seeded");
    let before = tree(&control_dir);

    for uri in [
        "/api/workspace/backup-catalog?targetId=fs-1",
        "/api/workspace/backup-catalog?targetId=fs-1&policyId=pol-a",
    ] {
        let (status, body, _, _) = fixture.request("GET", uri, true).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{uri}");
        assert_eq!(
            body_text(&body),
            r#"{"error":"backup-catalog for a registered target is not served by the native runtime yet","code":"NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED"}"#,
            "{uri}"
        );
    }

    assert_eq!(
        tree(&control_dir),
        before,
        "reading a registered target changed the control store"
    );
}
