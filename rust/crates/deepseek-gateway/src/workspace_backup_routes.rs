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
    Router::new().route("/api/workspace/backup-target-capabilities", any(dispatch))
}

async fn dispatch(method: Method, _headers: HeaderMap, _body: Bytes) -> Response {
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
