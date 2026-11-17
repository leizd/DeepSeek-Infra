//! Explicit Go-proxied backup target lifecycle routes:
//! - `GET /api/workspace/backup-targets`
//! - `POST /api/workspace/backup-targets`
//! - `POST /api/workspace/backup-targets/register-new`
//! - `DELETE /api/workspace/backup-targets/{target_id}`
//! - `POST /api/workspace/backup-targets/{target_id}/adopt`
//! - `POST /api/workspace/backup-targets/{target_id}/activate`
//! - `POST /api/workspace/backup-targets/{target_id}/probe`
//!
//! Go owns the `target` domain in its SQLite control store and dispatches these
//! endpoints via `/api/workspace/backup-targets` and `/api/workspace/backup-targets/`
//! in `go/internal/api/public.go` (`backup_targets.go` / `backup_target_writes.go`).

use axum::Router;
use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, Method};
use axum::response::Response;
use axum::routing::any;

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/backup-targets", any(dispatch_target))
        .route(
            "/api/workspace/backup-targets/register-new",
            any(dispatch_target),
        )
        .route(
            "/api/workspace/backup-targets/:target_id",
            any(dispatch_target),
        )
        .route(
            "/api/workspace/backup-targets/:target_id/adopt",
            any(dispatch_target),
        )
        .route(
            "/api/workspace/backup-targets/:target_id/activate",
            any(dispatch_target),
        )
        .route(
            "/api/workspace/backup-targets/:target_id/probe",
            any(dispatch_target),
        )
}

async fn dispatch_target(
    method: Method,
    headers: HeaderMap,
    uri: OriginalUri,
    body: Bytes,
) -> Response {
    crate::control_proxy::proxy_api_to_go(method, headers, uri, body).await
}
