//! Integration tests for S5 write routes (`restore_staging_store`):
//! - `POST /api/workspace/backups`
//! - `PUT /api/workspace/backups/{backup_id}/secret`
//! - `PUT /api/workspace/backups/{backup_id}/frontend-state`
//! - `DELETE /api/workspace/backups/{backup_id}`
//! - `PUT /api/workspace/restores/{restore_id}/secret`
//! - `DELETE /api/workspace/restores/{restore_id}`
//! - `POST /api/workspace/restores/cleanup`

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::python_json::dumps_compact;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s5-write-test-token";
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
                unsafe { std::env::set_var(name, value) };
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

fn build_app(root: &std::path::Path, mode: &str) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let env = EnvGuard::set(&[
        ("DEEPSEEK_INFRA_ROOT", root.to_string_lossy().into_owned()),
        ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

async fn call_json(
    app: &axum::Router,
    method: &str,
    uri: &str,
    payload: Option<&Value>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let body = if let Some(val) = payload {
        let encoded = serde_json::to_vec(val).unwrap();
        builder = builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, encoded.len().to_string());
        Body::from(encoded)
    } else {
        Body::empty()
    };
    let req = builder.body(body).unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let val = serde_json::from_slice(&bytes).unwrap();
    (status, val)
}

fn frontend_digest(value: &Value) -> String {
    let encoded = dumps_compact(value);
    let digest = Sha256::digest(encoded.as_bytes());
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

#[tokio::test]
async fn default_mode_refuses_all_s5_writes_without_touching_disk() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), "python_authoritative");

    for (method, uri, body) in [
        (
            "POST",
            "/api/workspace/backups",
            Some(json!({"mode": "full", "coveragePolicy": "best-effort"})),
        ),
        (
            "PUT",
            "/api/workspace/backups/backup_seed1/secret",
            Some(json!({"kind": "passphrase", "secret": "hunter2"})),
        ),
        (
            "PUT",
            "/api/workspace/backups/backup_seed1/frontend-state",
            Some(json!({"schemaVersion": 1, "digest": "a"})),
        ),
        ("DELETE", "/api/workspace/backups/backup_seed1", None),
        (
            "PUT",
            "/api/workspace/restores/restore_seed1/secret",
            Some(json!({"kind": "passphrase", "secret": "hunter2"})),
        ),
        ("DELETE", "/api/workspace/restores/restore_seed1", None),
        ("POST", "/api/workspace/restores/cleanup", None),
    ] {
        let (status, val) = call_json(&app, method, uri, body.as_ref()).await;
        assert_eq!(status, StatusCode::CONFLICT, "{method} {uri}");
        assert_eq!(
            val["code"], "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED",
            "{method} {uri}"
        );
    }
    assert!(!root.path().join(".backups").exists());
    assert!(!root.path().join(".restore-staging").exists());
}

#[tokio::test]
async fn python_disabled_executes_backup_session_secret_frontend_and_restore_lifecycle() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let mem_dir = root.path().join(".memory");
    std::fs::create_dir_all(&mem_dir).unwrap();
    std::fs::write(mem_dir.join("memories.json"), b"{\"items\":[]}").unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");

    // 1. Empty body fails matching oracle.
    let (status, val) = call_json(&app, "POST", "/api/workspace/backups", None).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(val["code"], "invalid_payload");

    // 2. Create backup session.
    let (status, created) = call_json(
        &app,
        "POST",
        "/api/workspace/backups",
        Some(&json!({"mode": "full", "coveragePolicy": "best-effort"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(created["ok"], true);
    assert_eq!(created["phase"], "preparing");
    assert_eq!(created["estimatedBytes"], 12);
    let backup_id = created["backupId"].as_str().unwrap().to_string();
    assert!(backup_id.starts_with("backup_"));

    // 3. Arm secret and validate rejection on invalid secret.
    let (status, _) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/backups/{backup_id}/secret"),
        Some(&json!({"kind": "passphrase", "secret": ""})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, sec) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/backups/{backup_id}/secret"),
        Some(&json!({"kind": "passphrase", "secret": "hunter2"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sec["sessionId"], backup_id);
    assert_eq!(sec["kind"], "passphrase");
    assert_eq!(sec["expiresInSeconds"], 300);

    // 4. Frontend state checks forbidden keys and valid digest.
    let (status, sensitive) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/backups/{backup_id}/frontend-state"),
        Some(&json!({"schemaVersion": 1, "apiKey": "leak", "digest": "x"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(sensitive["code"], "sensitive_content");

    let fe_body = json!({"schemaVersion": 1, "exportedAt": "2026-11-17T00:00:00Z", "items": []});
    let digest = frontend_digest(&fe_body);
    let mut fe_env = fe_body.as_object().unwrap().clone();
    fe_env.insert("digest".into(), Value::String(digest.clone()));
    let (status, fe_resp) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/backups/{backup_id}/frontend-state"),
        Some(&Value::Object(fe_env)),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        fe_resp,
        json!({"ok": true, "backupId": backup_id, "digest": digest})
    );

    // 5. Delete backup clears session directory cleanly.
    let (status, del) = call_json(
        &app,
        "DELETE",
        &format!("/api/workspace/backups/{backup_id}"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(del, json!({"ok": true, "deleted": true}));
    assert!(
        !root
            .path()
            .join(".backups/sessions")
            .join(&backup_id)
            .exists()
    );

    // 6. Restore secret arming integrates with GET /api/workspace/restores/:restore_id (`available`)
    //    and active fence refuses DELETE, while cleanup deletes expired complete restores.
    let r_old = root.path().join(".restore-staging").join("restore_old1");
    std::fs::create_dir_all(&r_old).unwrap();
    std::fs::write(
        r_old.join("plan.json"),
        serde_json::to_vec(&json!({
            "restoreId": "restore_old1",
            "phase": "complete",
            "completedAt": "2020-01-01T00:00:00Z",
        }))
        .unwrap(),
    )
    .unwrap();

    let (status, cleanup) = call_json(&app, "POST", "/api/workspace/restores/cleanup", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(cleanup, json!({"ok": true, "deleted": ["restore_old1"]}));

    let r_cur = root.path().join(".restore-staging").join("restore_cur1");
    std::fs::create_dir_all(&r_cur).unwrap();
    std::fs::write(
        r_cur.join("plan.json"),
        serde_json::to_vec(&json!({
            "restoreId": "restore_cur1",
            "phase": "inspected",
            "encrypted": true,
        }))
        .unwrap(),
    )
    .unwrap();

    let (status, r_sec) = call_json(
        &app,
        "PUT",
        "/api/workspace/restores/restore_cur1/secret",
        Some(&json!({"kind": "passphrase", "secret": "hunter2"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(r_sec["sessionId"], "restore_cur1");

    let (status, fetched) =
        call_json(&app, "GET", "/api/workspace/restores/restore_cur1", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(fetched["secretState"], "available");

    std::fs::write(
        root.path().join(".workspace-restore-fence.json"),
        serde_json::to_vec(&json!({"restoreId": "restore_cur1"})).unwrap(),
    )
    .unwrap();
    let (status, fenced) =
        call_json(&app, "DELETE", "/api/workspace/restores/restore_cur1", None).await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(fenced["code"], "invalid_request");

    std::fs::remove_file(root.path().join(".workspace-restore-fence.json")).unwrap();
    let (status, del_r) =
        call_json(&app, "DELETE", "/api/workspace/restores/restore_cur1", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(del_r, json!({"ok": true, "deleted": true}));
    assert!(!r_cur.exists());
}
