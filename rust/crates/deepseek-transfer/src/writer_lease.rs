//! Target writer-lease acquire/expiry/ownership decisions.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{
    format_iso, object_opt, parse_iso, python_int, python_int_or, python_str_or_empty, python_text,
};
use serde_json::{Map, Value, json};

const DEFAULT_LEASE_SECONDS: i64 = 300;

pub fn replay_writer_lease_case(case: &Value, now: &str) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "payload" => match payload(case, now) {
            Ok(payload) => json!({ "payload": payload }),
            Err(name) => json!({ "status": "error", "oracle_exception": name }),
        },
        "expired" => json!({ "expired": expired(case.get("existing"), now) }),
        "active" => json!({ "active": active(case.get("payload"), now) }),
        "same-run-takeover" => same_run_case(case),
        "acquire" => acquire(case, now),
        "assert-owned" => assert_owned(case, now),
        "release-allowed" => release_allowed(case),
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

fn payload(case: &Value, now: &str) -> Result<Value, &'static str> {
    let lease_seconds = match case.get("lease_seconds") {
        None => DEFAULT_LEASE_SECONDS,
        Some(value) => python_int(value)?,
    };
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    Ok(json!({
        "schemaVersion": 1,
        "targetId": case.get("target_id").cloned().unwrap_or(Value::Null),
        "ownerRunId": case.get("owner_run_id").cloned().unwrap_or(Value::Null),
        "ownerInstanceId": case.get("owner_instance_id").cloned().unwrap_or(Value::Null),
        "fencingToken": case.get("fencing_token").cloned().unwrap_or(Value::Null),
        "acquiredAt": now,
        "expiresAt": format_iso(current + lease_seconds),
    }))
}

fn expired(existing: Option<&Value>, now: &str) -> bool {
    let expires = match existing.and_then(Value::as_object) {
        Some(map) => python_str_or_empty(map.get("expiresAt")),
        None => String::new(),
    };
    expires < now_iso(now)
}

fn now_iso(now: &str) -> String {
    let current = parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
    format_iso(current)
}

fn active(payload: Option<&Value>, now: &str) -> bool {
    let Some(payload) = payload else {
        return true;
    };
    if !payload.is_object() {
        return true;
    }
    match payload.get("expiresAt") {
        Some(Value::String(expires_at)) => {
            match parse_iso(Some(&Value::String(expires_at.clone()))) {
                None => true,
                Some(expiry) => {
                    let current =
                        parse_iso(Some(&Value::String(now.to_string()))).expect("frozen now");
                    expiry > current
                }
            }
        }
        _ => true,
    }
}

fn same_run_takeover(
    existing: &Map<String, Value>,
    owner_run_id: &Value,
    fencing_token: i64,
) -> Result<bool, &'static str> {
    let token = python_int_or(existing.get("fencingToken"), -1)?;
    Ok(
        python_str_or_empty(existing.get("ownerRunId")) == python_text(Some(owner_run_id))
            && token < fencing_token,
    )
}

fn same_run_case(case: &Value) -> Value {
    let Some(existing) = object_opt(case.get("existing")) else {
        return json!({ "allowed": false });
    };
    let fencing = match python_int(case.get("fencing_token").unwrap_or(&Value::Null)) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    match same_run_takeover(
        &existing,
        case.get("owner_run_id").unwrap_or(&Value::Null),
        fencing,
    ) {
        Ok(allowed) => json!({ "allowed": allowed }),
        Err(name) => json!({ "status": "error", "oracle_exception": name }),
    }
}

fn acquire(case: &Value, now: &str) -> Value {
    let fencing_token = match python_int(case.get("fencing_token").unwrap_or(&Value::Null)) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let Some(existing) = object_opt(case.get("existing")) else {
        return match payload(case, now) {
            Ok(payload) => json!({ "decision": "create", "payload": payload }),
            Err(name) => json!({ "status": "error", "oracle_exception": name }),
        };
    };
    let same_run = match same_run_takeover(
        &existing,
        case.get("owner_run_id").unwrap_or(&Value::Null),
        fencing_token,
    ) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    if !expired(Some(&Value::Object(existing.clone())), now) && !same_run {
        return json!({ "decision": "busy", "error": "Target writer is busy with another run" });
    }
    let existing_token = match python_int_or(existing.get("fencingToken"), 0) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    if existing_token >= fencing_token {
        return json!({
            "decision": "newer-token",
            "error": "Target writer is held by a newer or equal fencing token",
        });
    }
    match payload(case, now) {
        Ok(payload) => json!({ "decision": "preempt", "payload": payload }),
        Err(name) => json!({ "status": "error", "oracle_exception": name }),
    }
}

fn assert_owned(case: &Value, now: &str) -> Value {
    let Some(existing) = object_opt(case.get("existing")) else {
        return json!({ "status": "error", "error": "missing" });
    };
    let fencing_token = match python_int(case.get("fencing_token").unwrap_or(&Value::Null)) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let existing_token = match python_int_or(existing.get("fencingToken"), -1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    if python_str_or_empty(existing.get("ownerRunId")) != python_text(case.get("owner_run_id"))
        || python_str_or_empty(existing.get("ownerInstanceId"))
            != python_text(case.get("owner_instance_id"))
        || existing_token != fencing_token
    {
        return json!({ "status": "error", "error": "stolen" });
    }
    if expired(Some(&Value::Object(existing)), now) {
        return json!({ "status": "error", "error": "expired" });
    }
    json!({ "status": "ok" })
}

fn release_allowed(case: &Value) -> Value {
    let Some(existing) = object_opt(case.get("existing")) else {
        return json!({ "delete": false });
    };
    let fencing_token = match python_int(case.get("fencing_token").unwrap_or(&Value::Null)) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let existing_token = match python_int_or(existing.get("fencingToken"), -1) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let allowed = python_str_or_empty(existing.get("ownerRunId"))
        == python_text(case.get("owner_run_id"))
        && existing_token == fencing_token;
    json!({ "delete": allowed })
}
