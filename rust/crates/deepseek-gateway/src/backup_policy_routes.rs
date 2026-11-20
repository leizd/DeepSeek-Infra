//! Explicit Go-proxied backup policy item routes:
//! `PATCH /api/workspace/backup-policies/{policy_id}` and
//! `DELETE /api/workspace/backup-policies/{policy_id}`.
//!
//! Go implements both mutations behind its authoritative `policy` domain
//! fence in `go/internal/api/backup_policy_writes.go` and dispatches them via
//! `/api/workspace/backup-policies/` in `go/internal/api/public.go`.

use axum::Router;
use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/backup-policies/:policy_id",
        any(dispatch_policy_item),
    )
}

async fn dispatch_policy_item(
    method: Method,
    headers: HeaderMap,
    uri: OriginalUri,
    body: Bytes,
) -> Response {
    crate::control_proxy::proxy_api_to_go(method, headers, uri, body).await
}
