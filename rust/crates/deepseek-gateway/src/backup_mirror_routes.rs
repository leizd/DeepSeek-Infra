//! The sealed frontend replica mirror's public surface, served natively.
//!
//! `GET /api/workspace/backup-mirrors`, `GET /api/workspace/backup-mirrors/{profile}`
//! and `PUT /api/workspace/backup-mirrors/{profile}/frontend` are the three calls the
//! browser makes (`frontend/src/api/workspaceBackupApi.ts`). Python serves them today;
//! this module serves them from [`deepseek_policy::backup_mirror`], whose store is
//! declared `frontend_mirror_store` (python -> rust at 4.9.4).
//!
//! # Why the routes are only mounted when the store has actually changed hands
//!
//! `/api/*` is proxied to the Go control plane by default. Registering these paths
//! unconditionally would **shadow** Python: a deployment that still runs the Python
//! server behind this edge would start answering mirror reads from a directory Python
//! is writing, and the reader would be the one thing the mirror is built to prevent —
//! two parties resolving different generations. So the routes exist only when both
//! ownership conditions hold (declared *and* `DEEPSEEK_RUNTIME_MODE=python_disabled`,
//! the same gate [`crate::may_write_native_store`] applies to every other store). Until
//! then the request falls through to the proxy exactly as before.
//!
//! # Where the recipient sets come from
//!
//! Not from disk. The mirror seals one variant per **enabled** policy, and which
//! policies are enabled is Go control state. [`recipient_source`] reads it over the
//! authenticated `control/v1` gRPC service on the existing loopback control listener;
//! reading `.backup-policies/*.json` instead would make Python's legacy projection a
//! second source of truth in front of a promoted domain. When that read is unavailable
//! the routes refuse with [`RECIPIENT_SOURCE_UNAVAILABLE`] rather than sealing to the
//! wrong recipients or reporting a status that skipped the recipient check.

use std::path::PathBuf;

use axum::body::to_bytes;
use axum::extract::{Path, Request};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::{
    Json, Router,
    routing::{get, put},
};
use deepseek_policy::backup_mirror::{MirrorError, MirrorStore, PutRequest, RecipientGroups};
use deepseek_policy::core_utils::{python_int_opt, python_truthy, text_or_empty};
use deepseek_policy::mutation_gate;
use deepseek_protocol::generated::deepseek::control::v1::{
    BackupPolicyRecipientsRequest, BackupPolicyRecipientsResponse,
    control_plane_client::ControlPlaneClient,
};
use serde_json::{Value, json};

/// The ownership id this module serves, as declared in
/// `release/native_runtime_ownership_v1.json`.
pub(crate) const MIRROR_DOMAIN: &str = "frontend_mirror_store";

/// The default Go control-plane origin, matching `control_proxy`.
pub(crate) const GO_CONTROL_ADDR_ENV: [&str; 2] = ["GO_CONTROL_ADDR", "DEEPSEEK_GO_CONTROL_URL"];

/// The bearer the Go control plane requires on `/internal/*`.
pub(crate) const INTERNAL_BEARER_ENV: &str = "DEEPSEEK_INTERNAL_BEARER";

/// `NATIVE_MIRROR_RECIPIENT_SOURCE_UNAVAILABLE` — the authoritative recipient sets could
/// not be read, so the mirror will not seal or report against a guessed set.
pub(crate) const RECIPIENT_SOURCE_UNAVAILABLE: &str = "NATIVE_MIRROR_RECIPIENT_SOURCE_UNAVAILABLE";

/// `NATIVE_MIRROR_NOT_OWNED` — the store has not changed hands.
pub(crate) const MIRROR_NOT_OWNED: &str = "NATIVE_MIRROR_NOT_OWNED";

pub fn router() -> Router {
    if !crate::may_write_native_store(MIRROR_DOMAIN) {
        return Router::new();
    }
    Router::new()
        .route("/api/workspace/backup-mirrors", get(api_mirror_list))
        .route(
            "/api/workspace/backup-mirrors/:profile_id",
            get(api_mirror_status),
        )
        .route(
            "/api/workspace/backup-mirrors/:profile_id/frontend",
            put(api_mirror_put),
        )
}

/// `DEEPSEEK_BACKUP_MIRROR_DIR`, else `<workspace root>/.backup-mirror`.
///
/// The oracle roots the directory at `config.ROOT/.backup-mirror`; the override exists
/// so a deployment can put the sealed replica on separate storage, which is the reason
/// it is a directory of ciphertext rather than a table.
fn mirror_root() -> PathBuf {
    match std::env::var_os("DEEPSEEK_BACKUP_MIRROR_DIR") {
        Some(root) if !root.is_empty() => PathBuf::from(root),
        _ => crate::data_routes::workspace_root().join(".backup-mirror"),
    }
}

/// The oracle's `AppError.to_response()` envelope: `{"error", "code"}`.
fn mirror_error_response(error: MirrorError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

fn unavailable(code: &'static str, message: &'static str) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": message, "code": code})),
    )
        .into_response()
}

/// The authoritative recipient sets, as Go reports them.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct RecipientSource {
    /// `active_recipients()` over every policy.
    pub recipients: Vec<String>,
    /// One entry per enabled policy, each that policy's own recipient list.
    pub groups: Vec<Vec<String>>,
}

/// Read the recipient sets from the Go control plane.
///
/// `Err(())` means the answer is unknown — an unconfigured origin or bearer, an
/// unreachable control plane, a control plane that does not yet own the policy domain,
/// or a body that is not the expected shape. Every one of those is a refusal, not an
/// empty set: an empty recipient list would silently skip the mirror's
/// `recipient-mismatch` check, which is the check that stops a restore from being told
/// a generation is current when the key it holds cannot open it.
pub(crate) async fn recipient_source() -> Result<RecipientSource, ()> {
    let origin = GO_CONTROL_ADDR_ENV
        .iter()
        .find_map(|name| std::env::var(name).ok())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .ok_or(())?;
    let bearer = std::env::var(INTERNAL_BEARER_ENV)
        .unwrap_or_default()
        .trim()
        .to_string();
    if bearer.len() < 32 {
        return Err(());
    }
    let parsed = reqwest::Url::parse(&origin).map_err(|_| ())?;
    let host = parsed.host_str().ok_or(())?;
    let loopback = host == "localhost"
        || host
            .trim_matches(['[', ']'])
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback());
    if !loopback
        || !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || parsed.path() != "/"
    {
        return Err(());
    }
    let channel = tonic::transport::Endpoint::from_shared(origin)
        .map_err(|_| ())?
        .connect_timeout(std::time::Duration::from_secs(3))
        .timeout(std::time::Duration::from_secs(10))
        .connect()
        .await
        .map_err(|_| ())?;
    let mut client = ControlPlaneClient::new(channel).max_decoding_message_size(1 << 20);
    let mut request = tonic::Request::new(BackupPolicyRecipientsRequest {});
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {bearer}").parse().map_err(|_| ())?,
    );
    let snapshot = client
        .get_backup_policy_recipients(request)
        .await
        .map_err(|_| ())?
        .into_inner();
    recipient_snapshot(snapshot)
}

fn recipient_snapshot(snapshot: BackupPolicyRecipientsResponse) -> Result<RecipientSource, ()> {
    if !snapshot.authoritative
        || snapshot.enabled_policy_count != snapshot.enabled_recipient_groups.len() as u64
        || snapshot.enabled_policy_count > snapshot.policy_count
    {
        return Err(());
    }
    Ok(RecipientSource {
        recipients: snapshot.recipients,
        groups: snapshot
            .enabled_recipient_groups
            .into_iter()
            .map(|group| group.recipients)
            .collect(),
    })
}

/// `GET /api/workspace/backup-mirrors` — `{"mirrors": [...]}`.
///
/// A read, and the one route here that needs no recipient set: `list_mirrors` never
/// consults them.
async fn api_mirror_list() -> Response {
    if !crate::may_write_native_store(MIRROR_DOMAIN) {
        return unavailable(
            MIRROR_NOT_OWNED,
            "The sealed frontend mirror is still Python's store.",
        );
    }
    let store = MirrorStore::new(mirror_root());
    match store.list() {
        Ok(mirrors) => Json(json!({"mirrors": mirrors})).into_response(),
        Err(error) => mirror_error_response(error),
    }
}

/// `GET /api/workspace/backup-mirrors/{profile_id}` — `mirror_status`.
///
/// The oracle passes `recipients=active_recipients() or None`, so an empty aggregate
/// means "do not check recipient sets" rather than "no recipient can open this".
async fn api_mirror_status(Path(profile_id): Path<String>) -> Response {
    if !crate::may_write_native_store(MIRROR_DOMAIN) {
        return unavailable(
            MIRROR_NOT_OWNED,
            "The sealed frontend mirror is still Python's store.",
        );
    }
    let source = match recipient_source().await {
        Ok(source) => source,
        Err(()) => {
            return unavailable(
                RECIPIENT_SOURCE_UNAVAILABLE,
                "The authoritative backup-policy recipients are unavailable, so mirror \
                 status cannot be reported without skipping the recipient check.",
            );
        }
    };
    let recipients = if source.recipients.is_empty() {
        None
    } else {
        Some(source.recipients.as_slice())
    };
    let store = MirrorStore::new(mirror_root());
    // `expected_epoch`, `max_age_seconds` and `excluded` are the caller's context in the
    // oracle (`backup_scheduled` passes them); the public route passes none of them, so
    // the status here is the route's, not the scheduler's.
    match store.status(Some(&profile_id), recipients, None, None, false, None) {
        Ok(status) => Json(status).into_response(),
        Err(error) => mirror_error_response(error),
    }
}

const MAX_MIRROR_REQUEST_BYTES: usize = 64_000_000;

/// Read the public oracle's JSON object independently of Content-Type. Missing or
/// rejected JSON must not be replaced with a default object and reach the sealer.
async fn read_mirror_body(request: Request) -> Result<Value, MirrorError> {
    let length = request
        .headers()
        .get("content-length")
        .map(|value| value.to_str())
        .transpose()
        .map_err(|_| MirrorError::invalid_payload("Invalid Content-Length"))?
        .unwrap_or("0");
    let length = python_int_opt(Some(&Value::String(length.to_string())))
        .filter(|length| *length >= 0)
        .ok_or_else(|| MirrorError::invalid_payload("Invalid Content-Length"))?;
    if length == 0 {
        return Err(MirrorError::invalid_payload("Request body is empty"));
    }
    if length as u64 > MAX_MIRROR_REQUEST_BYTES as u64 {
        return Err(MirrorError::new(
            "Request body is too large",
            "upload_too_large",
            413,
        ));
    }
    let bytes = to_bytes(request.into_body(), MAX_MIRROR_REQUEST_BYTES)
        .await
        .map_err(|_| MirrorError::new("Request body is too large", "upload_too_large", 413))?;
    let payload: Value = serde_json::from_slice(&bytes)
        .map_err(|error| MirrorError::invalid_payload(format!("Invalid JSON: {error}")))?;
    if !payload.is_object() {
        return Err(MirrorError::invalid_payload(
            "Request body must be a JSON object",
        ));
    }
    Ok(payload)
}

fn optional_text(value: Option<&Value>) -> Option<String> {
    let text = text_or_empty(value);
    (!text.is_empty()).then_some(text)
}

fn client_sequence(value: Option<&serde_json::Value>) -> Result<i64, MirrorError> {
    // The public oracle passes `payload.get("clientSequence") or 0`, then
    // clamps Python's int conversion to zero. Invalid truthy values raise;
    // they must never silently become a valid sequence zero.
    match value.filter(|value| python_truthy(value)) {
        None => Ok(0),
        Some(value) => python_int_opt(Some(value))
            .map(|value| value.max(0))
            .ok_or_else(|| {
                MirrorError::invalid_payload("Mirror clientSequence must be a non-negative integer")
            }),
    }
}

/// `PUT /api/workspace/backup-mirrors/{profile_id}/frontend`.
///
/// The oracle's route passes `recipients=None`, which means one sealed variant per
/// **enabled policy** — so the groups come from the control plane, and a group that is
/// empty or holds a non-`age1` recipient is refused by `normalize_recipients` rather
/// than quietly dropped.
async fn api_mirror_put(Path(profile_id): Path<String>, request: Request) -> Response {
    if !crate::may_write_native_store(MIRROR_DOMAIN) {
        return unavailable(
            MIRROR_NOT_OWNED,
            "The sealed frontend mirror is still Python's store.",
        );
    }
    let body = match read_mirror_body(request).await {
        Ok(body) => body,
        Err(error) => return mirror_error_response(error),
    };
    let source = match recipient_source().await {
        Ok(source) => source,
        Err(()) => {
            return unavailable(
                RECIPIENT_SOURCE_UNAVAILABLE,
                "The authoritative backup-policy recipients are unavailable, so mirror \
                 uploads cannot be sealed to the right keys.",
            );
        }
    };
    let envelope = body.get("envelope").cloned().unwrap_or(Value::Null);
    let envelope = match envelope {
        serde_json::Value::Object(_) => envelope,
        _ => serde_json::Value::Object(serde_json::Map::new()),
    };
    let sequence = match client_sequence(body.get("clientSequence")) {
        Ok(sequence) => sequence,
        Err(error) => return mirror_error_response(error),
    };
    let store = MirrorStore::new(mirror_root());
    let request = PutRequest {
        profile_id,
        envelope,
        source_epoch: text_or_empty(body.get("sourceEpoch")),
        recipients: RecipientGroups::FromPolicies(source.groups),
        acknowledged_at: optional_text(body.get("acknowledgedAt")),
        client_replica_id: text_or_empty(body.get("clientReplicaId")),
        client_sequence: sequence,
        expected_head_generation_id: optional_text(body.get("expectedHeadGenerationId")),
        now: None,
    };
    let workspace_root = crate::data_routes::workspace_root();
    let outcome = tokio::task::spawn_blocking(move || {
        // Restore and ownership transfer use this same OS lock. Recheck the durable
        // fence after acquiring it and hold it through sealing and HEAD publication.
        // Blocking I/O and age encryption must not block the async runtime's threads.
        let gate_error = |error: mutation_gate::GateError| {
            MirrorError::new(
                error.message,
                error.code.unwrap_or("invalid_request"),
                error.status.unwrap_or(423),
            )
        };
        let _gate = mutation_gate::exclusive_gate(&workspace_root).map_err(gate_error)?;
        let fenced = mutation_gate::read_fence(&workspace_root)
            .map_err(gate_error)?
            .is_some();
        store.put(request, fenced)
    })
    .await
    .unwrap_or_else(|_| Err(MirrorError::internal("Backup mirror update task failed")));
    match outcome {
        Ok(outcome) => {
            let mut metadata = outcome.metadata;
            if outcome.idempotent {
                if let serde_json::Value::Object(fields) = &mut metadata {
                    fields.insert("idempotent".into(), serde_json::Value::Bool(true));
                }
            }
            Json(metadata).into_response()
        }
        Err(error) => mirror_error_response(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_policy::backup_mirror::active_recipients;

    #[test]
    fn active_recipients_unions_every_policy_in_first_seen_order() {
        let policies = [
            json!({"protection": {"recipients": ["age1a", "age1b"]}}),
            json!({"protection": {"recipients": ["age1b"]}, "encryption": {"recipients": ["age1c"]}}),
            // An empty `protection` object is falsy in Python, so `encryption` is used;
            // a non-empty one without recipients is not.
            json!({"protection": {}, "encryption": {"recipients": ["age1d"]}}),
            json!({"protection": {"mode": "none"}}),
        ];
        assert_eq!(
            active_recipients(policies.iter()),
            vec!["age1a", "age1b", "age1d"]
        );
    }

    #[test]
    fn recipient_snapshot_refuses_unknown_authority_and_inconsistent_counts() {
        assert!(recipient_snapshot(BackupPolicyRecipientsResponse::default()).is_err());
        assert!(
            recipient_snapshot(BackupPolicyRecipientsResponse {
                authoritative: true,
                enabled_policy_count: 1,
                policy_count: 1,
                ..Default::default()
            })
            .is_err()
        );
        assert!(
            recipient_snapshot(BackupPolicyRecipientsResponse {
                authoritative: true,
                ..Default::default()
            })
            .is_ok()
        );
        let snapshot = BackupPolicyRecipientsResponse {
            authoritative: true,
            enabled_recipient_groups: vec![Default::default()],
            enabled_policy_count: 1,
            policy_count: 1,
            ..Default::default()
        };
        assert_eq!(
            recipient_snapshot(snapshot).unwrap().groups,
            vec![Vec::<String>::new()]
        );
    }
}
