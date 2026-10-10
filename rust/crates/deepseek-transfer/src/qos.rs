//! Transfer traffic-class, wave-sharing, concurrency, and destination-verify semantics.
//! These functions do not contact a provider or move payload bytes.

use crate::pycompat::{python_int, python_int_or, python_str_or_empty, python_text, python_truthy};
use serde_json::{Value, json};

const CLASSES: &[(&str, i64)] = &[
    ("P0_DISASTER_RECOVERY", 0),
    ("P1_BACKUP_PUBLISH", 1),
    ("P2_REQUIRED_REPAIR", 2),
    ("P3_REQUIRED_REPLICATION", 3),
    ("P4_SCRUB_DRILL", 4),
    ("P5_REBALANCE_DRAIN", 5),
    ("P6_BEST_EFFORT", 6),
];
const DEFAULT_GLOBAL: i64 = 200 * 1024 * 1024;
const DEFAULT_RESERVED_RECOVERY: i64 = 100 * 1024 * 1024;
const DEFAULT_BACKGROUND: i64 = 50 * 1024 * 1024;
const DEFAULT_CONCURRENCY: i64 = 4;
const RATE_FLOOR: i64 = 64 * 1024;
const MIB: i64 = 1024 * 1024;

pub fn replay_transfer_qos_case(case: &Value) -> Value {
    let op = case.get("op").and_then(Value::as_str).unwrap_or_default();
    match op {
        "traffic-class" => traffic_class(case),
        "share-wave" => share_wave(case),
        "concurrency" => concurrency(case),
        "base-rate" => json!({ "bytesPerSecond": base_rate(case) }),
        "verify-destination" => verify_destination(case),
        "multipart-missing" => json!({ "missing": multipart_missing(case) }),
        _ => json!({ "status": "error", "error": format!("unknown-op:{op}") }),
    }
}

fn traffic_class(case: &Value) -> Value {
    let Some(Value::String(value)) = case.get("value") else {
        return json!({ "status": "error", "oracle_exception": "AttributeError" });
    };
    let normalized = value.trim().to_uppercase();
    let (name, number) = CLASSES
        .iter()
        .copied()
        .find(|(name, number)| {
            *name == normalized || name.contains(&normalized) || number.to_string() == normalized
        })
        .unwrap_or(("P3_REQUIRED_REPLICATION", 3));
    json!({ "name": name, "value": number, "priority": number })
}

fn share_wave(case: &Value) -> Value {
    let existing = case
        .get("existing")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let types: Vec<String> = existing
        .iter()
        .map(|action| python_str_or_empty(action.get("type")).to_uppercase())
        .collect();
    let candidate = python_str_or_empty(case.get("candidate").and_then(|value| value.get("type")))
        .to_uppercase();
    let blocked = (candidate == "CREATE_REBALANCE_JOB"
        && types.iter().any(|item| item == "CREATE_REPAIR_JOB"))
        || (candidate == "CREATE_REPAIR_JOB"
            && types.iter().any(|item| item == "CREATE_REBALANCE_JOB"));
    if blocked {
        json!({ "allowed": false, "reason": "DEFERRED_TRANSFER_BUDGET" })
    } else {
        json!({ "allowed": true, "reason": "TRANSFER_BUDGET_AVAILABLE" })
    }
}

fn concurrency(case: &Value) -> Value {
    let traffic_class = match python_int(case.get("traffic_class").unwrap_or(&Value::Null)) {
        Ok(value) => value,
        Err(name) => return json!({ "status": "error", "oracle_exception": name }),
    };
    let max_concurrent = match case.get("max_concurrent") {
        Some(value) => match python_int(value) {
            Ok(value) => value,
            Err(name) => return json!({ "status": "error", "oracle_exception": name }),
        },
        None => DEFAULT_CONCURRENCY,
    };
    let active = case
        .get("active")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for key in ["source_target_id", "dest_target_id"] {
        let tid = case.get(key);
        if !tid.is_some_and(python_truthy) {
            continue;
        }
        let tid_text = python_text(tid);
        let active_count = active
            .iter()
            .filter(|item| item.get("source_target_id") == tid || item.get("dest_target_id") == tid)
            .count() as i64;
        if active_count >= max_concurrent && traffic_class != 0 {
            return json!({
                "status": "error",
                "error": "target-transfer-concurrency-exceeded",
                "activeCount": active_count,
                "max": max_concurrent,
                "targetId": tid_text,
            });
        }
    }
    json!({ "status": "ok" })
}

fn base_rate(case: &Value) -> i64 {
    let traffic_class = python_int(case.get("traffic_class").unwrap_or(&json!(6))).unwrap_or(6);
    let global_rate = case
        .get("global_bytes_per_second")
        .map_or(DEFAULT_GLOBAL, |value| {
            python_int(value).unwrap_or(DEFAULT_GLOBAL)
        });
    let reserved = case
        .get("reserved_recovery_bytes_per_sec")
        .map_or(DEFAULT_RESERVED_RECOVERY, |value| {
            python_int(value).unwrap_or(DEFAULT_RESERVED_RECOVERY)
        });
    let background = case
        .get("background_max_bytes_per_sec")
        .map_or(DEFAULT_BACKGROUND, |value| {
            python_int(value).unwrap_or(DEFAULT_BACKGROUND)
        });
    let durable = case
        .get("durable_transfers")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let local = case
        .get("local_transfers")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let has_active_recovery = durable.iter().any(|item| match item.get("trafficClass") {
        None => false,
        Some(value) => python_int(value) == Ok(0),
    }) || local
        .iter()
        .any(|item| python_int(item.get("traffic_class").unwrap_or(&Value::Null)) == Ok(0));
    let base_rate = if traffic_class == 0 {
        global_rate
    } else if has_active_recovery {
        background.min(MIB.max(global_rate - reserved))
    } else if (1..=3).contains(&traffic_class) {
        global_rate
    } else {
        background.min(global_rate)
    };
    RATE_FLOOR.max(base_rate)
}

fn verify_destination(case: &Value) -> Value {
    let expected = python_text(case.get("expected_digest"));
    let kind = case.get("kind").and_then(Value::as_str).unwrap_or("");
    let (valid, corrupt) = if matches!(kind, "none" | "missing") {
        (false, false)
    } else {
        let sha256 = case.get("sha256");
        let provider = case.get("provider_sha256");
        if (sha256.is_some_and(python_truthy) && python_text(sha256) == expected)
            || (provider.is_some_and(python_truthy) && python_text(provider) == expected)
        {
            (true, false)
        } else {
            let stream = case.get("stream_digest");
            let has_data = match case.get("has_data") {
                Some(value) => python_truthy(value),
                None => stream.is_some(),
            };
            if !has_data {
                (false, false)
            } else {
                let calc = python_str_or_empty(stream);
                (calc == expected, calc != expected)
            }
        }
    };
    json!({ "valid": valid, "corrupt": corrupt })
}

fn multipart_missing(case: &Value) -> bool {
    let status = if case.get("omit_status").is_some_and(python_truthy) {
        0
    } else {
        python_int_or(case.get("status"), 0).unwrap_or(0)
    };
    let message = python_str_or_empty(case.get("message")).to_lowercase();
    status == 404 || message.contains("multipart-upload-not-found")
}
