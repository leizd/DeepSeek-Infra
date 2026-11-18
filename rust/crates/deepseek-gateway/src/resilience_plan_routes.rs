//! Resilience snapshot, assessment, planning, and coordination endpoints (Slice 7b):
//! - `GET /api/workspace/resilience/snapshot` (strictly read-only)
//! - `POST /api/workspace/resilience/assess` (store-gated write)
//! - `POST /api/workspace/resilience/plan` (store-gated write)
//! - `GET|POST /api/workspace/resilience/coordination-plan` (`GET` read-only, `POST` store-gated)

use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde_json::{Map, Value, json};

use crate::resilience_read_routes::list_journal;
use crate::resilience_state_support::{
    calculate_resilience_score_readonly, evaluate_risk_snapshot_readonly,
    materialize_resilience_plan_db, now_unix_secs_and_iso, observe_risk_snapshot_and_slo,
    plan_coordinated_resilience_logic, plan_resilience_actions_logic,
};
use crate::route_support;

const MAX_BODY_BYTES: i64 = 1_048_576;
const WRITE_NOT_OWNED: &str = "NATIVE_RESILIENCE_STORE_WRITE_NOT_OWNED";

pub fn router() -> Router {
    Router::new()
        .route("/api/workspace/resilience/snapshot", any(dispatch_snapshot))
        .route("/api/workspace/resilience/assess", any(dispatch_assess))
        .route("/api/workspace/resilience/plan", any(dispatch_plan))
        .route(
            "/api/workspace/resilience/coordination-plan",
            any(dispatch_coordination_plan),
        )
}

fn write_not_owned() -> AppError {
    AppError {
        status: 409,
        code: WRITE_NOT_OWNED,
        message: "Resilience risk, SLO, and action journal stores are still written by the Python runtime, so this gateway refuses to mutate them.".into(),
    }
}

fn read_optional_json_object(
    headers: &HeaderMap,
    body: &Bytes,
) -> Result<Map<String, Value>, AppError> {
    if body.iter().all(|b| b.is_ascii_whitespace()) {
        return Ok(Map::new());
    }
    route_support::read_json_object(headers, body, MAX_BODY_BYTES)
}

async fn dispatch_snapshot(method: Method) -> Response {
    if method != Method::GET {
        return route_support::unallowed_api_method(method, Method::POST);
    }
    let joined = tokio::task::spawn_blocking(|| {
        let root = crate::data_routes::workspace_root();
        let (now_secs, now_iso) = now_unix_secs_and_iso();
        let score_res = calculate_resilience_score_readonly(&root, now_secs, &now_iso)?;
        let risks_res = evaluate_risk_snapshot_readonly(&root, false, now_secs, &now_iso)?;
        let actions_val = list_journal(None, None, 50)?;
        let actions = actions_val.as_array().cloned().unwrap_or_default();

        let critical_risks = risks_res
            .get("risks")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter(|r| {
                        matches!(
                            r.get("severity")
                                .filter(|v| python_truthy(v))
                                .map(|v| value_str(v).to_lowercase())
                                .as_deref(),
                            Some("critical" | "blocked")
                        )
                    })
                    .count()
            })
            .unwrap_or(0);
        let planned_actions = actions
            .iter()
            .filter(|a| {
                matches!(
                    a.get("state").and_then(Value::as_str),
                    Some("PENDING" | "APPROVAL_REQUIRED")
                )
            })
            .count();
        let running_actions = actions
            .iter()
            .filter(|a| a.get("state").and_then(Value::as_str) == Some("EXECUTING"))
            .count();

        Ok::<Value, AppError>(json!({
            "score": score_res.get("score").cloned().unwrap_or(json!(0)),
            "grade": score_res.get("grade").cloned().unwrap_or(json!("F")),
            "overallRisk": risks_res.get("overallRisk").cloned().unwrap_or(json!("healthy")),
            "criticalRisks": critical_risks,
            "plannedActions": planned_actions,
            "runningActions": running_actions,
            "factorBreakdown": score_res.get("factorBreakdown").cloned().unwrap_or(Value::Null),
            "weaknesses": score_res.get("weaknesses").cloned().unwrap_or_else(|| json!([])),
            "riskDigest": risks_res.get("riskDigest").cloned().unwrap_or(Value::Null),
            "generatedAt": score_res.get("generatedAt").cloned().unwrap_or(Value::Null),
        }))
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_assess(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body_obj = match read_optional_json_object(&headers, &body) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if !crate::python_is_de_authorised() {
        return route_support::app_error_response(write_not_owned());
    }
    let probe = body_obj.get("probe").is_some_and(python_truthy);
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let (now_secs, now_iso) = now_unix_secs_and_iso();
        let mut risks_res = evaluate_risk_snapshot_readonly(&root, probe, now_secs, &now_iso)?;
        observe_risk_snapshot_and_slo(&root, &mut risks_res, now_secs, &now_iso)?;
        let score_res = calculate_resilience_score_readonly(&root, now_secs, &now_iso)?;
        Ok::<Value, AppError>(json!({
            "riskSnapshot": risks_res,
            "resilienceScore": score_res,
        }))
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_plan(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body_obj = match read_optional_json_object(&headers, &body) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if !crate::python_is_de_authorised() {
        return route_support::app_error_response(write_not_owned());
    }
    let probe = body_obj.get("probe").is_some_and(python_truthy);
    let materialize = body_obj.get("materialize").is_none_or(python_truthy);
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let (now_secs, now_iso) = now_unix_secs_and_iso();
        let mut risks_res = evaluate_risk_snapshot_readonly(&root, probe, now_secs, &now_iso)?;
        observe_risk_snapshot_and_slo(&root, &mut risks_res, now_secs, &now_iso)?;
        let plan_res = plan_resilience_actions_logic(&root, &risks_res, true, &now_iso)?;
        if materialize {
            materialize_resilience_plan_db(&root, &plan_res, "resilience-api", &now_iso)
        } else {
            Ok(plan_res)
        }
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_coordination_plan(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::GET && method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    if method == Method::GET {
        let joined = tokio::task::spawn_blocking(|| {
            let root = crate::data_routes::workspace_root();
            let (now_secs, now_iso) = now_unix_secs_and_iso();
            let risks_res = evaluate_risk_snapshot_readonly(&root, false, now_secs, &now_iso)?;
            plan_coordinated_resilience_logic(&root, &risks_res, true, &now_iso)
        })
        .await;
        return match joined {
            Ok(Ok(payload)) => axum::Json(payload).into_response(),
            Ok(Err(err)) => route_support::app_error_response(err),
            Err(_) => route_support::app_error_response(route_support::unhandled_error()),
        };
    }

    let body_obj = match read_optional_json_object(&headers, &body) {
        Ok(obj) => obj,
        Err(err) => return route_support::app_error_response(err),
    };
    if !crate::python_is_de_authorised() {
        return route_support::app_error_response(write_not_owned());
    }
    let probe = body_obj.get("probe").is_some_and(python_truthy);
    let joined = tokio::task::spawn_blocking(move || {
        let root = crate::data_routes::workspace_root();
        let (now_secs, now_iso) = now_unix_secs_and_iso();
        let mut risks_res = evaluate_risk_snapshot_readonly(&root, probe, now_secs, &now_iso)?;
        observe_risk_snapshot_and_slo(&root, &mut risks_res, now_secs, &now_iso)?;
        plan_coordinated_resilience_logic(&root, &risks_res, true, &now_iso)
    })
    .await;

    match joined {
        Ok(Ok(payload)) => axum::Json(payload).into_response(),
        Ok(Err(err)) => route_support::app_error_response(err),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}
