//! Integration tests for S2 native catalog mutations and integrity routes:
//! - `POST|DELETE /api/workspace/backup-catalog/:backup_id/pin`
//! - `POST /api/workspace/backups/:backup_id/scrub`
//! - `POST /api/workspace/backups/:backup_id/verify-unlock`

use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_mirror::sha256_hex;
use deepseek_policy::backup_unlock::{encrypt_bytes_to_recipients, ephemeral_identity};
use deepseek_policy::python_json::dumps_compact;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-catalog-write-route-token";

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
            .map(|(name, _)| (*name, std::env::var(name).ok()))
            .collect();
        for (name, value) in pairs {
            // SAFETY: serialized across tests in this binary via `EnvLock`.
            unsafe {
                if value.is_empty() {
                    std::env::remove_var(name);
                } else {
                    std::env::set_var(name, value);
                }
            }
        }
        Self { saved }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            // SAFETY: serialized across tests in this binary via `EnvLock`.
            unsafe {
                match value {
                    Some(text) => std::env::set_var(name, text),
                    None => std::env::remove_var(name),
                }
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
    fn with_mode(mode: &str) -> Self {
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
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
            ("DEEPSEEK_GO_CONTROL", String::new()),
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

    async fn request_raw(
        &self,
        method: &str,
        uri: &str,
        body: Option<&str>,
        token: bool,
    ) -> (StatusCode, Vec<u8>, Option<String>, Option<String>) {
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if token {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let payload = if let Some(text) = body {
            builder = builder
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, text.len().to_string());
            Body::from(text.to_string())
        } else {
            Body::empty()
        };
        let response = self
            .app
            .clone()
            .oneshot(builder.body(payload).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let content_type = response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let allow = response
            .headers()
            .get(header::ALLOW)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec();
        (status, bytes, content_type, allow)
    }

    async fn request_json(
        &self,
        method: &str,
        uri: &str,
        body: Option<&str>,
        token: bool,
    ) -> (StatusCode, Value, Option<String>) {
        let (status, bytes, _, allow) = self.request_raw(method, uri, body, token).await;
        let parsed = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or_else(|err| {
                panic!(
                    "expected JSON body for {method} {uri} ({status}): {err}; raw={}",
                    String::from_utf8_lossy(&bytes)
                )
            })
        };
        (status, parsed, allow)
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn run_python(root: &Path, script: &str, stdin_json: &Value) -> Value {
    let mut child = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .env("DEEPSEEK_RUNTIME_MODE", "")
        .env("DEEPSEEK_GO_CONTROL", "")
        .env("PYTHONPATH", ".")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(stdin_json.to_string().as_bytes())
        .expect("write stdin");
    let output = child.wait_with_output().expect("wait python");
    assert!(
        output.status.success(),
        "python helper failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("python json stdout")
}

fn tree(root: &Path) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    fn walk(base: &Path, dir: &Path, out: &mut BTreeSet<String>) {
        let Ok(entries) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let rel = path
                .strip_prefix(base)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            if path.is_dir() {
                out.insert(format!("{rel}/"));
                walk(base, &path, out);
            } else {
                out.insert(rel);
            }
        }
    }
    walk(root, root, &mut out);
    out
}

fn make_zip_bytes(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let dir = tempfile::tempdir().unwrap();
    let payload: Vec<Value> = entries
        .iter()
        .map(|(name, bytes)| json!({"name": *name, "bytes": bytes.to_vec()}))
        .collect();
    let script = r#"
import io, json, sys, zipfile
items = json.load(sys.stdin)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
    for item in items:
        zf.writestr(item["name"], bytes(item["bytes"]))
print(json.dumps({"bytes": list(buf.getvalue())}))
"#;
    let res = run_python(dir.path(), script, &Value::Array(payload));
    res["bytes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_u64().unwrap() as u8)
        .collect()
}

#[tokio::test]
async fn python_authoritative_mode_returns_404_then_409_without_touching_disk() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("");
    let root = fixture.path().to_path_buf();

    // Seed one catalog entry using the Python catalog module.
    let seed_script = r#"
import json, sys
from pathlib import Path
from deepseek_infra.infra.workspace import backups, backup_catalog
backup_root = backups.BACKUP_DIR
backup_catalog.append_receipt(
    backup_root,
    {
        "backupId": "bak-seeded-1",
        "filename": "bak-seeded-1.dsibackup.age",
        "policyId": "pol-a",
        "createdAt": "2026-10-10T00:00:00Z",
        "sizeBytes": 128,
        "ciphertextSha256": "a" * 64,
        "manifestDigest": "b" * 64,
    },
)
print(json.dumps({"ok": True}))
"#;
    run_python(&root, seed_script, &json!({}));
    let before = tree(&root);

    // Unknown backup ID fails with 404 before store ownership gate and creates no dirs.
    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backup-catalog/missing/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["code"], "not_found");

    // Known backup ID is refused with 409 NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED across pin/scrub/verify-unlock.
    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backup-catalog/bak-seeded-1/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED");

    let (status, body, _) = fixture
        .request_json(
            "DELETE",
            "/api/workspace/backup-catalog/bak-seeded-1/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED");

    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-seeded-1/scrub",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED");

    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-seeded-1/verify-unlock",
            Some(r#"{"identity":"AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ3Q5D77"}"#),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED");

    assert_eq!(
        tree(&root),
        before,
        "409 refusals must leave workspace directories completely untouched"
    );
}

#[tokio::test]
async fn default_mode_matches_python_oracle_byte_for_byte_across_errors_and_methods() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("");
    let oracle_root = tempfile::tempdir().expect("oracle workspace");

    let cases = json!([
        {"name": "unauth", "method": "POST", "uri": "/api/workspace/backup-catalog/missing/pin", "token": false, "body": null},
        {"name": "options-pin", "method": "OPTIONS", "uri": "/api/workspace/backup-catalog/missing/pin", "token": true, "body": null},
        {"name": "get-pin-404", "method": "GET", "uri": "/api/workspace/backup-catalog/missing/pin", "token": true, "body": null},
        {"name": "put-pin-405", "method": "PUT", "uri": "/api/workspace/backup-catalog/missing/pin", "token": true, "body": null},
        {"name": "get-scrub-404", "method": "GET", "uri": "/api/workspace/backups/missing/scrub", "token": true, "body": null},
        {"name": "del-scrub-405", "method": "DELETE", "uri": "/api/workspace/backups/missing/scrub", "token": true, "body": null},
        {"name": "get-unlock-404", "method": "GET", "uri": "/api/workspace/backups/missing/verify-unlock", "token": true, "body": null},
        {"name": "put-unlock-405", "method": "PUT", "uri": "/api/workspace/backups/missing/verify-unlock", "token": true, "body": null},
        {"name": "post-pin-404", "method": "POST", "uri": "/api/workspace/backup-catalog/missing/pin", "token": true, "body": null},
        {"name": "del-pin-404", "method": "DELETE", "uri": "/api/workspace/backup-catalog/missing/pin", "token": true, "body": null},
        {"name": "post-scrub-404", "method": "POST", "uri": "/api/workspace/backups/missing/scrub", "token": true, "body": null},
        {"name": "post-unlock-empty", "method": "POST", "uri": "/api/workspace/backups/missing/verify-unlock", "token": true, "body": ""},
        {"name": "post-unlock-non-obj", "method": "POST", "uri": "/api/workspace/backups/missing/verify-unlock", "token": true, "body": "[1]"},
        {"name": "post-unlock-bad-id", "method": "POST", "uri": "/api/workspace/backups/missing/verify-unlock", "token": true, "body": "{\"identity\":\"bad\"}"},
        {"name": "post-unlock-404", "method": "POST", "uri": "/api/workspace/backups/missing/verify-unlock", "token": true, "body": "{\"identity\":\"AGE-SECRET-KEY-1ABC\"}"}
    ]);

    let oracle_script = r#"
import json, os, sys
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app

cases = json.load(sys.stdin)
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
results = []
try:
    for case in cases:
        headers = {"Host": "127.0.0.1"}
        if case["token"]:
            headers["Authorization"] = f"Bearer {os.environ['AUTH_TOKEN']}"
        content = case["body"].encode("utf-8") if case["body"] is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        r = client.request(case["method"], case["uri"], headers=headers, content=content)
        results.append({
            "name": case["name"],
            "status": r.status_code,
            "ct": r.headers.get("content-type"),
            "allow": r.headers.get("allow"),
            "body": r.text,
        })
finally:
    client.close()
print(json.dumps(results))
"#;
    let oracle_results = run_python(oracle_root.path(), oracle_script, &cases);

    for (case, expected) in cases
        .as_array()
        .unwrap()
        .iter()
        .zip(oracle_results.as_array().unwrap().iter())
    {
        let name = case["name"].as_str().unwrap();
        let method = case["method"].as_str().unwrap();
        let uri = case["uri"].as_str().unwrap();
        let body = case["body"].as_str();
        let token = case["token"].as_bool().unwrap();
        let (status, bytes, ct, allow) = fixture.request_raw(method, uri, body, token).await;
        assert_eq!(
            status.as_u16() as i64,
            expected["status"].as_i64().unwrap(),
            "{name}: status mismatch"
        );
        assert_eq!(
            ct.as_deref(),
            expected["ct"].as_str(),
            "{name}: content-type"
        );
        assert_eq!(
            allow.as_deref(),
            expected["allow"].as_str(),
            "{name}: allow"
        );
        assert_eq!(
            String::from_utf8(bytes).unwrap(),
            expected["body"].as_str().unwrap(),
            "{name}: body mismatch"
        );
    }
}

#[tokio::test]
async fn pin_unpin_method_guards_and_python_chain_verification() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("python_disabled");
    let root = fixture.path().to_path_buf();

    let seed_script = r#"
import json
from deepseek_infra.infra.workspace import backups, backup_catalog, backup_publish
root = backups.BACKUP_DIR
backup_catalog.append_receipt(
    root,
    {
        "backupId": "bak-pin-1",
        "filename": "bak-pin-1.dsibackup.age",
        "policyId": "pol-a",
        "createdAt": "2026-10-10T00:00:00Z",
        "sizeBytes": 64,
        "ciphertextSha256": "c" * 64,
        "manifestDigest": "d" * 64,
    },
)
commits = root / "commits" / "00"
commits.mkdir(parents=True, exist_ok=True)
(commits / "c1.json").write_text(json.dumps({
    "backupId": "bak-pin-1",
    "commitHash": "e" * 64,
    "targetGeneration": 7,
}), encoding="utf-8")
print(json.dumps({"ok": True}))
"#;
    run_python(&root, seed_script, &json!({}));

    // Unauthenticated request returns 401.
    let (status, _, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backup-catalog/bak-pin-1/pin",
            None,
            false,
        )
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    // GET hits FastAPI `/api/{path}` catch-all 404 parity; PUT returns 405 with Allow: OPTIONS.
    let (status, body, allow) = fixture
        .request_json(
            "GET",
            "/api/workspace/backup-catalog/bak-pin-1/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body, json!({"error": "Not found", "code": "not_found"}));
    assert_eq!(allow, None);

    let (status, body, allow) = fixture
        .request_json(
            "PUT",
            "/api/workspace/backup-catalog/bak-pin-1/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(body["detail"], "Method Not Allowed");
    assert_eq!(allow.as_deref(), Some("OPTIONS"));

    // Pin succeeds and stamps targetGeneration = 7.
    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backup-catalog/bak-pin-1/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, json!({"backupId": "bak-pin-1", "pinned": true}));
    assert!(!root.join(".backups/.target-lock/writer.json").exists());

    // Unpin succeeds.
    let (status, body, _) = fixture
        .request_json(
            "DELETE",
            "/api/workspace/backup-catalog/bak-pin-1/pin",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, json!({"backupId": "bak-pin-1", "pinned": false}));

    // Verify Python's verify_chain & catalog_state accept every Rust-appended line & event file.
    let verify_script = r#"
import json
from deepseek_infra.infra.workspace import backups, backup_catalog
root = backups.BACKUP_DIR
assert backup_catalog.verify_chain(root) is True
entries = backup_catalog._read_entries(root)
state = backup_catalog.catalog_state(root)
print(json.dumps({
    "count": len(entries),
    "pinGen": entries[1]["targetGeneration"],
    "pinned": state["bak-pin-1"]["pinned"],
    "eventExists": (root / "events" / entries[1]["entryHash"][:2] / f"{entries[1]['entryHash']}.json").is_file(),
}))
"#;
    let verified = run_python(&root, verify_script, &json!({}));
    assert_eq!(verified["count"], 3);
    assert_eq!(verified["pinGen"], 7);
    assert_eq!(verified["pinned"], false);
    assert_eq!(verified["eventExists"], true);
}

#[tokio::test]
async fn scrub_and_verify_unlock_object_set_with_frontend_mirror_and_lease_contention() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("python_disabled");
    let root = fixture.path().to_path_buf();
    let backups_root = root.join(".backups");

    let (secret, recipient) = ephemeral_identity();
    let (wrong_secret, _) = ephemeral_identity();

    // Build sealed frontend mirror envelope + encrypted frontend/sealed-state.age.
    let mut env_body = serde_json::Map::new();
    env_body.insert("schemaVersion".into(), json!(1));
    env_body.insert("profileId".into(), json!("default"));
    env_body.insert("sourceEpoch".into(), json!("epoch-1"));
    env_body.insert("conversations".into(), json!([{"id": "c1"}]));
    env_body.insert("conflicts".into(), json!([]));
    let digest = sha256_hex(dumps_compact(&Value::Object(env_body.clone())).as_bytes());
    env_body.insert("digest".into(), Value::String(digest.clone()));
    let sealed_frontend_bytes = encrypt_bytes_to_recipients(
        serde_json::to_vec(&env_body).unwrap().as_slice(),
        &[recipient.clone()],
    )
    .unwrap();
    let sealed_frontend_meta = serde_json::to_vec(&json!({
        "profileId": "default",
        "sourceEpoch": "epoch-1",
        "envelopeDigest": digest,
        "recipientSetDigest": "rset-1",
        "createdAt": "2026-10-10T00:00:00Z",
    }))
    .unwrap();

    // Build object-set payload zip + control zip.
    let app_file_bytes = br#"{"title":"notes"}"#;
    let app_rel = "payload/projects/p1/note.json";
    let manifest_bytes = serde_json::to_vec(&json!({
        "schemaVersion": "deepseek-workspace-backup.v1",
        "purpose": "restorable-backup",
        "snapshotKind": "full",
        "contributors": [{"id": "projects"}],
        "files": [
            {
                "path": app_rel,
                "size": app_file_bytes.len(),
                "sha256": sha256_hex(app_file_bytes),
            },
            {
                "path": "frontend/sealed-state.age",
                "size": sealed_frontend_bytes.len(),
                "sha256": sha256_hex(&sealed_frontend_bytes),
            },
            {
                "path": "frontend/sealed-state.meta.json",
                "size": sealed_frontend_meta.len(),
                "sha256": sha256_hex(&sealed_frontend_meta),
            }
        ],
    }))
    .unwrap();
    let manifest_digest = sha256_hex(&manifest_bytes);
    let component_map_bytes = serde_json::to_vec(&json!({
        "components": {
            "projects-p1": [
                app_rel,
                "frontend/sealed-state.age",
                "frontend/sealed-state.meta.json",
            ]
        }
    }))
    .unwrap();
    let payload_with_frontend_zip = make_zip_bytes(&[
        (app_rel, app_file_bytes),
        ("frontend/sealed-state.age", &sealed_frontend_bytes),
        ("frontend/sealed-state.meta.json", &sealed_frontend_meta),
    ]);
    let p2_plain_sha = sha256_hex(&payload_with_frontend_zip);
    let p2_plain_size = payload_with_frontend_zip.len() as u64;
    let p2_age =
        encrypt_bytes_to_recipients(&payload_with_frontend_zip, &[recipient.clone()]).unwrap();
    let p2_cipher_sha = sha256_hex(&p2_age);
    let p2_cipher_size = p2_age.len() as u64;

    let payload_index_bytes = serde_json::to_vec(&json!({
        "schemaVersion": 1,
        "storageProtocol": "object-set-v1",
        "snapshotFormat": "full",
        "payloadComponents": {
            "projects-p1": {
                "ciphertextDigest": p2_cipher_sha,
                "ciphertextSize": p2_cipher_size,
                "plaintextSize": p2_plain_size,
                "plaintextSha256": p2_plain_sha,
            }
        }
    }))
    .unwrap();

    let control_entries: Vec<(&str, &[u8])> = vec![
        ("component-map.json", &component_map_bytes),
        ("manifest.json", &manifest_bytes),
        ("payload-index.json", &payload_index_bytes),
    ];
    let mut checksum_text = String::new();
    for (name, bytes) in &control_entries {
        use std::fmt::Write as _;

        let _ = writeln!(checksum_text, "{}  {name}", sha256_hex(bytes));
    }
    let control_zip = make_zip_bytes(&[
        ("component-map.json", &component_map_bytes),
        ("manifest.json", &manifest_bytes),
        ("payload-index.json", &payload_index_bytes),
        ("checksums.sha256", checksum_text.as_bytes()),
    ]);
    let control_age = encrypt_bytes_to_recipients(&control_zip, &[recipient]).unwrap();
    let control_sha = sha256_hex(&control_age);
    let control_size = control_age.len() as u64;

    // Write objects and seed catalog receipt.
    for (digest_hex, bytes) in [(&control_sha, &control_age), (&p2_cipher_sha, &p2_age)] {
        let dir = backups_root
            .join("objects")
            .join("sha256")
            .join(&digest_hex[..2]);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(format!("{digest_hex}.age")), bytes).unwrap();
    }

    let seed_script = r#"
import json, sys
from deepseek_infra.infra.workspace import backups, backup_catalog, backup_object_set
params = json.load(sys.stdin)
objects = [
    {"digest": params["controlSha"], "size": params["controlSize"]},
    {"digest": params["payloadSha"], "size": params["payloadSize"]},
]
backup_catalog.append_receipt(
    backups.BACKUP_DIR,
    {
        "backupId": "bak-obj-1",
        "filename": "bak-obj-1.object-set",
        "policyId": "pol-a",
        "createdAt": "2026-10-10T00:00:00Z",
        "storageProtocol": "object-set-v1",
        "controlObjectDigest": params["controlSha"],
        "objectSetDigest": backup_object_set.object_inventory_digest(objects),
        "manifestDigest": params["manifestDigest"],
        "objects": objects,
    },
)
print(json.dumps({"ok": True}))
"#;
    run_python(
        &root,
        seed_script,
        &json!({
            "controlSha": control_sha,
            "controlSize": control_size,
            "payloadSha": p2_cipher_sha,
            "payloadSize": p2_cipher_size,
            "manifestDigest": manifest_digest,
        }),
    );

    // 1. Scrub succeeds and persists scrub_evidence in `.backup-dr/evidence.sqlite3`.
    let (status, body, _) = fixture
        .request_json("POST", "/api/workspace/backups/bak-obj-1/scrub", None, true)
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["backupId"], "bak-obj-1");
    assert_eq!(body["ok"], true);
    assert_eq!(body["objectsScrubbed"], 2);
    assert_eq!(body["checks"]["exists"], "PASS");
    assert_eq!(body["checks"]["not-symlink"], "PASS");
    assert_eq!(body["checks"]["size"], "PASS");
    assert_eq!(body["checks"]["sha256"], "PASS");
    assert_eq!(body["checks"]["age-header"], "PASS");

    let dr_conn = rusqlite::Connection::open(root.join(".backup-dr/evidence.sqlite3")).unwrap();
    let result: String = dr_conn
        .query_row(
            "SELECT result FROM scrub_evidence WHERE target_id = 'managed-local' AND backup_id = 'bak-obj-1' AND policy_id = 'pol-a'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(result, "success");

    // 2. Verify-unlock rejects malformed or wrong identity with 400 and leaves no staging behind.
    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-obj-1/verify-unlock",
            Some(r#"{"identity":"not-an-age-key"}"#),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_payload");

    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-obj-1/verify-unlock",
            Some(&json!({"identity": wrong_secret}).to_string()),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["code"], "invalid_payload");
    assert!(
        !root
            .join(".restore-staging/drills/drill_bak-obj-1")
            .exists()
    );

    // 3. Valid verify-unlock unlocks object-set + sealed frontend mirror and cleans staging.
    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-obj-1/verify-unlock",
            Some(&json!({"identity": secret}).to_string()),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["backupId"], "bak-obj-1");
    assert_eq!(body["ok"], true);
    assert_eq!(body["manifestDigest"], manifest_digest);
    assert_eq!(body["contributors"], 1);
    assert_eq!(body["sealedFrontend"]["mode"], "sealed-mirror");
    assert_eq!(body["sealedFrontend"]["conversations"], 1);
    assert!(body["userUnlockVerifiedAt"].as_str().is_some());
    assert!(
        !root
            .join(".restore-staging/drills/drill_bak-obj-1")
            .exists()
    );

    // 4. Active unexpired writer lease blocks scrub with 423.
    let lock_dir = backups_root.join(".target-lock");
    std::fs::create_dir_all(&lock_dir).unwrap();
    std::fs::write(
        lock_dir.join("writer.json"),
        serde_json::to_vec(&json!({
            "schemaVersion": 1,
            "targetId": "managed-local",
            "ownerRunId": "other-active-run",
            "ownerInstanceId": "other-worker",
            "fencingToken": 9,
            "acquiredAt": "2026-10-10T00:00:00Z",
            "expiresAt": "2099-01-01T00:00:00Z",
        }))
        .unwrap(),
    )
    .unwrap();
    let (status, body, _) = fixture
        .request_json("POST", "/api/workspace/backups/bak-obj-1/scrub", None, true)
        .await;
    assert_eq!(status, StatusCode::LOCKED);
    assert!(
        body["error"]
            .as_str()
            .unwrap()
            .contains("Target writer is busy")
    );
    std::fs::remove_file(lock_dir.join("writer.json")).unwrap();

    // 5. Tampering ciphertext causes scrub to record `ok: false` and `failed` DR evidence.
    let tampered_path = backups_root
        .join("objects")
        .join("sha256")
        .join(&p2_cipher_sha[..2])
        .join(format!("{p2_cipher_sha}.age"));
    std::fs::write(&tampered_path, b"not-age-corrupted").unwrap();
    let (status, body, _) = fixture
        .request_json("POST", "/api/workspace/backups/bak-obj-1/scrub", None, true)
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["ok"], false);
    assert!(
        body["checks"]["size"]
            .as_str()
            .unwrap()
            .starts_with("FAIL:")
    );
    assert!(
        body["checks"]["sha256"]
            .as_str()
            .unwrap()
            .starts_with("FAIL:")
    );
    assert!(
        body["checks"]["age-header"]
            .as_str()
            .unwrap()
            .starts_with("FAIL:")
    );
    let failed_result: String = dr_conn
        .query_row(
            "SELECT result FROM scrub_evidence WHERE target_id = 'managed-local' AND backup_id = 'bak-obj-1' AND policy_id = 'pol-a'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(failed_result, "failed");
}

#[tokio::test]
async fn legacy_archive_scrub_verify_unlock_and_expired_lease_takeover() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("python_disabled");
    let root = fixture.path().to_path_buf();
    let backups_root = root.join(".backups");

    let (secret, recipient) = ephemeral_identity();
    let app_file_bytes = br#"{"legacy":true}"#;
    let app_rel = "payload/projects/p-legacy/data.json";
    let manifest_bytes = serde_json::to_vec(&json!({
        "schemaVersion": "deepseek-workspace-backup.v1",
        "purpose": "restorable-backup",
        "snapshotKind": "full",
        "contributors": [{"id": "projects"}],
        "files": [{
            "path": app_rel,
            "size": app_file_bytes.len(),
            "sha256": sha256_hex(app_file_bytes),
        }],
    }))
    .unwrap();
    let manifest_digest = sha256_hex(&manifest_bytes);
    let checksum_text = format!(
        "{}  manifest.json\n{}  {app_rel}\n",
        manifest_digest,
        sha256_hex(app_file_bytes),
    );
    let archive_zip = make_zip_bytes(&[
        ("manifest.json", &manifest_bytes),
        ("checksums.sha256", checksum_text.as_bytes()),
        (app_rel, app_file_bytes),
    ]);
    let archive_age = encrypt_bytes_to_recipients(&archive_zip, &[recipient]).unwrap();
    let cipher_sha = sha256_hex(&archive_age);
    let cipher_size = archive_age.len() as u64;

    let backup_files_dir = backups_root.join("backups");
    std::fs::create_dir_all(&backup_files_dir).unwrap();
    std::fs::write(
        backup_files_dir.join("bak-legacy.dsibackup.age"),
        &archive_age,
    )
    .unwrap();

    let seed_script = r#"
import json, sys
from deepseek_infra.infra.workspace import backups, backup_catalog
params = json.load(sys.stdin)
backup_catalog.append_receipt(
    backups.BACKUP_DIR,
    {
        "backupId": "bak-legacy",
        "filename": "bak-legacy.dsibackup.age",
        "policyId": "pol-b",
        "createdAt": "2026-10-10T00:00:00Z",
        "size": params["size"],
        "sizeBytes": params["size"],
        "ciphertextSha256": params["sha"],
        "manifestDigest": params["manifestDigest"],
    },
)
print(json.dumps({"ok": True}))
"#;
    run_python(
        &root,
        seed_script,
        &json!({
            "size": cipher_size,
            "sha": cipher_sha,
            "manifestDigest": manifest_digest,
        }),
    );

    // Seed an expired writer lease to verify native route recovery takeover.
    let lock_dir = backups_root.join(".target-lock");
    std::fs::create_dir_all(&lock_dir).unwrap();
    std::fs::write(
        lock_dir.join("writer.json"),
        serde_json::to_vec(&json!({
            "schemaVersion": 1,
            "targetId": "managed-local",
            "ownerRunId": "crashed-run",
            "ownerInstanceId": "crashed-worker",
            "fencingToken": 42,
            "acquiredAt": "2020-01-01T00:00:00Z",
            "expiresAt": "2020-01-01T00:05:00Z",
        }))
        .unwrap(),
    )
    .unwrap();

    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-legacy/scrub",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["objectsScrubbed"], 1);
    assert!(!lock_dir.join("writer.json").exists());

    let (status, body, _) = fixture
        .request_json(
            "POST",
            "/api/workspace/backups/bak-legacy/verify-unlock",
            Some(&json!({"identity": secret}).to_string()),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["manifestDigest"], manifest_digest);
    assert!(body["sealedFrontend"].is_null());

    // Python verifies the entire appended chain + unlock verification timestamp.
    let check_script = r#"
import json
from deepseek_infra.infra.workspace import backups, backup_catalog
root = backups.BACKUP_DIR
assert backup_catalog.verify_chain(root) is True
state = backup_catalog.catalog_state(root)
print(json.dumps({
    "scrubOk": state["bak-legacy"]["scrubOk"],
    "unlocked": bool(state["bak-legacy"]["userUnlockVerifiedAt"]),
}))
"#;
    let verified = run_python(&root, check_script, &json!({}));
    assert_eq!(verified["scrubOk"], true);
    assert_eq!(verified["unlocked"], true);
}
