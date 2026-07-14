use std::path::Path;

use axum::{Json, Router, routing::post};
use deepseek_core::TraceId;
use deepseek_policy::PolicyDecision;
use deepseek_policy::audit::{redact_path_target, redact_url_target};
use deepseek_policy::capability::{Capability, RiskLevel, is_capability_allowed};
use deepseek_policy::path_guard::{PathPolicy, validate_workspace_path};
use deepseek_policy::url_guard::{UrlPolicy, validate_url_access};
use serde::de::DeserializeOwned;
use serde_json::Value;

pub fn router() -> Router {
    Router::new()
        .route("/policy/url", post(policy_url))
        .route("/policy/path", post(policy_path))
        .route("/policy/capability", post(policy_capability))
}

async fn policy_url(Json(req): Json<Value>) -> Json<PolicyDecision> {
    let trace_id = parse_trace_id(&req);
    let Some(url) = required_string(&req, "url") else {
        return audited(
            PolicyDecision::invalid_request("url is required").with_trace_id(trace_id),
            None,
        );
    };
    let Some(capability) = optional_enum(&req, "capability", Capability::NetworkFetch) else {
        return audited(
            PolicyDecision::invalid_request("capability is invalid").with_trace_id(trace_id),
            redact_url_target(url),
        );
    };
    let Some(risk_level) = optional_enum(&req, "risk_level", RiskLevel::High) else {
        return audited(
            PolicyDecision::invalid_request("risk_level is invalid").with_trace_id(trace_id),
            redact_url_target(url),
        );
    };

    let decision = validate_url_access(url, &UrlPolicy::default())
        .with_context(capability, risk_level)
        .with_trace_id(trace_id);
    audited(decision, redact_url_target(url))
}

async fn policy_path(Json(req): Json<Value>) -> Json<PolicyDecision> {
    let trace_id = parse_trace_id(&req);
    let Some(root) = required_string(&req, "root") else {
        return audited(
            PolicyDecision::invalid_request("root is required").with_trace_id(trace_id),
            None,
        );
    };
    let Some(requested) = required_string(&req, "requested") else {
        return audited(
            PolicyDecision::invalid_request("requested path is required").with_trace_id(trace_id),
            None,
        );
    };
    let Some(capability) = optional_enum(&req, "capability", Capability::ReadFile) else {
        return audited(
            PolicyDecision::invalid_request("capability is invalid").with_trace_id(trace_id),
            redact_path_target(requested),
        );
    };
    let Some(risk_level) = optional_enum(&req, "risk_level", RiskLevel::High) else {
        return audited(
            PolicyDecision::invalid_request("risk_level is invalid").with_trace_id(trace_id),
            redact_path_target(requested),
        );
    };

    let decision = validate_workspace_path(Path::new(root), Path::new(requested), &PathPolicy)
        .with_context(capability, risk_level)
        .with_trace_id(trace_id);
    audited(decision, redact_path_target(requested))
}

async fn policy_capability(Json(req): Json<Value>) -> Json<PolicyDecision> {
    let trace_id = parse_trace_id(&req);
    let Some(requested) = required_enum::<Capability>(&req, "requested") else {
        return audited(
            PolicyDecision::invalid_request("requested capability is invalid")
                .with_trace_id(trace_id),
            None,
        );
    };
    let Some(max_risk) = required_enum::<RiskLevel>(&req, "max_risk") else {
        return audited(
            PolicyDecision::invalid_request("max_risk is invalid")
                .with_context(requested, RiskLevel::High)
                .with_trace_id(trace_id),
            None,
        );
    };
    let Some(granted_values) = req.get("granted").and_then(Value::as_array) else {
        return audited(
            PolicyDecision::invalid_request("granted capabilities must be an array")
                .with_context(requested, max_risk)
                .with_trace_id(trace_id),
            None,
        );
    };
    let granted: Option<Vec<Capability>> = granted_values.iter().map(parse_enum).collect();
    let Some(granted) = granted else {
        return audited(
            PolicyDecision::invalid_request("granted contains an invalid capability")
                .with_context(requested, max_risk)
                .with_trace_id(trace_id),
            None,
        );
    };

    let decision = is_capability_allowed(requested, &granted, max_risk).with_trace_id(trace_id);
    audited(decision, None)
}

fn audited(decision: PolicyDecision, target: Option<String>) -> Json<PolicyDecision> {
    // The request middleware emits bounded operational fields.  Do not log
    // policy targets, URLs, paths, tool names, reasons, or caller trace IDs.
    drop(target);
    Json(decision)
}

fn required_string<'a>(request: &'a Value, key: &str) -> Option<&'a str> {
    request
        .get(key)?
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn parse_trace_id(request: &Value) -> Option<TraceId> {
    required_string(request, "trace_id").map(|value| TraceId(value.to_string()))
}

fn required_enum<T: DeserializeOwned>(request: &Value, key: &str) -> Option<T> {
    request.get(key).and_then(parse_enum)
}

fn optional_enum<T: DeserializeOwned>(request: &Value, key: &str, default: T) -> Option<T> {
    match request.get(key) {
        Some(value) => parse_enum(value),
        None => Some(default),
    }
}

fn parse_enum<T: DeserializeOwned>(value: &Value) -> Option<T> {
    serde_json::from_value(value.clone()).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_trace_id_ignores_blank_values() {
        assert!(parse_trace_id(&serde_json::json!({"trace_id": "  "})).is_none());
    }

    #[test]
    fn invalid_enum_values_are_not_defaulted() {
        let request = serde_json::json!({"capability": "RootAccess"});
        let parsed = optional_enum(&request, "capability", Capability::ReadFile);
        assert!(parsed.is_none());
    }
}
