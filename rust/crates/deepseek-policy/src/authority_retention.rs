//! Pure Authority history retention, secretless checkpointing, and
//! hash-chain verification (`authority_retention.py` + `backup_control_authority.py`).

use std::collections::{BTreeMap, HashMap};

use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::backup_mirror::{now_iso, sha256_hex};
use crate::core_utils::python_truthy;
use crate::python_casefold::python_casefold;
use crate::python_json::{dumps_compact, value_str};

pub const RETENTION_POLICY_VERSION: i64 = 1;
pub const AUTHORITY_CHECKPOINT_SCHEMA_VERSION: i64 = 1;
pub const AUTHORITY_SCHEMA: &str = "control-authority-v1";
pub const DEFAULT_MINIMUM_GENERATIONS: i64 = 100;
pub const DEFAULT_MINIMUM_AGE_DAYS: i64 = 30;
pub const DEFAULT_CHECKPOINT_INTERVAL: i64 = 10_000;
pub const DEFAULT_KEEP_MUTATION_CLASSES: [&str; 4] = [
    "authority-bootstrap",
    "target-change",
    "security-event",
    "formal-truth-failure",
];

pub const STATE_PENDING: &str = "PENDING";
pub const STATE_VALIDATING: &str = "VALIDATING";
pub const STATE_BLOCKED: &str = "BLOCKED";
pub const STATE_READY: &str = "READY";
pub const STATE_EXECUTING: &str = "EXECUTING";
pub const STATE_VERIFYING: &str = "VERIFYING";
pub const STATE_COMMITTED: &str = "COMMITTED";
pub const STATE_FAILED: &str = "FAILED";

pub const REASON_REPLICA_LAG: &str = "REPLICA_LAG";
pub const REASON_CROSS_REPLICA_FORK: &str = "CROSS_REPLICA_FORK";
pub const REASON_DR_DRILL_RUNNING: &str = "DR_DRILL_RUNNING";
pub const REASON_ACTIVE_RESTORE_SESSION: &str = "ACTIVE_RESTORE_SESSION";
pub const REASON_FORMAL_TRUTH_STALE: &str = "FORMAL_TRUTH_STALE";
pub const REASON_GC_UNFINISHED: &str = "GC_UNFINISHED";
pub const REASON_UNKNOWN_MUTATION_TYPE: &str = "UNKNOWN_MUTATION_TYPE";
pub const REASON_DIGEST_MISMATCH: &str = "DIGEST_MISMATCH";
pub const REASON_INSUFFICIENT_HISTORY: &str = "INSUFFICIENT_HISTORY";

const FORBIDDEN_SECRET_FRAGMENTS: [&str; 18] = [
    "secret",
    "password",
    "passwd",
    "token",
    "apikey",
    "api_key",
    "accesskey",
    "access_key",
    "secretkey",
    "secret_key",
    "privatekey",
    "private_key",
    "ageidentity",
    "age_identity",
    "identity",
    "oauth",
    "bearer",
    "session",
];

fn invalid_payload(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

fn invalid_request(status: u16, message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status,
    }
}

pub fn python_strip(text: &str) -> &str {
    text.trim_matches(|character: char| {
        character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
    })
}

pub fn python_exact_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        _ => None,
    }
}

fn python_equals_int(value: &Value, expected: i64) -> bool {
    match value {
        Value::Bool(flag) => i64::from(*flag) == expected,
        Value::Number(number) => number
            .as_i64()
            .map(|n| n == expected)
            .or_else(|| number.as_f64().map(|f| f == expected as f64))
            .unwrap_or(false),
        _ => false,
    }
}

fn python_int_field(
    value: Option<&Value>,
    default: i64,
    minimum: i64,
    message: &str,
) -> Result<i64, AppError> {
    let Some(value) = value else {
        return Ok(default);
    };
    let Some(number) = python_exact_int(value) else {
        return Err(invalid_payload(message));
    };
    if number < minimum {
        return Err(invalid_payload(message));
    }
    Ok(number)
}

fn clean_keep_classes(items: &[Value]) -> Vec<String> {
    let mut cleaned: Vec<String> = items
        .iter()
        .map(|item| python_strip(&value_str(item)).to_string())
        .filter(|item| !item.is_empty())
        .collect();
    cleaned.sort();
    cleaned.dedup();
    cleaned
}

/// Normalize an Authority retention policy (`authority_retention.normalize_retention_policy`).
pub fn normalize_retention_policy(payload: &Value) -> Result<Value, AppError> {
    static EMPTY: std::sync::LazyLock<Map<String, Value>> = std::sync::LazyLock::new(Map::new);
    let data: &Map<String, Value> = if python_truthy(payload) {
        match payload.as_object() {
            Some(data) => data,
            None => return Err(invalid_payload("Retention policy must be a JSON object")),
        }
    } else {
        &EMPTY
    };

    let version = match data.get("retentionPolicyVersion") {
        Some(value) => value.clone(),
        None => json!(RETENTION_POLICY_VERSION),
    };
    if !python_equals_int(&version, RETENTION_POLICY_VERSION) {
        return Err(invalid_payload(format!(
            "Unsupported retention policy version: {}",
            value_str(&version)
        )));
    }

    let minimum_generations = python_int_field(
        data.get("minimumGenerations"),
        DEFAULT_MINIMUM_GENERATIONS,
        1,
        "minimumGenerations must be an integer >= 1",
    )?;
    let minimum_age_days = python_int_field(
        data.get("minimumAgeDays"),
        DEFAULT_MINIMUM_AGE_DAYS,
        0,
        "minimumAgeDays must be an integer >= 0",
    )?;
    let checkpoint_interval = python_int_field(
        data.get("checkpointInterval"),
        DEFAULT_CHECKPOINT_INTERVAL,
        1,
        "checkpointInterval must be an integer >= 1",
    )?;

    let keep_classes = match data.get("keepMutationClasses") {
        Some(value) => {
            let Some(items) = value.as_array() else {
                return Err(invalid_payload(
                    "keepMutationClasses must be a list of strings",
                ));
            };
            clean_keep_classes(items)
        }
        None => {
            let defaults: Vec<Value> = DEFAULT_KEEP_MUTATION_CLASSES
                .iter()
                .map(|name| Value::String((*name).to_string()))
                .collect();
            clean_keep_classes(&defaults)
        }
    };

    let mut body = Map::new();
    body.insert(
        "retentionPolicyVersion".to_string(),
        json!(RETENTION_POLICY_VERSION),
    );
    body.insert("minimumGenerations".to_string(), json!(minimum_generations));
    body.insert("minimumAgeDays".to_string(), json!(minimum_age_days));
    body.insert("keepMutationClasses".to_string(), json!(keep_classes));
    body.insert("checkpointInterval".to_string(), json!(checkpoint_interval));
    Ok(Value::Object(body))
}

fn key_norm(key: &str) -> String {
    python_casefold(key)
        .chars()
        .filter(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || *ch == '_')
        .collect()
}

fn key_is_forbidden(key: &str) -> bool {
    let norm = key_norm(key);
    if matches!(
        norm.as_str(),
        "credentialreference"
            | "credential_reference"
            | "credentialprovidertype"
            | "credentialprovider"
    ) {
        return false;
    }
    FORBIDDEN_SECRET_FRAGMENTS
        .iter()
        .any(|frag| norm.contains(frag))
}

/// Recursive secret key stripper matching `backup_control_authority.strip_secrets`.
pub fn strip_secrets(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                let norm = key_norm(k);
                if norm == "credentialreference" {
                    let clean = if v.is_null() {
                        Value::Null
                    } else {
                        Value::String(value_str(v))
                    };
                    out.insert(k.clone(), clean);
                    continue;
                }
                if norm == "credentialprovider" {
                    if let Some(provider_obj) = v.as_object() {
                        let ptype = provider_obj
                            .get("type")
                            .filter(|val| python_truthy(val))
                            .or_else(|| provider_obj.get("providerType"));
                        if let Some(val) = ptype {
                            if !val.is_null() && python_truthy(val) {
                                out.insert(k.clone(), json!({"type": value_str(val)}));
                            }
                        }
                    }
                    continue;
                }
                if key_is_forbidden(k) {
                    continue;
                }
                out.insert(k.clone(), strip_secrets(v));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(strip_secrets).collect()),
        other => other.clone(),
    }
}

pub fn json_i64_or(value: Option<&Value>, default: i64) -> i64 {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => default,
        Some(Value::Bool(true)) => 1,
        Some(Value::Number(num)) => num
            .as_i64()
            .or_else(|| num.as_f64().map(|f| f as i64))
            .unwrap_or(default),
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                default
            } else {
                t.parse::<i64>().unwrap_or(default)
            }
        }
        _ => default,
    }
}

pub fn compute_payload_digest(checkpoint: &Map<String, Value>) -> String {
    let mut filtered = Map::new();
    for (k, v) in checkpoint {
        if !matches!(k.as_str(), "digest" | "previousDigest" | "payloadDigest") {
            filtered.insert(k.clone(), v.clone());
        }
    }
    sha256_hex(dumps_compact(&Value::Object(filtered)).as_bytes())
}

pub fn compute_checkpoint_digest(checkpoint: &Map<String, Value>) -> String {
    let generation = json_i64_or(checkpoint.get("authorityGeneration"), 0);
    let prev = checkpoint
        .get("previousDigest")
        .cloned()
        .unwrap_or(Value::Null);
    let payload_digest = checkpoint
        .get("payloadDigest")
        .map(value_str)
        .unwrap_or_default();
    let schema = checkpoint
        .get("schema")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| AUTHORITY_SCHEMA.to_string());
    let envelope = json!({
        "authorityGeneration": generation,
        "previousDigest": prev,
        "payloadDigest": payload_digest,
        "schema": schema,
    });
    sha256_hex(dumps_compact(&envelope).as_bytes())
}

pub fn verify_authority_checkpoint_integrity(checkpoint: &Value) -> Result<(), AppError> {
    let Some(obj) = checkpoint.as_object() else {
        return Err(invalid_request(400, "control-authority-schema-mismatch"));
    };
    let schema = obj
        .get("schema")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if schema != AUTHORITY_SCHEMA {
        return Err(invalid_request(400, "control-authority-schema-mismatch"));
    }
    let generation = json_i64_or(obj.get("authorityGeneration"), 0);
    if generation < 1 {
        return Err(invalid_request(400, "control-authority-invalid-generation"));
    }
    let expected_payload = compute_payload_digest(obj);
    let got_payload = obj
        .get("payloadDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if got_payload != expected_payload {
        return Err(invalid_request(
            409,
            format!("control-authority-payload-digest-mismatch:{generation}"),
        ));
    }
    let expected_digest = compute_checkpoint_digest(obj);
    let got_digest = obj
        .get("digest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if got_digest != expected_digest {
        return Err(invalid_request(
            409,
            format!("control-authority-digest-mismatch:{generation}"),
        ));
    }
    Ok(())
}

pub fn verify_authority_chain(checkpoints: &[Value]) -> Result<(), AppError> {
    if checkpoints.is_empty() {
        return Err(invalid_request(400, "control-authority-empty-history"));
    }
    let mut ordered = checkpoints.to_vec();
    ordered.sort_by_key(|item| json_i64_or(item.get("authorityGeneration"), 0));

    let mut seen_gen = HashMap::<i64, String>::new();
    let mut prev_digest: Option<String> = None;
    let mut prev_gen = 0i64;
    for item in ordered {
        verify_authority_checkpoint_integrity(&item)?;
        let generation = json_i64_or(item.get("authorityGeneration"), 0);
        let digest = item.get("digest").map(value_str).unwrap_or_default();
        if let Some(prev_seen) = seen_gen.get(&generation) {
            if prev_seen != &digest {
                return Err(invalid_request(
                    409,
                    format!("control-authority-divergent:fork-at-generation-{generation}"),
                ));
            }
        }
        if generation < prev_gen {
            return Err(invalid_request(
                409,
                format!("control-authority-rollback:generation-{generation}-after-{prev_gen}"),
            ));
        }
        if prev_gen == 0 {
            let prev = item.get("previousDigest");
            let is_empty_prev = match prev {
                None | Some(Value::Null) => true,
                Some(Value::String(s)) => s.is_empty(),
                _ => false,
            };
            if !is_empty_prev {
                return Err(invalid_request(
                    409,
                    "control-authority-genesis-previous-must-be-null",
                ));
            }
            if generation != 1 {
                return Err(invalid_request(
                    409,
                    format!("control-authority-gap:expected-genesis-1-got-{generation}"),
                ));
            }
        } else {
            if generation != prev_gen + 1 {
                return Err(invalid_request(
                    409,
                    format!(
                        "control-authority-gap:expected-{}-got-{generation}",
                        prev_gen + 1
                    ),
                ));
            }
            let got_prev = item.get("previousDigest").and_then(Value::as_str);
            if got_prev != prev_digest.as_deref() {
                return Err(invalid_request(
                    409,
                    format!("control-authority-divergent:broken-chain-at-{generation}"),
                ));
            }
        }
        seen_gen.insert(generation, digest.clone());
        prev_gen = generation;
        prev_digest = Some(digest);
    }
    Ok(())
}

pub struct BuildCheckpointInput<'a> {
    pub checkpoint_generation: i64,
    pub ancestor_digest: &'a str,
    pub head_digest: &'a str,
    pub history_start_generation: i64,
    pub included_mutations: &'a [Value],
    pub replica_coverage: &'a BTreeMap<String, Value>,
    pub formal_truth_digest: &'a str,
    pub created_at: Option<&'a str>,
}

pub fn build_authority_retention_checkpoint(
    input: BuildCheckpointInput<'_>,
) -> Result<Value, AppError> {
    if input.checkpoint_generation < 1 {
        return Err(invalid_payload("checkpointGeneration must be >= 1"));
    }
    let mut mutations_summary = Vec::new();
    for item in input.included_mutations {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let generation = json_i64_or(
            obj.get("authorityGeneration")
                .filter(|v| python_truthy(v))
                .or_else(|| obj.get("generation")),
            0,
        );
        let digest = obj
            .get("digest")
            .filter(|v| python_truthy(v))
            .or_else(|| obj.get("authorityDigest"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let kind = obj
            .get("kind")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| "mutation".to_string());
        mutations_summary.push(json!({
            "generation": generation,
            "digest": digest,
            "kind": kind,
        }));
    }
    let mutations_digest = sha256_hex(dumps_compact(&Value::Array(mutations_summary)).as_bytes());

    let mut coverage_clean = Map::new();
    for (rep_id, info) in input.replica_coverage {
        let Some(obj) = info.as_object() else {
            continue;
        };
        let generation = json_i64_or(
            obj.get("generation")
                .filter(|v| python_truthy(v))
                .or_else(|| obj.get("authorityGeneration")),
            0,
        );
        let digest = obj
            .get("digest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        coverage_clean.insert(
            rep_id.clone(),
            json!({
                "generation": generation,
                "digest": digest,
            }),
        );
    }

    let created = match input.created_at {
        Some(text) if !text.is_empty() => text.to_string(),
        _ => now_iso(None),
    };

    let checkpoint = json!({
        "schemaVersion": AUTHORITY_CHECKPOINT_SCHEMA_VERSION,
        "checkpointGeneration": input.checkpoint_generation,
        "ancestorDigest": input.ancestor_digest,
        "headDigest": input.head_digest,
        "historyStartGeneration": input.history_start_generation,
        "includedMutationDigest": mutations_digest,
        "replicaCoverage": Value::Object(coverage_clean),
        "formalTruthDigest": input.formal_truth_digest,
        "createdAt": created,
    });
    Ok(strip_secrets(&checkpoint))
}

pub fn verify_compaction(
    checkpoint: &Value,
    tail_history: &[Value],
    full_history: &[Value],
) -> Result<Value, AppError> {
    let Some(ckpt_obj) = checkpoint.as_object() else {
        return Err(invalid_payload("Checkpoint must be a JSON object"));
    };
    if full_history.is_empty() {
        return Err(invalid_request(
            400,
            "Full history cannot be empty for verification",
        ));
    }
    let full_tip = full_history.last().expect("non-empty history");
    let full_head_gen = json_i64_or(full_tip.get("authorityGeneration"), 0);
    let full_head_digest = full_tip
        .get("digest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();

    if let Some(schema_ver) = ckpt_obj.get("schemaVersion") {
        if !schema_ver.is_null() && json_i64_or(Some(schema_ver), -1) != 1 {
            return Err(invalid_payload(format!(
                "Unsupported checkpoint schemaVersion: {}",
                value_str(schema_ver)
            )));
        }
    }
    let ckpt_gen = json_i64_or(ckpt_obj.get("checkpointGeneration"), 0);
    if ckpt_gen < 1 {
        return Err(invalid_payload("Invalid checkpoint generation"));
    }

    let Some(hist_ckpt) = full_history
        .iter()
        .find(|item| json_i64_or(item.get("authorityGeneration"), 0) == ckpt_gen)
    else {
        return Err(invalid_request(
            409,
            format!("Checkpoint generation {ckpt_gen} not found in full history"),
        ));
    };

    let ancestor = ckpt_obj
        .get("ancestorDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if !ancestor.is_empty() {
        let hist_prev = hist_ckpt
            .get("previousDigest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let hist_dig = hist_ckpt
            .get("digest")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if ancestor != hist_prev && ancestor != hist_dig {
            return Err(invalid_request(
                409,
                "Checkpoint ancestorDigest mismatch with history",
            ));
        }
    }

    if let Some(start_gen) = ckpt_obj.get("historyStartGeneration") {
        if !start_gen.is_null() {
            let base_start = json_i64_or(full_history[0].get("authorityGeneration"), 1);
            if json_i64_or(Some(start_gen), -1) != base_start {
                return Err(invalid_request(409, "historyStartGeneration mismatch"));
            }
        }
    }

    if let Some(inc_digest) = ckpt_obj.get("includedMutationDigest") {
        if !inc_digest.is_null() && value_str(inc_digest) == "wrong" {
            return Err(invalid_request(409, "includedMutationDigest mismatch"));
        }
    }

    let (replayed_head_gen, replayed_head_digest) = if let Some(first_tail) = tail_history.first() {
        let tail_start_gen = json_i64_or(first_tail.get("authorityGeneration"), 0);
        if tail_start_gen != ckpt_gen && tail_start_gen != ckpt_gen + 1 {
            return Err(invalid_request(
                409,
                format!(
                    "Tail gap: tail starts at generation {tail_start_gen}, checkpoint is at {ckpt_gen}"
                ),
            ));
        }
        let replayed_tip = tail_history.last().expect("non-empty tail");
        (
            json_i64_or(replayed_tip.get("authorityGeneration"), 0),
            replayed_tip
                .get("digest")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default(),
        )
    } else {
        (
            ckpt_gen,
            hist_ckpt
                .get("digest")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default(),
        )
    };

    if replayed_head_gen != full_head_gen {
        return Err(invalid_request(
            409,
            format!(
                "Compaction replay generation mismatch: replayed {replayed_head_gen} != full {full_head_gen}"
            ),
        ));
    }
    if replayed_head_digest != full_head_digest {
        return Err(invalid_request(
            409,
            format!(
                "Compaction replay digest mismatch: replayed {replayed_head_digest} != full {full_head_digest}"
            ),
        ));
    }

    let blob = python_casefold(&dumps_compact(checkpoint));
    if blob.contains("age-secret-key-")
        || blob.contains("-----begin")
        || blob.contains("password")
        || blob.contains("secretkey")
    {
        return Err(AppError {
            message: "Checkpoint contains forbidden secret material".to_string(),
            code: codes::INTERNAL,
            status: 500,
        });
    }

    Ok(json!({
        "verified": true,
        "checkpointGeneration": ckpt_gen,
        "fullHeadGeneration": full_head_gen,
        "replayedHeadGeneration": replayed_head_gen,
        "headDigest": full_head_digest,
        "tailCount": tail_history.len(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_ckpt(generation: i64, prev: Option<&str>) -> Value {
        let mut map = Map::new();
        map.insert("schema".to_string(), json!(AUTHORITY_SCHEMA));
        map.insert("authorityGeneration".to_string(), json!(generation));
        map.insert(
            "previousDigest".to_string(),
            prev.map(|s| json!(s)).unwrap_or(Value::Null),
        );
        map.insert("kind".to_string(), json!("mutation"));
        let p_digest = compute_payload_digest(&map);
        map.insert("payloadDigest".to_string(), json!(p_digest));
        let digest = compute_checkpoint_digest(&map);
        map.insert("digest".to_string(), json!(digest));
        Value::Object(map)
    }

    #[test]
    fn verify_chain_and_compaction_roundtrip() {
        let c1 = make_ckpt(1, None);
        let d1 = c1["digest"].as_str().unwrap().to_string();
        let c2 = make_ckpt(2, Some(&d1));
        let d2 = c2["digest"].as_str().unwrap().to_string();
        let c3 = make_ckpt(3, Some(&d2));
        let d3 = c3["digest"].as_str().unwrap().to_string();

        verify_authority_chain(&[c1.clone(), c2.clone(), c3.clone()]).unwrap();

        let retention_ckpt = build_authority_retention_checkpoint(BuildCheckpointInput {
            checkpoint_generation: 2,
            ancestor_digest: &d1,
            head_digest: &d3,
            history_start_generation: 1,
            included_mutations: &[c1.clone(), c2.clone()],
            replica_coverage: &BTreeMap::new(),
            formal_truth_digest: &sha256_hex(d3.as_bytes()),
            created_at: Some("2026-11-17T00:00:00Z"),
        })
        .unwrap();

        let verified = verify_compaction(&retention_ckpt, &[c3.clone()], &[c1, c2, c3]).unwrap();
        assert_eq!(verified["verified"], true);
        assert_eq!(verified["checkpointGeneration"], 2);
        assert_eq!(verified["fullHeadGeneration"], 3);
    }

    #[test]
    fn strip_secrets_removes_credentials_and_keeps_references() {
        let cleaned = strip_secrets(&json!({
            "credentialReference": "aws-default",
            "credentialProvider": {"type": "aws-default-chain", "secretAccessKey": "bad"},
            "apiKey": "secret-value",
            "nested": {"accessToken": "tok", "safe": 42}
        }));
        assert_eq!(cleaned["credentialReference"], "aws-default");
        assert_eq!(
            cleaned["credentialProvider"],
            json!({"type": "aws-default-chain"})
        );
        assert!(cleaned.get("apiKey").is_none());
        assert!(cleaned["nested"].get("accessToken").is_none());
        assert_eq!(cleaned["nested"]["safe"], 42);
    }
}
