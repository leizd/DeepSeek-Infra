//! Integration tests for S5 read-only backup & restore routes:
//! - `GET|HEAD /api/workspace/backups/{backup_id}`
//! - `GET|HEAD /api/workspace/backups/{backup_id}/download`
//! - `GET|HEAD /api/workspace/restores`
//! - `GET|HEAD /api/workspace/restores/{restore_id}`

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s5-read-test-token";
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

fn build_app(root: &std::path::Path) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let env = EnvGuard::set(&[
        ("DEEPSEEK_INFRA_ROOT", root.to_string_lossy().into_owned()),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

async fn call_raw(
    app: &axum::Router,
    method: &str,
    uri: &str,
) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, headers, bytes)
}

fn json_body(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).unwrap()
}

#[tokio::test]
async fn backup_session_and_download_match_oracle_contracts() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let backups = root.path().join(".backups");
    std::fs::create_dir_all(backups.join("sessions").join("backup_ready1")).unwrap();
    std::fs::create_dir_all(backups.join("sessions").join("backup_pending1")).unwrap();
    std::fs::create_dir_all(backups.join("sessions").join("backup_escape1")).unwrap();

    let zip_path = backups.join("workspace.dsibackup");
    std::fs::write(&zip_path, b"PK\x03\x04zip-bytes").unwrap();
    let age_path = backups.join("workspace.dsibackup.age");
    std::fs::write(&age_path, b"age-bytes").unwrap();

    let escaped = root.path().join("outside.dsibackup");
    std::fs::write(&escaped, b"secret").unwrap();

    std::fs::write(
        backups
            .join("sessions")
            .join("backup_ready1")
            .join("session.json"),
        serde_json::to_vec(&json!({
            "backupId": "backup_ready1",
            "phase": "ready",
            "path": zip_path.to_string_lossy(),
            "context": {"secret": "omit"},
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        backups
            .join("sessions")
            .join("backup_pending1")
            .join("session.json"),
        serde_json::to_vec(&json!({
            "backupId": "backup_pending1",
            "phase": "created",
            "path": age_path.to_string_lossy(),
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        backups
            .join("sessions")
            .join("backup_escape1")
            .join("session.json"),
        serde_json::to_vec(&json!({
            "backupId": "backup_escape1",
            "phase": "ready",
            "path": escaped.to_string_lossy(),
        }))
        .unwrap(),
    )
    .unwrap();

    let (_static_root, _env, app) = build_app(root.path());

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/backups/backup_ready1").await;
    assert_eq!(status, StatusCode::OK);
    let body = json_body(&bytes);
    assert_eq!(
        body,
        json!({"ok": true, "backupId": "backup_ready1", "phase": "ready"})
    );

    let (status, _, bytes) = call_raw(&app, "HEAD", "/api/workspace/backups/backup_ready1").await;
    assert_eq!(status, StatusCode::OK);
    assert!(bytes.is_empty());

    let (status, headers, bytes) =
        call_raw(&app, "GET", "/api/workspace/backups/backup_ready1/download").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bytes, b"PK\x03\x04zip-bytes");
    assert_eq!(
        headers
            .get(header::CACHE_CONTROL)
            .unwrap()
            .to_str()
            .unwrap(),
        "no-store"
    );
    assert_eq!(
        headers.get(header::CONTENT_TYPE).unwrap().to_str().unwrap(),
        "application/vnd.deepseek-infra.backup+zip"
    );
    assert!(
        headers
            .get(header::CONTENT_DISPOSITION)
            .unwrap()
            .to_str()
            .unwrap()
            .contains("workspace.dsibackup")
    );

    let (status, _, bytes) = call_raw(
        &app,
        "GET",
        "/api/workspace/backups/backup_pending1/download",
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Backup is not ready", "code": "invalid_request"})
    );

    let (status, _, bytes) = call_raw(
        &app,
        "GET",
        "/api/workspace/backups/backup_escape1/download",
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Backup file not found", "code": "not_found"})
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/backups/bad-id").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Invalid backup id", "code": "invalid_payload"})
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/backups/backup_missing").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Backup session not found", "code": "not_found"})
    );

    let (status, _, _) = call_raw(&app, "POST", "/api/workspace/backups/backup_ready1").await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn restore_list_and_detail_match_oracle_contracts() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let staging = root.path().join(".restore-staging");

    let r1 = staging.join("restore_aaa111");
    std::fs::create_dir_all(r1.join("verified").join("frontend")).unwrap();
    std::fs::write(
        r1.join("verified").join("frontend").join("state.json"),
        br#"{"tab":"chat"}"#,
    )
    .unwrap();
    std::fs::write(
        r1.join("plan.json"),
        serde_json::to_vec(&json!({
            "restoreId": "restore_aaa111",
            "encrypted": true,
            "secretArmedAt": 1.0,
            "manifest": {"backupId": "backup_src1"},
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        r1.join("transaction.json"),
        serde_json::to_vec(&json!({
            "phase": "complete",
            "contributors": [{"id": "memory", "swapped": true}, {"id": "rag", "swapped": false}],
            "identityMap": {"old1": "new1"},
            "operations": [{"op": "swap"}],
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(r1.join("remote-fetch.json"), br#"{"phase":"done"}"#).unwrap();

    let r2 = staging.join("restore_bbb222");
    std::fs::create_dir_all(&r2).unwrap();
    std::fs::write(
        r2.join("remote-fetch.json"),
        serde_json::to_vec(&json!({
            "restoreId": "restore_bbb222",
            "phase": "fetching",
            "encrypted": false,
        }))
        .unwrap(),
    )
    .unwrap();

    let r3 = staging.join("restore_ccc333");
    std::fs::create_dir_all(&r3).unwrap();
    std::fs::write(r3.join("plan.json"), b"{bad json").unwrap();

    let (_static_root, _env, app) = build_app(root.path());

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/restores").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        json_body(&bytes),
        json!({
            "ok": true,
            "restores": [
                {
                    "ok": true,
                    "phase": "complete",
                    "restoreId": "restore_aaa111",
                    "secretState": "not-required",
                    "applied": ["memory"],
                    "restoredIdentities": [
                        {
                            "originalId": "old1",
                            "restoredId": "new1",
                            "reason": "collision",
                            "sourceBackupId": "backup_src1",
                        }
                    ],
                },
                {
                    "ok": true,
                    "restoreId": "restore_bbb222",
                    "phase": "fetching",
                    "encrypted": false,
                    "secretState": "not-required",
                    "applied": [],
                    "restoredIdentities": [],
                },
                {
                    "restoreId": "restore_ccc333",
                    "phase": "recovery-required",
                    "compatible": false,
                },
            ],
        })
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/restores/restore_aaa111").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        json_body(&bytes),
        json!({
            "ok": true,
            "phase": "complete",
            "restoreId": "restore_aaa111",
            "secretState": "not-required",
            "applied": ["memory"],
            "restoredIdentities": [
                {
                    "originalId": "old1",
                    "restoredId": "new1",
                    "reason": "collision",
                    "sourceBackupId": "backup_src1",
                }
            ],
            "frontend": {"tab": "chat"},
            "remoteRestorePhase": "done",
        })
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/restores/restore_bbb222").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        json_body(&bytes),
        json!({
            "ok": true,
            "restoreId": "restore_bbb222",
            "phase": "fetching",
            "encrypted": false,
            "secretState": "not-required",
        })
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/restores/restore_ccc333").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Invalid backup metadata: plan.json", "code": "invalid_payload"})
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/restores/bad-id").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Invalid restore id", "code": "invalid_payload"})
    );

    let (status, _, bytes) = call_raw(&app, "GET", "/api/workspace/restores/restore_missing").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(
        json_body(&bytes),
        json!({"error": "Restore plan not found", "code": "not_found"})
    );
}
