//! Integration tests for S5 archive routes (`restore_staging_store`):
//! - `POST /api/workspace/backups/{backup_id}/finalize`
//! - `POST /api/workspace/restores/inspect`
//! - `POST /api/workspace/restores/{restore_id}/unlock`

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_unlock::ephemeral_identity;
use deepseek_policy::mutation_gate;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s5-archive-test-token";
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

async fn call_raw(
    app: &axum::Router,
    method: &str,
    uri: &str,
    content_type: &str,
    bytes: Vec<u8>,
) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .header(header::CONTENT_TYPE, content_type)
        .header(header::CONTENT_LENGTH, bytes.len().to_string())
        .body(Body::from(bytes))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let resp_bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let val = serde_json::from_slice(&resp_bytes).unwrap();
    (status, val)
}

fn multipart_backup_body(boundary: &str, filename: &str, content: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"backup\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        )
        .as_bytes(),
    );
    out.extend_from_slice(content);
    out.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    out
}

#[tokio::test]
async fn default_mode_refuses_finalize_inspect_and_unlock_without_touching_disk() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), "python_authoritative");

    let (st_fin, val_fin) = call_json(
        &app,
        "POST",
        "/api/workspace/backups/backup_seed1/finalize",
        Some(&json!({})),
    )
    .await;
    assert_eq!(st_fin, StatusCode::CONFLICT);
    assert_eq!(
        val_fin["code"],
        "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED"
    );

    let (st_ins, val_ins) = call_raw(
        &app,
        "POST",
        "/api/workspace/restores/inspect",
        "application/vnd.deepseek-infra.backup+zip",
        b"zipdata".to_vec(),
    )
    .await;
    assert_eq!(st_ins, StatusCode::CONFLICT);
    assert_eq!(
        val_ins["code"],
        "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED"
    );

    let (st_unl, val_unl) = call_json(
        &app,
        "POST",
        "/api/workspace/restores/restore_seed1/unlock",
        None,
    )
    .await;
    assert_eq!(st_unl, StatusCode::CONFLICT);
    assert_eq!(
        val_unl["code"],
        "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED"
    );

    assert!(!root.path().join(".backups").exists());
    assert!(!root.path().join(".restore-staging").exists());
}

#[tokio::test]
async fn python_disabled_finalize_download_inspect_raw_and_multipart_lifecycle() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let proj_dir = root.path().join(".projects/p1");
    std::fs::create_dir_all(&proj_dir).unwrap();
    std::fs::write(
        proj_dir.join("project.json"),
        br#"{"id":"p1","name":"Demo"}"#,
    )
    .unwrap();
    std::fs::write(proj_dir.join(".env"), b"SECRET=never-exported").unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");

    // Wrong method -> GET hits /api/* 404 catch-all, while DELETE returns 405.
    let (st_get, _) = call_json(&app, "GET", "/api/workspace/restores/inspect", None).await;
    assert_eq!(st_get, StatusCode::NOT_FOUND);
    let (st_del, _) = call_json(&app, "DELETE", "/api/workspace/restores/inspect", None).await;
    assert_eq!(st_del, StatusCode::METHOD_NOT_ALLOWED);

    // Create + finalize plaintext backup.
    let (st, created) = call_json(
        &app,
        "POST",
        "/api/workspace/backups",
        Some(&json!({"mode": "full", "requiresFrontendState": false, "coveragePolicy": "best-effort"})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    let backup_id = created["backupId"].as_str().unwrap();

    let (st, finalized) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/backups/{backup_id}/finalize"),
        Some(&json!({})),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(finalized["phase"], "ready");
    assert!(
        finalized["filename"]
            .as_str()
            .unwrap()
            .ends_with(".dsibackup")
    );

    // Download archive bytes.
    let dl_req = Request::builder()
        .method("GET")
        .uri(format!("/api/workspace/backups/{backup_id}/download"))
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let dl_resp = app.clone().oneshot(dl_req).await.unwrap();
    assert_eq!(dl_resp.status(), StatusCode::OK);
    let archive_bytes = to_bytes(dl_resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();

    // Inspect raw binary body.
    let (st_ins, inspected) = call_raw(
        &app,
        "POST",
        "/api/workspace/restores/inspect",
        "application/vnd.deepseek-infra.backup+zip",
        archive_bytes.clone(),
    )
    .await;
    assert_eq!(st_ins, StatusCode::OK);
    assert_eq!(inspected["phase"], "inspected");
    assert_eq!(inspected["encrypted"], Value::Bool(false));
    assert_eq!(inspected["compatible"], Value::Bool(true));
    assert_eq!(inspected["purpose"], "restorable-backup");
    let restore_id = inspected["restoreId"].as_str().unwrap();

    // Idempotent unlock on plaintext archive.
    let (st_unl, unlocked) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_id}/unlock"),
        None,
    )
    .await;
    assert_eq!(st_unl, StatusCode::OK);
    assert_eq!(unlocked["phase"], "inspected");

    // Inspect multipart upload & invalid uploads.
    let boundary = "----DeepSeekBoundary123";
    let mp_body = multipart_backup_body(boundary, "upload.dsibackup", &archive_bytes);
    let (st_mp, mp_plan) = call_raw(
        &app,
        "POST",
        "/api/workspace/restores/inspect",
        &format!("multipart/form-data; boundary={boundary}"),
        mp_body,
    )
    .await;
    assert_eq!(st_mp, StatusCode::OK);
    assert_eq!(mp_plan["phase"], "inspected");

    let invalid_mp = multipart_backup_body(boundary, "bad.dsibackup", b"not-a-zip");
    let (st_bad, _) = call_raw(
        &app,
        "POST",
        "/api/workspace/restores/inspect",
        &format!("multipart/form-data; boundary={boundary}"),
        invalid_mp,
    )
    .await;
    assert_eq!(st_bad, StatusCode::BAD_REQUEST);

    let (st_empty, _) = call_raw(
        &app,
        "POST",
        "/api/workspace/restores/inspect",
        "application/vnd.deepseek-infra.backup+zip",
        Vec::new(),
    )
    .await;
    assert_eq!(st_empty, StatusCode::BAD_REQUEST);

    let uploads_dir = root.path().join(".restore-staging/.uploads");
    let leftovers: Vec<_> = std::fs::read_dir(&uploads_dir)
        .map(|entries| entries.flatten().collect())
        .unwrap_or_default();
    assert!(leftovers.is_empty(), "leftover .uploads files remain");
}

#[tokio::test]
async fn python_disabled_age_recipient_recovery_and_restore_fence() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let mem_dir = root.path().join(".memory");
    std::fs::create_dir_all(&mem_dir).unwrap();
    std::fs::write(mem_dir.join("memories.json"), b"{\"items\":[]}").unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");
    let (secret_key, recipient) = ephemeral_identity();
    let (wrong_key, _) = ephemeral_identity();

    let (st, created) = call_json(
        &app,
        "POST",
        "/api/workspace/backups",
        Some(&json!({
            "mode": "full",
            "requiresFrontendState": false,
            "coveragePolicy": "best-effort",
            "protection": {"mode": "age-recipient", "recipients": [recipient]},
        })),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    let backup_id = created["backupId"].as_str().unwrap();

    // Mismatched recovery identity rejects finalize.
    call_json(
        &app,
        "PUT",
        &format!("/api/workspace/backups/{backup_id}/secret"),
        Some(&json!({"kind": "age-identity", "secret": wrong_key})),
    )
    .await;
    let (st_mismatch, mismatch_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/backups/{backup_id}/finalize"),
        Some(&json!({})),
    )
    .await;
    assert_eq!(st_mismatch, StatusCode::BAD_REQUEST);
    assert!(
        mismatch_val["error"]
            .as_str()
            .unwrap()
            .contains("does not match")
    );

    // Matching recovery identity finalizes.
    call_json(
        &app,
        "PUT",
        &format!("/api/workspace/backups/{backup_id}/secret"),
        Some(&json!({"kind": "age-identity", "secret": secret_key})),
    )
    .await;
    let (st_fin, fin) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/backups/{backup_id}/finalize"),
        Some(&json!({})),
    )
    .await;
    assert_eq!(st_fin, StatusCode::OK);
    assert!(
        fin["filename"]
            .as_str()
            .unwrap()
            .ends_with(".dsibackup.age")
    );

    let archive_bytes = std::fs::read(
        root.path()
            .join(".backups")
            .join(fin["filename"].as_str().unwrap()),
    )
    .unwrap();
    let (st_ins, locked) = call_raw(
        &app,
        "POST",
        "/api/workspace/restores/inspect",
        "application/age",
        archive_bytes,
    )
    .await;
    assert_eq!(st_ins, StatusCode::OK);
    assert_eq!(locked["phase"], "locked");
    assert_eq!(locked["protection"], "age-recipient");
    let restore_id = locked["restoreId"].as_str().unwrap();

    call_json(
        &app,
        "PUT",
        &format!("/api/workspace/restores/{restore_id}/secret"),
        Some(&json!({"kind": "age-identity", "secret": secret_key})),
    )
    .await;
    let (st_unl, unlocked) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_id}/unlock"),
        None,
    )
    .await;
    assert_eq!(st_unl, StatusCode::OK);
    assert_eq!(unlocked["phase"], "inspected");
    assert_eq!(unlocked["encrypted"], Value::Bool(true));

    // Active restore fence blocks subsequent finalize with 423.
    mutation_gate::write_fence(
        &json!({"schemaVersion": 1, "restoreId": restore_id, "phase": "preparing"}),
        root.path(),
    )
    .unwrap();
    let (st_fenced, _) = call_json(
        &app,
        "POST",
        "/api/workspace/backups/backup_any/finalize",
        Some(&json!({})),
    )
    .await;
    assert_eq!(st_fenced, StatusCode::LOCKED);
}
