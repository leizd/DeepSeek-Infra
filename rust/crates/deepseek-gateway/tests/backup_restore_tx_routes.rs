//! Integration tests for S5 restore transaction and session-control routes (`restore_staging_store`):
//! - `POST /api/workspace/restores/{restore_id}/prepare`
//! - `PUT  /api/workspace/restores/{restore_id}/frontend-prepared`
//! - `POST /api/workspace/restores/{restore_id}/commit`
//! - `POST /api/workspace/restores/{restore_id}/complete`
//! - `POST /api/workspace/restores/{restore_id}/abort`
//! - `POST /api/workspace/restores/{restore_id}/pause`
//! - `POST /api/workspace/restores/{restore_id}/resume`
//! - `POST /api/workspace/restores/{restore_id}/apply`

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::python_json::dumps_compact;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s5-tx-test-token";
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

async fn call_raw(
    app: &axum::Router,
    method: &str,
    uri: &str,
    content_type: Option<&str>,
    raw_body: Option<Vec<u8>>,
) -> (StatusCode, Vec<u8>) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let body = if let Some(encoded) = raw_body {
        if let Some(ct) = content_type {
            builder = builder.header(header::CONTENT_TYPE, ct);
        }
        builder = builder.header(header::CONTENT_LENGTH, encoded.len().to_string());
        Body::from(encoded)
    } else {
        Body::empty()
    };
    let req = builder.body(body).unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    (status, bytes.to_vec())
}

async fn call_json(
    app: &axum::Router,
    method: &str,
    uri: &str,
    payload: Option<&Value>,
) -> (StatusCode, Value) {
    let encoded = payload.map(|val| serde_json::to_vec(val).unwrap());
    let (status, bytes) = call_raw(
        app,
        method,
        uri,
        payload.map(|_| "application/json"),
        encoded,
    )
    .await;
    let val = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, val)
}

fn sha256_hex_compact(value: &Value) -> String {
    let encoded = dumps_compact(value);
    let digest = Sha256::digest(encoded.as_bytes());
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn sha256_hex_bytes(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

async fn create_and_inspect_backup(
    app: &axum::Router,
    requires_frontend: bool,
) -> (String, String, Option<String>) {
    let (st, created) = call_json(
        app,
        "POST",
        "/api/workspace/backups",
        Some(&json!({
            "mode": "full",
            "requiresFrontendState": requires_frontend,
            "coveragePolicy": "best-effort",
        })),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "create backup failed: {created}");
    let backup_id = created["backupId"].as_str().unwrap().to_string();

    let mut fe_digest = None;
    if requires_frontend {
        let fe_body = json!({
            "schemaVersion": 1,
            "exportedAt": "2026-11-18T00:00:00Z",
            "conversations": [{"id": "conv-1", "title": "Hello"}],
            "conflicts": []
        });
        let digest = sha256_hex_compact(&fe_body);
        let mut with_digest = fe_body.as_object().unwrap().clone();
        with_digest.insert("digest".into(), Value::String(digest.clone()));
        let (fe_st, fe_val) = call_json(
            app,
            "PUT",
            &format!("/api/workspace/backups/{backup_id}/frontend-state"),
            Some(&Value::Object(with_digest)),
        )
        .await;
        assert_eq!(fe_st, StatusCode::OK, "frontend-state failed: {fe_val}");
        fe_digest = Some(digest);
    }

    let (fin_st, fin_val) = call_json(
        app,
        "POST",
        &format!("/api/workspace/backups/{backup_id}/finalize"),
        Some(&json!({})),
    )
    .await;
    assert_eq!(fin_st, StatusCode::OK, "finalize failed: {fin_val}");

    let (dl_st, dl_bytes) = call_raw(
        app,
        "GET",
        &format!("/api/workspace/backups/{backup_id}/download"),
        None,
        None,
    )
    .await;
    assert_eq!(dl_st, StatusCode::OK);

    let (insp_st, insp_raw) = call_raw(
        app,
        "POST",
        "/api/workspace/restores/inspect",
        Some("application/vnd.deepseek-infra.backup+zip"),
        Some(dl_bytes),
    )
    .await;
    let insp_val: Value = serde_json::from_slice(&insp_raw).unwrap();
    assert_eq!(insp_st, StatusCode::OK, "inspect failed: {insp_val}");
    let restore_id = insp_val["restoreId"].as_str().unwrap().to_string();
    (backup_id, restore_id, fe_digest)
}

#[tokio::test]
async fn default_mode_refuses_all_s5_restore_tx_routes_without_touching_disk() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let restore_dir = root.path().join(".restore-staging").join("restore_tx1");
    std::fs::create_dir_all(&restore_dir).unwrap();
    std::fs::write(
        restore_dir.join("plan.json"),
        r#"{"restoreId":"restore_tx1","compatible":true}"#,
    )
    .unwrap();
    let remote_before = r#"{"restoreId":"restore_tx1","phase":"fetching-components"}"#;
    std::fs::write(restore_dir.join("remote-fetch.json"), remote_before).unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_authoritative");

    for (method, uri, payload) in [
        (
            "POST",
            "/api/workspace/restores/restore_tx1/prepare",
            Some(json!({"mode": "merge"})),
        ),
        (
            "PUT",
            "/api/workspace/restores/restore_tx1/frontend-prepared",
            Some(json!({"digest": "a".repeat(64)})),
        ),
        (
            "POST",
            "/api/workspace/restores/restore_tx1/commit",
            Some(json!({})),
        ),
        (
            "POST",
            "/api/workspace/restores/restore_tx1/complete",
            Some(json!({})),
        ),
        ("POST", "/api/workspace/restores/restore_tx1/abort", None),
        ("POST", "/api/workspace/restores/restore_tx1/pause", None),
        ("POST", "/api/workspace/restores/restore_tx1/resume", None),
        (
            "POST",
            "/api/workspace/restores/restore_tx1/apply",
            Some(json!({"mode": "merge"})),
        ),
    ] {
        let (st, val) = call_json(&app, method, uri, payload.as_ref()).await;
        assert_eq!(st, StatusCode::CONFLICT, "{method} {uri} -> {val}");
        assert_eq!(
            val["code"], "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED",
            "{method} {uri} -> {val}"
        );
    }

    let (get_st, _) = call_raw(
        &app,
        "GET",
        "/api/workspace/restores/restore_tx1/prepare",
        None,
        None,
    )
    .await;
    assert_eq!(get_st, StatusCode::NOT_FOUND);
    let (del_st, _) = call_raw(
        &app,
        "DELETE",
        "/api/workspace/restores/restore_tx1/prepare",
        None,
        None,
    )
    .await;
    assert_eq!(del_st, StatusCode::METHOD_NOT_ALLOWED);

    assert!(!restore_dir.join("transaction.json").exists());
    assert_eq!(
        std::fs::read_to_string(restore_dir.join("remote-fetch.json")).unwrap(),
        remote_before
    );
}

#[tokio::test]
async fn python_disabled_executes_coordinated_frontend_restore_with_collision_remap() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let p1 = root.path().join(".projects").join("proj1");
    std::fs::create_dir_all(&p1).unwrap();
    std::fs::write(
        p1.join("project.json"),
        r#"{"id":"proj1","name":"Original V1","sourcePath":"proj1/notes.txt"}"#,
    )
    .unwrap();
    std::fs::write(p1.join("notes.txt"), "hello-v1").unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");
    let (_backup_id, restore_id, fe_digest) = create_and_inspect_backup(&app, true).await;
    let fe_digest = fe_digest.unwrap();

    // Diverge live project so merge collides and deterministically remaps incoming copy.
    std::fs::write(
        p1.join("project.json"),
        r#"{"id":"proj1","name":"Modified V2","sourcePath":"proj1/notes.txt"}"#,
    )
    .unwrap();

    let (prep_st, prep_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_id}/prepare"),
        Some(&json!({
            "mode": "merge",
            "previousEpoch": "epoch-old",
            "targetEpoch": "epoch-new",
            "ownerDocumentId": "tab-1",
        })),
    )
    .await;
    assert_eq!(prep_st, StatusCode::OK, "prepare failed: {prep_val}");
    assert_eq!(prep_val["phase"], "backend-staged");
    assert!(prep_val["safetyBackupId"].as_str().is_some());
    assert!(prep_val["frontend"].is_object());
    let identities = prep_val["restoredIdentities"].as_array().unwrap();
    let remapped_p1 = identities
        .iter()
        .find(|item| item["originalId"] == "proj1")
        .expect("collision identity mapping missing")["restoredId"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(remapped_p1.starts_with("proj1-imported-"));

    // Reject bad digest, accept valid frontend-prepared, enforce conflict on mismatch.
    let (bad_fe_st, _) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/restores/{restore_id}/frontend-prepared"),
        Some(&json!({"digest": "not-hex"})),
    )
    .await;
    assert_eq!(bad_fe_st, StatusCode::BAD_REQUEST);

    let (fe_prep_st, fe_prep_val) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/restores/{restore_id}/frontend-prepared"),
        Some(&json!({"digest": fe_digest})),
    )
    .await;
    assert_eq!(
        fe_prep_st,
        StatusCode::OK,
        "frontend-prepared failed: {fe_prep_val}"
    );
    assert_eq!(fe_prep_val["phase"], "frontend-staged");

    let (mismatch_st, _) = call_json(
        &app,
        "PUT",
        &format!("/api/workspace/restores/{restore_id}/frontend-prepared"),
        Some(&json!({"digest": "b".repeat(64)})),
    )
    .await;
    assert_eq!(mismatch_st, StatusCode::CONFLICT);

    // First commit call with frontendCommitted=false pauses at commit-intent.
    let (intent_st, intent_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_id}/commit"),
        Some(&json!({"frontendCommitted": false, "frontendDigest": fe_digest})),
    )
    .await;
    assert_eq!(
        intent_st,
        StatusCode::OK,
        "commit-intent failed: {intent_val}"
    );
    assert_eq!(intent_val["phase"], "commit-intent");
    assert!(!root.path().join(".projects").join(&remapped_p1).exists());

    // Second commit call with frontendCommitted=true swaps backend dirs.
    let (com_st, com_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_id}/commit"),
        Some(&json!({"frontendCommitted": true, "frontendDigest": fe_digest})),
    )
    .await;
    assert_eq!(com_st, StatusCode::OK, "commit failed: {com_val}");
    assert_eq!(com_val["phase"], "backend-committed");
    assert!(
        com_val["applied"]
            .as_array()
            .unwrap()
            .contains(&json!("projects"))
    );

    let live_raw: Value =
        serde_json::from_str(&std::fs::read_to_string(p1.join("project.json")).unwrap()).unwrap();
    assert_eq!(live_raw["name"], "Modified V2");
    let imported_dir = root.path().join(".projects").join(&remapped_p1);
    assert!(imported_dir.is_dir());
    let imported_json: Value =
        serde_json::from_str(&std::fs::read_to_string(imported_dir.join("project.json")).unwrap())
            .unwrap();
    assert_eq!(imported_json["id"], remapped_p1);
    assert_eq!(
        imported_json["sourcePath"],
        format!("{remapped_p1}/notes.txt")
    );
    assert_eq!(imported_json["importedFrom"]["originalId"], "proj1");

    // Complete clears fence and cleans staged directory.
    let (comp_st, comp_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_id}/complete"),
        Some(&json!({"frontendDigest": fe_digest})),
    )
    .await;
    assert_eq!(comp_st, StatusCode::OK, "complete failed: {comp_val}");
    assert_eq!(comp_val["phase"], "complete");
    assert!(!root.path().join(".workspace-mutation.fence").exists());
    assert!(
        !root
            .path()
            .join(".restore-staging")
            .join(&restore_id)
            .join("staged")
            .exists()
    );
}

#[tokio::test]
async fn python_disabled_supports_single_call_apply_and_abort_rollback() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let mem = root.path().join(".memory");
    std::fs::create_dir_all(&mem).unwrap();
    std::fs::write(mem.join("notes.json"), r#"{"entries":["from-backup"]}"#).unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");
    let (_b1, restore_abort_id, _fe) = create_and_inspect_backup(&app, false).await;
    let (_b2, restore_apply_id, _fe2) = create_and_inspect_backup(&app, false).await;
    let (_b3, restore_fe_req_id, _fe3) = create_and_inspect_backup(&app, true).await;

    // apply rejects when frontend ack is required.
    let (fe_apply_st, _) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_fe_req_id}/apply"),
        Some(&json!({"mode": "merge"})),
    )
    .await;
    assert_eq!(fe_apply_st, StatusCode::CONFLICT);

    // Prepare + abort rolls back cleanly and clears fence.
    let (prep_st, _) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_abort_id}/prepare"),
        Some(&json!({"mode": "merge"})),
    )
    .await;
    assert_eq!(prep_st, StatusCode::OK);
    let (ab_st, ab_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_abort_id}/abort"),
        None,
    )
    .await;
    assert_eq!(ab_st, StatusCode::OK, "abort failed: {ab_val}");
    assert_eq!(ab_val["phase"], "rolled-back");
    assert!(!root.path().join(".workspace-mutation.fence").exists());

    // Clear .memory and run replace-empty via single-call apply.
    std::fs::remove_dir_all(&mem).unwrap();
    let (ap_st, ap_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{restore_apply_id}/apply"),
        Some(&json!({"mode": "replace-empty"})),
    )
    .await;
    assert_eq!(ap_st, StatusCode::OK, "apply failed: {ap_val}");
    assert_eq!(ap_val["phase"], "complete");
    assert!(ap_val["restoreEpoch"].as_i64().is_some());
    assert!(mem.join("notes.json").is_file());
}

#[tokio::test]
async fn python_disabled_handles_remote_restore_pause_resume_and_abort_controls() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");

    // Nonexistent session returns 404.
    let (miss_p, _) = call_json(
        &app,
        "POST",
        "/api/workspace/restores/missing-remote/pause",
        None,
    )
    .await;
    assert_eq!(miss_p, StatusCode::NOT_FOUND);
    let (miss_r, _) = call_json(
        &app,
        "POST",
        "/api/workspace/restores/missing-remote/resume",
        None,
    )
    .await;
    assert_eq!(miss_r, StatusCode::NOT_FOUND);

    let rid = "remote-ctrl-1";
    let rdir = root.path().join(".restore-staging").join(rid);
    std::fs::create_dir_all(&rdir).unwrap();
    std::fs::write(
        rdir.join("remote-fetch.json"),
        serde_json::to_vec(&json!({
            "restoreId": rid,
            "phase": "fetching-components",
            "targetId": "managed-local",
            "holdKeys": ["holds/h1.json"],
            "schemaVersion": 4,
        }))
        .unwrap(),
    )
    .unwrap();

    // `release_session_holds` resolves a `holdKeys` entry against the target
    // store root, which is `.backups` for `managed-local`. The file therefore
    // sits at `.backups/holds/h1.json`; an extra `publish/` segment put it
    // outside the tree the release walks, so the test asserted on a path the
    // implementation is not supposed to touch.
    let hold_path = root.path().join(".backups").join("holds").join("h1.json");
    std::fs::create_dir_all(hold_path.parent().unwrap()).unwrap();
    std::fs::write(&hold_path, "{}").unwrap();
    let pin_hash = sha256_hex_bytes(rid.as_bytes());
    let pin_path = root
        .path()
        .join(".backup-component-cache")
        .join("pins")
        .join(format!("{pin_hash}.json"));
    std::fs::create_dir_all(pin_path.parent().unwrap()).unwrap();
    std::fs::write(&pin_path, r#"{"digests":[],"schemaVersion":1}"#).unwrap();

    let (p1_st, p1_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/pause"),
        None,
    )
    .await;
    assert_eq!(p1_st, StatusCode::OK, "pause failed: {p1_val}");
    assert_eq!(p1_val["phase"], "paused");
    assert_eq!(p1_val["pausedFromPhase"], "fetching-components");

    let (p2_st, p2_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/pause"),
        None,
    )
    .await;
    assert_eq!(p2_st, StatusCode::OK);
    assert_eq!(p2_val["phase"], "paused");

    let (r1_st, r1_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/resume"),
        None,
    )
    .await;
    assert_eq!(r1_st, StatusCode::OK, "resume failed: {r1_val}");
    assert_eq!(r1_val["phase"], "fetching-components");

    let (r2_st, r2_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/resume"),
        None,
    )
    .await;
    assert_eq!(r2_st, StatusCode::OK);
    assert_eq!(r2_val["phase"], "fetching-components");

    let (ab_st, ab_val) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/abort"),
        None,
    )
    .await;
    assert_eq!(ab_st, StatusCode::OK, "remote abort failed: {ab_val}");
    assert_eq!(ab_val["phase"], "aborted");
    assert!(!hold_path.exists());
    assert!(!pin_path.exists());

    // Terminal session rejects subsequent pause or abort with 409.
    let (term_p, _) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/pause"),
        None,
    )
    .await;
    assert_eq!(term_p, StatusCode::CONFLICT);
    let (term_a, _) = call_json(
        &app,
        "POST",
        &format!("/api/workspace/restores/{rid}/abort"),
        None,
    )
    .await;
    assert_eq!(term_a, StatusCode::CONFLICT);
}
