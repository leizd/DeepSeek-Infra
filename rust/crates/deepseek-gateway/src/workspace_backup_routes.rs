//! Read-only workspace backup control routes (migration plan S1).
//!
//! The first route is `GET /api/workspace/backup-target-capabilities`. The
//! original handler answers a capability description of the serving runtime:
//! `s3TargetAvailable` follows `backup_target_s3.s3_sdk_available()` (an optional
//! `boto3` import in Python), `webdavTargetAvailable` is always false, and
//! `webdav` is a reserved kind. The native runtime ships a real S3 client
//! (`deepseek-storage`/`object_store`), so the native answer is the capability of
//! *this* runtime: S3 available, WebDAV reserved. The S1 oracle test pins the
//! byte-exact body by giving the Python subprocess a stub `boto3` module, which
//! is exactly the deployment shape where the Python answer is also S3-enabled.
//!
//! S0 prerequisites (store ownership declarations, data cutover) are not needed
//! for this route: it reads no durable store and writes nothing. Later S1
//! routes that touch backup indexes join this module only after their owning
//! plane is declared.

use axum::body::Bytes;
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use serde_json::json;

use crate::route_support;

pub fn router() -> Router {
    Router::new()
        .route(
            "/api/workspace/backup-target-capabilities",
            any(dispatch_target_capabilities),
        )
        .route(
            "/api/workspace/backups/capabilities",
            any(dispatch_backup_capabilities),
        )
}

async fn dispatch_target_capabilities(
    method: Method,
    _headers: HeaderMap,
    _body: Bytes,
) -> Response {
    if method == Method::GET {
        return Json(capabilities()).into_response();
    }
    if method == Method::HEAD {
        // The native edge answers HEAD like the other migrated GET routes: the
        // contract status without a body.
        return StatusCode::OK.into_response();
    }
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    route_support::method_not_allowed()
}

/// The original `api_backup_target_capabilities` body, with the native runtime's
/// actual S3 support.
fn capabilities() -> serde_json::Value {
    json!({
        "s3TargetAvailable": true,
        "webdavTargetAvailable": false,
        "supportedKinds": ["filesystem", "s3"],
        "reservedKinds": ["webdav"],
    })
}

/// `GET /api/workspace/backups/capabilities`: the original capability body for
/// the native runtime.
///
/// The Python body mixes constants with two capability probes: the optional
/// backup-crypto helper (always available natively, because `backup-crypto` is
/// part of the Rust runtime) and the external stateless-MCP contributor
/// (probed over HTTP, exactly like the original 2-second request).
async fn dispatch_backup_capabilities(
    method: Method,
    _headers: HeaderMap,
    _body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        let joined = tokio::task::spawn_blocking(backup_capabilities).await;
        return match joined {
            Ok(_) if method == Method::HEAD => StatusCode::OK.into_response(),
            Ok(body) => Json(body).into_response(),
            Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
            Err(_) => route_support::app_error_response(route_support::unhandled_error()),
        };
    }
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    route_support::method_not_allowed()
}

fn backup_capabilities() -> serde_json::Value {
    json!({
        "ok": true,
        "schemaVersion": "deepseek-workspace-backup.v1",
        "purpose": "restorable-backup",
        "encrypted": true,
        "encryptedBackupAvailable": true,
        "formats": ["age-v1"],
        "protectionModes": ["none", "passphrase", "age-recipient"],
        "integrityVerified": true,
        "modes": ["full", "project"],
        "restoreModes": ["merge", "project-copy", "replace-empty"],
        "coveragePolicies": ["strict", "best-effort"],
        "includedByDefault": [
            "projects",
            "project-files",
            "artifacts",
            "memory",
            "media",
            "custom-skills-and-packs",
            "automations",
            "reminders",
        ],
        "optionalHistory": ["agent-checkpoints", "a2a-tasks", "traces-and-run-history"],
        "restoreProjection": {
            "projects": {"granularity": "project"},
            "project-files": {"granularity": "contributor"},
            "artifacts": {"granularity": "contributor"},
            "memory": {"granularity": "contributor"},
            "media": {"granularity": "contributor"},
            "custom-skills-and-packs": {"granularity": "contributor"},
            "automations": {"granularity": "contributor"},
            "reminders": {"granularity": "contributor"},
            "agent-checkpoints": {"granularity": "contributor"},
            "a2a-tasks": {"granularity": "contributor"},
            "traces-and-run-history": {"granularity": "contributor"},
            "frontend": {"granularity": "contributor"},
            "stateless-mcp": {"granularity": "contributor"},
        },
        "externalContributors": [stateless_mcp_capabilities()],
        "alwaysExcluded": [
            ".auth-token",
            ".env",
            "active tasks",
            "api-key",
            "api_key",
            "auth-token",
            "bearer-token",
            "credentials",
            "credentials.json",
            "rebuildable indexes",
            "runtime locks",
            "tavily-key",
        ],
        "limits": {
            "maxPackageBytes": 2_000_000_000,
            "maxExpandedBytes": 5_000_000_000,
            "maxEntries": 10_000,
            "maxPathLength": 240,
        },
    })
}

/// `StatelessMcpContributor.capabilities`: a 2-second probe of the configured
/// external service; any failure is the original "service unavailable" shape.
fn stateless_mcp_capabilities() -> serde_json::Value {
    let unavailable = || {
        json!({
            "id": "stateless-mcp",
            "available": false,
            "schemaVersion": 1,
            "reason": "service unavailable",
        })
    };
    let base = std::env::var("STATELESS_MCP_BACKUP_URL")
        .unwrap_or_default()
        .trim()
        .trim_end_matches('/')
        .to_string();
    if base.is_empty() {
        return unavailable();
    }
    let client = match reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(client) => client,
        Err(_) => return unavailable(),
    };
    let mut request = client
        .get(format!("{base}/internal/backups/capabilities"))
        .header(
            reqwest::header::ACCEPT,
            "application/json, application/x-ndjson",
        );
    if let Ok(token) = std::env::var("STATELESS_MCP_BACKUP_TOKEN") {
        let token = token.trim().to_string();
        if !token.is_empty() {
            request = request.header(reqwest::header::AUTHORIZATION, format!("Bearer {token}"));
        }
    }
    let Ok(response) = request.send() else {
        return unavailable();
    };
    let Ok(text) = response.text() else {
        return unavailable();
    };
    let Ok(payload) = serde_json::from_str::<serde_json::Value>(&text) else {
        return unavailable();
    };
    let available = payload
        .as_object()
        .map(|object| {
            object
                .get("contributorId")
                .and_then(serde_json::Value::as_str)
                == Some("stateless-mcp")
                && object
                    .get("schemaVersion")
                    .and_then(serde_json::Value::as_f64)
                    == Some(1.0)
        })
        .unwrap_or(false);
    json!({"id": "stateless-mcp", "available": available, "schemaVersion": 1})
}

#[cfg(test)]
mod tests {
    use super::capabilities;

    #[test]
    fn native_capabilities_advertise_only_implemented_kinds() {
        let body = capabilities();
        assert_eq!(body["s3TargetAvailable"], serde_json::json!(true));
        assert_eq!(body["webdavTargetAvailable"], serde_json::json!(false));
        assert_eq!(
            body["supportedKinds"],
            serde_json::json!(["filesystem", "s3"])
        );
        assert_eq!(body["reservedKinds"], serde_json::json!(["webdav"]));
    }
}
