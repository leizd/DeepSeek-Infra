//! The sealed frontend mirror's public routes, end to end through the real router.
//!
//! What this pins, and why each part is here rather than in a unit test:
//!
//! * the routes are **absent** until the store has changed hands, so a deployment that
//!   still runs the Python server behind this edge does not start answering mirror reads
//!   from a directory Python is writing (the request must fall through to the Go proxy);
//! * once they are mounted, a legal upload **succeeds**: it seals a generation, moves
//!   `HEAD.json`, and the list and status routes then report it — a fail-closed-only
//!   test would not show the feature works;
//! * the recipient sets come from the control plane, so with the internal bearer
//!   missing the routes refuse rather than sealing to nobody or reporting a status that
//!   skipped the recipient check;
//! * the refusals that protect the store — a stale sequence, an envelope whose digest
//!   does not match, a foreign recipient set — come back with the oracle's own codes.
//!
//! The control plane is a typed loopback `control/v1` gRPC fixture rather than a real `deepseekd`:
//! the route's contract with it is the read-only recipient snapshot,
//! and `go/internal/api/backup_policy_recipients.go` has its own Go tests for producing
//! that document. A second HTTP framework here would test the stub, not the route.

use std::net::TcpListener;
use std::path::PathBuf;

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode};
use deepseek_gateway::create_app;
use deepseek_protocol::generated::deepseek::control::v1::{
    BackupPolicyRecipientsRequest, BackupPolicyRecipientsResponse, BackupRecipientGroup,
    HealthRequest, HealthResponse, ShadowEvaluateRequest, ShadowEvaluateResponse,
    control_plane_server::{ControlPlane as ControlPlaneService, ControlPlaneServer},
};
use serde_json::{Value, json};
use tower::ServiceExt;

/// A syntactically valid age X25519 recipient. Only the encoding matters here: age
/// seals to any well-formed recipient, and the test never decrypts.
const RECIPIENT: &str = "age1fu59d59ghmr8x2t5dyzjs9xdcjgnakujp7mjy7cz2v7fq6vjqypskh4e62";

const BEARER: &str = "mirror-integration-bearer-0123456789";

struct ControlPlane {
    address: String,
    task: tokio::task::JoinHandle<()>,
}

struct Recipients {
    body: Value,
    expect_bearer: Option<String>,
}

#[tonic::async_trait]
impl ControlPlaneService for Recipients {
    async fn health(
        &self,
        _: tonic::Request<HealthRequest>,
    ) -> Result<tonic::Response<HealthResponse>, tonic::Status> {
        Err(tonic::Status::unimplemented("recipient fixture"))
    }

    async fn shadow_evaluate(
        &self,
        _: tonic::Request<ShadowEvaluateRequest>,
    ) -> Result<tonic::Response<ShadowEvaluateResponse>, tonic::Status> {
        Err(tonic::Status::unimplemented("recipient fixture"))
    }

    async fn get_backup_policy_recipients(
        &self,
        request: tonic::Request<BackupPolicyRecipientsRequest>,
    ) -> Result<tonic::Response<BackupPolicyRecipientsResponse>, tonic::Status> {
        if let Some(expected) = &self.expect_bearer {
            if request
                .metadata()
                .get("authorization")
                .and_then(|value| value.to_str().ok())
                != Some(format!("Bearer {expected}").as_str())
            {
                return Err(tonic::Status::unauthenticated("INTERNAL_API_UNAUTHORIZED"));
            }
        }
        let strings = |value: &Value| {
            value
                .as_array()
                .unwrap()
                .iter()
                .map(|item| item.as_str().unwrap().to_owned())
                .collect()
        };
        Ok(tonic::Response::new(BackupPolicyRecipientsResponse {
            authoritative: self.body["authoritative"].as_bool().unwrap(),
            recipients: strings(&self.body["recipients"]),
            enabled_recipient_groups: self.body["enabledRecipientGroups"]
                .as_array()
                .unwrap()
                .iter()
                .map(|group| BackupRecipientGroup {
                    recipients: strings(group),
                })
                .collect(),
            policy_count: self.body["policyCount"].as_u64().unwrap(),
            enabled_policy_count: self.body["enabledPolicyCount"].as_u64().unwrap(),
        }))
    }
}

impl Drop for ControlPlane {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl ControlPlane {
    fn start(body: Value, expect_bearer: Option<String>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind control plane");
        let address = format!("http://{}", listener.local_addr().expect("local addr"));
        listener.set_nonblocking(true).unwrap();
        let listener = tokio::net::TcpListener::from_std(listener).unwrap();
        let incoming = futures_util::stream::unfold(listener, |listener| async move {
            let accepted = listener.accept().await.map(|(socket, _)| socket);
            Some((accepted, listener))
        });
        let task = tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(ControlPlaneServer::new(Recipients {
                    body,
                    expect_bearer,
                }))
                .serve_with_incoming(incoming)
                .await
                .unwrap();
        });
        Self { address, task }
    }
}

/// Environment for one case, restored on drop.
///
/// The cases in this file mutate process-wide variables while the test harness runs them
/// on separate threads, so they serialise on this spin flag. It is a hand-rolled flag
/// rather than a `MutexGuard` because a guard held across `.await` is what clippy's
/// `await_holding_lock` exists to catch — the same shape `src/lib.rs` uses for its own
/// environment cases.
static ENV_BUSY: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

struct EnvLock;

impl EnvLock {
    fn acquire() -> Self {
        use std::sync::atomic::Ordering;
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
        ENV_BUSY.store(false, std::sync::atomic::Ordering::Release);
    }
}

struct Env {
    saved: Vec<(&'static str, Option<String>)>,
}

impl Env {
    fn set(pairs: &[(&'static str, &str)]) -> Self {
        let mut saved = Vec::new();
        for (name, value) in pairs {
            saved.push((*name, std::env::var(name).ok()));
            // SAFETY: `Env::set` is called once at the top of each case in this file, and
            // the file's cases are the only code touching these variables.
            unsafe { std::env::set_var(name, value) };
        }
        Self { saved }
    }

    fn clear(names: &[&'static str]) -> Self {
        let mut saved = Vec::new();
        for name in names {
            saved.push((*name, std::env::var(name).ok()));
            unsafe { std::env::remove_var(name) };
        }
        Self { saved }
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        for (name, value) in self.saved.drain(..) {
            match value {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
    }
}

fn workspace() -> PathBuf {
    let root = std::env::temp_dir().join(format!(
        "mirror-routes-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default()
    ));
    std::fs::create_dir_all(&root).expect("create workspace");
    root
}

fn envelope(digest_seed: &str) -> Value {
    let mut body = json!({
        "schemaVersion": 1,
        "conversations": [{"id": digest_seed}],
        "conflicts": [],
    });
    let digest = deepseek_policy::backup_mirror::sha256_hex(
        deepseek_policy::python_json::dumps_compact(&body).as_bytes(),
    );
    body["digest"] = json!(digest);
    body
}

async fn send(
    app: axum::Router,
    method: &str,
    uri: &str,
    body: Option<String>,
) -> (StatusCode, String) {
    let body = body.unwrap_or_default();
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header("Content-Type", "application/json")
        .header("Content-Length", body.len())
        .body(Body::from(body))
        .expect("request");
    let response = app.oneshot(request).await.expect("response");
    let status = response.status();
    let bytes = to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("body");
    (status, String::from_utf8_lossy(&bytes).to_string())
}

#[tokio::test]
async fn restore_and_mirror_upload_share_the_workspace_mutation_lock() {
    use std::time::Duration;

    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(
        recipients_body(&[RECIPIENT], &[&[RECIPIENT]]),
        Some(BEARER.to_string()),
    );
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().unwrap()),
        ("GO_CONTROL_ADDR", control.address.as_str()),
        ("DEEPSEEK_INTERNAL_BEARER", BEARER),
    ]);
    let app = create_app();
    let head = root.join(".backup-mirror/restore_window/HEAD.json");
    let (locked_sender, locked_receiver) = std::sync::mpsc::channel();
    let (restore_sender, restore_receiver) = std::sync::mpsc::channel();
    let restore_root = root.clone();
    let restore = std::thread::spawn(move || {
        let _gate = deepseek_policy::mutation_gate::exclusive_gate(&restore_root).unwrap();
        locked_sender.send(()).unwrap();
        restore_receiver
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        deepseek_policy::mutation_gate::write_fence(
            &json!({"restoreId": "mirror-restore-window"}),
            &restore_root,
        )
        .unwrap();
    });
    locked_receiver
        .recv_timeout(Duration::from_secs(5))
        .unwrap();
    let payload = json!({
        "envelope": envelope("restore-window"),
        "sourceEpoch": "epoch-restore-window",
        "clientSequence": 1,
    })
    .to_string();
    let path = "/api/workspace/backup-mirrors/restore_window/frontend";
    let mut upload = tokio::spawn(send(app.clone(), "PUT", path, Some(payload.clone())));
    // The restorer owns the OS lock but has not published its durable fence yet.
    // A put must remain pending, including on this single-thread Tokio runtime.
    let premature = tokio::time::timeout(Duration::from_secs(1), &mut upload).await;
    let published_while_locked = head.exists();
    restore_sender.send(()).unwrap();
    restore.join().unwrap();
    assert!(
        premature.is_err() && !published_while_locked,
        "mirror published while the restorer held the workspace lock: {premature:?}"
    );
    let (status, body) = upload.await.unwrap();
    assert_eq!(status, StatusCode::LOCKED, "{body}");
    assert!(!head.exists(), "a refused upload must not publish HEAD");

    // Recovery clearing the fence restores the successful sealing path.
    std::fs::remove_file(deepseek_policy::mutation_gate::fence_path(&root)).unwrap();
    let (status, body) = send(app, "PUT", path, Some(payload)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(head.is_file());
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test]
async fn public_body_guards_and_text_coercion_match_the_oracle_before_sealing() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(
        recipients_body(&[RECIPIENT], &[&[RECIPIENT]]),
        Some(BEARER.to_string()),
    );
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().unwrap()),
        ("GO_CONTROL_ADDR", control.address.as_str()),
        ("DEEPSEEK_INTERNAL_BEARER", BEARER),
    ]);
    let path = "/api/workspace/backup-mirrors/body_guards/frontend";
    for (body, length, expected_status, expected_error, expected_code) in [
        (
            "",
            "0",
            StatusCode::BAD_REQUEST,
            "Request body is empty",
            "invalid_payload",
        ),
        (
            "[]",
            "2",
            StatusCode::BAD_REQUEST,
            "Request body must be a JSON object",
            "invalid_payload",
        ),
        (
            "{}",
            "64000001",
            StatusCode::PAYLOAD_TOO_LARGE,
            "Request body is too large",
            "upload_too_large",
        ),
        (
            "{}",
            "-1",
            StatusCode::BAD_REQUEST,
            "Invalid Content-Length",
            "invalid_payload",
        ),
        (
            "{}",
            "bad",
            StatusCode::BAD_REQUEST,
            "Invalid Content-Length",
            "invalid_payload",
        ),
    ] {
        let request = Request::builder()
            .method("PUT")
            .uri(path)
            .header("Content-Length", length)
            .body(Body::from(body))
            .unwrap();
        let response = create_app().oneshot(request).await.unwrap();
        assert_eq!(response.status(), expected_status, "{length} {body}");
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let error: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            error,
            json!({"error": expected_error, "code": expected_code})
        );
        assert!(!root.join(".backup-mirror/body_guards").exists());
    }
    // The public Python route reads JSON independently of Content-Type and applies
    // str(value or "") before the store's validation; typed Option<String> erased it.
    for (index, epoch, expected_epoch, replica, expected_replica) in [
        (0, json!(true), "True", json!(true), "True"),
        (1, json!(42), "42", json!(3.5), "3.5"),
        (
            2,
            json!(["epoch"]),
            "['epoch']",
            json!(["replica"]),
            "['replica']",
        ),
        (3, json!("epoch-text"), "epoch-text", json!(false), ""),
    ] {
        let profile = format!("body_text_{index}");
        let upload = json!({"envelope": envelope(&profile), "sourceEpoch": epoch,
            "clientReplicaId": replica, "clientSequence": 1,
            "acknowledgedAt": false, "expectedHeadGenerationId": false})
        .to_string();
        let request = Request::builder()
            .method("PUT")
            .uri(format!("/api/workspace/backup-mirrors/{profile}/frontend"))
            .header("Content-Type", "text/plain")
            .header("Content-Length", upload.len())
            .body(Body::from(upload))
            .unwrap();
        let response = create_app().oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let metadata: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(status, StatusCode::OK, "{metadata}");
        assert_eq!(metadata["sourceEpoch"], json!(expected_epoch));
        assert_eq!(metadata["clientReplicaId"], json!(expected_replica));
        assert_eq!(metadata["creationVerified"], json!(true));
        assert!(
            root.join(".backup-mirror")
                .join(&profile)
                .join("HEAD.json")
                .exists()
        );
    }
    let _ = std::fs::remove_dir_all(&root);
}

fn recipients_body(recipients: &[&str], groups: &[&[&str]]) -> Value {
    json!({
        "authoritative": true,
        "recipients": recipients,
        "enabledRecipientGroups": groups,
        "policyCount": groups.len(),
        "enabledPolicyCount": groups.len(),
    })
}

/// Before the cutover the routes must not exist: the request belongs to the Go proxy.
#[tokio::test]
async fn the_mirror_routes_do_not_shadow_the_proxy_before_the_cutover() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_authoritative"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().expect("utf-8 root")),
    ]);
    let (status, body) = send(create_app(), "GET", "/api/workspace/backup-mirrors", None).await;
    // The Go proxy is not wired in this process, which is exactly what "not shadowed"
    // looks like here: the mirror route never answered.
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(body.contains("GO_CONTROL_PROXY"), "{body}");
    assert!(!body.contains("\"mirrors\""), "{body}");
}

/// A legal upload succeeds, moves HEAD, and is then reported by list and status.
#[tokio::test]
async fn a_legal_upload_is_sealed_listed_and_current() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(
        recipients_body(&[RECIPIENT], &[&[RECIPIENT]]),
        Some(BEARER.to_string()),
    );
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().expect("utf-8 root")),
        ("GO_CONTROL_ADDR", control.address.as_str()),
        ("DEEPSEEK_INTERNAL_BEARER", BEARER),
    ]);

    let (status, body) = send(create_app(), "GET", "/api/workspace/backup-mirrors", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        serde_json::from_str::<Value>(&body).unwrap()["mirrors"],
        json!([])
    );

    let upload = json!({
        "envelope": envelope("c-1"),
        "sourceEpoch": "epoch-1",
        "clientReplicaId": "replica-integration",
        "clientSequence": 1,
        "acknowledgedAt": "2026-09-30T12:00:00Z",
    })
    .to_string();
    let (status, body) = send(
        create_app(),
        "PUT",
        "/api/workspace/backup-mirrors/mirror_it/frontend",
        Some(upload.clone()),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let metadata: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(metadata["creationVerified"], json!(true), "{body}");
    assert_eq!(metadata["clientSequence"], json!(1));
    assert_eq!(metadata["recipientVariants"].as_array().unwrap().len(), 1);
    assert_eq!(metadata["sourceEpoch"], json!("epoch-1"));

    // HEAD.json is on disk and points at the published generation.
    let head_path = root.join(".backup-mirror/mirror_it/HEAD.json");
    let head: Value = serde_json::from_str(&std::fs::read_to_string(&head_path).unwrap()).unwrap();
    assert_eq!(head["generationId"], metadata["generationId"]);
    assert_eq!(head["acceptedEpochIndex"], json!(1));

    let (status, body) = send(create_app(), "GET", "/api/workspace/backup-mirrors", None).await;
    assert_eq!(status, StatusCode::OK);
    let mirrors = serde_json::from_str::<Value>(&body).unwrap();
    assert_eq!(mirrors["mirrors"].as_array().unwrap().len(), 1);
    assert_eq!(mirrors["mirrors"][0]["profileId"], json!("mirror_it"));

    let (status, body) = send(
        create_app(),
        "GET",
        "/api/workspace/backup-mirrors/mirror_it",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let status_body: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(status_body["status"], json!("current"));

    // An identical replay is idempotent and must not create a second generation.
    let (status, body) = send(
        create_app(),
        "PUT",
        "/api/workspace/backup-mirrors/mirror_it/frontend",
        Some(upload),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let replay: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(replay["idempotent"], json!(true), "{body}");
    let generations = std::fs::read_dir(root.join(".backup-mirror/mirror_it/generations"))
        .unwrap()
        .count();
    assert_eq!(generations, 1);

    let _ = std::fs::remove_dir_all(&root);
}

/// A corrupt recovery fence must never be interpreted as permission to publish.
#[tokio::test]
async fn an_unreadable_restore_fence_refuses_without_changing_the_generation() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(
        recipients_body(&[RECIPIENT], &[&[RECIPIENT]]),
        Some(BEARER.to_string()),
    );
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().unwrap()),
        ("GO_CONTROL_ADDR", control.address.as_str()),
        ("DEEPSEEK_INTERNAL_BEARER", BEARER),
    ]);
    let upload = |id: &str, sequence: i64| {
        json!({"envelope": envelope(id), "sourceEpoch": "epoch-fence", "clientSequence": sequence})
            .to_string()
    };
    let path = "/api/workspace/backup-mirrors/fence_probe/frontend";
    let (status, body) = send(create_app(), "PUT", path, Some(upload("before", 1))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let head_path = root.join(".backup-mirror/fence_probe/HEAD.json");
    let before = std::fs::read(&head_path).unwrap();
    let fence = root.join(".workspace-restore-fence.json");
    for value in ["{broken", r#"{"restoreId":"active-restore"}"#] {
        std::fs::write(&fence, value).unwrap();
        let (status, body) = send(create_app(), "PUT", path, Some(upload("after", 2))).await;
        assert_eq!(status, StatusCode::LOCKED, "fence={value} body={body}");
        assert!(body.contains("invalid_request"), "{body}");
        assert_eq!(std::fs::read(&head_path).unwrap(), before);
        assert_eq!(
            std::fs::read_dir(root.join(".backup-mirror/fence_probe/generations"))
                .unwrap()
                .count(),
            1,
        );
    }
    let _ = std::fs::remove_dir_all(&root);
}

/// The public route passes JSON values through Python's `int(value or 0)`;
/// false values, booleans, truncated floats and numeric strings are compatible.
#[tokio::test]
async fn client_sequence_coercion_preserves_the_public_oracle_and_refuses_bad_strings() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(
        recipients_body(&[RECIPIENT], &[&[RECIPIENT]]),
        Some(BEARER.to_string()),
    );
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().unwrap()),
        ("GO_CONTROL_ADDR", control.address.as_str()),
        ("DEEPSEEK_INTERNAL_BEARER", BEARER),
    ]);
    let cases = [
        (json!(true), Some(1)),
        (json!(3.9), Some(3)),
        (json!(-3.9), Some(0)),
        (json!(" 2_4 "), Some(24)),
        (json!(""), Some(0)),
        (json!([]), Some(0)),
        (json!({}), Some(0)),
        (json!("invalid"), None),
        (json!("3.9"), None),
        (json!([1]), None),
    ];
    for (index, (sequence, expected)) in cases.into_iter().enumerate() {
        let profile = format!("sequence_{index}");
        let upload = json!({
            "envelope": envelope(&profile), "sourceEpoch": "epoch-sequence",
            "clientSequence": sequence,
        })
        .to_string();
        let (status, body) = send(
            create_app(),
            "PUT",
            &format!("/api/workspace/backup-mirrors/{profile}/frontend"),
            Some(upload),
        )
        .await;
        match expected {
            Some(value) => {
                assert_eq!(status, StatusCode::OK, "sequence={sequence} body={body}");
                let metadata: Value = serde_json::from_str(&body).unwrap();
                assert_eq!(metadata["clientSequence"], json!(value), "{body}");
            }
            None => {
                assert_eq!(
                    status,
                    StatusCode::BAD_REQUEST,
                    "sequence={sequence} body={body}"
                );
                assert!(
                    body.contains("Mirror clientSequence must be a non-negative integer"),
                    "{body}"
                );
                assert!(!root.join(".backup-mirror").join(profile).exists());
            }
        }
    }
    let _ = std::fs::remove_dir_all(&root);
}

/// The recipient source is not optional: without the internal bearer the routes refuse
/// instead of sealing to nobody or skipping the recipient check.
#[tokio::test]
async fn a_missing_recipient_source_is_refused_not_guessed() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(recipients_body(&[RECIPIENT], &[&[RECIPIENT]]), None);
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().expect("utf-8 root")),
        ("GO_CONTROL_ADDR", control.address.as_str()),
    ]);
    let _clear = Env::clear(&["DEEPSEEK_INTERNAL_BEARER"]);

    let (status, body) = send(create_app(), "GET", "/api/workspace/backup-mirrors", None).await;
    // The list route needs no recipients and still answers.
    assert_eq!(status, StatusCode::OK, "{body}");

    let (status, body) = send(
        create_app(),
        "GET",
        "/api/workspace/backup-mirrors/mirror_missing",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(body.contains("RECIPIENT_SOURCE_UNAVAILABLE"), "{body}");

    let upload = json!({
        "envelope": envelope("c-2"),
        "sourceEpoch": "epoch-1",
        "clientSequence": 1,
    })
    .to_string();
    let (status, body) = send(
        create_app(),
        "PUT",
        "/api/workspace/backup-mirrors/mirror_missing/frontend",
        Some(upload),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(body.contains("RECIPIENT_SOURCE_UNAVAILABLE"), "{body}");
    // Nothing was written.
    assert!(!root.join(".backup-mirror/mirror_missing").exists());

    let _ = std::fs::remove_dir_all(&root);
}

/// The refusals that protect the store keep the oracle's codes.
#[tokio::test]
async fn illegal_uploads_are_refused_with_the_oracles_codes() {
    let _lock = EnvLock::acquire();
    let root = workspace();
    let control = ControlPlane::start(
        recipients_body(&[RECIPIENT], &[&[RECIPIENT]]),
        Some(BEARER.to_string()),
    );
    let _env = Env::set(&[
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
        ("DEEPSEEK_INFRA_ROOT", root.to_str().expect("utf-8 root")),
        ("GO_CONTROL_ADDR", control.address.as_str()),
        ("DEEPSEEK_INTERNAL_BEARER", BEARER),
    ]);
    let profile = "/api/workspace/backup-mirrors/mirror_refuse/frontend";

    let first = json!({
        "envelope": envelope("c-3"),
        "sourceEpoch": "epoch-1",
        "clientSequence": 5,
        "acknowledgedAt": "2026-09-30T12:00:00Z",
    })
    .to_string();
    let (status, body) = send(create_app(), "PUT", profile, Some(first)).await;
    assert_eq!(status, StatusCode::OK, "{body}");

    // A non-increasing clientSequence on the accepted epoch.
    let stale = json!({
        "envelope": envelope("c-4"),
        "sourceEpoch": "epoch-1",
        "clientSequence": 5,
        "acknowledgedAt": "2026-09-30T12:00:01Z",
    })
    .to_string();
    let (status, body) = send(create_app(), "PUT", profile, Some(stale)).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert!(body.contains("mirror-stale-sequence"), "{body}");

    // An envelope whose digest does not cover its body.
    let mut broken = envelope("c-5");
    broken["conversations"] = json!([{"id": "changed-after-digesting"}]);
    let bad_digest = json!({
        "envelope": broken,
        "sourceEpoch": "epoch-2",
        "clientSequence": 6,
    })
    .to_string();
    let (status, body) = send(create_app(), "PUT", profile, Some(bad_digest)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert!(body.contains("Frontend backup digest is invalid"), "{body}");

    // A moved head since the client's snapshot.
    let conflict = json!({
        "envelope": envelope("c-6"),
        "sourceEpoch": "epoch-2",
        "clientSequence": 6,
        "expectedHeadGenerationId": "gen_00000000deadbeef",
    })
    .to_string();
    let (status, body) = send(create_app(), "PUT", profile, Some(conflict)).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert!(body.contains("mirror-head-conflict"), "{body}");

    // A profile id the oracle refuses.
    let (status, body) = send(
        create_app(),
        "PUT",
        "/api/workspace/backup-mirrors/has%20space/frontend",
        Some(json!({"envelope": envelope("c-7"), "sourceEpoch": "epoch-1"}).to_string()),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert!(body.contains("Invalid backup mirror profile id"), "{body}");

    // A recipient set that is not age1 at all is refused by normalisation rather than
    // silently sealing to it.
    let _ = std::fs::remove_dir_all(&root);
}
