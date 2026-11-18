//! Read-only authoritative resilience state helpers shared across Slice 7 routes.
//!
//! Computes risk snapshots, authority verification, fresh-state bundles, blast
//! wave coordination simulation, and mutation-domain state digests directly from
//! durable control/DR/journal/target state without creating directories or mutating SQLite.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::backup_projection::sha256_bytes;
use deepseek_policy::backup_retention::chrono::{DateTime, Utc};
use deepseek_policy::backup_unlock::is_plain_sha256;
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::{dumps_compact, value_str};
use deepseek_policy::tzif::load_iana_timezone;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};

use crate::backup_capacity_routes::{
    estimate_horizon_readonly, now_utc_iso, probe_target_capacity_with_probe, round2,
};
use crate::backup_catalog_routes::{
    list_registered_targets, open_readonly_sqlite, utc_iso_from_unix,
};
use crate::backup_catalog_write_routes::verify_registered_target_ready_readonly;
use crate::backup_dr_slo_routes::{build_body_at, load_drill_items, parse_dr_time};
use crate::backup_dr_status_routes::list_logical_recovery_copies;
use crate::resilience_advisory_routes::list_policies_readonly;
use crate::resilience_read_routes::{list_journal, table_exists};
use crate::route_support::unhandled_error;
use crate::transfer_budget_routes::budget_body;

const AUTHORITY_SCHEMA: &str = "control-authority-v1";
const TERMINAL_REPLICATION_PHASES: [&str; 4] =
    ["superseded", "failed-terminal", "failed", "committed"];
const ACTIVE_ACTION_STATES: [&str; 5] = [
    "CLAIMED",
    "EXECUTING",
    "RECONCILING",
    "VERIFYING",
    "ASSESSING_EFFECT",
];

#[derive(Clone)]
struct AuthorityLocator {
    replica_id: String,
    kind: String,
    root: Option<String>,
}

pub(crate) fn sha256_compact(value: &Value) -> String {
    let encoded = dumps_compact(value);
    sha256_bytes(encoded.as_bytes())
}

pub(crate) fn now_unix_secs_and_iso() -> (u64, String) {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0);
    (secs, utc_iso_from_unix(secs))
}

fn control_db_path(root: &Path) -> PathBuf {
    root.join(".backup-control").join("control.sqlite3")
}

fn dr_db_path(root: &Path) -> PathBuf {
    root.join(".backup-dr").join("evidence.sqlite3")
}

fn value_as_nonbool_i64(val: &Value) -> Option<i64> {
    if val.is_boolean() { None } else { val.as_i64() }
}

fn value_as_nonbool_f64(val: &Value) -> Option<f64> {
    if val.is_boolean() { None } else { val.as_f64() }
}

pub(crate) fn list_control_policies(
    control_conn: Option<&Connection>,
) -> Result<Vec<Value>, AppError> {
    let Some(conn) = control_conn else {
        return Ok(Vec::new());
    };
    if !table_exists(conn, "control_policies")? {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare("SELECT payload_json FROM control_policies ORDER BY policy_id")
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| unhandled_error())?;
    let mut out = Vec::new();
    for row in rows {
        let raw = row.map_err(|_| unhandled_error())?;
        let parsed: Value = serde_json::from_str(&raw).map_err(|_| unhandled_error())?;
        if !parsed.is_object() {
            return Err(AppError {
                message: "storage control authority contains invalid JSON".to_string(),
                code: codes::INTERNAL,
                status: 500,
            });
        }
        out.push(parsed);
    }
    Ok(out)
}

pub(crate) fn get_control_policy(
    control_conn: Option<&Connection>,
    policy_id: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = control_conn else {
        return Ok(None);
    };
    if !table_exists(conn, "control_policies")? {
        return Ok(None);
    }
    let raw: Option<String> = conn
        .query_row(
            "SELECT payload_json FROM control_policies WHERE policy_id = ?",
            [policy_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    let Some(text) = raw else {
        return Ok(None);
    };
    let parsed: Value = serde_json::from_str(&text).map_err(|_| unhandled_error())?;
    if !parsed.is_object() {
        return Err(AppError {
            message: "storage control authority contains invalid JSON".to_string(),
            code: codes::INTERNAL,
            status: 500,
        });
    }
    Ok(Some(parsed))
}

pub(crate) fn list_control_targets(
    control_conn: Option<&Connection>,
) -> Result<Vec<Value>, AppError> {
    let Some(conn) = control_conn else {
        return Ok(Vec::new());
    };
    if !table_exists(conn, "control_targets")? {
        return Ok(Vec::new());
    }
    let mut stmt = conn
        .prepare("SELECT payload_json FROM control_targets ORDER BY target_id")
        .map_err(|_| unhandled_error())?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| unhandled_error())?;
    let mut out = Vec::new();
    for row in rows {
        let raw = row.map_err(|_| unhandled_error())?;
        let parsed: Value = serde_json::from_str(&raw).map_err(|_| unhandled_error())?;
        if !parsed.is_object() {
            return Err(AppError {
                message: "storage control authority contains invalid JSON".to_string(),
                code: codes::INTERNAL,
                status: 500,
            });
        }
        out.push(parsed);
    }
    Ok(out)
}

pub(crate) fn latest_recovery_point(
    dr_conn: Option<&Connection>,
    policy_id: &str,
) -> Result<Option<Value>, AppError> {
    let Some(conn) = dr_conn else {
        return Ok(None);
    };
    if !table_exists(conn, "recovery_points")? {
        return Ok(None);
    }
    conn.query_row(
        "SELECT target_id, policy_id, backup_id, committed_at, snapshot_kind, parent_backup_id, \
                chain_digest, chain_length, ciphertext_bytes, logical_bytes, recoverable, \
                verified_at, storage_protocol, metadata_json \
         FROM recovery_points WHERE policy_id = ? ORDER BY committed_at DESC LIMIT 1",
        [policy_id],
        |row| {
            let meta_raw: Option<String> = row.get(13)?;
            let meta = meta_raw
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .filter(Value::is_object)
                .unwrap_or_else(|| json!({}));
            let parent_id: Option<String> = row.get(5)?;
            let chain_digest: Option<String> = row.get(6)?;
            let proto: Option<String> = row.get(12)?;
            Ok(json!({
                "targetId": row.get::<_, String>(0)?,
                "policyId": row.get::<_, String>(1)?,
                "backupId": row.get::<_, String>(2)?,
                "committedAt": row.get::<_, String>(3)?,
                "snapshotKind": row.get::<_, String>(4)?,
                "parentBackupId": parent_id.filter(|s| !s.is_empty()),
                "chainDigest": chain_digest.filter(|s| !s.is_empty()),
                "chainLength": row.get::<_, i64>(7)?,
                "ciphertextBytes": row.get::<_, i64>(8)?,
                "logicalBytes": row.get::<_, i64>(9)?,
                "recoverable": row.get::<_, i64>(10)? != 0,
                "verifiedAt": row.get::<_, String>(11)?,
                "storageProtocol": proto.filter(|s| !s.is_empty()),
                "metadata": meta,
            }))
        },
    )
    .optional()
    .map_err(|_| unhandled_error())
}

fn authority_mode_from_env() -> String {
    let mut mode = std::env::var("DEEPSEEK_CONTROL_AUTHORITY_MODE")
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    if mode.is_empty() {
        if let Ok(path_raw) = std::env::var("DEEPSEEK_CONTROL_AUTHORITY_BOOTSTRAP") {
            let path = Path::new(path_raw.trim());
            if path.is_file() {
                if let Ok(text) = fs::read_to_string(path) {
                    if let Ok(payload) = serde_json::from_str::<Value>(&text) {
                        if let Some(m) = payload
                            .get("controlAuthority")
                            .and_then(|v| v.get("mode"))
                            .and_then(Value::as_str)
                        {
                            mode = m.trim().to_lowercase();
                        }
                    }
                }
            }
        }
    }
    if matches!(mode.as_str(), "local-only" | "local" | "localonly") {
        "local-only".to_string()
    } else {
        "replicated".to_string()
    }
}

fn min_durable_replicas_from_env() -> Result<i64, AppError> {
    let Ok(raw) = std::env::var("DEEPSEEK_CONTROL_AUTHORITY_MIN_DURABLE_REPLICAS") else {
        return Ok(1);
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(1);
    }
    let parsed: i64 = trimmed.parse().map_err(|_| AppError {
        message: "control-authority-min-durable-invalid".to_string(),
        code: codes::INVALID_REQUEST,
        status: 400,
    })?;
    Ok(parsed.max(1))
}

fn parse_replica_locators(raw: &Value) -> Result<Vec<AuthorityLocator>, AppError> {
    let Some(items) = raw.as_array() else {
        return Err(AppError {
            message: "control-authority-bootstrap-must-be-list".to_string(),
            code: codes::INVALID_REQUEST,
            status: 400,
        });
    };
    let mut out = Vec::new();
    for (idx, item) in items.iter().enumerate() {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let target = obj.get("target").and_then(Value::as_object).unwrap_or(obj);
        let kind = target
            .get("kind")
            .or_else(|| obj.get("kind"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        let replica_id = obj
            .get("replicaId")
            .or_else(|| obj.get("id"))
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| format!("replica-{idx}"));
        if matches!(kind.as_str(), "fs" | "filesystem" | "file" | "path") {
            let root = target
                .get("root")
                .or_else(|| target.get("path"))
                .or_else(|| obj.get("root"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(ToString::to_string);
            if root.is_none() {
                continue;
            }
            out.push(AuthorityLocator {
                replica_id,
                kind: "filesystem".to_string(),
                root,
            });
        } else if matches!(kind.as_str(), "s3" | "minio" | "object" | "object-store") {
            out.push(AuthorityLocator {
                replica_id,
                kind: "s3".to_string(),
                root: None,
            });
        }
    }
    Ok(out)
}

fn load_bootstrap_locators() -> Result<Vec<AuthorityLocator>, AppError> {
    let mut locators = Vec::new();
    if let Ok(path_raw) = std::env::var("DEEPSEEK_CONTROL_AUTHORITY_BOOTSTRAP") {
        let trimmed = path_raw.trim();
        if !trimmed.is_empty() {
            let path = Path::new(trimmed);
            if path.is_file() {
                let text = fs::read_to_string(path).map_err(|_| AppError {
                    message: format!("control-authority-bootstrap-unreadable:{}", path.display()),
                    code: codes::INVALID_REQUEST,
                    status: 400,
                })?;
                let payload: Value = serde_json::from_str(&text).map_err(|_| AppError {
                    message: format!("control-authority-bootstrap-unreadable:{}", path.display()),
                    code: codes::INVALID_REQUEST,
                    status: 400,
                })?;
                let source = payload
                    .get("controlAuthority")
                    .and_then(Value::as_object)
                    .and_then(|ca| ca.get("replicas"))
                    .or_else(|| payload.get("replicas"))
                    .unwrap_or(&payload);
                locators.extend(parse_replica_locators(source)?);
            }
        }
    }
    if let Ok(env_json) = std::env::var("DEEPSEEK_CONTROL_AUTHORITY_REPLICAS") {
        let trimmed = env_json.trim();
        if !trimmed.is_empty() {
            let parsed: Value = serde_json::from_str(trimmed).map_err(|_| AppError {
                message: "control-authority-bootstrap-invalid-json".to_string(),
                code: codes::INVALID_REQUEST,
                status: 400,
            })?;
            locators.extend(parse_replica_locators(&parsed)?);
        }
    }
    let mut seen = HashSet::new();
    let mut unique = Vec::new();
    for loc in locators {
        if seen.insert(loc.replica_id.clone()) {
            unique.push(loc);
        }
    }
    Ok(unique)
}

fn provider_status_from_env() -> Result<(Value, Vec<PathBuf>), AppError> {
    let mode = authority_mode_from_env();
    let min_durable = min_durable_replicas_from_env()?;
    let locators = load_bootstrap_locators()?;
    if locators.is_empty() {
        return Ok((
            json!({
                "mode": mode,
                "configuredReplicaCount": 0,
                "resolvedReplicaCount": 0,
                "resolveErrors": [],
                "replicaIds": [],
                "locatorIds": [],
                "minDurableReplicas": min_durable,
            }),
            Vec::new(),
        ));
    }
    let mut resolve_errors = Vec::new();
    let mut replica_ids = Vec::new();
    let mut locator_ids = Vec::new();
    let mut fs_roots = Vec::new();
    for loc in &locators {
        locator_ids.push(loc.replica_id.clone());
        if loc.kind == "filesystem" {
            if let Some(root) = &loc.root {
                replica_ids.push(loc.replica_id.clone());
                fs_roots.push(PathBuf::from(root));
            } else {
                resolve_errors.push(format!("{}:filesystem-root-missing", loc.replica_id));
            }
        } else {
            resolve_errors.push(format!("{}:s3-store-unresolved", loc.replica_id));
        }
    }
    Ok((
        json!({
            "mode": mode,
            "configuredReplicaCount": locators.len(),
            "resolvedReplicaCount": replica_ids.len(),
            "resolveErrors": resolve_errors,
            "replicaIds": replica_ids,
            "locatorIds": locator_ids,
            "minDurableReplicas": min_durable,
        }),
        fs_roots,
    ))
}

fn control_db_is_healthy(control_conn: Option<&Connection>) -> bool {
    let Some(conn) = control_conn else {
        return false;
    };
    let check: Option<String> = conn
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .optional()
        .ok()
        .flatten();
    check.map(|s| s.eq_ignore_ascii_case("ok")).unwrap_or(false)
}

pub(crate) fn authority_health_snapshot_with_conn(
    control_conn: Option<&Connection>,
) -> Result<(Value, Vec<PathBuf>), AppError> {
    let (provider, fs_roots) = provider_status_from_env()?;
    let healthy = control_db_is_healthy(control_conn);

    let mut boot_epoch: Value = Value::Null;
    let mut recovery_state: Value = Value::Null;
    let mut boot_reason: Value = Value::Null;
    let mut head_gen: Value = Value::Null;
    let mut head_digest: Value = Value::Null;
    let mut pending: i64 = 0;
    let mut formal_total: i64 = 0;
    let mut formal_complete: i64 = 0;

    if let (true, Some(conn)) = (healthy, control_conn) {
        if table_exists(conn, "control_boot_state").unwrap_or(false) {
            if let Ok(Some((epoch, state, reason))) = conn
                .query_row(
                    "SELECT boot_epoch, recovery_state, reason FROM control_boot_state WHERE id = 1",
                    [],
                    |row| {
                        Ok((
                            row.get::<_, i64>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, Option<String>>(2)?,
                        ))
                    },
                )
                .optional()
            {
                boot_epoch = json!(epoch);
                recovery_state = json!(state);
                boot_reason = reason
                    .filter(|s| !s.is_empty())
                    .map(Value::String)
                    .unwrap_or(Value::Null);
            } else {
                boot_epoch = json!(1);
                recovery_state = json!("active");
            }
        } else {
            boot_epoch = json!(1);
            recovery_state = json!("active");
        }

        if table_exists(conn, "control_authority_head").unwrap_or(false) {
            if let Ok(Some((generation, dig))) = conn
                .query_row(
                    "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1",
                    [],
                    |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
                )
                .optional()
            {
                head_gen = json!(generation);
                head_digest = json!(dig);
            }
        }

        if table_exists(conn, "control_authority_outbox").unwrap_or(false) {
            if let Ok(c) = conn.query_row(
                "SELECT COUNT(*) FROM control_authority_outbox WHERE state = 'pending'",
                [],
                |row| row.get::<_, i64>(0),
            ) {
                pending += c;
            }
        }
        if table_exists(conn, "control_authority_mutations").unwrap_or(false) {
            if let Ok(c) = conn.query_row(
                "SELECT COUNT(*) FROM control_authority_mutations \
                 WHERE state IN ('anchoring','divergent','prepared','remote-outcome-unknown')",
                [],
                |row| row.get::<_, i64>(0),
            ) {
                pending += c;
            }
        }

        if table_exists(conn, "control_targets").unwrap_or(false) {
            if let Ok(mut stmt) = conn.prepare("SELECT target_id FROM control_targets") {
                if let Ok(rows) = stmt.query_map([], |row| row.get::<_, String>(0)) {
                    let has_cov = table_exists(conn, "target_index_coverage").unwrap_or(false);
                    for tid in rows.flatten() {
                        formal_total += 1;
                        if has_cov {
                            let st: Option<String> = conn
                                .query_row(
                                    "SELECT state FROM target_index_coverage WHERE target_id = ?",
                                    [&tid],
                                    |r| r.get(0),
                                )
                                .optional()
                                .ok()
                                .flatten();
                            if st.as_deref() == Some("complete") {
                                formal_complete += 1;
                            }
                        }
                    }
                }
            }
        }
    }

    let formal_incomplete = formal_total - formal_complete;
    let workers = recovery_state.as_str() == Some("active") && pending == 0;
    let health = json!({
        "verdict": recovery_state,
        "recoveryState": recovery_state,
        "controlBootEpoch": boot_epoch,
        "canonicalGeneration": head_gen,
        "canonicalDigest": head_digest,
        "configuredReplicaCount": provider.get("configuredReplicaCount").cloned().unwrap_or(json!(0)),
        "resolvedReplicaCount": provider.get("resolvedReplicaCount").cloned().unwrap_or(json!(0)),
        "resolveErrors": provider.get("resolveErrors").cloned().unwrap_or_else(|| json!([])),
        "minDurableReplicas": provider.get("minDurableReplicas").cloned().unwrap_or(json!(1)),
        "unresolvedMutationCount": pending,
        "formalTruth": {
            "targetCount": formal_total,
            "completeTargets": formal_complete,
            "incompleteTargets": formal_incomplete,
        },
        "workersAllowed": workers,
        "mutationsAllowed": workers,
        "providerMode": provider.get("mode").cloned().unwrap_or_else(|| json!("replicated")),
        "reason": boot_reason,
    });
    Ok((health, fs_roots))
}

fn verify_checkpoint_integrity(ckpt: &Map<String, Value>) -> Result<(), (String, bool)> {
    let schema = ckpt
        .get("schema")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if schema != AUTHORITY_SCHEMA {
        return Err(("control-authority-schema-mismatch".to_string(), false));
    }
    let generation = ckpt
        .get("authorityGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if generation < 1 {
        return Err(("control-authority-invalid-generation".to_string(), false));
    }
    let mut payload_obj = Map::new();
    for (k, v) in ckpt {
        if !matches!(k.as_str(), "digest" | "previousDigest" | "payloadDigest") {
            payload_obj.insert(k.clone(), v.clone());
        }
    }
    let expected_payload = sha256_compact(&Value::Object(payload_obj));
    if ckpt.get("payloadDigest").and_then(Value::as_str) != Some(expected_payload.as_str()) {
        return Err((
            format!("control-authority-payload-digest-mismatch:{generation}"),
            false,
        ));
    }
    let envelope = json!({
        "authorityGeneration": generation,
        "previousDigest": ckpt.get("previousDigest").cloned().unwrap_or(Value::Null),
        "payloadDigest": ckpt.get("payloadDigest").and_then(Value::as_str).unwrap_or_default(),
        "schema": if schema.is_empty() { AUTHORITY_SCHEMA } else { schema },
    });
    let expected_digest = sha256_compact(&envelope);
    if ckpt.get("digest").and_then(Value::as_str) != Some(expected_digest.as_str()) {
        return Err((
            format!("control-authority-digest-mismatch:{generation}"),
            false,
        ));
    }
    Ok(())
}

fn verify_authority_chain_entries(history: &[Value]) -> Result<BTreeMap<i64, String>, String> {
    if history.is_empty() {
        return Err("control-authority-empty-history".to_string());
    }
    let mut items: Vec<(i64, &Map<String, Value>)> = Vec::with_capacity(history.len());
    for entry in history {
        let Some(obj) = entry.as_object() else {
            return Err("control-authority-schema-mismatch".to_string());
        };
        let generation = obj
            .get("authorityGeneration")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        items.push((generation, obj));
    }
    items.sort_by_key(|(generation, _)| *generation);

    let mut seen_gen: BTreeMap<i64, String> = BTreeMap::new();
    let mut prev_digest: Option<String> = None;
    let mut prev_gen: i64 = 0;

    for (generation, obj) in items {
        verify_checkpoint_integrity(obj).map_err(|(msg, _)| msg)?;
        let digest = obj
            .get("digest")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if let Some(prev) = seen_gen.get(&generation) {
            if prev != &digest {
                return Err(format!(
                    "control-authority-divergent:fork-at-generation-{generation}"
                ));
            }
        }
        if generation < prev_gen {
            return Err(format!(
                "control-authority-rollback:generation-{generation}-after-{prev_gen}"
            ));
        }
        let item_prev = obj
            .get("previousDigest")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        if prev_gen == 0 {
            if item_prev.is_some() {
                return Err("control-authority-genesis-previous-must-be-null".to_string());
            }
            if generation != 1 {
                return Err(format!(
                    "control-authority-gap:expected-genesis-1-got-{generation}"
                ));
            }
        } else {
            if generation != prev_gen + 1 {
                return Err(format!(
                    "control-authority-gap:expected-{}-got-{generation}",
                    prev_gen + 1
                ));
            }
            if item_prev != prev_digest.as_deref() {
                return Err(format!(
                    "control-authority-divergent:broken-chain-at-{generation}"
                ));
            }
        }
        seen_gen.insert(generation, digest.clone());
        prev_digest = Some(digest);
        prev_gen = generation;
    }
    Ok(seen_gen)
}

struct DiscoveredReplica {
    replica_id: String,
    generation: i64,
    digest: String,
    history: Vec<Value>,
    checkpoint: Map<String, Value>,
}

fn load_filesystem_replica(root: &Path, index: usize) -> Option<DiscoveredReplica> {
    let head_path = root.join("control").join("authority").join("head.json");
    if !head_path.is_file() {
        return None;
    }
    let head_text = fs::read_to_string(&head_path).ok()?;
    let head_val: Value = serde_json::from_str(&head_text).ok()?;
    let head_obj = head_val.as_object()?;
    let ckpt_dir = root.join("control").join("authority").join("checkpoints");
    let mut history = Vec::new();
    if ckpt_dir.is_dir() {
        let mut entries: Vec<PathBuf> = fs::read_dir(&ckpt_dir)
            .ok()?
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && p.extension().and_then(|s| s.to_str()) == Some("json"))
            .collect();
        entries.sort();
        for p in entries {
            let text = fs::read_to_string(&p).ok()?;
            let val: Value = serde_json::from_str(&text).ok()?;
            history.push(val);
        }
    }
    if !history.is_empty() && verify_authority_chain_entries(&history).is_err() {
        return None;
    }
    let tip = history.last()?.as_object()?.clone();
    let head_digest = head_obj
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let tip_digest = tip
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if tip_digest != head_digest {
        return None;
    }
    let name = root
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or_default();
    let replica_id = format!("replica-{index}:{name}");
    let generation = head_obj
        .get("authorityGeneration")
        .and_then(Value::as_i64)
        .or_else(|| tip.get("authorityGeneration").and_then(Value::as_i64))
        .unwrap_or(0);
    Some(DiscoveredReplica {
        replica_id,
        generation,
        digest: if head_digest.is_empty() {
            tip_digest.to_string()
        } else {
            head_digest.to_string()
        },
        history,
        checkpoint: tip,
    })
}

pub(crate) fn authority_verify_readonly(
    control_conn: Option<&Connection>,
) -> Result<Value, AppError> {
    let (health, fs_roots) = authority_health_snapshot_with_conn(control_conn)?;
    let mut issues = Vec::new();
    let mut discovered = Vec::new();
    for (idx, root) in fs_roots.iter().enumerate() {
        if let Some(rep) = load_filesystem_replica(root, idx) {
            discovered.push(rep);
        }
    }

    let mut selected_gen: Option<i64> = None;
    let mut selected_digest: Option<String> = None;
    let mut lagging_replicas: HashSet<String> = HashSet::new();
    let mut cross_replica_divergent = false;

    if !discovered.is_empty() {
        let mut views: Vec<(&DiscoveredReplica, BTreeMap<i64, String>, String)> = Vec::new();
        let mut selection_err: Option<String> = None;
        for rep in &discovered {
            let mut by_gen = match verify_authority_chain_entries(&rep.history) {
                Ok(m) => m,
                Err(e) => {
                    selection_err = Some(e);
                    break;
                }
            };
            if by_gen.is_empty() && !rep.digest.is_empty() {
                by_gen.insert(rep.generation, rep.digest.clone());
            }
            let tip_digest = by_gen
                .get(&rep.generation)
                .cloned()
                .unwrap_or_else(|| rep.digest.clone());
            if by_gen.get(&rep.generation).map(String::as_str) != Some(rep.digest.as_str())
                && by_gen.contains_key(&rep.generation)
            {
                selection_err = Some(format!(
                    "control-authority-divergent:head-history-mismatch:{}",
                    rep.replica_id
                ));
                break;
            }
            views.push((rep, by_gen, tip_digest));
        }

        if selection_err.is_none() && !views.is_empty() {
            let best_gen = views
                .iter()
                .map(|(r, _, _)| r.generation)
                .max()
                .unwrap_or(0);
            let at_best: Vec<_> = views
                .iter()
                .filter(|(r, _, _)| r.generation == best_gen)
                .collect();
            let best_digests: HashSet<&str> =
                at_best.iter().map(|(_, _, td)| td.as_str()).collect();
            if best_digests.len() != 1 {
                selection_err = Some(format!("control-authority-divergent:generation-{best_gen}"));
            } else {
                let canonical = at_best
                    .iter()
                    .find(|(r, _, _)| r.checkpoint.contains_key("policies"))
                    .copied()
                    .unwrap_or(at_best[0]);
                let canonical_tip = canonical.2.clone();
                let mut canonical_history: BTreeMap<i64, String> = BTreeMap::new();
                'merge: for (_, by_gen, _) in &views {
                    for (&g, d) in by_gen {
                        if let Some(existing) = canonical_history.get(&g) {
                            if existing != d {
                                selection_err = Some(format!(
                                    "control-authority-divergent:fork-at-generation-{g}"
                                ));
                                break 'merge;
                            }
                        }
                        canonical_history.insert(g, d.clone());
                    }
                }
                if selection_err.is_none() {
                    if let Some(prev) = canonical
                        .0
                        .checkpoint
                        .get("previousDigest")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        if best_gen >= 2 {
                            canonical_history
                                .entry(best_gen - 1)
                                .or_insert_with(|| prev.to_string());
                        }
                    }
                    canonical_history
                        .entry(best_gen)
                        .or_insert_with(|| canonical_tip.clone());
                    for (rep, by_gen, td) in &views {
                        if rep.generation == best_gen && td == &canonical_tip {
                            continue;
                        }
                        let ok = rep.generation >= 1
                            && canonical_history.contains_key(&rep.generation)
                            && (1..=rep.generation).all(|g| {
                                by_gen
                                    .get(&g)
                                    .is_none_or(|cand| canonical_history.get(&g) == Some(cand))
                            })
                            && by_gen.get(&rep.generation)
                                == canonical_history.get(&rep.generation);
                        if !ok {
                            selection_err = Some(format!(
                                "control-authority-divergent:non-ancestor:{}:tip-{}",
                                rep.replica_id, rep.generation
                            ));
                            break;
                        }
                        lagging_replicas.insert(rep.replica_id.clone());
                    }
                }
                if selection_err.is_none() {
                    selected_gen = Some(best_gen);
                    selected_digest = Some(canonical_tip);
                }
            }
        }

        if let Some(msg) = selection_err {
            issues.push(format!("divergent-or-invalid:{msg}"));
            let low = msg.to_lowercase();
            if low.contains("divergent") || low.contains("non-ancestor") || low.contains("fork") {
                cross_replica_divergent = true;
                issues.push("cross-replica-divergent".to_string());
            }
        }
    }

    let mut replica_reports = Vec::new();
    for rep in &discovered {
        let history_valid = if rep.history.is_empty() {
            true
        } else if let Err(_e) = verify_authority_chain_entries(&rep.history) {
            issues.push(format!("history-invalid:{}", rep.replica_id));
            false
        } else {
            true
        };
        let lag = selected_gen
            .map(|sg| (sg - rep.generation).max(0))
            .unwrap_or(0);
        let repairable = lag > 0 && history_valid && lagging_replicas.contains(&rep.replica_id);
        let divergent = (!history_valid) || cross_replica_divergent;
        replica_reports.push(json!({
            "replicaId": rep.replica_id,
            "reachable": true,
            "tipGeneration": rep.generation,
            "tipDigest": rep.digest,
            "historyValid": history_valid,
            "lagGenerations": lag,
            "repairable": repairable,
            "divergent": divergent,
        }));
    }

    let configured = health
        .get("configuredReplicaCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let resolved = health
        .get("resolvedReplicaCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if configured > resolved {
        issues.push("configured-exceeds-resolved".to_string());
    }
    let incomplete_targets = health
        .get("formalTruth")
        .and_then(|v| v.get("incompleteTargets"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if incomplete_targets > 0 {
        issues.push("formal-truth-incomplete".to_string());
    }
    let unresolved = health
        .get("unresolvedMutationCount")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if unresolved > 0 {
        issues.push("unresolved-authority-mutations".to_string());
    }
    let min_durable = health
        .get("minDurableReplicas")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    let reachable = replica_reports
        .iter()
        .filter(|r| {
            r.get("reachable") == Some(&Value::Bool(true))
                && r.get("historyValid") == Some(&Value::Bool(true))
        })
        .count() as i64;
    if configured > 0 && reachable < min_durable {
        issues.push("durability-unsatisfied".to_string());
    }

    let any_divergent = cross_replica_divergent
        || replica_reports
            .iter()
            .any(|r| r.get("divergent") == Some(&Value::Bool(true)));
    let overall = if any_divergent {
        "DIVERGENT"
    } else if issues.iter().any(|s| s == "durability-unsatisfied") {
        "DURABILITY_UNSATISFIED"
    } else if configured > 0 && resolved == 0 {
        "UNAVAILABLE"
    } else if !issues.is_empty() {
        "DEGRADED"
    } else {
        "HEALTHY"
    };

    Ok(json!({
        "status": if overall == "HEALTHY" { "ok" } else { "degraded" },
        "overall": overall,
        "issues": issues,
        "health": health,
        "replicas": replica_reports,
        "canonicalGeneration": selected_gen,
        "canonicalDigest": selected_digest,
        "readOnly": true,
    }))
}

fn has_open_required_jobs(root: &Path, policy_id: &str, backup_id: &str) -> bool {
    let dir = root.join(".backup-replication");
    if !dir.is_dir() {
        return false;
    }
    let Ok(read_dir) = fs::read_dir(&dir) else {
        return false;
    };
    let mut names: Vec<String> = read_dir
        .flatten()
        .filter_map(|e| {
            let p = e.path();
            let name = e.file_name().into_string().ok()?;
            if name.starts_with('.') || !name.to_lowercase().ends_with(".json") || !p.is_file() {
                return None;
            }
            Some(name)
        })
        .collect();
    names.sort_by(|a, b| {
        b.to_lowercase()
            .cmp(&a.to_lowercase())
            .then_with(|| b.cmp(a))
    });
    let mut count = 0usize;
    for name in names {
        let Ok(raw) = fs::read_to_string(dir.join(&name)) else {
            continue;
        };
        let Ok(data) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        let job_id = data.get("jobId").map(value_str).unwrap_or_default();
        if job_id.is_empty() {
            continue;
        }
        let pid = data
            .get("policyId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if pid != policy_id {
            continue;
        }
        let bid = data
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if !backup_id.is_empty() && bid != backup_id {
            continue;
        }
        count += 1;
        let mode = data
            .get("mode")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let phase = data
            .get("phase")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if mode == "required" && !TERMINAL_REPLICATION_PHASES.contains(&phase.as_str()) {
            return true;
        }
        if count >= 500 {
            break;
        }
    }
    false
}

fn list_repair_jobs_readonly(root: &Path) -> Vec<Value> {
    let dir = root.join(".backup-replication").join("repairs");
    if !dir.is_dir() {
        return Vec::new();
    }
    let Ok(read_dir) = fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut paths: Vec<PathBuf> = read_dir
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|s| s.to_str())
                .is_some_and(|n| !n.starts_with('.') && n.to_lowercase().ends_with(".json"))
        })
        .collect();
    paths.sort_by(|a, b| b.cmp(a));
    let mut repairs = Vec::new();
    for p in paths {
        let Ok(raw) = fs::read_to_string(&p) else {
            continue;
        };
        let Ok(data) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        if data
            .get("repairId")
            .and_then(Value::as_str)
            .is_some_and(|id| !id.is_empty())
        {
            repairs.push(data);
            if repairs.len() >= 200 {
                break;
            }
        }
    }
    repairs
}

fn format_days_opt(val: Option<i64>) -> String {
    match val {
        Some(v) => v.to_string(),
        None => "None".to_string(),
    }
}

fn format_py_num(val: &Value) -> String {
    match val {
        Value::Number(num) => {
            if let Some(i) = num.as_i64() {
                i.to_string()
            } else if let Some(f) = num.as_f64() {
                if f.fract() == 0.0 {
                    format!("{f:.1}")
                } else {
                    f.to_string()
                }
            } else {
                val.to_string()
            }
        }
        _ => value_str(val),
    }
}

pub(crate) fn compute_risk_digest(snapshot: &Value) -> String {
    let version = snapshot
        .get("riskSnapshotVersion")
        .cloned()
        .unwrap_or(json!(1));
    let overall = snapshot
        .get("overallRisk")
        .and_then(Value::as_str)
        .unwrap_or("healthy")
        .to_string();
    let raw_risks = snapshot
        .get("risks")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);

    let mut normalized: Vec<(String, String, String, String, Value)> = Vec::new();
    for r in raw_risks {
        let typ = r
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let target = r
            .get("target")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let policy_id = r
            .get("policyId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let severity = r
            .get("severity")
            .and_then(Value::as_str)
            .unwrap_or("healthy")
            .to_string();
        let confidence = r
            .get("confidence")
            .and_then(Value::as_str)
            .unwrap_or("verified")
            .to_string();
        let mut ev: Vec<String> = r
            .get("evidence")
            .and_then(Value::as_array)
            .map(|arr| arr.iter().map(value_str).collect())
            .unwrap_or_default();
        ev.sort();
        let entry = json!({
            "type": typ,
            "target": target,
            "policyId": policy_id,
            "severity": severity,
            "confidence": confidence,
            "evidence": ev,
        });
        normalized.push((severity, typ, target, policy_id, entry));
    }
    normalized.sort_by(|a, b| (&b.0, &b.1, &b.2, &b.3).cmp(&(&a.0, &a.1, &a.2, &a.3)));
    let risks_out: Vec<Value> = normalized.into_iter().map(|item| item.4).collect();
    sha256_compact(&json!({
        "riskSnapshotVersion": version,
        "overallRisk": overall,
        "risks": risks_out,
    }))
}

fn severity_rank(severity: &str) -> i32 {
    match severity {
        "healthy" => 1,
        "warning" => 2,
        "degraded" => 3,
        "critical" => 4,
        "blocked" => 5,
        _ => 1,
    }
}

pub(crate) fn evaluate_risk_snapshot_readonly(
    root: &Path,
    probe: bool,
    now_secs: u64,
    now_iso: &str,
) -> Result<Value, AppError> {
    let control_conn = open_readonly_sqlite(&control_db_path(root))?;
    let dr_conn = open_readonly_sqlite(&dr_db_path(root))?;
    let mut all_risks: Vec<Value> = Vec::new();

    // 1. Capacity risks across targets
    let registered_targets = list_registered_targets()?;
    let target_map: HashMap<String, Value> = registered_targets
        .iter()
        .filter_map(|t| {
            t.get("targetId")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(|id| (id.to_string(), t.clone()))
        })
        .collect();
    let mut effective_targets = vec!["managed-local".to_string()];
    for t in &registered_targets {
        if let Some(id) = t.get("targetId").and_then(Value::as_str) {
            if !id.is_empty() {
                effective_targets.push(id.to_string());
            }
        }
    }
    let mut seen_targets = BTreeSet::new();
    for tid in effective_targets {
        if !seen_targets.insert(tid.clone()) {
            continue;
        }
        let target_val = target_map.get(&tid).cloned().unwrap_or_else(|| json!({}));
        let cap = probe_target_capacity_with_probe(
            &target_val,
            &tid,
            control_conn.as_ref(),
            dr_conn.as_ref(),
            probe,
        )?;
        let horizon = estimate_horizon_readonly(&tid, &cap, control_conn.as_ref())?;
        let free_pct = cap.get("freePercent").and_then(value_as_nonbool_f64);
        let free_bytes = cap.get("freeBytes").cloned().unwrap_or(Value::Null);
        let total_bytes = cap.get("totalBytes").cloned().unwrap_or(Value::Null);
        let days_to_full = horizon
            .get("estimatedDaysToFull")
            .and_then(value_as_nonbool_i64);
        let details = json!({
            "targetId": tid,
            "freePercent": cap.get("freePercent").cloned().unwrap_or(Value::Null),
            "freeBytes": free_bytes,
            "totalBytes": total_bytes,
            "estimatedDaysToFull": days_to_full.map(Value::from).unwrap_or(Value::Null),
            "forecastStatus": horizon.get("forecastStatus").cloned().unwrap_or(Value::Null),
        });
        let Some(pct) = free_pct.filter(|_| !total_bytes.is_null()) else {
            all_risks.push(json!({
                "type": "CAPACITY_EXHAUSTION",
                "target": tid,
                "severity": "healthy",
                "confidence": "high",
                "evidence": ["unconstrained-quota"],
                "details": details,
            }));
            continue;
        };
        let (severity, evidence_line) = if pct < 5.0 || days_to_full.is_some_and(|d| d < 7) {
            (
                "critical",
                format!(
                    "free-space-critical:{pct:.1}%<5.0% or horizon={}d<7d",
                    format_days_opt(days_to_full)
                ),
            )
        } else if pct < 10.0 || days_to_full.is_some_and(|d| d < 30) {
            (
                "degraded",
                format!(
                    "free-space-degraded:{pct:.1}%<10.0% or horizon={}d<30d",
                    format_days_opt(days_to_full)
                ),
            )
        } else if pct <= 20.0 {
            ("warning", format!("free-space-warning:{pct:.1}%<=20.0%"))
        } else {
            ("healthy", format!("free-space-healthy:{pct:.1}%>20.0%"))
        };
        let confidence = if cap.get("observedAt").is_some_and(python_truthy)
            || horizon.get("confidence").and_then(Value::as_str) == Some("high")
        {
            "verified"
        } else {
            "high"
        };
        all_risks.push(json!({
            "type": "CAPACITY_EXHAUSTION",
            "target": tid,
            "severity": severity,
            "confidence": confidence,
            "evidence": [evidence_line],
            "details": details,
        }));
    }

    // 2. Policy replica & failure domain risks
    let policies = list_policies_readonly(root)?;
    let mut seen_policies = BTreeSet::new();
    for pol in &policies {
        let Some(pid) = pol.get("policyId").and_then(Value::as_str) else {
            continue;
        };
        if pid.is_empty() || !seen_policies.insert(pid.to_string()) {
            continue;
        }
        let Some(repl) = pol.get("replication").and_then(Value::as_object) else {
            continue;
        };
        if !repl.get("enabled").is_some_and(python_truthy) {
            continue;
        }
        let min_copies = repl
            .get("minCommittedCopies")
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v != 0)
            .unwrap_or(1) as usize;
        let min_fds = repl
            .get("minFailureDomains")
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v != 0)
            .unwrap_or(1) as usize;
        let Some(latest_pt) = latest_recovery_point(dr_conn.as_ref(), pid)? else {
            continue;
        };
        let backup_id = latest_pt
            .get("backupId")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let copies =
            list_logical_recovery_copies(dr_conn.as_ref(), Some(pid), Some(backup_id), None, 1000)?;
        let committed: Vec<&Value> = copies
            .iter()
            .filter(|c| {
                let st = c
                    .get("status")
                    .or_else(|| c.get("state"))
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_lowercase();
                matches!(st.as_str(), "committed" | "active" | "healthy")
            })
            .collect();
        let mut domains = BTreeSet::new();
        for c in &committed {
            let dom = c
                .get("failureDomain")
                .filter(|v| python_truthy(v))
                .or_else(|| c.get("targetId").filter(|v| python_truthy(v)))
                .map(value_str)
                .unwrap_or_default();
            if !dom.is_empty() {
                domains.insert(dom);
            }
        }
        let (rep_sev, rep_ev) = if committed.len() < min_copies {
            if committed.is_empty() {
                ("critical", format!("zero-committed-copies:0<{min_copies}"))
            } else {
                (
                    "degraded",
                    format!(
                        "insufficient-committed-copies:{}<{min_copies}",
                        committed.len()
                    ),
                )
            }
        } else if has_open_required_jobs(root, pid, backup_id) {
            ("warning", "pending-replication-jobs".to_string())
        } else {
            (
                "healthy",
                format!(
                    "committed-copies-satisfied:{}>={min_copies}",
                    committed.len()
                ),
            )
        };
        all_risks.push(json!({
            "type": "REPLICA_LAG",
            "policyId": pid,
            "backupId": backup_id,
            "severity": rep_sev,
            "confidence": "verified",
            "evidence": [rep_ev],
            "details": {
                "backupId": backup_id,
                "committedCopies": committed.len(),
                "requiredCopies": min_copies,
            },
        }));

        let (fd_sev, fd_ev) = if domains.len() < min_fds {
            let sev = if domains.is_empty() {
                "critical"
            } else {
                "degraded"
            };
            (
                sev,
                format!("failure-domain-deficit:{}<{min_fds}", domains.len()),
            )
        } else {
            (
                "healthy",
                format!("failure-domains-satisfied:{}>={min_fds}", domains.len()),
            )
        };
        all_risks.push(json!({
            "type": "FAILURE_DOMAIN_VIOLATION",
            "policyId": pid,
            "backupId": backup_id,
            "severity": fd_sev,
            "confidence": "verified",
            "evidence": [fd_ev],
            "details": {
                "failureDomainsPresent": domains.into_iter().collect::<Vec<_>>(),
                "requiredFailureDomains": min_fds,
            },
        }));
    }

    // 3. DR Staleness risk
    let drills = load_drill_items(root)?;
    let successful_drills: Vec<&Value> = drills
        .iter()
        .filter(|d| {
            d.get("success") == Some(&Value::Bool(true))
                || matches!(
                    d.get("status")
                        .filter(|v| python_truthy(v))
                        .map(|v| value_str(v).to_lowercase())
                        .as_deref(),
                    Some("pass" | "success")
                )
                || d.get("result")
                    .filter(|v| python_truthy(v))
                    .is_some_and(|v| value_str(v).eq_ignore_ascii_case("success"))
        })
        .collect();
    if let Some(latest_drill) = successful_drills.first() {
        let ts_val = latest_drill
            .get("finishedAt")
            .filter(|v| python_truthy(v))
            .or_else(|| latest_drill.get("startedAt").filter(|v| python_truthy(v)))
            .or_else(|| latest_drill.get("observedAt").filter(|v| python_truthy(v)));
        let age_days = if let Some(epoch) = parse_dr_time(ts_val) {
            (((now_secs as i64 - epoch) as f64) / 86_400.0).max(0.0)
        } else {
            999.0
        };
        let (severity, ev) = if age_days > 30.0 {
            ("critical", format!("dr-drill-stale:{age_days:.1}d>30d"))
        } else if age_days > 7.0 {
            ("warning", format!("dr-drill-warning:{age_days:.1}d>7d"))
        } else {
            ("healthy", format!("dr-drill-fresh:{age_days:.1}d<=7d"))
        };
        let finished_str = latest_drill
            .get("finishedAt")
            .filter(|v| python_truthy(v))
            .or_else(|| latest_drill.get("observedAt").filter(|v| python_truthy(v)))
            .map(value_str)
            .unwrap_or_default();
        all_risks.push(json!({
            "type": "DR_STALENESS",
            "policyId": Value::Null,
            "severity": severity,
            "confidence": "verified",
            "evidence": [ev],
            "details": {
                "lastDrillId": latest_drill.get("drillId").cloned().unwrap_or(Value::Null),
                "lastSuccessfulDrillAgeDays": round2(age_days),
                "finishedAt": finished_str,
            },
        }));
    } else {
        all_risks.push(json!({
            "type": "DR_STALENESS",
            "policyId": Value::Null,
            "severity": "critical",
            "confidence": "verified",
            "evidence": ["no-successful-dr-drills-recorded"],
            "details": {"lastSuccessfulDrillAgeDays": Value::Null},
        }));
    }

    // 4. Restore latency SLA risk
    let slo = build_body_at(now_secs)?;
    let rto = slo.get("rtoSeconds").cloned().unwrap_or_else(|| json!({}));
    let rto_p95_val = rto.get("p95").cloned().unwrap_or(Value::Null);
    let rto_p95 = rto_p95_val.as_f64();
    let success_rate = slo.get("restoreSuccessRate").and_then(value_as_nonbool_f64);
    let (rto_sev, rto_ev) = if let Some(rate) = success_rate.filter(|r| *r < 0.99) {
        (
            "critical",
            format!("restore-success-rate-low:{:.2}%<99%", rate * 100.0),
        )
    } else if rto_p95.is_some_and(|v| v > 1800.0) {
        (
            "degraded",
            format!("rto-p95-exceeded:{}s>1800s", format_py_num(&rto_p95_val)),
        )
    } else if rto_p95.is_some_and(|v| v > 900.0) {
        (
            "warning",
            format!("rto-p95-warning:{}s>900s", format_py_num(&rto_p95_val)),
        )
    } else {
        ("healthy", "restore-latency-within-bounds".to_string())
    };
    all_risks.push(json!({
        "type": "RESTORE_LATENCY_BREACH",
        "policyId": Value::Null,
        "severity": rto_sev,
        "confidence": "high",
        "evidence": [rto_ev],
        "details": {
            "rtoSecondsP50": rto.get("p50").cloned().unwrap_or(Value::Null),
            "rtoSecondsP95": rto_p95_val,
            "rtoSecondsP99": rto.get("p99").cloned().unwrap_or(Value::Null),
            "restoreSuccessRate": slo.get("restoreSuccessRate").cloned().unwrap_or(Value::Null),
        },
    }));

    // 5. Repair backlog risk
    let repairs = list_repair_jobs_readonly(root);
    let open_repairs: Vec<&Value> = repairs
        .iter()
        .filter(|r| {
            let phase = r
                .get("phase")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_lowercase();
            !matches!(phase.as_str(), "complete" | "resolved")
        })
        .collect();
    let failed_count = open_repairs
        .iter()
        .filter(|r| {
            r.get("phase")
                .and_then(Value::as_str)
                .is_some_and(|p| p.eq_ignore_ascii_case("failed"))
        })
        .count();
    let (rep_backlog_sev, rep_backlog_ev) = if failed_count >= 3 {
        (
            "critical",
            format!("excessive-failed-repairs:{failed_count}>=3"),
        )
    } else if failed_count > 0 || open_repairs.len() >= 5 {
        (
            "degraded",
            format!(
                "repair-backlog-accumulating:open={},failed={failed_count}",
                open_repairs.len()
            ),
        )
    } else if !open_repairs.is_empty() {
        (
            "warning",
            format!("repairs-in-progress:open={}", open_repairs.len()),
        )
    } else {
        ("healthy", "no-repair-backlog".to_string())
    };
    all_risks.push(json!({
        "type": "REPAIR_BACKLOG",
        "severity": rep_backlog_sev,
        "confidence": "verified",
        "evidence": [rep_backlog_ev],
        "details": {
            "totalRepairs": repairs.len(),
            "openRepairs": open_repairs.len(),
            "failedRepairs": failed_count,
        },
    }));

    // 6. Authority risk
    let auth_risk = evaluate_authority_risk(control_conn.as_ref())?;
    all_risks.push(auth_risk);

    let mut worst_severity = "healthy";
    let mut worst_order = 1;
    for r in &all_risks {
        let sev = r
            .get("severity")
            .and_then(Value::as_str)
            .unwrap_or("healthy")
            .to_lowercase();
        let order = severity_rank(&sev);
        if order > worst_order {
            worst_order = order;
            worst_severity = match sev.as_str() {
                "blocked" => "blocked",
                "critical" => "critical",
                "degraded" => "degraded",
                "warning" => "warning",
                _ => "healthy",
            };
        }
    }

    let targets_cov: Vec<String> = seen_targets.into_iter().collect();
    let policies_cov: Vec<String> = seen_policies.into_iter().collect();
    let mut snapshot = json!({
        "riskSnapshotVersion": 1,
        "generatedAt": now_iso,
        "overallRisk": worst_severity,
        "risks": all_risks,
        "coverage": {
            "CAPACITY_EXHAUSTION": {"targets": targets_cov, "complete": true},
            "REPLICA_LAG": {"policies": policies_cov, "complete": true},
            "FAILURE_DOMAIN_VIOLATION": {"policies": policies_cov, "complete": true},
            "DR_STALENESS": {"complete": true},
            "RESTORE_LATENCY_BREACH": {"complete": true},
            "REPAIR_BACKLOG": {"complete": true},
            "AUTHORITY_DEGRADATION": {"complete": true},
        },
    });
    let risk_digest = compute_risk_digest(&snapshot);
    snapshot["riskDigest"] = Value::String(risk_digest);
    Ok(snapshot)
}

fn evaluate_authority_risk(control_conn: Option<&Connection>) -> Result<Value, AppError> {
    let mut details = Map::new();
    let Some(conn) = control_conn else {
        return Ok(json!({
            "type": "AUTHORITY_DEGRADATION",
            "severity": "blocked",
            "confidence": "high",
            "evidence": ["authority-db-unreadable:control sqlite not present"],
            "details": {"error": "control sqlite not present"},
        }));
    };
    if !table_exists(conn, "control_authority_head")? {
        return Ok(json!({
            "type": "AUTHORITY_DEGRADATION",
            "severity": "blocked",
            "confidence": "high",
            "evidence": ["authority-db-unreadable:no such table: control_authority_head"],
            "details": {"error": "no such table: control_authority_head"},
        }));
    }
    let head_row = conn
        .query_row(
            "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    let (head_gen, head_digest) = match head_row {
        Some((g, d)) => (json!(g), json!(d)),
        None => (json!(0), Value::Null),
    };
    details.insert("authorityGeneration".to_string(), head_gen);
    details.insert("authorityHeadDigest".to_string(), head_digest);

    let v_res = authority_verify_readonly(Some(conn))?;
    let overall = v_res
        .get("overall")
        .and_then(Value::as_str)
        .unwrap_or("HEALTHY")
        .to_uppercase();
    let issues: Vec<String> = v_res
        .get("issues")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(value_str).collect())
        .unwrap_or_default();
    let replica_count = v_res
        .get("replicas")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    details.insert(
        "authorityVerify".to_string(),
        json!({
            "overall": overall,
            "issues": issues,
            "configuredReplicaCount": v_res.get("health").and_then(|h| h.get("configuredReplicaCount")).cloned().unwrap_or(Value::Null),
            "resolvedReplicaCount": v_res.get("health").and_then(|h| h.get("resolvedReplicaCount")).cloned().unwrap_or(Value::Null),
            "replicaCount": replica_count,
        }),
    );

    let mut evidence: Vec<String> = issues
        .iter()
        .map(|i| format!("authority-issue:{i}"))
        .collect();
    let severity = if overall == "DIVERGENT" {
        evidence.push("authority-cross-replica-fork-detected".to_string());
        "blocked"
    } else if overall == "UNAVAILABLE" || overall == "DURABILITY_UNSATISFIED" {
        evidence.push(format!("authority-{}-critical", overall.to_lowercase()));
        "critical"
    } else if overall == "DEGRADED" {
        evidence.push("authority-degraded-detected".to_string());
        if issues.len() == 1
            && (issues.iter().any(|i| i == "authority-replica-lag-detected")
                || issues.iter().any(|i| i.contains("lag")))
        {
            "warning"
        } else {
            "degraded"
        }
    } else {
        evidence.push("authority-consensus-verified".to_string());
        "healthy"
    };
    Ok(json!({
        "type": "AUTHORITY_DEGRADATION",
        "severity": severity,
        "confidence": "verified",
        "evidence": evidence,
        "details": Value::Object(details),
    }))
}

fn action_target_ids(actions: &[Value]) -> Vec<String> {
    let mut ids = BTreeSet::new();
    for action in actions {
        let params = action.get("parameters").and_then(Value::as_object);
        for cand in [
            params.and_then(|p| p.get("sourceTargetId")),
            params.and_then(|p| p.get("destTargetId")),
            params.and_then(|p| p.get("targetId")),
            action.get("source"),
            action.get("destination"),
            action.get("target"),
        ]
        .into_iter()
        .flatten()
        {
            let trimmed = value_str(cand).trim().to_string();
            if !trimmed.is_empty() {
                ids.insert(trimmed);
            }
        }
    }
    ids.into_iter().collect()
}

fn action_scope(action: &Value) -> (String, BTreeSet<String>, String) {
    let params = action.get("parameters").and_then(Value::as_object);
    let policy_id = params
        .and_then(|p| p.get("policyId"))
        .filter(|v| python_truthy(v))
        .or_else(|| action.get("policyId").filter(|v| python_truthy(v)))
        .map(value_str)
        .unwrap_or_default();
    let targets: BTreeSet<String> = action_target_ids(std::slice::from_ref(action))
        .into_iter()
        .collect();
    let failure_domain = params
        .and_then(|p| p.get("failureDomain"))
        .filter(|v| python_truthy(v))
        .or_else(|| {
            action
                .get("riskSubject")
                .and_then(|rs| rs.get("failureDomain"))
                .filter(|v| python_truthy(v))
        })
        .map(value_str)
        .unwrap_or_default();
    (policy_id, targets, failure_domain)
}

fn read_action_rate_limits(root: &Path) -> Value {
    let mut limits = json!({
        "maxConcurrentActions": 3,
        "maxActionsPerHour": 20,
        "maxConcurrentPerTarget": 2,
        "maxConcurrentPerPolicy": 2,
        "maxSimultaneousFailureDomainsTouched": 1,
        "maxRebalancesPerTargetPerHour": 5,
        "maxDrillsPerPolicyPerDay": 2,
    });
    let file = root
        .join(".resilience-policy")
        .join("autonomous_policy.json");
    if let Ok(raw) = fs::read_to_string(&file) {
        if let Ok(parsed) = serde_json::from_str::<Value>(&raw) {
            if let Some(configured) = parsed.get("rateLimits").and_then(Value::as_object) {
                if let Some(target_map) = limits.as_object_mut() {
                    for (k, v) in configured {
                        if let Some(num) = value_as_nonbool_i64(v).filter(|n| *n > 0) {
                            target_map.insert(k.clone(), json!(num));
                        }
                    }
                }
            }
        }
    }
    limits
}

fn build_budget_snapshot(
    root: &Path,
    actions: &[Value],
    running_effects: &[Value],
) -> Result<Value, AppError> {
    let limits = read_action_rate_limits(root);
    let transfer_state = budget_body()?;
    let global_rate = transfer_state
        .get("globalBytesPerSecond")
        .and_then(Value::as_i64)
        .unwrap_or(200 * 1024 * 1024)
        .max(1);
    let repair_rate = transfer_state
        .get("repairReservedBytesPerSecond")
        .and_then(Value::as_i64)
        .unwrap_or(100 * 1024 * 1024);
    let recovery_rate = transfer_state
        .get("reservedRecoveryBytesPerSecond")
        .and_then(Value::as_i64)
        .unwrap_or(100 * 1024 * 1024);
    let round3 = |x: f64| -> f64 { (x * 1000.0).round() / 1000.0 };
    let repair_pct = round3(100.0 * (repair_rate as f64) / (global_rate as f64));
    let drill_reserve = recovery_rate.min(global_rate / 4);
    let dr_pct = round3(100.0 * (drill_reserve as f64) / (global_rate as f64));
    let opportunistic_pct = round3(100.0 - repair_pct - dr_pct).max(0.0);

    let action_types: Vec<String> = actions
        .iter()
        .map(|a| {
            a.get("type")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_uppercase()
        })
        .collect();
    let repair_actions = action_types
        .iter()
        .filter(|t| *t == "CREATE_REPAIR_JOB")
        .count();
    let drill_actions = action_types
        .iter()
        .filter(|t| *t == "START_DR_DRILL")
        .count();
    let rebalance_actions = action_types
        .iter()
        .filter(|t| *t == "CREATE_REBALANCE_JOB")
        .count();
    let rebalance_blocked = repair_actions > 0 && rebalance_actions > 0;
    let transfer_budget = json!({
        "globalBytesPerSecond": global_rate,
        "repairReservedBytesPerSecond": repair_rate,
        "drDrillReservedBytesPerSecond": drill_reserve,
        "repairReservePercent": repair_pct,
        "drDrillReservePercent": dr_pct,
        "rebalanceOpportunisticPercent": opportunistic_pct,
        "repairActions": repair_actions,
        "drillActions": drill_actions,
        "rebalanceActions": rebalance_actions,
        "rebalanceBlockedByRepairReserve": rebalance_blocked,
    });

    let mut reasons = Vec::new();
    let combined_len = running_effects.len() + actions.len();
    let max_global = limits
        .get("maxConcurrentActions")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if (combined_len as i64) > max_global {
        reasons.push("GLOBAL_CONCURRENCY_EXCEEDED");
    }

    let mut target_counts: BTreeMap<String, i64> = BTreeMap::new();
    let mut policy_counts: BTreeMap<String, i64> = BTreeMap::new();
    let mut failure_domains = BTreeSet::new();
    for action in running_effects.iter().chain(actions.iter()) {
        let (pid, tids, fd) = action_scope(action);
        if !pid.is_empty() {
            *policy_counts.entry(pid).or_insert(0) += 1;
        }
        for tid in tids {
            *target_counts.entry(tid).or_insert(0) += 1;
        }
        if !fd.is_empty() {
            failure_domains.insert(fd);
        }
    }
    let max_target = limits
        .get("maxConcurrentPerTarget")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let max_policy = limits
        .get("maxConcurrentPerPolicy")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let max_fd = limits
        .get("maxSimultaneousFailureDomainsTouched")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if target_counts.values().any(|c| *c > max_target) {
        reasons.push("TARGET_CONCURRENCY_EXCEEDED");
    }
    if policy_counts.values().any(|c| *c > max_policy) {
        reasons.push("POLICY_CONCURRENCY_EXCEEDED");
    }
    if (failure_domains.len() as i64) > max_fd {
        reasons.push("FAILURE_DOMAIN_LIMIT_EXCEEDED");
    }
    if rebalance_blocked {
        reasons.push("TRANSFER_BUDGET_CLASS_CONFLICT");
    }
    let fd_vec: Vec<String> = failure_domains.into_iter().collect();
    Ok(json!({
        "admitted": reasons.is_empty(),
        "reasons": reasons,
        "actionLimits": limits,
        "activeActionCount": running_effects.len(),
        "proposedActionCount": actions.len(),
        "targetCounts": target_counts,
        "policyCounts": policy_counts,
        "failureDomains": fd_vec,
        "transferBudget": transfer_budget,
        "transferState": transfer_state,
    }))
}

fn evaluate_maintenance_window_action(
    control_conn: Option<&Connection>,
    action: &Value,
    now_secs: u64,
) -> Result<Value, AppError> {
    let action_id = action
        .get("actionId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let params = action.get("parameters").and_then(Value::as_object);
    let policy_id = params
        .and_then(|p| p.get("policyId"))
        .filter(|v| python_truthy(v))
        .or_else(|| action.get("policyId").filter(|v| python_truthy(v)))
        .map(value_str)
        .unwrap_or_default();
    let mut raw_window = params
        .and_then(|p| p.get("maintenanceWindow"))
        .filter(|v| v.is_object())
        .cloned()
        .or_else(|| {
            action
                .get("maintenanceWindow")
                .filter(|v| v.is_object())
                .cloned()
        });
    if raw_window.is_none() && !policy_id.is_empty() {
        if let Some(pol) = get_control_policy(control_conn, &policy_id)? {
            raw_window = pol
                .get("placement")
                .and_then(|pl| pl.get("maintenanceWindow"))
                .filter(|v| v.is_object())
                .cloned();
        }
    }
    let Some(win) = raw_window.as_ref().and_then(Value::as_object) else {
        return Ok(json!({
            "actionId": action_id,
            "allowed": true,
            "inWindow": true,
            "override": false,
            "reason": "NO_MAINTENANCE_WINDOW",
        }));
    };

    let action_type = action
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_uppercase();
    let severity = action
        .get("severity")
        .filter(|v| python_truthy(v))
        .or_else(|| action.get("severityBefore").filter(|v| python_truthy(v)))
        .map(|v| value_str(v).to_lowercase())
        .unwrap_or_else(|| "warning".to_string());
    if action_type == "CREATE_REPAIR_JOB" && matches!(severity.as_str(), "critical" | "blocked") {
        return Ok(json!({
            "actionId": action_id,
            "allowed": true,
            "inWindow": false,
            "override": true,
            "reason": "CRITICAL_DURABILITY_OVERRIDE",
        }));
    }
    if action_type == "START_DR_DRILL"
        && (matches!(severity.as_str(), "critical" | "blocked")
            || params.and_then(|p| p.get("drStalenessCritical")) == Some(&Value::Bool(true)))
    {
        return Ok(json!({
            "actionId": action_id,
            "allowed": true,
            "inWindow": false,
            "override": true,
            "reason": "CRITICAL_DR_STALENESS_OVERRIDE",
        }));
    }

    let tz_name = win
        .get("timezone")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| "UTC".to_string());
    let start_text = win
        .get("start")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let end_text = win
        .get("end")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let parse_hm = |text: &str| -> Option<(u32, u32)> {
        let (h, m) = text.split_once(':')?;
        let hh: u32 = h.parse().ok()?;
        let mm: u32 = m.parse().ok()?;
        if hh <= 23 && mm <= 59 {
            Some((hh, mm))
        } else {
            None
        }
    };
    let Ok(tz) = load_iana_timezone(&tz_name) else {
        return Ok(json!({
            "actionId": action_id,
            "allowed": false,
            "inWindow": false,
            "override": false,
            "reason": "INVALID_MAINTENANCE_WINDOW",
            "timezone": tz_name,
        }));
    };
    let (Some((sh, sm)), Some((eh, em))) = (parse_hm(&start_text), parse_hm(&end_text)) else {
        return Ok(json!({
            "actionId": action_id,
            "allowed": false,
            "inWindow": false,
            "override": false,
            "reason": "INVALID_MAINTENANCE_WINDOW",
            "timezone": tz_name,
        }));
    };

    let utc_dt = DateTime::<Utc>::from_timestamp(now_secs as i64, 0).unwrap_or_else(Utc::now);
    let local_dt = tz.to_local(utc_dt);
    let (lh, lm) = tz.local_hour_minute(now_secs as i64);
    let local_min = lh * 60 + lm;
    let start_val = sh * 60 + sm;
    let end_val = eh * 60 + em;
    let inside = if start_val == end_val {
        true
    } else if start_val < end_val {
        start_val <= local_min && local_min < end_val
    } else {
        local_min >= start_val || local_min < end_val
    };
    Ok(json!({
        "actionId": action_id,
        "allowed": inside,
        "inWindow": inside,
        "override": false,
        "reason": if inside { "WITHIN_MAINTENANCE_WINDOW" } else { "OUTSIDE_MAINTENANCE_WINDOW" },
        "timezone": tz_name,
        "localTime": local_dt.format("%Y-%m-%dT%H:%M%:z").to_string(),
        "start": start_text,
        "end": end_text,
    }))
}

pub(crate) fn simulate_coordination_wave(
    control_conn: Option<&Connection>,
    dr_conn: Option<&Connection>,
    actions: &[Value],
    running_actions: &[Value],
) -> Result<Value, AppError> {
    let targets = list_registered_targets().unwrap_or_default();
    let mut target_map = HashMap::new();
    let mut domain_map = HashMap::new();
    for t in &targets {
        if let Some(tid) = t.get("targetId").and_then(Value::as_str) {
            if !tid.is_empty() {
                target_map.insert(tid.to_string(), t.clone());
                let fd = t
                    .get("failureDomain")
                    .filter(|v| python_truthy(v))
                    .map(value_str)
                    .unwrap_or_else(|| tid.to_string());
                domain_map.insert(tid.to_string(), fd);
            }
        }
    }

    let mut combined: Vec<(bool, &Value)> = Vec::new();
    let mut seen_ids = HashSet::new();
    for (is_running, act) in running_actions
        .iter()
        .map(|a| (true, a))
        .chain(actions.iter().map(|a| (false, a)))
    {
        let aid = act
            .get("actionId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| format!("anon-{}", combined.len()));
        if seen_ids.insert(aid) {
            combined.push((is_running, act));
        }
    }

    let mut policies_touched: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (_, act) in &combined {
        let params = act.get("parameters").and_then(Value::as_object);
        let pid = act
            .get("policyId")
            .filter(|v| python_truthy(v))
            .or_else(|| {
                params
                    .and_then(|p| p.get("policyId"))
                    .filter(|v| python_truthy(v))
            })
            .map(value_str)
            .unwrap_or_default();
        let bid = act
            .get("backupId")
            .filter(|v| python_truthy(v))
            .or_else(|| {
                params
                    .and_then(|p| p.get("backupId"))
                    .filter(|v| python_truthy(v))
            })
            .map(value_str)
            .unwrap_or_default();
        if !pid.is_empty() {
            let entry = policies_touched.entry(pid).or_default();
            if !bid.is_empty() {
                entry.insert(bid);
            }
        }
    }

    let mut policy_evals = Map::new();
    let mut all_passed = true;
    let mut failure_reason = String::new();

    for (pid, bids) in policies_touched {
        let pol = get_control_policy(control_conn, &pid)?.unwrap_or_else(|| json!({}));
        let repl = pol.get("replication").and_then(Value::as_object);
        let min_copies = repl
            .and_then(|r| r.get("minCommittedCopies"))
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v != 0)
            .unwrap_or(1) as usize;
        let min_fds = repl
            .and_then(|r| r.get("minFailureDomains"))
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v != 0)
            .unwrap_or(1) as usize;

        let mut effective_bids = bids;
        if effective_bids.is_empty() {
            if let Some(pt) = latest_recovery_point(dr_conn, &pid)? {
                if let Some(b) = pt
                    .get("backupId")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                {
                    effective_bids.insert(b.to_string());
                }
            }
        }

        for bid in effective_bids {
            let raw_copies =
                list_logical_recovery_copies(dr_conn, Some(&pid), Some(&bid), None, 1000)?;
            let committed_before: Vec<&Value> = raw_copies
                .iter()
                .filter(|c| {
                    let st = c
                        .get("status")
                        .or_else(|| c.get("state"))
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_lowercase();
                    matches!(st.as_str(), "committed" | "active" | "healthy")
                })
                .collect();
            let copies_before = committed_before.len();
            let mut domains_before = BTreeSet::new();
            for c in &committed_before {
                let tid = c.get("targetId").map(value_str).unwrap_or_default();
                let dom = domain_map.get(&tid).cloned().unwrap_or_else(|| {
                    c.get("failureDomain")
                        .filter(|v| python_truthy(v))
                        .or_else(|| c.get("targetId").filter(|v| python_truthy(v)))
                        .map(value_str)
                        .unwrap_or_else(|| "default".to_string())
                });
                domains_before.insert(dom);
            }

            let mut transient_copy_loss = 0usize;
            let mut transient_domain_loss = BTreeSet::new();
            let mut added_copies = 0usize;
            let mut added_domains = BTreeSet::new();
            let mut running_count = 0usize;

            for (is_running, act) in &combined {
                let params = act.get("parameters").and_then(Value::as_object);
                let apid = act
                    .get("policyId")
                    .filter(|v| python_truthy(v))
                    .or_else(|| {
                        params
                            .and_then(|p| p.get("policyId"))
                            .filter(|v| python_truthy(v))
                    })
                    .map(value_str)
                    .unwrap_or_default();
                let abid = act
                    .get("backupId")
                    .filter(|v| python_truthy(v))
                    .or_else(|| {
                        params
                            .and_then(|p| p.get("backupId"))
                            .filter(|v| python_truthy(v))
                    })
                    .map(value_str)
                    .unwrap_or_default();
                if apid != pid || (!abid.is_empty() && abid != bid) {
                    continue;
                }
                if *is_running {
                    running_count += 1;
                }
                let atype = act
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_uppercase();
                if atype == "CREATE_REPAIR_JOB" {
                    let dst = params
                        .and_then(|p| p.get("destTargetId"))
                        .filter(|v| python_truthy(v))
                        .or_else(|| act.get("destination").filter(|v| python_truthy(v)))
                        .map(value_str)
                        .unwrap_or_default();
                    added_copies += 1;
                    if !dst.is_empty() {
                        added_domains.insert(domain_map.get(&dst).cloned().unwrap_or(dst));
                    }
                } else if atype == "CREATE_REBALANCE_JOB" {
                    let src = params
                        .and_then(|p| p.get("sourceTargetId"))
                        .filter(|v| python_truthy(v))
                        .or_else(|| act.get("source").filter(|v| python_truthy(v)))
                        .map(value_str)
                        .unwrap_or_default();
                    let dst = params
                        .and_then(|p| p.get("destTargetId"))
                        .filter(|v| python_truthy(v))
                        .or_else(|| act.get("destination").filter(|v| python_truthy(v)))
                        .map(value_str)
                        .unwrap_or_default();
                    let eff_phase = act
                        .get("effectPhase")
                        .filter(|v| python_truthy(v))
                        .or_else(|| {
                            params
                                .and_then(|p| p.get("effectPhase"))
                                .filter(|v| python_truthy(v))
                        })
                        .map(|v| value_str(v).to_lowercase())
                        .unwrap_or_default();
                    let src_unavail = params
                        .and_then(|p| p.get("sourceUnavailableDuring"))
                        .is_some_and(python_truthy)
                        || params
                            .and_then(|p| p.get("removesSourceBeforeCommit"))
                            .is_some_and(python_truthy)
                        || matches!(
                            eff_phase.as_str(),
                            "pruning_source" | "source_removed" | "retirement_pending"
                        );
                    let src_status = target_map
                        .get(&src)
                        .and_then(|t| {
                            t.get("status")
                                .filter(|v| python_truthy(v))
                                .or_else(|| t.get("drainState").filter(|v| python_truthy(v)))
                        })
                        .map(|v| value_str(v).to_lowercase())
                        .unwrap_or_default();
                    if src_unavail
                        || matches!(
                            src_status.as_str(),
                            "draining" | "retiring" | "failed" | "drained"
                        )
                    {
                        transient_copy_loss += 1;
                        if let Some(dom) = domain_map.get(&src) {
                            transient_domain_loss.insert(dom.clone());
                        }
                    }
                    if !dst.is_empty() {
                        added_domains.insert(domain_map.get(&dst).cloned().unwrap_or(dst));
                    }
                }
            }

            let copies_during = copies_before.saturating_sub(transient_copy_loss);
            let domains_during: BTreeSet<String> = domains_before
                .difference(&transient_domain_loss)
                .cloned()
                .collect();
            let copies_after = copies_during + added_copies;
            let domains_after: BTreeSet<String> =
                domains_during.union(&added_domains).cloned().collect();
            let copy_floor = if copies_before >= min_copies {
                min_copies
            } else {
                copies_before
            };
            let domain_floor = if domains_before.len() >= min_fds {
                min_fds
            } else {
                domains_before.len()
            };

            let mut entry = json!({
                "policyId": pid,
                "backupId": bid,
                "minCommittedCopies": min_copies,
                "minFailureDomains": min_fds,
                "copiesBefore": copies_before,
                "copiesDuring": copies_during,
                "copiesAfter": copies_after,
                "failureDomainsBefore": domains_before.into_iter().collect::<Vec<_>>(),
                "failureDomainsDuring": domains_during.iter().cloned().collect::<Vec<_>>(),
                "failureDomainsAfter": domains_after.into_iter().collect::<Vec<_>>(),
                "copySafetyFloor": copy_floor,
                "failureDomainSafetyFloor": domain_floor,
                "runningEffectCount": running_count,
                "passed": true,
            });
            if copies_during < copy_floor {
                let reason =
                    format!("copies-during-below-safety-floor:{copies_during}<{copy_floor}");
                entry["passed"] = Value::Bool(false);
                entry["reason"] = Value::String(reason.clone());
                all_passed = false;
                failure_reason = reason;
            }
            if domains_during.len() < domain_floor {
                let reason = format!(
                    "failure-domains-during-below-safety-floor:{}<{domain_floor}",
                    domains_during.len()
                );
                entry["passed"] = Value::Bool(false);
                entry["reason"] = Value::String(reason.clone());
                all_passed = false;
                failure_reason = reason;
            }
            policy_evals.insert(format!("{pid}:{bid}"), entry);
        }
    }

    let proposed_ids: Vec<String> = actions
        .iter()
        .filter_map(|a| {
            a.get("actionId")
                .filter(|v| python_truthy(v))
                .map(value_str)
        })
        .collect();
    let running_ids: Vec<String> = running_actions
        .iter()
        .filter_map(|a| {
            a.get("actionId")
                .filter(|v| python_truthy(v))
                .map(value_str)
        })
        .collect();

    Ok(json!({
        "passed": all_passed,
        "proposedActionIds": proposed_ids,
        "runningActionIds": running_ids,
        "evaluations": Value::Object(policy_evals),
        "reason": if all_passed {
            "blast-radius-invariants-satisfied".to_string()
        } else {
            failure_reason
        },
    }))
}

fn validate_capacity_snapshot(snapshot: &Value) -> bool {
    let Some(targets) = snapshot.get("targets").and_then(Value::as_array) else {
        return false;
    };
    if targets.is_empty() {
        return false;
    }
    for target in targets {
        let Some(obj) = target.as_object() else {
            return false;
        };
        if obj.get("targetId").filter(|v| python_truthy(v)).is_none()
            || obj.get("observedAt").filter(|v| python_truthy(v)).is_none()
        {
            return false;
        }
        let Some(used) = obj
            .get("usedBytes")
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v >= 0)
        else {
            return false;
        };
        let Some(free) = obj
            .get("freeBytes")
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v >= 0)
        else {
            return false;
        };
        let Some(total) = obj
            .get("totalBytes")
            .and_then(value_as_nonbool_i64)
            .filter(|v| *v >= 0)
        else {
            return false;
        };
        if used.saturating_add(free) > total {
            return false;
        }
        if obj
            .get("source")
            .and_then(Value::as_str)
            .is_some_and(|s| s.eq_ignore_ascii_case("unknown"))
        {
            return false;
        }
    }
    true
}

pub(crate) fn build_fresh_state_bundle_readonly(
    root: &Path,
    wave_actions: &[Value],
    now_secs: u64,
    now_iso: &str,
) -> Result<Value, String> {
    let control_conn = open_readonly_sqlite(&control_db_path(root)).map_err(|e| e.message)?;
    let dr_conn = open_readonly_sqlite(&dr_db_path(root)).map_err(|e| e.message)?;

    let (authority, _) = authority_health_snapshot_with_conn(control_conn.as_ref())
        .map_err(|e| format!("AUTHORITY_OBSERVATION_UNAVAILABLE: {}", e.message))?;
    let head_digest = authority
        .get("canonicalDigest")
        .and_then(Value::as_str)
        .filter(|s| is_plain_sha256(s))
        .ok_or_else(|| "AUTHORITY_OBSERVATION_UNAVAILABLE".to_string())?
        .to_string();

    let risk = evaluate_risk_snapshot_readonly(root, true, now_secs, now_iso)
        .map_err(|e| format!("RISK_SNAPSHOT_UNAVAILABLE: {}", e.message))?;
    let risk_digest = risk
        .get("riskDigest")
        .and_then(Value::as_str)
        .filter(|s| is_plain_sha256(s))
        .ok_or_else(|| "RISK_SNAPSHOT_UNAVAILABLE".to_string())?
        .to_string();
    let coverage_complete = risk
        .get("coverage")
        .and_then(Value::as_object)
        .is_some_and(|cov| {
            !cov.is_empty()
                && cov
                    .values()
                    .all(|v| v.get("complete") == Some(&Value::Bool(true)))
        });
    if !coverage_complete {
        return Err("RISK_SNAPSHOT_INCOMPLETE".to_string());
    }

    let mut target_ids = action_target_ids(wave_actions);
    let registered_targets = list_registered_targets()
        .map_err(|e| format!("CAPACITY_SNAPSHOT_UNAVAILABLE: {}", e.message))?;
    if target_ids.is_empty() {
        let mut reg_ids: BTreeSet<String> = BTreeSet::new();
        for t in &registered_targets {
            if let Some(id) = t
                .get("targetId")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                reg_ids.insert(id.to_string());
            }
        }
        target_ids = reg_ids.into_iter().collect();
    }
    if target_ids.is_empty() {
        return Err("CAPACITY_SNAPSHOT_UNAVAILABLE".to_string());
    }
    let reg_map: HashMap<&str, &Value> = registered_targets
        .iter()
        .filter_map(|t| t.get("targetId").and_then(Value::as_str).map(|id| (id, t)))
        .collect();
    let mut cap_targets = Vec::with_capacity(target_ids.len());
    for tid in &target_ids {
        let target_val = reg_map
            .get(tid.as_str())
            .copied()
            .cloned()
            .unwrap_or_else(|| json!({}));
        let cap = probe_target_capacity_with_probe(
            &target_val,
            tid,
            control_conn.as_ref(),
            dr_conn.as_ref(),
            true,
        )
        .map_err(|e| format!("CAPACITY_SNAPSHOT_UNAVAILABLE: {}", e.message))?;
        cap_targets.push(cap);
    }
    let capacity = json!({"targets": cap_targets});
    if !validate_capacity_snapshot(&capacity) {
        return Err("CAPACITY_SNAPSHOT_UNAVAILABLE".to_string());
    }

    let mut running_effects = Vec::new();
    for state in ACTIVE_ACTION_STATES {
        let batch = list_journal(Some(state.to_string()), None, 500)
            .map_err(|e| format!("RUNNING_EFFECTS_UNAVAILABLE: {}", e.message))?;
        if let Some(arr) = batch.as_array() {
            running_effects.extend(arr.iter().cloned());
        }
    }
    running_effects.sort_by(|a, b| {
        let aid = a.get("actionId").map(value_str).unwrap_or_default();
        let bid = b.get("actionId").map(value_str).unwrap_or_default();
        aid.cmp(&bid)
    });

    let budgets = build_budget_snapshot(root, wave_actions, &running_effects)
        .map_err(|e| format!("BUDGET_SNAPSHOT_UNAVAILABLE: {}", e.message))?;
    if !budgets.get("admitted").is_some_and(Value::is_boolean) {
        return Err("BUDGET_SNAPSHOT_UNAVAILABLE".to_string());
    }

    let mut maintenance = Vec::with_capacity(wave_actions.len());
    for act in wave_actions {
        let dec = evaluate_maintenance_window_action(control_conn.as_ref(), act, now_secs)
            .map_err(|e| format!("MAINTENANCE_DECISION_UNAVAILABLE: {}", e.message))?;
        if !dec.get("allowed").is_some_and(Value::is_boolean) {
            return Err("MAINTENANCE_DECISION_UNAVAILABLE".to_string());
        }
        maintenance.push(dec);
    }

    let blast = simulate_coordination_wave(
        control_conn.as_ref(),
        dr_conn.as_ref(),
        wave_actions,
        &running_effects,
    )
    .map_err(|e| format!("BLAST_SIMULATION_UNAVAILABLE: {}", e.message))?;
    if !blast.get("passed").is_some_and(Value::is_boolean) {
        return Err("BLAST_SIMULATION_UNAVAILABLE".to_string());
    }

    let cap_digest = sha256_compact(&capacity);
    let running_val = Value::Array(running_effects);
    let running_digest = sha256_compact(&running_val);
    let budget_rev = sha256_compact(&budgets);
    let maint_val = Value::Array(maintenance);
    let maint_digest = sha256_compact(&maint_val);
    let blast_digest = sha256_compact(&blast);
    let bundle_digest = sha256_compact(&json!({
        "authorityHeadDigest": head_digest,
        "riskDigest": risk_digest,
        "capacitySnapshotDigest": cap_digest,
        "runningEffectsDigest": running_digest,
        "budgetRevision": budget_rev,
        "maintenanceDecisionDigest": maint_digest,
        "blastSimulationDigest": blast_digest,
        "observedAt": now_iso,
    }));

    Ok(json!({
        "authorityHeadDigest": head_digest,
        "riskDigest": risk_digest,
        "capacitySnapshotDigest": cap_digest,
        "runningEffectsDigest": running_digest,
        "budgetRevision": budget_rev,
        "maintenanceDecisionDigest": maint_digest,
        "blastSimulationDigest": blast_digest,
        "observedAt": now_iso,
        "authorityState": authority,
        "riskSnapshot": risk,
        "capacitySnapshot": capacity,
        "runningEffects": running_val,
        "budgets": budgets,
        "maintenanceDecisions": maint_val,
        "blastSimulation": blast,
        "freshStateBundleDigest": bundle_digest,
    }))
}

fn verify_filesystem_target_readonly(
    workspace_root: &Path,
    record: &Value,
) -> Result<PathBuf, String> {
    let tid = record
        .get("targetId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let path_str = record
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let raw_path = Path::new(path_str);
    if !raw_path.is_absolute() {
        return Err("blocked-target-unavailable: Backup target path must be absolute".to_string());
    }
    if !raw_path.is_dir() {
        return Err(
            "blocked-target-unavailable: Backup target directory does not exist".to_string(),
        );
    }
    let resolved = raw_path
        .canonicalize()
        .map_err(|_| "blocked-target-unavailable: Backup target resolve failed".to_string())?;
    for (label, p) in [
        ("runtime root", workspace_root.to_path_buf()),
        ("restore staging", workspace_root.join(".restore-staging")),
        ("managed backup directory", workspace_root.join(".backups")),
    ] {
        let base = p.canonicalize().unwrap_or(p);
        if resolved == base || resolved.starts_with(&base) {
            return Err(format!(
                "blocked-target-unavailable: Unsafe backup target: target is inside the {label}"
            ));
        }
    }
    verify_registered_target_ready_readonly(tid, &resolved, record)?;
    Ok(resolved)
}

fn collect_filesystem_objects_sorted(base: &Path) -> Result<Vec<(String, PathBuf)>, String> {
    fn walk(base: &Path, dir: &Path, out: &mut Vec<(String, PathBuf)>) -> Result<(), String> {
        let entries = fs::read_dir(dir)
            .map_err(|e| format!("mutation state unavailable: target read error: {e}"))?;
        for entry in entries {
            let entry =
                entry.map_err(|e| format!("mutation state unavailable: target read error: {e}"))?;
            let path = entry.path();
            if path.is_dir() {
                walk(base, &path, out)?;
            } else if path.is_file() {
                let rel = path
                    .strip_prefix(base)
                    .map_err(|e| format!("mutation state unavailable: {e}"))?
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((rel, path));
            }
        }
        Ok(())
    }
    let mut out = Vec::new();
    walk(base, base, &mut out)?;
    out.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(out)
}

fn target_inventory_readonly(
    workspace_root: &Path,
    target_record: &Value,
) -> Result<Value, String> {
    let target_id = target_record
        .get("targetId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let kind = target_record
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("filesystem");
    if kind != "filesystem" {
        return Err(format!(
            "mutation state unavailable: target '{target_id}' kind '{kind}' inventory unavailable offline"
        ));
    }
    let dir = verify_filesystem_target_readonly(workspace_root, target_record)
        .map_err(|e| format!("mutation state unavailable: AppError: {e}"))?;
    let files = collect_filesystem_objects_sorted(&dir)?;
    let mut objects = Vec::with_capacity(files.len());
    for (rel, path) in files {
        let data = fs::read(&path).map_err(|e| format!("mutation state unavailable: io: {e}"))?;
        let digest = sha256_bytes(&data);
        let etag = format!("\"{digest}\"");
        objects.push(json!({
            "key": rel,
            "size": data.len(),
            "etag": etag,
            "sha256": digest,
            "versionId": Value::Null,
            "lastModified": Value::Null,
        }));
    }
    objects.sort_by(|a, b| {
        let ka = a.get("key").and_then(Value::as_str).unwrap_or_default();
        let va = a
            .get("versionId")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let ea = a.get("etag").and_then(Value::as_str).unwrap_or_default();
        let kb = b.get("key").and_then(Value::as_str).unwrap_or_default();
        let vb = b
            .get("versionId")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let eb = b.get("etag").and_then(Value::as_str).unwrap_or_default();
        (ka, va, ea).cmp(&(kb, vb, eb))
    });
    let count = objects.len();
    let digest = sha256_compact(&Value::Array(objects));
    Ok(json!({
        "targetId": target_id,
        "objectCount": count,
        "inventoryDigest": digest,
    }))
}

pub(crate) fn capture_mutation_state(root: &Path) -> Result<Value, String> {
    let control_conn = open_readonly_sqlite(&control_db_path(root)).map_err(|e| e.message)?;
    let targets = list_control_targets(control_conn.as_ref())
        .map_err(|e| format!("mutation state unavailable: {}", e.message))?;
    let by_id: HashMap<&str, &Value> = targets
        .iter()
        .filter_map(|t| t.get("targetId").and_then(Value::as_str).map(|id| (id, t)))
        .collect();
    let mut selected_ids: BTreeSet<String> = BTreeSet::new();
    for t in &targets {
        if let Some(id) = t
            .get("targetId")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            selected_ids.insert(id.to_string());
        }
    }
    let mut inventory = Vec::with_capacity(selected_ids.len());
    for tid in &selected_ids {
        let rec = by_id
            .get(tid.as_str())
            .ok_or_else(|| format!("mutation state unavailable: target '{tid}' not found"))?;
        inventory.push(target_inventory_readonly(root, rec)?);
    }
    let (authority, _) = authority_health_snapshot_with_conn(control_conn.as_ref())
        .map_err(|e| format!("mutation state unavailable: {}", e.message))?;
    let actions = list_journal(None, None, 1_000_000)
        .map_err(|e| format!("mutation state unavailable: {}", e.message))?;
    let policies = list_control_policies(control_conn.as_ref())
        .map_err(|e| format!("mutation state unavailable: {}", e.message))?;

    let inv_val = Value::Array(inventory);
    let pol_val = Value::Array(policies);
    let tgt_val = Value::Array(targets);
    let digests = json!({
        "storage": sha256_compact(&inv_val),
        "authority": sha256_compact(&authority),
        "actionJournal": sha256_compact(&actions),
        "policy": sha256_compact(&pol_val),
        "target": sha256_compact(&tgt_val),
    });
    let state_digest = sha256_compact(&digests);
    let target_ids_vec: Vec<String> = selected_ids.into_iter().collect();
    Ok(json!({
        "stateDigest": state_digest,
        "digests": digests,
        "storageInventory": inv_val,
        "targetIds": target_ids_vec,
        "capturedAt": now_utc_iso(),
    }))
}
