//! `GET /api/workspace/resilience/federation`.
//!
//! `build_federation_snapshot` is a pure function. The route always passes the
//! same wire list, empty failure domains, unknown cost, and `UNKNOWN`
//! readiness. `fleetId` defaults to `local`. Nothing is written.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Bytes;
use axum::extract::OriginalUri;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_mirror::sha256_hex;
use deepseek_policy::python_json::dumps_compact;
use deepseek_policy::tool_policy::utc_isoformat_seconds;
use serde_json::{Map, Value, json};

const SNAPSHOT_SCHEMA: &str = "federation-readiness-snapshot-v1";
const SUPPORTED_WIRES: [&str; 8] = [
    "object-set-v1",
    "receipt-v4",
    "commit-v4",
    "fastcdc-v3",
    "control-authority-v1",
    "authority-checkpoint-v1",
    "dr-readiness-proof-v1",
    "evidence-proof-v2",
];
const REQUIRED_WIRES: [&str; 4] = ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"];
const ROUTE_WIRES: [&str; 4] = ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"];
const FORBIDDEN_KEY_FRAGMENTS: [&str; 8] = [
    "credential",
    "secret",
    "password",
    "privatekey",
    "ageidentity",
    "identity",
    "token",
    "authorityprivate",
];

pub fn router() -> Router {
    Router::new().route("/api/workspace/resilience/federation", any(dispatch))
}

async fn dispatch(
    method: Method,
    OriginalUri(uri): OriginalUri,
    _headers: HeaderMap,
    _body: Bytes,
) -> Response {
    if method == Method::GET || method == Method::HEAD {
        return snapshot_response(method, fleet_query(&uri));
    }
    method_not_allowed()
}

fn snapshot_response(method: Method, fleet_id: String) -> Response {
    match build_snapshot(&fleet_id) {
        Ok(_) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(body) => Json(body).into_response(),
        Err(error) if method == Method::HEAD => StatusCode::from_u16(error.status)
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Err(error) => error_response(error),
    }
}

/// `str(query.get("fleetId") or "local")`. Whitespace is truthy.
fn fleet_query(uri: &Uri) -> String {
    match first_query(uri.query().unwrap_or(""), "fleetId") {
        Some(value) if !value.is_empty() => value,
        _ => "local".to_string(),
    }
}

fn first_query(query: &str, wanted: &str) -> Option<String> {
    if query.is_empty() {
        return None;
    }
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        if percent_decode_query(key) == wanted {
            return Some(percent_decode_query(value));
        }
    }
    None
}

fn percent_decode_query(raw: &str) -> String {
    let spaced = raw.replace('+', " ");
    percent_encoding::percent_decode_str(&spaced)
        .decode_utf8_lossy()
        .into_owned()
}

fn build_snapshot(fleet_id: &str) -> Result<Value, AppError> {
    let fleet = fleet_id.trim();
    if fleet.is_empty() {
        return Err(server_error());
    }
    let mut wires: Vec<&str> = ROUTE_WIRES.to_vec();
    wires.sort_unstable();
    wires.dedup();
    let unknown: Vec<&str> = wires
        .iter()
        .copied()
        .filter(|item| !SUPPORTED_WIRES.contains(item))
        .collect();
    let missing: Vec<&str> = {
        let mut required: Vec<&str> = REQUIRED_WIRES
            .iter()
            .copied()
            .filter(|item| !wires.contains(item))
            .collect();
        required.sort_unstable();
        required
    };
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    let mut payload = Map::new();
    payload.insert("snapshotSchema".to_string(), json!(SNAPSHOT_SCHEMA));
    payload.insert("fleetId".to_string(), json!(fleet));
    payload.insert(
        "wireCompatibility".to_string(),
        json!(
            wires
                .iter()
                .map(|item| (*item).to_string())
                .collect::<Vec<_>>()
        ),
    );
    payload.insert("availableFailureDomains".to_string(), json!([]));
    payload.insert("forecastHeadroom".to_string(), Value::Null);
    payload.insert("costClass".to_string(), json!("unknown"));
    payload.insert("readiness".to_string(), json!("UNKNOWN"));
    payload.insert(
        "status".to_string(),
        json!(if unknown.is_empty() && missing.is_empty() {
            "OK"
        } else {
            "INCOMPATIBLE"
        }),
    );
    payload.insert("incompatibleWireVersions".to_string(), json!(unknown));
    payload.insert("missingRequiredWireVersions".to_string(), json!(missing));
    payload.insert(
        "generatedAt".to_string(),
        json!(utc_isoformat_seconds(seconds)),
    );
    let value = Value::Object(payload);
    if contains_forbidden(&value) {
        return Err(server_error());
    }
    let digest = snapshot_digest(&value);
    let mut object = match value {
        Value::Object(object) => object,
        _ => return Err(server_error()),
    };
    object.insert("snapshotDigest".to_string(), json!(digest));
    Ok(Value::Object(object))
}

fn snapshot_digest(snapshot: &Value) -> String {
    let mut fields = Map::new();
    if let Some(object) = snapshot.as_object() {
        for (key, value) in object {
            if key != "snapshotDigest" {
                fields.insert(key.clone(), value.clone());
            }
        }
    }
    sha256_hex(dumps_compact(&Value::Object(fields)).as_bytes())
}

fn contains_forbidden(value: &Value) -> bool {
    match value {
        Value::Object(fields) => fields.iter().any(|(key, item)| {
            let lowered: String = key
                .chars()
                .filter(|character| *character != '_' && *character != '-')
                .collect::<String>()
                .to_lowercase();
            FORBIDDEN_KEY_FRAGMENTS
                .iter()
                .any(|fragment| lowered.contains(fragment))
                || contains_forbidden(item)
        }),
        Value::Array(items) => items.iter().any(contains_forbidden),
        _ => false,
    }
}

fn method_not_allowed() -> Response {
    let mut response = (
        StatusCode::METHOD_NOT_ALLOWED,
        Json(json!({"detail": "Method Not Allowed"})),
    )
        .into_response();
    response
        .headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET, HEAD"));
    response
}

fn server_error() -> AppError {
    AppError {
        status: 500,
        code: codes::INTERNAL,
        message: "Server error".into(),
    }
}

fn error_response(error: AppError) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
