//! `POST /api/workspace/backups/recovery-identities`.
//!
//! Generates a fresh in-memory x25519 Age identity/recipient pair matching
//! `backups.generate_recovery_identity()` (`backup_crypto.generate_identity()`).
//! Never creates `.backups/` or `.restore-staging/`. `GET`, `HEAD`, and `DELETE`
//! forward through the control proxy so `/api/workspace/backups/:backup_id` is
//! not shadowed with 405.

use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::backup_unlock::ephemeral_identity;
use serde_json::json;

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/backups/recovery-identities",
        any(dispatch_recovery_identity),
    )
}

async fn dispatch_recovery_identity(
    method: Method,
    headers: HeaderMap,
    uri: OriginalUri,
    body: Bytes,
) -> Response {
    if method == Method::POST {
        return match tokio::task::spawn_blocking(ephemeral_identity).await {
            Ok((identity, recipient)) => Json(json!({
                "ok": true,
                "identity": identity,
                "recipient": recipient,
                "displayedOnce": true,
            }))
            .into_response(),
            Err(_) => {
                crate::route_support::app_error_response(crate::route_support::unhandled_error())
            }
        };
    }
    if method == Method::GET || method == Method::HEAD || method == Method::DELETE {
        return crate::control_proxy::proxy_api_to_go(method, headers, uri, body).await;
    }
    if method == Method::OPTIONS {
        return crate::route_support::options_response();
    }
    crate::route_support::method_not_allowed()
}
