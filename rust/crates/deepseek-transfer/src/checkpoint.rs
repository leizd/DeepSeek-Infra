//! Read-only replica-repair multipart resume checkpoints.
//! These functions do not contact a provider or move payload bytes.

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use md5::{Digest as Md5Digest, Md5};
use serde_json::{Map, Value, json};
use sha2::Sha256;
use std::fmt;

const NOW_FIELD: &str = "reconciledAt";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MultipartCheckpoint {
    pub outcome: String,
    pub progress: Map<String, Value>,
    pub error: Option<String>,
}

pub fn reconcile_multipart_checkpoint(
    local_parts: &[Value],
    remote_parts: &[Value],
    source_chunks: &[Vec<u8>],
    next_offset: i64,
    upload_id: &str,
    now: &str,
    upload_missing: bool,
) -> MultipartCheckpoint {
    let mut progress = Map::new();
    progress.insert(
        "multipartUploadId".to_string(),
        Value::String(upload_id.to_string()),
    );
    progress.insert("parts".to_string(), Value::Array(local_parts.to_vec()));
    progress.insert("nextOffset".to_string(), json!(next_offset));
    if upload_missing {
        progress.insert(
            "multipartRestart".to_string(),
            json!({
                "previousUploadId": upload_id,
                "reason": "provider-upload-not-found",
                "restartedAt": now,
            }),
        );
        progress.remove("multipartUploadId");
        progress.insert("parts".to_string(), json!([]));
        progress.insert("nextOffset".to_string(), json!(0));
        return MultipartCheckpoint {
            outcome: "restart".to_string(),
            progress,
            error: None,
        };
    }

    let local_parts: Vec<Map<String, Value>> = local_parts
        .iter()
        .filter_map(Value::as_object)
        .cloned()
        .collect();
    let mut remote_parts: Vec<Map<String, Value>> = remote_parts
        .iter()
        .filter_map(Value::as_object)
        .cloned()
        .collect();
    remote_parts.sort_by_key(part_number);
    let mut conflict_reason = if remote_parts.len() < local_parts.len() {
        Some(format!(
            "remote-part-count-behind:{}<{}",
            remote_parts.len(),
            local_parts.len()
        ))
    } else {
        None
    };
    let mut canonical_remote = Vec::new();
    let mut expected_local_offset: i64 = 0;
    let mut chunks = source_chunks.iter();
    for (index, remote_part) in remote_parts.iter().enumerate() {
        let index = index + 1;
        if part_number(remote_part) != index as i64 {
            conflict_reason = conflict_reason.or(Some(format!(
                "non-contiguous-remote-part:{}",
                part_number(remote_part)
            )));
            break;
        }
        let Some(source_chunk) = chunks.next() else {
            conflict_reason = conflict_reason.or(Some("remote-parts-exceed-source".to_string()));
            break;
        };
        if let Err(match_reason) = part_matches_source(remote_part, source_chunk) {
            conflict_reason =
                conflict_reason.or(Some(format!("remote-part-{index}-{match_reason}")));
            break;
        }
        if index <= local_parts.len() {
            let local_part = &local_parts[index - 1];
            if part_number(local_part) != index as i64 {
                conflict_reason = conflict_reason.or(Some(format!(
                    "non-contiguous-local-part:{}",
                    part_number(local_part)
                )));
                break;
            }
            let local_size = python_int(local_part.get("size"), source_chunk.len() as i64);
            let remote_size = python_int(remote_part.get("size"), 0);
            if local_size != remote_size {
                conflict_reason =
                    conflict_reason.or(Some(format!("part-{index}-local-remote-size-conflict")));
                break;
            }
            let local_etag = normalized_etag(local_part.get("etag"));
            let remote_etag = normalized_etag(remote_part.get("etag"));
            if (local_etag.len() == 32 || local_etag.len() == 64) && local_etag != remote_etag {
                conflict_reason =
                    conflict_reason.or(Some(format!("part-{index}-local-remote-etag-conflict")));
                break;
            }
            let local_checksum = value_or_empty(local_part.get("checksumSha256"));
            if !local_checksum.is_empty() && local_checksum != sha256_hex(source_chunk) {
                conflict_reason =
                    conflict_reason.or(Some(format!("part-{index}-local-checksum-conflict")));
                break;
            }
            expected_local_offset += source_chunk.len() as i64;
        }
        canonical_remote.push(canonical_progress_part(remote_part, source_chunk));
    }

    let local_next_offset = python_int(progress.get("nextOffset"), 0);
    if conflict_reason.is_none() && local_next_offset != expected_local_offset {
        conflict_reason = Some(format!(
            "local-offset-conflict:{local_next_offset}!={expected_local_offset}"
        ));
    }
    if let Some(reason) = conflict_reason {
        let error = format!("multipart-reconciliation-conflict:{reason}");
        progress.insert(
            "multipartQuarantine".to_string(),
            json!({
                "uploadId": upload_id,
                "reason": reason,
                "localParts": local_parts,
                "remoteParts": remote_parts,
                "quarantinedAt": now,
            }),
        );
        progress.remove("multipartUploadId");
        progress.insert("parts".to_string(), json!([]));
        progress.insert("nextOffset".to_string(), json!(0));
        return MultipartCheckpoint {
            outcome: "conflict".to_string(),
            progress,
            error: Some(error),
        };
    }

    let remote_len = remote_parts.len();
    let local_len = local_parts.len();
    progress.insert("parts".to_string(), Value::Array(canonical_remote.clone()));
    let next = canonical_remote
        .iter()
        .map(|item| python_int(item.get("size"), 0))
        .sum::<i64>();
    progress.insert("nextOffset".to_string(), json!(next));
    let status = if remote_len > local_len {
        "remote-ahead-adopted"
    } else {
        "remote-matches-local"
    };
    progress.insert(
        "multipartReconciliation".to_string(),
        json!({
            "status": status,
            "uploadId": upload_id,
            "localPartCount": local_len,
            "remotePartCount": remote_len,
            NOW_FIELD: now,
        }),
    );
    MultipartCheckpoint {
        outcome: "adopt".to_string(),
        progress,
        error: None,
    }
}

fn part_number(part: &Map<String, Value>) -> i64 {
    python_int(part.get("partNumber").or_else(|| part.get("number")), 0)
}

fn normalized_etag(value: Option<&Value>) -> String {
    value_or_empty(value)
        .trim()
        .trim_matches('"')
        .to_lowercase()
}

fn part_matches_source(part: &Map<String, Value>, chunk: &[u8]) -> Result<(), String> {
    let size = python_int(part.get("size"), 0);
    if size != chunk.len() as i64 {
        return Err(format!("size-mismatch:{size}!={}", chunk.len()));
    }
    let checksum_b64 = value_or_empty(part.get("checksumSHA256"));
    let expected_sha256 = sha256_hex(chunk);
    if !checksum_b64.is_empty() {
        let expected_b64 = STANDARD.encode(Sha256::digest(chunk));
        if checksum_b64 != expected_b64 {
            return Err("checksum-mismatch".to_string());
        }
        return Ok(());
    }
    let etag = normalized_etag(part.get("etag"));
    let expected_md5 = md5_hex(chunk);
    if etag != expected_sha256 && etag != expected_md5 {
        return Err("etag-unverifiable-or-mismatch".to_string());
    }
    Ok(())
}

fn canonical_progress_part(part: &Map<String, Value>, chunk: &[u8]) -> Value {
    json!({
        "number": part_number(part),
        "etag": value_or_empty(part.get("etag")),
        "size": chunk.len(),
        "checksumSha256": sha256_hex(chunk),
    })
}

fn value_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) if !value.is_empty() => value.clone(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) | None | Some(Value::Null) => String::new(),
        Some(Value::Number(number)) => {
            if python_truthy(&Value::Number(number.clone())) {
                number.to_string()
            } else {
                String::new()
            }
        }
        Some(Value::Array(items)) if !items.is_empty() => format!("{items:?}"),
        Some(Value::Object(fields)) if !fields.is_empty() => format!("{fields:?}"),
        _ => String::new(),
    }
}

fn python_int(value: Option<&Value>, fallback: i64) -> i64 {
    let Some(value) = value else {
        return fallback;
    };
    if !python_truthy(value) {
        return fallback;
    }
    match value {
        Value::Bool(true) => 1,
        Value::Bool(false) => fallback,
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_u64().map(|value| value as i64))
            .or_else(|| number.as_f64().map(|value| value as i64))
            .unwrap_or(fallback),
        Value::String(text) => text.parse().unwrap_or(fallback),
        _ => fallback,
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => {
            let raw = value.to_string();
            if raw.contains(['.', 'e', 'E']) {
                raw.parse::<f64>().is_ok_and(|number| number != 0.0)
            } else {
                raw.bytes().any(|byte| (b'1'..=b'9').contains(&byte))
            }
        }
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn sha256_hex(value: &[u8]) -> String {
    hex(Sha256::digest(value).as_slice())
}

fn md5_hex(value: &[u8]) -> String {
    hex(Md5::digest(value).as_slice())
}

fn hex(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = fmt::Write::write_fmt(&mut encoded, format_args!("{byte:02x}"));
    }
    encoded
}
