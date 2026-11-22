//! Read-only authoritative resilience state helpers shared across Slice 7 routes.
//!
//! Computes risk snapshots, authority verification, fresh-state bundles, blast
//! wave coordination simulation, and mutation-domain state digests directly from
//! durable control/DR/journal/target state without creating directories or mutating SQLite.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
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
    list_registered_targets, open_readonly_sqlite, parse_python_iso, utc_iso_from_unix,
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

pub(crate) fn control_db_path(root: &Path) -> PathBuf {
    root.join(".backup-control").join("control.sqlite3")
}

pub(crate) fn dr_db_path(root: &Path) -> PathBuf {
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

pub(crate) fn severity_rank(severity: &str) -> i32 {
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
    let (head_gen, head_digest) = if let Some(conn) = control_conn {
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
        match head_row {
            Some((g, d)) => (json!(g), json!(d)),
            None => (json!(0), Value::Null),
        }
    } else {
        (json!(0), Value::Null)
    };
    details.insert("authorityGeneration".to_string(), head_gen);
    details.insert("authorityHeadDigest".to_string(), head_digest);

    let v_res = authority_verify_readonly(control_conn)?;
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
            // `resilience_fresh_state._action_target_ids` is
            // `str(value or "").strip()`, so a JSON null collapses to the empty
            // string and is dropped. `value_str` renders null as the literal
            // "None", which is truthy and would have been kept as a target id.
            if !python_truthy(cand) {
                continue;
            }
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

pub(crate) fn read_action_rate_limits(root: &Path) -> Value {
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
    let inside = match start_val.cmp(&end_val) {
        std::cmp::Ordering::Equal => true,
        std::cmp::Ordering::Less => start_val <= local_min && local_min < end_val,
        std::cmp::Ordering::Greater => local_min >= start_val || local_min < end_val,
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

static HEX_ID_SEQ: AtomicU64 = AtomicU64::new(1);

const RISK_LEDGER_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS resilience_risk_observations (
    risk_subject_digest TEXT PRIMARY KEY,
    subject_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    open_since_at TEXT,
    last_seen_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    current_severity TEXT NOT NULL,
    peak_severity TEXT NOT NULL,
    status TEXT NOT NULL,
    last_cleared_at TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    policy_id TEXT,
    backup_id TEXT,
    target_id TEXT,
    failure_domain TEXT,
    last_snapshot_digest TEXT,
    last_snapshot_observation_key TEXT,
    closure_reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_risk_observations_status
ON resilience_risk_observations(status, open_since_at);
";

const SLO_LEDGER_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS resilience_slo_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL UNIQUE,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    policy_id TEXT,
    action_id TEXT,
    risk_subject_digest TEXT,
    outcome TEXT,
    metadata_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_slo_metric_time
ON resilience_slo_samples(metric_name, observed_at);

CREATE TABLE IF NOT EXISTS resilience_slo_burn_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_key TEXT NOT NULL UNIQUE,
    indicator TEXT NOT NULL,
    bad_units REAL NOT NULL,
    total_units REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resilience_slo_burn_time
ON resilience_slo_burn_observations(indicator, observed_at);
";

const JOURNAL_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS resilience_plans (
    plan_id TEXT PRIMARY KEY,
    plan_version INTEGER NOT NULL,
    input_risk_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    overall_risk TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resilience_actions (
    action_id TEXT PRIMARY KEY,
    plan_id TEXT,
    action_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    input_risk_digest TEXT,
    plan_digest TEXT,
    state TEXT NOT NULL,
    parameters_json TEXT,
    owner_instance_id TEXT,
    lease_until TEXT,
    claim_token TEXT,
    execution_epoch INTEGER NOT NULL DEFAULT 0,
    effect_class TEXT,
    compensation_state TEXT,
    effect_handle_json TEXT,
    risk_subject_json TEXT,
    expected_effect TEXT,
    severity_before TEXT,
    coordination_plan_id TEXT,
    execution_result_json TEXT,
    verification_result_json TEXT,
    decision_proof_json TEXT,
    risk_before_digest TEXT,
    risk_after_digest TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resilience_actions_state ON resilience_actions(state);
CREATE INDEX IF NOT EXISTS idx_resilience_actions_type ON resilience_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_resilience_actions_plan ON resilience_actions(plan_id);

CREATE TABLE IF NOT EXISTS resilience_action_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    owner_instance_id TEXT,
    execution_epoch INTEGER NOT NULL,
    claim_token_sha256 TEXT,
    effect_handle_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resilience_resource_locks (
    lock_key TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    owner_instance_id TEXT,
    acquired_at TEXT NOT NULL,
    lease_until TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resilience_locks_action ON resilience_resource_locks(action_id);
";

pub(crate) const NEVER_AUTONOMOUS: [&str; 4] = [
    "PRIMARY_PROMOTION",
    "POLICY_CHANGE",
    "COPY_DELETION",
    "TOPOLOGY_MUTATION",
];

const DEFAULT_ALLOWED_ACTIONS: [&str; 3] = [
    "CREATE_REPAIR_JOB",
    "CREATE_REBALANCE_JOB",
    "START_DR_DRILL",
];

pub(crate) fn new_hex_id() -> String {
    let seq = HEX_ID_SEQ.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    sha256_bytes(format!("{}:{}:{}", std::process::id(), nanos, seq).as_bytes())
}

fn non_empty_str_field(obj: &Map<String, Value>, key: &str) -> Option<String> {
    obj.get(key)
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
}

pub(crate) fn canonical_risk_subject(risk: &Value) -> Value {
    let Some(obj) = risk.as_object() else {
        return json!({
            "type": "",
            "policyId": Value::Null,
            "backupId": Value::Null,
            "targetId": Value::Null,
            "failureDomain": Value::Null,
        });
    };
    let policy_id = non_empty_str_field(obj, "policyId")
        .map(Value::String)
        .unwrap_or(Value::Null);
    let backup_id = non_empty_str_field(obj, "backupId")
        .map(Value::String)
        .unwrap_or(Value::Null);
    let target_id = non_empty_str_field(obj, "targetId")
        .or_else(|| non_empty_str_field(obj, "target"))
        .map(Value::String)
        .unwrap_or(Value::Null);
    let failure_domain = non_empty_str_field(obj, "failureDomain")
        .map(Value::String)
        .unwrap_or(Value::Null);
    let r_type = non_empty_str_field(obj, "type")
        .unwrap_or_default()
        .to_uppercase();
    json!({
        "type": r_type,
        "policyId": policy_id,
        "backupId": backup_id,
        "targetId": target_id,
        "failureDomain": failure_domain,
    })
}

pub(crate) fn risk_subject_digest(subject_or_risk: &Value) -> String {
    let is_subject = subject_or_risk.as_object().is_some_and(|obj| {
        obj.keys().all(|k| {
            matches!(
                k.as_str(),
                "type" | "policyId" | "backupId" | "targetId" | "failureDomain"
            )
        })
    });
    let subject = if is_subject {
        subject_or_risk.clone()
    } else {
        canonical_risk_subject(subject_or_risk)
    };
    sha256_compact(&subject)
}

fn calculate_grade(score: i64) -> &'static str {
    if score >= 90 {
        "A"
    } else if score >= 80 {
        "B"
    } else if score >= 70 {
        "C"
    } else if score >= 60 {
        "D"
    } else {
        "F"
    }
}

pub(crate) fn calculate_resilience_score_readonly(
    root: &Path,
    now_secs: u64,
    now_iso: &str,
) -> Result<Value, AppError> {
    let control_conn = open_readonly_sqlite(&control_db_path(root))?;
    let dr_conn = open_readonly_sqlite(&dr_db_path(root))?;
    let mut weaknesses: Vec<String> = Vec::new();

    // 1. DR Drill (Weight: 25)
    let drills = load_drill_items(root)?;
    let successful_drills: Vec<&Value> = drills
        .iter()
        .filter(|d| {
            d.get("success") == Some(&Value::Bool(true))
                || d.get("status")
                    .filter(|v| python_truthy(v))
                    .is_some_and(|v| value_str(v).eq_ignore_ascii_case("pass"))
        })
        .collect();
    let (drill_score, drill_status) = if let Some(latest) = successful_drills.first() {
        let ts_val = latest
            .get("finishedAt")
            .filter(|v| python_truthy(v))
            .or_else(|| latest.get("startedAt").filter(|v| python_truthy(v)));
        let age_days = parse_dr_time(ts_val)
            .map(|epoch| ((now_secs as i64 - epoch) as f64) / 86_400.0)
            .unwrap_or(999.0);
        if age_days < 7.0 {
            (25_i64, "healthy")
        } else if age_days <= 30.0 {
            weaknesses.push("dr-drill-older-than-7-days".to_string());
            (15_i64, "warning")
        } else {
            weaknesses.push("dr-drill-stale-over-30-days".to_string());
            (5_i64, "critical")
        }
    } else {
        weaknesses.push("no-successful-dr-drills".to_string());
        (0_i64, "critical")
    };

    // 2. Replica Health (Weight: 25)
    let policies = list_policies_readonly(root)?;
    let mut replica_score = 25_i64;
    let mut replica_status = "healthy";
    for pol in &policies {
        let pid = pol
            .get("policyId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
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
        let Some(latest_pt) = latest_recovery_point(dr_conn.as_ref(), &pid)? else {
            replica_score = replica_score.min(10);
            replica_status = "degraded";
            weaknesses.push(format!("policy-{pid}-no-recovery-point"));
            continue;
        };
        let bid = latest_pt
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let copies =
            list_logical_recovery_copies(dr_conn.as_ref(), Some(&pid), Some(&bid), None, 1000)?;
        let committed_count = copies
            .iter()
            .filter(|c| {
                matches!(
                    c.get("status")
                        .filter(|v| python_truthy(v))
                        .or_else(|| c.get("state").filter(|v| python_truthy(v)))
                        .map(|v| value_str(v).to_lowercase())
                        .as_deref(),
                    Some("committed" | "active" | "healthy")
                )
            })
            .count();
        if committed_count < min_copies {
            if committed_count == 0 {
                replica_score = 0;
                replica_status = "critical";
                weaknesses.push(format!("policy-{pid}-zero-committed-replicas"));
            } else {
                replica_score = replica_score.min(15);
                replica_status = "degraded";
                weaknesses.push(format!("policy-{pid}-missing-required-replicas"));
            }
        }
    }

    // 3. Capacity (Weight: 20)
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
    let mut all_target_ids = vec!["managed-local".to_string()];
    for t in &registered_targets {
        if let Some(id) = t.get("targetId").and_then(Value::as_str) {
            if !id.is_empty() {
                all_target_ids.push(id.to_string());
            }
        }
    }
    let mut capacity_score = 20_i64;
    let mut capacity_status = "healthy";
    for tid in all_target_ids {
        if tid.is_empty() {
            continue;
        }
        let target_val = target_map.get(&tid).cloned().unwrap_or_else(|| json!({}));
        let cap = probe_target_capacity_with_probe(
            &target_val,
            &tid,
            control_conn.as_ref(),
            dr_conn.as_ref(),
            false,
        )?;
        let horizon = estimate_horizon_readonly(&tid, &cap, control_conn.as_ref())?;
        let Some(free_pct) = cap.get("freePercent").and_then(value_as_nonbool_f64) else {
            continue;
        };
        let days_to_full = horizon
            .get("estimatedDaysToFull")
            .and_then(value_as_nonbool_i64);
        if free_pct < 5.0 || days_to_full.is_some_and(|d| d < 7) {
            capacity_score = capacity_score.min(0);
            capacity_status = "critical";
            weaknesses.push(format!("{tid}-capacity-exhaustion-imminent"));
        } else if free_pct < 10.0 || days_to_full.is_some_and(|d| d < 30) {
            capacity_score = capacity_score.min(8);
            capacity_status = "degraded";
            weaknesses.push(format!("{tid}-capacity-under-10-percent"));
        } else if free_pct <= 20.0 {
            capacity_score = capacity_score.min(14);
            if capacity_status == "healthy" {
                capacity_status = "warning";
            }
            weaknesses.push(format!("{tid}-capacity-watermark-warning"));
        }
    }

    // 4. Restore Performance (Weight: 15)
    let slo = build_body_at(now_secs)?;
    let success_rate = slo.get("restoreSuccessRate").and_then(value_as_nonbool_f64);
    let rto_p95_val = slo.get("rtoSecondsP95").cloned().unwrap_or(Value::Null);
    let rto_p95 = value_as_nonbool_f64(&rto_p95_val);
    let (restore_score, restore_status) = if let Some(rate) = success_rate.filter(|r| *r < 0.99) {
        weaknesses.push(format!("restore-success-rate-low:{:.1}%", rate * 100.0));
        (0_i64, "critical")
    } else if rto_p95.is_some_and(|v| v > 1800.0) {
        weaknesses.push(format!("restore-rto-high:{}s", format_py_num(&rto_p95_val)));
        (5_i64, "degraded")
    } else if rto_p95.is_some_and(|v| v > 900.0) {
        weaknesses.push(format!(
            "restore-rto-warning:{}s",
            format_py_num(&rto_p95_val)
        ));
        (10_i64, "warning")
    } else {
        (15_i64, "healthy")
    };

    // 5. Authority Integrity (Weight: 15)
    let (auth_score, auth_status) = match authority_verify_readonly(control_conn.as_ref()) {
        Ok(v_res) => {
            let overall = v_res
                .get("overall")
                .and_then(Value::as_str)
                .unwrap_or("HEALTHY")
                .to_uppercase();
            if overall == "DIVERGENT" {
                weaknesses.push("authority-replica-fork-detected".to_string());
                (0_i64, "blocked")
            } else if overall == "UNAVAILABLE" || overall == "DURABILITY_UNSATISFIED" {
                weaknesses.push(format!("authority-{}", overall.to_lowercase()));
                (5_i64, "degraded")
            } else if overall == "DEGRADED" {
                weaknesses.push("authority-degraded".to_string());
                (10_i64, "warning")
            } else {
                (15_i64, "healthy")
            }
        }
        Err(_) => (10_i64, "warning"),
    };

    let total_score =
        (drill_score + replica_score + capacity_score + restore_score + auth_score).clamp(0, 100);
    let grade = calculate_grade(total_score);

    Ok(json!({
        "score": total_score,
        "grade": grade,
        "factorBreakdown": {
            "drDrill": {"score": drill_score, "max": 25, "status": drill_status},
            "replicaHealth": {"score": replica_score, "max": 25, "status": replica_status},
            "capacity": {"score": capacity_score, "max": 20, "status": capacity_status},
            "restorePerformance": {"score": restore_score, "max": 15, "status": restore_status},
            "authorityIntegrity": {"score": auth_score, "max": 15, "status": auth_status},
        },
        "weaknesses": weaknesses,
        "generatedAt": now_iso,
    }))
}

fn open_slo_rw(root: &Path) -> Result<Connection, AppError> {
    let dir = root.join(".resilience-slo");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let conn = Connection::open(dir.join("slo.sqlite3")).map_err(|_| unhandled_error())?;
    conn.execute_batch("PRAGMA busy_timeout = 30000; PRAGMA journal_mode = WAL;")
        .map_err(|_| unhandled_error())?;
    conn.execute_batch(SLO_LEDGER_SCHEMA)
        .map_err(|_| unhandled_error())?;
    Ok(conn)
}

pub(crate) fn try_record_slo_sample_rw(
    root: &Path,
    metric_name: &str,
    value: f64,
    observed_at: &str,
    digest: Option<&str>,
    sample_key: &str,
) {
    let name = metric_name.trim();
    if name.is_empty() || !value.is_finite() || value < 0.0 {
        return;
    }
    let Ok(conn) = open_slo_rw(root) else {
        return;
    };
    let _ = conn.execute(
        "INSERT OR IGNORE INTO resilience_slo_samples (
            sample_key, metric_name, value, policy_id, action_id,
            risk_subject_digest, outcome, metadata_json, observed_at
         ) VALUES (?, ?, ?, NULL, NULL, ?, NULL, '{}', ?)",
        rusqlite::params![sample_key, name, value, digest, observed_at],
    );
}

pub(crate) fn try_record_slo_burn_rw(
    root: &Path,
    indicator: &str,
    bad: f64,
    total: f64,
    started_at: &str,
    observed_at: &str,
    key: &str,
) {
    let name = indicator.trim();
    if name.is_empty()
        || !bad.is_finite()
        || !total.is_finite()
        || bad < 0.0
        || total < 0.0
        || bad > total
    {
        return;
    }
    let Ok(conn) = open_slo_rw(root) else {
        return;
    };
    let _ = conn.execute(
        "INSERT OR IGNORE INTO resilience_slo_burn_observations (
            observation_key, indicator, bad_units, total_units,
            metadata_json, started_at, observed_at
         ) VALUES (?, ?, ?, ?, '{}', ?, ?)",
        rusqlite::params![key, name, bad, total, started_at, observed_at],
    );
}

fn risk_db_path(root: &Path) -> PathBuf {
    root.join(".resilience-risk").join("risk.sqlite3")
}

fn ensure_risk_columns(conn: &Connection) -> Result<(), AppError> {
    let mut stmt = conn
        .prepare("PRAGMA table_info(resilience_risk_observations)")
        .map_err(|_| unhandled_error())?;
    let cols: HashSet<String> = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|_| unhandled_error())?
        .flatten()
        .collect();
    if !cols.contains("last_snapshot_observation_key") {
        conn.execute(
            "ALTER TABLE resilience_risk_observations ADD COLUMN last_snapshot_observation_key TEXT",
            [],
        )
        .map_err(|_| unhandled_error())?;
    }
    if !cols.contains("closure_reason") {
        conn.execute(
            "ALTER TABLE resilience_risk_observations ADD COLUMN closure_reason TEXT",
            [],
        )
        .map_err(|_| unhandled_error())?;
    }
    Ok(())
}

fn open_risk_ledger_rw(root: &Path) -> Result<Connection, AppError> {
    let dir = root.join(".resilience-risk");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let conn = Connection::open(dir.join("risk.sqlite3")).map_err(|_| unhandled_error())?;
    conn.execute_batch("PRAGMA busy_timeout = 30000; PRAGMA journal_mode = WAL;")
        .map_err(|_| unhandled_error())?;
    conn.execute_batch(RISK_LEDGER_SCHEMA)
        .map_err(|_| unhandled_error())?;
    ensure_risk_columns(&conn)?;
    Ok(conn)
}

#[derive(Clone)]
struct RiskObsRow {
    risk_subject_digest: String,
    subject_json: String,
    first_seen_at: String,
    open_since_at: Option<String>,
    last_seen_at: String,
    observation_count: i64,
    current_severity: String,
    peak_severity: String,
    status: String,
    last_cleared_at: Option<String>,
    reopen_count: i64,
    last_snapshot_observation_key: Option<String>,
}

impl RiskObsRow {
    fn from_row(row: &rusqlite::Row<'_>) -> Result<Self, rusqlite::Error> {
        Ok(Self {
            risk_subject_digest: row.get(0)?,
            subject_json: row.get(1)?,
            first_seen_at: row.get(2)?,
            open_since_at: row.get(3)?,
            last_seen_at: row.get(4)?,
            observation_count: row.get(5)?,
            current_severity: row.get(6)?,
            peak_severity: row.get(7)?,
            status: row.get(8)?,
            last_cleared_at: row.get(9)?,
            reopen_count: row.get(10)?,
            last_snapshot_observation_key: row.get(11)?,
        })
    }
}

fn risk_obs_row_to_value(conn: &Connection, digest: &str) -> Result<Value, AppError> {
    let row = conn
        .query_row(
            "SELECT risk_subject_digest, subject_json, first_seen_at, open_since_at,
                    last_seen_at, observation_count, current_severity, peak_severity,
                    status, last_cleared_at, reopen_count, policy_id, backup_id,
                    target_id, failure_domain, last_snapshot_digest,
                    last_snapshot_observation_key, closure_reason, updated_at
             FROM resilience_risk_observations WHERE risk_subject_digest = ?",
            [digest],
            |r| {
                let subj_raw: String = r.get(1)?;
                let subj: Value = serde_json::from_str(&subj_raw).unwrap_or_else(|_| json!({}));
                Ok(json!({
                    "riskSubjectDigest": r.get::<_, String>(0)?,
                    "riskSubject": subj,
                    "firstSeenAt": r.get::<_, String>(2)?,
                    "openSinceAt": r.get::<_, Option<String>>(3)?,
                    "lastSeenAt": r.get::<_, String>(4)?,
                    "observationCount": r.get::<_, i64>(5)?,
                    "currentSeverity": r.get::<_, String>(6)?,
                    "peakSeverity": r.get::<_, String>(7)?,
                    "status": r.get::<_, String>(8)?,
                    "lastClearedAt": r.get::<_, Option<String>>(9)?,
                    "reopenCount": r.get::<_, i64>(10)?,
                    "policyId": r.get::<_, Option<String>>(11)?,
                    "backupId": r.get::<_, Option<String>>(12)?,
                    "targetId": r.get::<_, Option<String>>(13)?,
                    "failureDomain": r.get::<_, Option<String>>(14)?,
                    "lastSnapshotDigest": r.get::<_, Option<String>>(15)?,
                    "lastSnapshotObservationKey": r.get::<_, Option<String>>(16)?,
                    "closureReason": r.get::<_, Option<String>>(17)?,
                    "updatedAt": r.get::<_, String>(18)?,
                }))
            },
        )
        .map_err(|_| unhandled_error())?;
    Ok(row)
}

pub(crate) fn is_open_severity(sev: &str) -> bool {
    matches!(sev, "warning" | "degraded" | "critical" | "blocked")
}

pub(crate) fn parse_utc_epoch(raw: Option<&str>) -> Option<i64> {
    let text = raw.filter(|s| !s.is_empty())?;
    parse_python_iso(text).ok().flatten()
}

fn infer_absent_closure_reason(
    subject: &Value,
    coverage: Option<&Value>,
) -> (&'static str, &'static str) {
    let r_type = subject
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_uppercase();
    let entry = coverage
        .and_then(Value::as_object)
        .and_then(|c| c.get(&r_type))
        .and_then(Value::as_object);
    let Some(entry_obj) = entry else {
        return ("UNKNOWN_COVERAGE", "UNKNOWN_COVERAGE");
    };
    if entry_obj.get("complete") != Some(&Value::Bool(true)) {
        return ("UNKNOWN_COVERAGE", "UNKNOWN_COVERAGE");
    }
    let collect_set = |key: &str| -> HashSet<String> {
        entry_obj
            .get(key)
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter(|v| python_truthy(v))
                    .map(value_str)
                    .filter(|s| !s.is_empty())
                    .collect()
            })
            .unwrap_or_default()
    };
    let targets = collect_set("targets");
    let policies = collect_set("policies");
    let backups = collect_set("backups");
    let target_id = subject
        .get("targetId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let policy_id = subject
        .get("policyId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let backup_id = subject
        .get("backupId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();

    if targets.is_empty() && policies.is_empty() && backups.is_empty() {
        return ("RETIRED", "SCOPE_RETIRED");
    }
    if !policy_id.is_empty() && !policies.is_empty() && !policies.contains(&policy_id) {
        return ("RETIRED", "POLICY_DISABLED");
    }
    if !target_id.is_empty() && !targets.is_empty() && !targets.contains(&target_id) {
        return ("RETIRED", "TARGET_REMOVED");
    }
    if !backup_id.is_empty()
        && ((!backups.is_empty() && !backups.contains(&backup_id))
            || (!policy_id.is_empty() && (policies.is_empty() || policies.contains(&policy_id))))
    {
        return ("SUPERSEDED", "SUPERSEDED_BACKUP");
    }
    if !policy_id.is_empty() && policies.is_empty() && backups.is_empty() && targets.is_empty() {
        return ("RETIRED", "POLICY_DISABLED");
    }
    ("CLEARED", "HEALTHY")
}

struct RiskUpsertParams<'a> {
    digest: &'a str,
    subject: &'a Value,
    first_seen: &'a str,
    open_since: Option<&'a str>,
    last_seen: &'a str,
    count: i64,
    severity: &'a str,
    peak: &'a str,
    status: &'a str,
    last_cleared: Option<&'a str>,
    reopen_count: i64,
    snapshot_digest: &'a str,
    snapshot_observation_key: &'a str,
    closure_reason: Option<&'a str>,
    updated_at: &'a str,
}

fn upsert_risk_observation_row(
    conn: &Connection,
    p: &RiskUpsertParams<'_>,
) -> Result<Value, AppError> {
    let subj_json = dumps_compact(p.subject);
    let subj_obj = p.subject.as_object();
    let policy_id = subj_obj.and_then(|o| o.get("policyId").and_then(Value::as_str));
    let backup_id = subj_obj.and_then(|o| o.get("backupId").and_then(Value::as_str));
    let target_id = subj_obj.and_then(|o| o.get("targetId").and_then(Value::as_str));
    let failure_domain = subj_obj.and_then(|o| o.get("failureDomain").and_then(Value::as_str));
    conn.execute(
        "INSERT INTO resilience_risk_observations (
            risk_subject_digest, subject_json, first_seen_at, open_since_at,
            last_seen_at, observation_count, current_severity, peak_severity,
            status, last_cleared_at, reopen_count, policy_id, backup_id,
            target_id, failure_domain, last_snapshot_digest,
            last_snapshot_observation_key, closure_reason, updated_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(risk_subject_digest) DO UPDATE SET
            subject_json = excluded.subject_json,
            open_since_at = excluded.open_since_at,
            last_seen_at = excluded.last_seen_at,
            observation_count = excluded.observation_count,
            current_severity = excluded.current_severity,
            peak_severity = excluded.peak_severity,
            status = excluded.status,
            last_cleared_at = excluded.last_cleared_at,
            reopen_count = excluded.reopen_count,
            policy_id = excluded.policy_id,
            backup_id = excluded.backup_id,
            target_id = excluded.target_id,
            failure_domain = excluded.failure_domain,
            last_snapshot_digest = excluded.last_snapshot_digest,
            last_snapshot_observation_key = excluded.last_snapshot_observation_key,
            closure_reason = excluded.closure_reason,
            updated_at = excluded.updated_at",
        rusqlite::params![
            p.digest,
            subj_json,
            p.first_seen,
            p.open_since,
            p.last_seen,
            p.count,
            p.severity,
            p.peak,
            p.status,
            p.last_cleared,
            p.reopen_count,
            policy_id,
            backup_id,
            target_id,
            failure_domain,
            p.snapshot_digest,
            p.snapshot_observation_key,
            p.closure_reason,
            p.updated_at,
        ],
    )
    .map_err(|_| unhandled_error())?;
    risk_obs_row_to_value(conn, p.digest)
}

pub(crate) fn observe_risk_snapshot_and_slo(
    root: &Path,
    snapshot: &mut Value,
    now_secs: u64,
    now_iso: &str,
) -> Result<(), AppError> {
    if let Some(risks_arr) = snapshot.get("risks").and_then(Value::as_array) {
        for r in risks_arr {
            if r.get("type").and_then(Value::as_str) == Some("DR_STALENESS") {
                if let Some(age_days) = r
                    .get("details")
                    .and_then(|d| d.get("lastSuccessfulDrillAgeDays"))
                    .and_then(value_as_nonbool_f64)
                {
                    let key = format!("dr-readiness:{now_iso}");
                    try_record_slo_sample_rw(
                        root,
                        "dr_readiness_age_hours",
                        (age_days * 24.0).max(0.0),
                        now_iso,
                        None,
                        &key,
                    );
                }
            }
        }
    }

    let snapshot_digest = snapshot
        .get("riskDigest")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let snapshot_generated_at = snapshot
        .get("generatedAt")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(now_iso)
        .to_string();
    let snapshot_obs_key = sha256_compact(&json!({
        "generatedAt": snapshot_generated_at,
        "riskDigest": snapshot_digest,
    }));

    let raw_risks = snapshot
        .get("risks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let coverage = snapshot.get("coverage").cloned();

    let conn = open_risk_ledger_rw(root)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;

    let mut slo_samples: Vec<(&'static str, f64, String, String)> = Vec::new();
    let mut burn_obs: Vec<(&'static str, f64, f64, String, String)> = Vec::new();
    let mut records_by_digest: HashMap<String, Value> = HashMap::new();
    let mut present_digests: HashSet<String> = HashSet::new();

    let tx_res: Result<(), AppError> = (|| {
        for raw_risk in &raw_risks {
            let Some(risk_obj) = raw_risk.as_object() else {
                continue;
            };
            let subject = canonical_risk_subject(raw_risk);
            let digest = risk_subject_digest(&subject);
            present_digests.insert(digest.clone());
            let severity = risk_obj
                .get("severity")
                .filter(|v| python_truthy(v))
                .map(|v| value_str(v).to_lowercase())
                .unwrap_or_else(|| "healthy".to_string());
            let is_open = is_open_severity(&severity);

            let existing = conn
                .query_row(
                    "SELECT risk_subject_digest, subject_json, first_seen_at, open_since_at,
                            last_seen_at, observation_count, current_severity, peak_severity,
                            status, last_cleared_at, reopen_count, last_snapshot_observation_key
                     FROM resilience_risk_observations WHERE risk_subject_digest = ?",
                    [&digest],
                    RiskObsRow::from_row,
                )
                .optional()
                .map_err(|_| unhandled_error())?;

            if existing.is_none() && !is_open {
                continue;
            }
            if existing
                .as_ref()
                .and_then(|ex| ex.last_snapshot_observation_key.as_deref())
                == Some(&snapshot_obs_key)
            {
                records_by_digest.insert(digest.clone(), risk_obs_row_to_value(&conn, &digest)?);
                continue;
            }

            let detected_str = risk_obj
                .get("detectedAt")
                .or_else(|| risk_obj.get("observedAt"))
                .or_else(|| risk_obj.get("generatedAt"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .or_else(|| Some(snapshot_generated_at.clone()));
            let detected_epoch = parse_utc_epoch(detected_str.as_deref());

            let rec = match existing {
                None => {
                    if let Some(det) = detected_epoch {
                        let lat_ms = (((now_secs as i64 - det) as f64) * 1000.0).max(0.0);
                        slo_samples.push((
                            "risk_detection_latency_ms",
                            lat_ms,
                            format!("risk-detected:{digest}:{now_iso}"),
                            digest.clone(),
                        ));
                    }
                    upsert_risk_observation_row(
                        &conn,
                        &RiskUpsertParams {
                            digest: &digest,
                            subject: &subject,
                            first_seen: now_iso,
                            open_since: Some(now_iso),
                            last_seen: now_iso,
                            count: 1,
                            severity: &severity,
                            peak: &severity,
                            status: "OPEN",
                            last_cleared: None,
                            reopen_count: 0,
                            snapshot_digest: &snapshot_digest,
                            snapshot_observation_key: &snapshot_obs_key,
                            closure_reason: None,
                            updated_at: now_iso,
                        },
                    )?
                }
                Some(ex) => {
                    // `observe_risk_snapshot` returns the stored row untouched
                    // when the incoming snapshot key matches the last one, so a
                    // repeated assess/plan inside the same second does not
                    // inflate the count:
                    //
                    //   if existing["last_snapshot_observation_key"] == snapshot_observation_key:
                    //       records.append(_row_to_record(existing)); continue
                    if ex.last_snapshot_observation_key.as_deref()
                        == Some(snapshot_obs_key.as_str())
                    {
                        records_by_digest
                            .insert(digest.clone(), risk_obs_row_to_value(&conn, &digest)?);
                        continue;
                    }
                    let count = ex.observation_count + 1;
                    let peak = if severity_rank(&severity) > severity_rank(&ex.peak_severity) {
                        severity.clone()
                    } else {
                        ex.peak_severity.clone()
                    };
                    let mut reopen_count = ex.reopen_count;
                    let (status, open_since, last_cleared, closure_reason): (
                        &str,
                        Option<String>,
                        Option<String>,
                        Option<&str>,
                    ) = if is_open
                        && matches!(ex.status.as_str(), "CLEARED" | "SUPERSEDED" | "RETIRED")
                    {
                        reopen_count += 1;
                        if let Some(det) = detected_epoch {
                            let lat_ms = (((now_secs as i64 - det) as f64) * 1000.0).max(0.0);
                            slo_samples.push((
                                "risk_detection_latency_ms",
                                lat_ms,
                                format!("risk-reopened:{digest}:{now_iso}"),
                                digest.clone(),
                            ));
                        }
                        (
                            "REOPENED",
                            Some(now_iso.to_string()),
                            ex.last_cleared_at.clone(),
                            None,
                        )
                    } else if is_open {
                        let st = if ex.status == "OPEN" || ex.status == "REOPENED" {
                            ex.status.as_str()
                        } else {
                            "OPEN"
                        };
                        let since = ex
                            .open_since_at
                            .clone()
                            .unwrap_or_else(|| now_iso.to_string());
                        (st, Some(since), ex.last_cleared_at.clone(), None)
                    } else {
                        if let Some(open_ep) = parse_utc_epoch(ex.open_since_at.as_deref()) {
                            let lat_ms = (((now_secs as i64 - open_ep) as f64) * 1000.0).max(0.0);
                            slo_samples.push((
                                "risk_clear_latency_ms",
                                lat_ms,
                                format!("risk-cleared:{digest}:{now_iso}"),
                                digest.clone(),
                            ));
                        }
                        ("CLEARED", None, Some(now_iso.to_string()), Some("HEALTHY"))
                    };

                    if let Some(prev_ep) = parse_utc_epoch(Some(&ex.last_seen_at)) {
                        let elapsed_min = (((now_secs as i64 - prev_ep) as f64) / 60.0).max(0.0);
                        if elapsed_min > 0.0 {
                            let prev_sev = ex.current_severity.to_lowercase();
                            let bad_crit = if matches!(prev_sev.as_str(), "critical" | "blocked") {
                                elapsed_min
                            } else {
                                0.0
                            };
                            burn_obs.push((
                                "critical_durability_risk_minutes",
                                bad_crit,
                                elapsed_min,
                                format!("critical-risk:{digest}:{now_iso}"),
                                ex.last_seen_at.clone(),
                            ));
                            if subject
                                .get("type")
                                .and_then(Value::as_str)
                                .is_some_and(|t| t.contains("DR"))
                            {
                                let bad_dr = if is_open_severity(&prev_sev) {
                                    elapsed_min
                                } else {
                                    0.0
                                };
                                burn_obs.push((
                                    "dr_stale_minutes",
                                    bad_dr,
                                    elapsed_min,
                                    format!("dr-stale:{digest}:{now_iso}"),
                                    ex.last_seen_at.clone(),
                                ));
                            }
                        }
                    }

                    upsert_risk_observation_row(
                        &conn,
                        &RiskUpsertParams {
                            digest: &digest,
                            subject: &subject,
                            first_seen: &ex.first_seen_at,
                            open_since: open_since.as_deref(),
                            last_seen: now_iso,
                            count,
                            severity: &severity,
                            peak: &peak,
                            status,
                            last_cleared: last_cleared.as_deref(),
                            reopen_count,
                            snapshot_digest: &snapshot_digest,
                            snapshot_observation_key: &snapshot_obs_key,
                            closure_reason,
                            updated_at: now_iso,
                        },
                    )?
                }
            };
            records_by_digest.insert(digest, rec);
        }

        let mut absent_stmt = conn
            .prepare(
                "SELECT risk_subject_digest, subject_json, first_seen_at, open_since_at,
                        last_seen_at, observation_count, current_severity, peak_severity,
                        status, last_cleared_at, reopen_count, last_snapshot_observation_key
                 FROM resilience_risk_observations
                 WHERE status IN ('OPEN', 'REOPENED', 'UNKNOWN_COVERAGE')",
            )
            .map_err(|_| unhandled_error())?;
        let absent_rows: Vec<RiskObsRow> = absent_stmt
            .query_map([], RiskObsRow::from_row)
            .map_err(|_| unhandled_error())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| unhandled_error())?;
        drop(absent_stmt);

        for ex in absent_rows {
            if present_digests.contains(&ex.risk_subject_digest) {
                continue;
            }
            if ex.last_snapshot_observation_key.as_deref() == Some(&snapshot_obs_key) {
                let rec = risk_obs_row_to_value(&conn, &ex.risk_subject_digest)?;
                records_by_digest.insert(ex.risk_subject_digest.clone(), rec);
                continue;
            }
            let subject: Value =
                serde_json::from_str(&ex.subject_json).unwrap_or_else(|_| json!({}));
            let (st, reason) = infer_absent_closure_reason(&subject, coverage.as_ref());
            let closed = matches!(st, "CLEARED" | "SUPERSEDED" | "RETIRED");
            let open_since = if closed {
                None
            } else {
                ex.open_since_at.as_deref()
            };
            let last_cleared = if closed {
                Some(now_iso)
            } else {
                ex.last_cleared_at.as_deref()
            };
            let rec = upsert_risk_observation_row(
                &conn,
                &RiskUpsertParams {
                    digest: &ex.risk_subject_digest,
                    subject: &subject,
                    first_seen: &ex.first_seen_at,
                    open_since,
                    last_seen: &ex.last_seen_at,
                    count: ex.observation_count,
                    severity: &ex.current_severity,
                    peak: &ex.peak_severity,
                    status: st,
                    last_cleared,
                    reopen_count: ex.reopen_count,
                    snapshot_digest: &snapshot_digest,
                    snapshot_observation_key: &snapshot_obs_key,
                    closure_reason: Some(reason),
                    updated_at: now_iso,
                },
            )?;
            records_by_digest.insert(ex.risk_subject_digest, rec);
        }
        Ok(())
    })();

    if let Err(err) = tx_res {
        let _ = conn.execute_batch("ROLLBACK;");
        return Err(err);
    }
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;

    for (metric, val, key, dig) in slo_samples {
        try_record_slo_sample_rw(root, metric, val, now_iso, Some(&dig), &key);
    }
    for (ind, bad, total, key, started) in burn_obs {
        try_record_slo_burn_rw(root, ind, bad, total, &started, now_iso, &key);
    }

    if let Some(risks_mut) = snapshot.get_mut("risks").and_then(Value::as_array_mut) {
        for risk in risks_mut.iter_mut() {
            let subject = canonical_risk_subject(risk);
            let digest = risk_subject_digest(&subject);
            if let Some(obj) = risk.as_object_mut() {
                obj.insert("riskSubject".to_string(), subject);
                obj.insert(
                    "riskSubjectDigest".to_string(),
                    Value::String(digest.clone()),
                );
                if let Some(obs) = records_by_digest.get(&digest) {
                    obj.insert("riskObservation".to_string(), obs.clone());
                }
            }
        }
    }
    Ok(())
}

pub(crate) fn read_autonomous_action_policy(root: &Path) -> Value {
    let mut base = json!({
        "automationPolicyVersion": 1,
        "allowedActions": DEFAULT_ALLOWED_ACTIONS,
        "approvalRequired": NEVER_AUTONOMOUS,
        "rateLimits": read_action_rate_limits(root),
        "enabled": true,
    });
    let file = root
        .join(".resilience-policy")
        .join("autonomous_policy.json");
    if let Ok(raw) = fs::read_to_string(&file) {
        if let Ok(Value::Object(custom)) = serde_json::from_str::<Value>(&raw) {
            if let Some(obj) = base.as_object_mut() {
                for (k, v) in custom {
                    obj.insert(k, v);
                }
            }
        }
    }
    base
}

pub(crate) fn is_action_autonomous(act_type: &str, policy: &Value) -> bool {
    let upper = act_type.to_uppercase();
    if NEVER_AUTONOMOUS.contains(&upper.as_str()) {
        return false;
    }
    if policy.get("enabled") == Some(&Value::Bool(false)) {
        return false;
    }
    let approval_has = policy
        .get("approvalRequired")
        .and_then(Value::as_array)
        .is_some_and(|arr| arr.iter().any(|v| v.as_str().is_some_and(|s| s == upper)));
    if approval_has {
        return false;
    }
    if let Some(allowed) = policy.get("allowedActions").and_then(Value::as_array) {
        return allowed
            .iter()
            .any(|v| v.as_str().is_some_and(|s| s == upper));
    }
    DEFAULT_ALLOWED_ACTIONS.contains(&upper.as_str())
}

fn risk_lifecycle_fields_readonly(
    risk_conn: Option<&Connection>,
    risk: &Value,
) -> Map<String, Value> {
    let subject = canonical_risk_subject(risk);
    let digest = risk_subject_digest(&subject);
    let mut out = Map::new();
    out.insert(
        "riskSubjectDigest".to_string(),
        Value::String(digest.clone()),
    );
    let Some(conn) = risk_conn else {
        return out;
    };
    if !table_exists(conn, "resilience_risk_observations").unwrap_or(false) {
        return out;
    }
    let Ok(obs) = risk_obs_row_to_value(conn, &digest) else {
        return out;
    };
    let open_since = obs
        .get("openSinceAt")
        .filter(|v| !v.is_null())
        .or_else(|| obs.get("firstSeenAt"))
        .cloned()
        .unwrap_or(Value::Null);
    out.insert("riskFirstSeenAt".to_string(), open_since);
    out.insert(
        "riskLifecycleFirstSeenAt".to_string(),
        obs.get("firstSeenAt").cloned().unwrap_or(Value::Null),
    );
    out.insert(
        "riskLastSeenAt".to_string(),
        obs.get("lastSeenAt").cloned().unwrap_or(Value::Null),
    );
    out.insert(
        "riskObservationCount".to_string(),
        obs.get("observationCount").cloned().unwrap_or(Value::Null),
    );
    out.insert(
        "riskLifecycleStatus".to_string(),
        obs.get("status").cloned().unwrap_or(Value::Null),
    );
    out.insert(
        "riskReopenCount".to_string(),
        obs.get("reopenCount").cloned().unwrap_or(Value::Null),
    );
    out
}

pub(crate) fn compute_plan_digest(plan: &Value) -> String {
    let plan_version = plan
        .get("planVersion")
        .and_then(value_as_nonbool_i64)
        .unwrap_or(1);
    let input_risk_digest = plan
        .get("inputRiskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let mut actions: Vec<(String, String, String, String, String, String, Value)> = plan
        .get("actions")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|a| {
                    let params = a.get("parameters").and_then(Value::as_object);
                    let act_type = a
                        .get("type")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let source = a
                        .get("source")
                        .filter(|v| python_truthy(v))
                        .or_else(|| {
                            params
                                .and_then(|p| p.get("sourceTargetId"))
                                .filter(|v| python_truthy(v))
                        })
                        .map(value_str)
                        .unwrap_or_default();
                    let destination = a
                        .get("destination")
                        .filter(|v| python_truthy(v))
                        .or_else(|| {
                            params
                                .and_then(|p| p.get("destTargetId"))
                                .filter(|v| python_truthy(v))
                        })
                        .map(value_str)
                        .unwrap_or_default();
                    let target = a
                        .get("target")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let policy_id = a
                        .get("policyId")
                        .filter(|v| python_truthy(v))
                        .or_else(|| {
                            params
                                .and_then(|p| p.get("policyId"))
                                .filter(|v| python_truthy(v))
                        })
                        .map(value_str)
                        .unwrap_or_default();
                    let backup_id = a
                        .get("backupId")
                        .filter(|v| python_truthy(v))
                        .or_else(|| {
                            params
                                .and_then(|p| p.get("backupId"))
                                .filter(|v| python_truthy(v))
                        })
                        .map(value_str)
                        .unwrap_or_default();
                    let reason = a
                        .get("reason")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let req_appr = a.get("requiresApproval").is_some_and(python_truthy);
                    let item = json!({
                        "type": act_type,
                        "source": source,
                        "destination": destination,
                        "target": target,
                        "policyId": policy_id,
                        "backupId": backup_id,
                        "reason": reason,
                        "requiresApproval": req_appr,
                    });
                    (
                        act_type,
                        target,
                        source,
                        destination,
                        policy_id,
                        backup_id,
                        item,
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    actions.sort_by(|a, b| {
        (&a.0, &a.1, &a.2, &a.3, &a.4, &a.5).cmp(&(&b.0, &b.1, &b.2, &b.3, &b.4, &b.5))
    });
    let sorted_actions: Vec<Value> = actions.into_iter().map(|entry| entry.6).collect();
    sha256_compact(&json!({
        "planVersion": plan_version,
        "inputRiskDigest": input_risk_digest,
        "actions": sorted_actions,
    }))
}

pub(crate) fn compute_coordination_plan_digest(plan: &Value) -> String {
    let version = plan
        .get("coordinationPlanVersion")
        .and_then(value_as_nonbool_i64)
        .unwrap_or(1);
    let risk_digest = plan
        .get("riskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let mut objectives: Vec<String> = plan
        .get("objectives")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().map(value_str).collect())
        .unwrap_or_default();
    objectives.sort();

    let mut actions: Vec<(String, String, String, String, String, Value)> = plan
        .get("actions")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|a| {
                    let act_id = a
                        .get("actionId")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let act_type = a
                        .get("type")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let pid = a
                        .get("policyId")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let bid = a
                        .get("backupId")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let src = a
                        .get("source")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let dst = a
                        .get("destination")
                        .filter(|v| python_truthy(v))
                        .map(value_str)
                        .unwrap_or_default();
                    let req = a.get("requiresApproval").is_some_and(python_truthy);
                    let item = json!({
                        "actionId": act_id,
                        "type": act_type,
                        "policyId": pid,
                        "backupId": bid,
                        "source": src,
                        "destination": dst,
                        "requiresApproval": req,
                    });
                    (act_type, pid, bid, src, dst, item)
                })
                .collect()
        })
        .unwrap_or_default();
    actions.sort_by(|a, b| (&a.0, &a.1, &a.2, &a.3, &a.4).cmp(&(&b.0, &b.1, &b.2, &b.3, &b.4)));
    let sorted_actions: Vec<Value> = actions.into_iter().map(|entry| entry.5).collect();

    let sort_pairs = |key: &str| -> Vec<Vec<String>> {
        let mut pairs: Vec<Vec<String>> = plan
            .get(key)
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(Value::as_array)
                    .filter(|p| p.len() >= 2)
                    .map(|p| {
                        let mut v = vec![value_str(&p[0]), value_str(&p[1])];
                        v.sort();
                        v
                    })
                    .collect()
            })
            .unwrap_or_default();
        pairs.sort();
        pairs
    };

    sha256_compact(&json!({
        "coordinationPlanVersion": version,
        "riskDigest": risk_digest,
        "objectives": objectives,
        "actions": sorted_actions,
        "dependencies": sort_pairs("dependencies"),
        "conflicts": sort_pairs("conflicts"),
    }))
}

fn select_rebalance_destination_readonly(
    source_target_id: &str,
    control_conn: Option<&Connection>,
    dr_conn: Option<&Connection>,
) -> Result<Option<String>, AppError> {
    let all_targets = list_registered_targets()?;
    let mut candidates: Vec<(f64, i64, String)> = Vec::new();
    for t in &all_targets {
        let tid = t
            .get("targetId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if tid.is_empty() || tid == source_target_id {
            continue;
        }
        if t.get("status")
            .filter(|v| python_truthy(v))
            .is_some_and(|v| value_str(v).eq_ignore_ascii_case("draining"))
        {
            continue;
        }
        let cap = probe_target_capacity_with_probe(t, &tid, control_conn, dr_conn, false)?;
        let free_bytes = cap.get("freeBytes").and_then(value_as_nonbool_i64);
        if free_bytes.is_some_and(|b| b < 0) {
            continue;
        }
        let free_pct = cap.get("freePercent").and_then(value_as_nonbool_f64);
        let effective_pct = free_pct.unwrap_or(100.0);
        if effective_pct <= 20.0 {
            continue;
        }
        let effective_free = free_bytes.unwrap_or(1_000_000_000_000);
        candidates.push((effective_pct, effective_free, tid));
    }
    candidates.sort_by(|a, b| {
        b.0.total_cmp(&a.0)
            .then_with(|| b.1.cmp(&a.1))
            .then_with(|| b.2.cmp(&a.2))
    });
    Ok(candidates.into_iter().next().map(|c| c.2))
}

fn find_rebalance_candidate_copy_readonly(
    policies: &[Value],
    dr_conn: Option<&Connection>,
    source_target_id: &str,
) -> Result<Option<(String, String)>, AppError> {
    for p in policies {
        let pid = p
            .get("policyId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if pid.is_empty() {
            continue;
        }
        let Some(pt) = latest_recovery_point(dr_conn, &pid)? else {
            continue;
        };
        let bid = pt
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        if bid.is_empty() {
            continue;
        }
        if pt
            .get("targetId")
            .filter(|v| python_truthy(v))
            .is_some_and(|v| value_str(v) == source_target_id)
        {
            return Ok(Some((pid, bid)));
        }
        let copies = list_logical_recovery_copies(dr_conn, Some(&pid), Some(&bid), None, 1000)?;
        for c in copies {
            let matches_target = c
                .get("targetId")
                .filter(|v| python_truthy(v))
                .is_some_and(|v| value_str(v) == source_target_id);
            let matches_status = c
                .get("status")
                .filter(|v| python_truthy(v))
                .is_some_and(|v| {
                    matches!(
                        value_str(v).to_lowercase().as_str(),
                        "committed" | "active" | "healthy"
                    )
                });
            if matches_target && matches_status {
                return Ok(Some((pid, bid)));
            }
        }
    }
    Ok(None)
}

fn select_repair_destination_readonly(
    policies: &[Value],
    policy_id: &str,
    existing_targets: &HashSet<String>,
    control_conn: Option<&Connection>,
    dr_conn: Option<&Connection>,
) -> Result<Option<String>, AppError> {
    let policy = policies.iter().find(|p| {
        p.get("policyId")
            .filter(|v| python_truthy(v))
            .is_some_and(|v| value_str(v) == policy_id)
    });
    let mut target_candidates: Vec<String> = Vec::new();
    if let Some(repl) = policy
        .and_then(|p| p.get("replication"))
        .and_then(Value::as_object)
    {
        let raw_list = repl
            .get("targetIds")
            .filter(|v| python_truthy(v))
            .and_then(Value::as_array)
            .cloned()
            .or_else(|| {
                repl.get("destTargets")
                    .filter(|v| python_truthy(v))
                    .and_then(Value::as_array)
                    .cloned()
            })
            .or_else(|| {
                repl.get("targets").and_then(Value::as_array).map(|arr| {
                    arr.iter()
                        .filter_map(|t| t.get("targetId").cloned())
                        .collect()
                })
            })
            .unwrap_or_default();
        for tid_val in raw_list {
            let tid = value_str(&tid_val);
            if !tid.is_empty() && !existing_targets.contains(&tid) {
                target_candidates.push(tid);
            }
        }
    }

    let registered = list_registered_targets()?;
    let all_targets: HashMap<String, Value> = registered
        .into_iter()
        .filter_map(|t| {
            let tid = t
                .get("targetId")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())?
                .to_string();
            Some((tid, t))
        })
        .collect();
    let pool: Vec<String> = if target_candidates.is_empty() {
        all_targets.keys().cloned().collect()
    } else {
        target_candidates
    };
    let mut valid_candidates: Vec<(f64, i64, String)> = Vec::new();
    for tid in pool {
        if tid.is_empty() || existing_targets.contains(&tid) {
            continue;
        }
        let Some(t_meta) = all_targets.get(&tid) else {
            continue;
        };
        if t_meta
            .get("status")
            .filter(|v| python_truthy(v))
            .is_some_and(|v| value_str(v).eq_ignore_ascii_case("draining"))
        {
            continue;
        }
        let cap = probe_target_capacity_with_probe(t_meta, &tid, control_conn, dr_conn, false)?;
        let free_pct = cap.get("freePercent").and_then(value_as_nonbool_f64);
        let free_bytes = cap.get("freeBytes").and_then(value_as_nonbool_i64);
        let effective_pct = free_pct.unwrap_or(100.0);
        if effective_pct <= 20.0 {
            continue;
        }
        let effective_free = free_bytes.unwrap_or(1_000_000_000_000);
        valid_candidates.push((effective_pct, effective_free, tid));
    }
    valid_candidates.sort_by(|a, b| {
        b.0.total_cmp(&a.0)
            .then_with(|| b.1.cmp(&a.1))
            .then_with(|| b.2.cmp(&a.2))
    });
    Ok(valid_candidates.into_iter().next().map(|c| c.2))
}

pub(crate) fn plan_resilience_actions_logic(
    root: &Path,
    risk_snapshot: &Value,
    read_observations: bool,
    now_iso: &str,
) -> Result<Value, AppError> {
    let control_conn = open_readonly_sqlite(&control_db_path(root))?;
    let dr_conn = open_readonly_sqlite(&dr_db_path(root))?;
    let risk_conn = if read_observations {
        open_readonly_sqlite(&risk_db_path(root))?
    } else {
        None
    };
    let policies = list_policies_readonly(root)?;
    let action_policy = read_autonomous_action_policy(root);

    let input_risk_digest = risk_snapshot
        .get("riskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let overall_risk = risk_snapshot
        .get("overallRisk")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| "healthy".to_string());
    let risks = risk_snapshot
        .get("risks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut actions: Vec<Value> = Vec::new();
    for r in &risks {
        let r_type = r
            .get("type")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let r_sev = r
            .get("severity")
            .filter(|v| python_truthy(v))
            .map(|v| value_str(v).to_lowercase())
            .unwrap_or_else(|| "healthy".to_string());
        if r_sev == "healthy" || r_sev == "low" {
            continue;
        }
        let lifecycle = risk_lifecycle_fields_readonly(risk_conn.as_ref(), r);
        let confidence = r
            .get("confidence")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| "verified".to_string());
        let evidence = r.get("evidence").cloned().unwrap_or_else(|| json!([]));

        if r_type == "CAPACITY_EXHAUSTION" {
            let target_id = r
                .get("target")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let dest_opt = select_rebalance_destination_readonly(
                &target_id,
                control_conn.as_ref(),
                dr_conn.as_ref(),
            )?;
            let cand_opt =
                find_rebalance_candidate_copy_readonly(&policies, dr_conn.as_ref(), &target_id)?;
            if let (Some(dest_target), Some((cand_pid, cand_bid))) = (dest_opt, cand_opt) {
                if !target_id.is_empty() {
                    let act_type = "CREATE_REBALANCE_JOB";
                    let req_appr = !is_action_autonomous(act_type, &action_policy);
                    let mut obj = Map::new();
                    obj.insert(
                        "actionId".to_string(),
                        Value::String(format!("act_{}", &new_hex_id()[..12])),
                    );
                    obj.insert("type".to_string(), Value::String(act_type.to_string()));
                    obj.insert("source".to_string(), Value::String(target_id.clone()));
                    obj.insert(
                        "destination".to_string(),
                        Value::String(dest_target.clone()),
                    );
                    obj.insert("target".to_string(), Value::String(target_id.clone()));
                    obj.insert("policyId".to_string(), Value::String(cand_pid.clone()));
                    obj.insert("backupId".to_string(), Value::String(cand_bid.clone()));
                    obj.insert(
                        "reason".to_string(),
                        Value::String("capacity-risk".to_string()),
                    );
                    obj.insert("severity".to_string(), Value::String(r_sev.clone()));
                    obj.insert("confidence".to_string(), Value::String(confidence));
                    obj.insert("requiresApproval".to_string(), Value::Bool(req_appr));
                    obj.insert("riskSubject".to_string(), canonical_risk_subject(r));
                    for (k, v) in lifecycle {
                        obj.insert(k, v);
                    }
                    obj.insert("severityBefore".to_string(), Value::String(r_sev));
                    obj.insert(
                        "expectedEffect".to_string(),
                        Value::String("severity-decrease".to_string()),
                    );
                    obj.insert(
                        "parameters".to_string(),
                        json!({
                            "policyId": cand_pid,
                            "backupId": cand_bid,
                            "sourceTargetId": target_id,
                            "destTargetId": dest_target,
                            "reason": "proactive-capacity-rebalance",
                        }),
                    );
                    obj.insert("evidence".to_string(), evidence);
                    actions.push(Value::Object(obj));
                }
            }
        } else if r_type == "REPLICA_LAG" || r_type == "FAILURE_DOMAIN_VIOLATION" {
            let policy_id = r
                .get("policyId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let latest_pt = if policy_id.is_empty() {
                None
            } else {
                latest_recovery_point(dr_conn.as_ref(), &policy_id)?
            };
            let backup_id = latest_pt
                .as_ref()
                .and_then(|pt| pt.get("backupId"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let source_target = latest_pt
                .as_ref()
                .and_then(|pt| pt.get("targetId"))
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let mut existing_targets = HashSet::new();
            if !policy_id.is_empty() && !backup_id.is_empty() {
                let copies = list_logical_recovery_copies(
                    dr_conn.as_ref(),
                    Some(&policy_id),
                    Some(&backup_id),
                    None,
                    1000,
                )?;
                for c in copies {
                    if c.get("status")
                        .filter(|v| python_truthy(v))
                        .is_some_and(|v| {
                            matches!(
                                value_str(v).to_lowercase().as_str(),
                                "committed" | "active" | "healthy"
                            )
                        })
                    {
                        if let Some(tid) = c
                            .get("targetId")
                            .filter(|v| python_truthy(v))
                            .map(value_str)
                            .filter(|s| !s.is_empty())
                        {
                            existing_targets.insert(tid);
                        }
                    }
                }
            }
            if !source_target.is_empty() {
                existing_targets.insert(source_target.clone());
            }
            let dest_target = select_repair_destination_readonly(
                &policies,
                &policy_id,
                &existing_targets,
                control_conn.as_ref(),
                dr_conn.as_ref(),
            )?
            .unwrap_or_default();
            let act_type = "CREATE_REPAIR_JOB";
            let req_appr = !is_action_autonomous(act_type, &action_policy);
            let target_field = if dest_target.is_empty() {
                source_target.clone()
            } else {
                dest_target.clone()
            };
            let reason = if r_type == "REPLICA_LAG" {
                "replica-lag-risk"
            } else {
                "failure-domain-deficit"
            };
            let mut obj = Map::new();
            obj.insert(
                "actionId".to_string(),
                Value::String(format!("act_{}", &new_hex_id()[..12])),
            );
            obj.insert("type".to_string(), Value::String(act_type.to_string()));
            obj.insert("policyId".to_string(), Value::String(policy_id.clone()));
            obj.insert("backupId".to_string(), Value::String(backup_id.clone()));
            obj.insert("source".to_string(), Value::String(source_target.clone()));
            obj.insert(
                "destination".to_string(),
                Value::String(dest_target.clone()),
            );
            obj.insert("target".to_string(), Value::String(target_field));
            obj.insert("reason".to_string(), Value::String(reason.to_string()));
            obj.insert("severity".to_string(), Value::String(r_sev.clone()));
            obj.insert("confidence".to_string(), Value::String(confidence));
            obj.insert("requiresApproval".to_string(), Value::Bool(req_appr));
            obj.insert("riskSubject".to_string(), canonical_risk_subject(r));
            for (k, v) in lifecycle {
                obj.insert(k, v);
            }
            obj.insert("severityBefore".to_string(), Value::String(r_sev));
            obj.insert(
                "expectedEffect".to_string(),
                Value::String("severity-decrease".to_string()),
            );
            obj.insert(
                "parameters".to_string(),
                json!({
                    "policyId": policy_id,
                    "backupId": backup_id,
                    "sourceTargetId": source_target,
                    "destTargetId": dest_target,
                    "reason": "replica-repair",
                }),
            );
            obj.insert("evidence".to_string(), evidence);
            actions.push(Value::Object(obj));
        } else if r_type == "DR_STALENESS" {
            let policy_id_val = r.get("policyId").cloned().unwrap_or(Value::Null);
            let policy_id_str = if python_truthy(&policy_id_val) {
                Some(value_str(&policy_id_val))
            } else {
                None
            };
            let latest_pt = if let Some(pid) = policy_id_str.as_deref() {
                latest_recovery_point(dr_conn.as_ref(), pid)?
            } else {
                None
            };
            let drill_backup_id = latest_pt
                .as_ref()
                .and_then(|pt| pt.get("backupId"))
                .filter(|v| python_truthy(v))
                .map(|v| Value::String(value_str(v)))
                .unwrap_or(Value::Null);
            let drill_target_id = latest_pt
                .as_ref()
                .and_then(|pt| pt.get("targetId"))
                .filter(|v| python_truthy(v))
                .map(|v| Value::String(value_str(v)))
                .unwrap_or(Value::Null);
            let act_type = "START_DR_DRILL";
            let req_appr = !is_action_autonomous(act_type, &action_policy);
            let mut obj = Map::new();
            obj.insert(
                "actionId".to_string(),
                Value::String(format!("act_{}", &new_hex_id()[..12])),
            );
            obj.insert("type".to_string(), Value::String(act_type.to_string()));
            obj.insert("policyId".to_string(), policy_id_val.clone());
            obj.insert("backupId".to_string(), drill_backup_id.clone());
            obj.insert("target".to_string(), drill_target_id.clone());
            obj.insert(
                "reason".to_string(),
                Value::String("dr-drill-staleness".to_string()),
            );
            obj.insert("severity".to_string(), Value::String(r_sev.clone()));
            obj.insert("confidence".to_string(), Value::String(confidence));
            obj.insert("requiresApproval".to_string(), Value::Bool(req_appr));
            obj.insert("riskSubject".to_string(), canonical_risk_subject(r));
            for (k, v) in lifecycle {
                obj.insert(k, v);
            }
            obj.insert("severityBefore".to_string(), Value::String(r_sev));
            obj.insert(
                "expectedEffect".to_string(),
                Value::String("severity-decrease".to_string()),
            );
            obj.insert(
                "parameters".to_string(),
                json!({
                    "policyId": policy_id_val,
                    "backupId": drill_backup_id,
                    "targetId": drill_target_id,
                }),
            );
            obj.insert("evidence".to_string(), evidence);
            actions.push(Value::Object(obj));
        } else if r_type == "AUTHORITY_DEGRADATION" && (r_sev == "critical" || r_sev == "blocked") {
            let mut obj = Map::new();
            obj.insert(
                "actionId".to_string(),
                Value::String(format!("act_{}", &new_hex_id()[..12])),
            );
            obj.insert(
                "type".to_string(),
                Value::String("PRIMARY_PROMOTION".to_string()),
            );
            obj.insert(
                "reason".to_string(),
                Value::String("authority-fail-closed-intervention-required".to_string()),
            );
            obj.insert("severity".to_string(), Value::String(r_sev.clone()));
            obj.insert("confidence".to_string(), Value::String(confidence));
            obj.insert("requiresApproval".to_string(), Value::Bool(true));
            obj.insert(
                "riskSubject".to_string(),
                json!({"type": "AUTHORITY_DEGRADATION"}),
            );
            for (k, v) in lifecycle {
                obj.insert(k, v);
            }
            obj.insert("severityBefore".to_string(), Value::String(r_sev));
            obj.insert(
                "expectedEffect".to_string(),
                Value::String("severity-decrease".to_string()),
            );
            obj.insert("parameters".to_string(), json!({}));
            obj.insert("evidence".to_string(), evidence);
            actions.push(Value::Object(obj));
        }
    }

    let mut plan = json!({
        "planVersion": 1,
        "planId": format!("plan_{}", &new_hex_id()[..16]),
        "generatedAt": now_iso,
        "inputRiskDigest": input_risk_digest,
        "overallRisk": overall_risk,
        "status": "PROPOSED",
        "actions": actions,
    });
    let plan_digest = compute_plan_digest(&plan);
    plan["planDigest"] = Value::String(plan_digest);
    Ok(plan)
}

pub(crate) fn plan_coordinated_resilience_logic(
    root: &Path,
    risk_snapshot: &Value,
    read_observations: bool,
    now_iso: &str,
) -> Result<Value, AppError> {
    let control_conn = open_readonly_sqlite(&control_db_path(root))?;
    let dr_conn = open_readonly_sqlite(&dr_db_path(root))?;
    let policies = list_policies_readonly(root)?;

    let risk_digest = risk_snapshot
        .get("riskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let overall_risk = risk_snapshot
        .get("overallRisk")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_lowercase())
        .unwrap_or_else(|| "healthy".to_string());
    let base_plan = plan_resilience_actions_logic(root, risk_snapshot, read_observations, now_iso)?;
    let mut actions = base_plan
        .get("actions")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let auth_risk = risk_snapshot
        .get("risks")
        .and_then(Value::as_array)
        .and_then(|arr| {
            arr.iter()
                .find(|r| r.get("type").and_then(Value::as_str) == Some("AUTHORITY_DEGRADATION"))
        });
    let auth_blocked = auth_risk.is_some_and(|r| {
        matches!(
            r.get("severity")
                .filter(|v| python_truthy(v))
                .map(|v| value_str(v).to_lowercase())
                .as_deref(),
            Some("critical" | "blocked")
        )
    });

    let mut objectives: BTreeSet<String> = BTreeSet::new();
    if auth_blocked || overall_risk == "blocked" {
        objectives.insert("authority-circuit-breaker-engaged".to_string());
        for act in &mut actions {
            let prev_reason = act
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if let Some(obj) = act.as_object_mut() {
                obj.insert("requiresApproval".to_string(), Value::Bool(true));
                obj.insert(
                    "reason".to_string(),
                    Value::String(format!("blocked-by-authority-risk:{prev_reason}")),
                );
            }
        }
    } else {
        for act in &actions {
            match act.get("type").and_then(Value::as_str).unwrap_or_default() {
                "CREATE_REPAIR_JOB" => {
                    objectives.insert("restore-replica-durability".to_string());
                }
                "CREATE_REBALANCE_JOB" => {
                    objectives.insert("relieve-capacity-pressure".to_string());
                }
                "START_DR_DRILL" => {
                    objectives.insert("verify-dr-readiness".to_string());
                }
                _ => {}
            }
        }
    }

    let mut dependencies: Vec<Value> = Vec::new();
    let mut conflicts: Vec<Value> = Vec::new();
    let repair_actions: Vec<&Value> = actions
        .iter()
        .filter(|a| a.get("type").and_then(Value::as_str) == Some("CREATE_REPAIR_JOB"))
        .collect();
    let rebalance_actions: Vec<&Value> = actions
        .iter()
        .filter(|a| a.get("type").and_then(Value::as_str) == Some("CREATE_REBALANCE_JOB"))
        .collect();
    let drill_actions: Vec<&Value> = actions
        .iter()
        .filter(|a| a.get("type").and_then(Value::as_str) == Some("START_DR_DRILL"))
        .collect();

    for rep in &repair_actions {
        let rep_id = rep.get("actionId").map(value_str).unwrap_or_default();
        let rep_pid = rep
            .get("policyId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let rep_bid = rep
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        for reb in &rebalance_actions {
            let reb_id = reb.get("actionId").map(value_str).unwrap_or_default();
            let reb_pid = reb
                .get("policyId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            let reb_bid = reb
                .get("backupId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            if rep_pid == reb_pid {
                dependencies.push(json!([rep_id, reb_id]));
                if rep_bid == reb_bid && !rep_bid.is_empty() {
                    conflicts.push(json!([rep_id, reb_id]));
                }
            }
        }
    }
    for dr in &drill_actions {
        let dr_id = dr.get("actionId").map(value_str).unwrap_or_default();
        let dr_bid = dr
            .get("backupId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        for reb in &rebalance_actions {
            let reb_id = reb.get("actionId").map(value_str).unwrap_or_default();
            let reb_bid = reb
                .get("backupId")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_default();
            if !dr_bid.is_empty() && dr_bid == reb_bid {
                dependencies.push(json!([dr_id, reb_id]));
                conflicts.push(json!([dr_id, reb_id]));
            }
        }
    }

    let wave_sim =
        simulate_coordination_wave(control_conn.as_ref(), dr_conn.as_ref(), &actions, &[])?;
    let wave_passed = wave_sim.get("passed") == Some(&Value::Bool(true));
    let wave_reason = wave_sim
        .get("reason")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();

    for act in &mut actions {
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
        let key = format!("{pid}:{bid}");
        let eval_info = wave_sim
            .get("evaluations")
            .and_then(|e| e.get(&key))
            .and_then(Value::as_object);
        let (passed_flag, reason_override) = if let Some(ei) = eval_info {
            if ei.get("passed") != Some(&Value::Bool(true)) {
                let r = ei
                    .get("reason")
                    .filter(|v| python_truthy(v))
                    .map(value_str)
                    .unwrap_or_default();
                (false, Some(format!("BLOCKED_BY_BLAST_RADIUS:{r}")))
            } else {
                (true, None)
            }
        } else if !wave_passed && !auth_blocked {
            (
                false,
                Some(format!("BLOCKED_BY_BLAST_RADIUS:{wave_reason}")),
            )
        } else {
            (true, None)
        };
        let min_copies = if pid.is_empty() {
            None
        } else {
            let pol = policies
                .iter()
                .find(|p| p.get("policyId").and_then(Value::as_str) == Some(&pid));
            let min_c = pol
                .and_then(|p| p.get("replication"))
                .and_then(|r| r.get("minCommittedCopies"))
                .and_then(value_as_nonbool_i64)
                .filter(|v| *v != 0)
                .unwrap_or(1);
            Some(min_c)
        };
        if let Some(obj) = act.as_object_mut() {
            obj.insert("blastRadiusVerified".to_string(), Value::Bool(passed_flag));
            if let Some(reason_str) = reason_override {
                obj.insert("requiresApproval".to_string(), Value::Bool(true));
                obj.insert("reason".to_string(), Value::String(reason_str));
            }
            if let Some(min_c) = min_copies {
                obj.insert("minCommittedCopies".to_string(), json!(min_c));
            }
        }
    }

    let limits = read_action_rate_limits(root);
    let max_concurrent = limits
        .get("maxConcurrentActions")
        .and_then(value_as_nonbool_i64)
        .unwrap_or(3);
    let objectives_vec: Vec<String> = objectives.into_iter().collect();
    let mut plan = json!({
        "coordinationPlanVersion": 1,
        "coordinationPlanId": format!("coord_{}", &new_hex_id()[..16]),
        "generatedAt": now_iso,
        "riskDigest": risk_digest,
        "overallRisk": overall_risk,
        "objectives": objectives_vec,
        "actions": actions,
        "dependencies": dependencies,
        "conflicts": conflicts,
        "budget": {
            "maxConcurrentActions": max_concurrent,
            "maxConcurrentActionsPerTarget": 2,
            "maxConcurrentActionsPerPolicy": 2,
            "maxSimultaneousFailureDomainsTouched": 1,
            "repairReserve": 1,
            "drReserve": 1,
        },
        "expectedRiskVector": {
            "overallRiskTarget": if auth_blocked { "blocked" } else { "healthy" },
            "targetSeverity": "healthy",
        },
        "blastRadiusSimulation": wave_sim,
        "status": "PROPOSED",
    });
    let plan_digest = compute_coordination_plan_digest(&plan);
    plan["planDigest"] = Value::String(plan_digest);
    Ok(plan)
}

fn validate_action_intent(action: &Value) -> Result<(), Vec<&'static str>> {
    let Some(obj) = action.as_object() else {
        return Err(vec!["action-intent-must-be-object"]);
    };
    let act_type = obj
        .get("type")
        .filter(|v| python_truthy(v))
        .map(|v| value_str(v).to_uppercase())
        .unwrap_or_default();
    if act_type.is_empty() {
        return Err(vec!["missing-action-type"]);
    }
    let params = obj.get("parameters").and_then(Value::as_object);
    let has_field = |p_key: &str, a_keys: &[&str]| -> bool {
        params.and_then(|p| p.get(p_key)).is_some_and(python_truthy)
            || a_keys
                .iter()
                .any(|k| obj.get(*k).is_some_and(python_truthy))
    };
    let mut issues = Vec::new();
    if act_type == "CREATE_REBALANCE_JOB" {
        if !has_field("sourceTargetId", &["source", "target"]) {
            issues.push("missing-sourceTargetId");
        }
        if !has_field("destTargetId", &["destination"]) {
            issues.push("missing-destTargetId");
        }
        if !has_field("policyId", &["policyId"]) {
            issues.push("missing-policyId");
        }
        if !has_field("backupId", &["backupId"]) {
            issues.push("missing-backupId");
        }
    } else if act_type == "CREATE_REPAIR_JOB" {
        if !has_field("policyId", &["policyId"]) {
            issues.push("missing-policyId");
        }
        if !has_field("backupId", &["backupId"]) {
            issues.push("missing-backupId");
        }
        if !has_field("destTargetId", &["destination", "target"]) {
            issues.push("missing-destTargetId");
        }
    }
    if issues.is_empty() {
        Ok(())
    } else {
        Err(issues)
    }
}

pub(crate) fn open_journal_rw(root: &Path) -> Result<Connection, AppError> {
    let dir = root.join(".resilience-journal");
    fs::create_dir_all(&dir).map_err(|_| unhandled_error())?;
    let conn = Connection::open(dir.join("journal.sqlite3")).map_err(|_| unhandled_error())?;
    conn.execute_batch("PRAGMA busy_timeout = 30000; PRAGMA journal_mode = WAL;")
        .map_err(|_| unhandled_error())?;
    conn.execute_batch(JOURNAL_SCHEMA)
        .map_err(|_| unhandled_error())?;
    let mut stmt = conn
        .prepare("PRAGMA table_info(resilience_actions)")
        .map_err(|_| unhandled_error())?;
    let cols: HashSet<String> = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|_| unhandled_error())?
        .flatten()
        .collect();
    drop(stmt);
    for (col_name, col_type) in [
        ("owner_instance_id", "TEXT"),
        ("lease_until", "TEXT"),
        ("claim_token", "TEXT"),
        ("execution_epoch", "INTEGER NOT NULL DEFAULT 0"),
        ("effect_class", "TEXT"),
        ("compensation_state", "TEXT"),
        ("effect_handle_json", "TEXT"),
        ("risk_subject_json", "TEXT"),
        ("expected_effect", "TEXT"),
        ("severity_before", "TEXT"),
        ("coordination_plan_id", "TEXT"),
        ("risk_before_digest", "TEXT"),
        ("risk_after_digest", "TEXT"),
    ] {
        if !cols.contains(col_name) {
            let _ = conn.execute(
                &format!("ALTER TABLE resilience_actions ADD COLUMN {col_name} {col_type};"),
                [],
            );
        }
    }
    Ok(conn)
}

fn parse_opt_json(raw: Option<String>) -> Value {
    raw.and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .unwrap_or(Value::Null)
}

pub(crate) fn get_action_from_conn(
    conn: &Connection,
    action_id: &str,
) -> Result<Option<Value>, AppError> {
    let row = conn
        .query_row(
            "SELECT action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
                    state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
                    effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
                    severity_before, coordination_plan_id, execution_result_json, verification_result_json,
                    decision_proof_json, risk_before_digest, risk_after_digest, error_message,
                    created_at, updated_at
             FROM resilience_actions WHERE action_id = ?",
            [action_id],
            |r| {
                let params = r
                    .get::<_, Option<String>>(7)?
                    .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                    .unwrap_or_else(|| json!({}));
                Ok(json!({
                    "actionId": r.get::<_, String>(0)?,
                    "planId": r.get::<_, Option<String>>(1)?,
                    "type": r.get::<_, String>(2)?,
                    "createdBy": r.get::<_, String>(3)?,
                    "inputRiskDigest": r.get::<_, Option<String>>(4)?,
                    "planDigest": r.get::<_, Option<String>>(5)?,
                    "state": r.get::<_, String>(6)?,
                    "parameters": params,
                    "ownerInstanceId": r.get::<_, Option<String>>(8)?,
                    "leaseUntil": r.get::<_, Option<String>>(9)?,
                    "claimToken": r.get::<_, Option<String>>(10)?,
                    "executionEpoch": r.get::<_, Option<i64>>(11)?.unwrap_or(0),
                    "effectClass": r.get::<_, Option<String>>(12)?,
                    "compensationState": r.get::<_, Option<String>>(13)?,
                    "effectHandle": parse_opt_json(r.get::<_, Option<String>>(14)?),
                    "riskSubject": parse_opt_json(r.get::<_, Option<String>>(15)?),
                    "expectedEffect": r.get::<_, Option<String>>(16)?,
                    "severityBefore": r.get::<_, Option<String>>(17)?,
                    "coordinationPlanId": r.get::<_, Option<String>>(18)?,
                    "executionResult": parse_opt_json(r.get::<_, Option<String>>(19)?),
                    "verificationResult": parse_opt_json(r.get::<_, Option<String>>(20)?),
                    "decisionProof": parse_opt_json(r.get::<_, Option<String>>(21)?),
                    "riskBeforeDigest": r.get::<_, Option<String>>(22)?,
                    "riskAfterDigest": r.get::<_, Option<String>>(23)?,
                    "error": r.get::<_, Option<String>>(24)?,
                    "createdAt": r.get::<_, String>(25)?,
                    "updatedAt": r.get::<_, String>(26)?,
                }))
            },
        )
        .optional()
        .map_err(|_| unhandled_error())?;
    Ok(row)
}

pub(crate) fn conflict_error(msg: impl Into<String>) -> AppError {
    AppError {
        status: 409,
        code: codes::INVALID_REQUEST,
        message: msg.into(),
    }
}

pub(crate) fn materialize_resilience_plan_db(
    root: &Path,
    plan: &Value,
    created_by: &str,
    now_iso: &str,
) -> Result<Value, AppError> {
    let plan_obj = plan
        .as_object()
        .ok_or_else(|| AppError::invalid_payload("plan must be an object"))?;
    let plan_id = plan_obj
        .get("planId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| format!("plan_{}", &new_hex_id()[..16]));
    let plan_version = plan_obj
        .get("planVersion")
        .and_then(value_as_nonbool_i64)
        .filter(|v| *v != 0)
        .unwrap_or(1);
    let input_risk_digest = plan_obj
        .get("inputRiskDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let declared_digest = plan_obj
        .get("planDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    let overall_risk = plan_obj
        .get("overallRisk")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| "healthy".to_string());
    let plan_status = plan_obj
        .get("status")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| "PROPOSED".to_string());
    let actions = plan_obj
        .get("actions")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let computed_digest = compute_plan_digest(plan);
    if !declared_digest.is_empty() && declared_digest != computed_digest {
        return Err(AppError::invalid_payload(format!(
            "Plan digest mismatch: declared={declared_digest}, computed={computed_digest}"
        )));
    }
    let effective_plan_digest = computed_digest;
    let action_policy = read_autonomous_action_policy(root);

    let mut validated_actions: Vec<Value> = Vec::with_capacity(actions.len());
    for act in &actions {
        if let Err(issues) = validate_action_intent(act) {
            let aid = act
                .get("actionId")
                .map(value_str)
                .unwrap_or_else(|| "None".to_string());
            return Err(AppError::invalid_payload(format!(
                "Action '{aid}' has invalid parameters: {}",
                issues.join(", ")
            )));
        }
        let act_id = act
            .get("actionId")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| format!("act_{}", &new_hex_id()[..12]));
        let act_type = act
            .get("type")
            .filter(|v| python_truthy(v))
            .map(|v| value_str(v).to_uppercase())
            .unwrap_or_default();
        let req_approval = act.get("requiresApproval").is_some_and(python_truthy);
        let is_auto = is_action_autonomous(&act_type, &action_policy);
        let initial_state = if req_approval || !is_auto {
            "APPROVAL_REQUIRED"
        } else {
            "PENDING"
        };
        let params = act
            .get("parameters")
            .filter(|v| v.is_object())
            .cloned()
            .unwrap_or_else(|| json!({}));
        let risk_subject = act.get("riskSubject").cloned().unwrap_or(Value::Null);
        let expected_effect = act
            .get("expectedEffect")
            .cloned()
            .unwrap_or_else(|| Value::String("severity-decrease".to_string()));
        let severity_before = act.get("severityBefore").cloned().unwrap_or(Value::Null);
        let coord_plan_id = act
            .get("coordinationPlanId")
            .cloned()
            .unwrap_or(Value::Null);
        validated_actions.push(json!({
            "actionId": act_id,
            "planId": plan_id,
            "type": act_type,
            "createdBy": created_by,
            "inputRiskDigest": input_risk_digest,
            "planDigest": effective_plan_digest,
            "state": initial_state,
            "parameters": params,
            "ownerInstanceId": Value::Null,
            "leaseUntil": Value::Null,
            "claimToken": Value::Null,
            "executionEpoch": 0,
            "effectClass": "NO_EFFECT",
            "compensationState": Value::Null,
            "effectHandle": Value::Null,
            "riskSubject": risk_subject,
            "expectedEffect": expected_effect,
            "severityBefore": severity_before,
            "coordinationPlanId": coord_plan_id,
            "executionResult": Value::Null,
            "verificationResult": Value::Null,
            "decisionProof": Value::Null,
            "riskBeforeDigest": Value::Null,
            "riskAfterDigest": Value::Null,
            "error": Value::Null,
            "createdAt": now_iso,
            "updatedAt": now_iso,
        }));
    }

    let conn = open_journal_rw(root)?;
    conn.execute_batch("BEGIN IMMEDIATE;")
        .map_err(|_| unhandled_error())?;

    let persist_res: Result<Vec<Value>, AppError> = (|| {
        let existing_plan: Option<String> = conn
            .query_row(
                "SELECT plan_digest FROM resilience_plans WHERE plan_id = ?",
                [&plan_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| unhandled_error())?;
        if let Some(existing_digest) = existing_plan {
            if existing_digest != effective_plan_digest {
                return Err(conflict_error(format!(
                    "Plan identity conflict: plan '{plan_id}' exists with different digest ({existing_digest} != {effective_plan_digest})"
                )));
            }
        } else {
            let plan_json_text = serde_json::to_string(plan).map_err(|_| unhandled_error())?;
            conn.execute(
                "INSERT INTO resilience_plans (
                    plan_id, plan_version, input_risk_digest, plan_digest,
                    overall_risk, status, plan_json, created_by, created_at, updated_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rusqlite::params![
                    plan_id,
                    plan_version,
                    input_risk_digest,
                    effective_plan_digest,
                    overall_risk,
                    plan_status,
                    plan_json_text,
                    created_by,
                    now_iso,
                    now_iso,
                ],
            )
            .map_err(|_| unhandled_error())?;
        }

        let mut persisted_actions = Vec::with_capacity(validated_actions.len());
        for va in &validated_actions {
            let act_id = va
                .get("actionId")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let existing_params_raw: Option<Option<String>> = conn
                .query_row(
                    "SELECT parameters_json FROM resilience_actions WHERE action_id = ?",
                    [act_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| unhandled_error())?;
            if let Some(opt_raw) = existing_params_raw {
                let existing_params = opt_raw
                    .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                    .unwrap_or_else(|| json!({}));
                let va_params = va.get("parameters").cloned().unwrap_or_else(|| json!({}));
                if existing_params != va_params {
                    return Err(conflict_error(format!(
                        "Action identity conflict: action '{act_id}' exists with different parameters"
                    )));
                }
                if let Some(existing_act) = get_action_from_conn(&conn, act_id)? {
                    persisted_actions.push(existing_act);
                }
            } else {
                let params_json =
                    serde_json::to_string(&va["parameters"]).map_err(|_| unhandled_error())?;
                let risk_subj_json = if va["riskSubject"].is_null() {
                    None
                } else {
                    Some(serde_json::to_string(&va["riskSubject"]).map_err(|_| unhandled_error())?)
                };
                let act_type = va.get("type").and_then(Value::as_str).unwrap_or_default();
                let act_state = va.get("state").and_then(Value::as_str).unwrap_or("PENDING");
                let expected_eff = va
                    .get("expectedEffect")
                    .and_then(Value::as_str)
                    .unwrap_or("severity-decrease");
                let sev_before = va.get("severityBefore").and_then(Value::as_str);
                let coord_id = va.get("coordinationPlanId").and_then(Value::as_str);
                conn.execute(
                    "INSERT INTO resilience_actions (
                        action_id, plan_id, action_type, created_by, input_risk_digest, plan_digest,
                        state, parameters_json, owner_instance_id, lease_until, claim_token, execution_epoch,
                        effect_class, compensation_state, effect_handle_json, risk_subject_json, expected_effect,
                        severity_before, coordination_plan_id, execution_result_json, verification_result_json,
                        decision_proof_json, risk_before_digest, risk_after_digest, error_message,
                        created_at, updated_at
                     ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, 'NO_EFFECT', NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                    rusqlite::params![
                        act_id,
                        plan_id,
                        act_type,
                        created_by,
                        input_risk_digest,
                        effective_plan_digest,
                        act_state,
                        params_json,
                        risk_subj_json,
                        expected_eff,
                        sev_before,
                        coord_id,
                        now_iso,
                        now_iso,
                    ],
                )
                .map_err(|_| unhandled_error())?;
                persisted_actions.push(va.clone());
            }
        }
        Ok(persisted_actions)
    })();

    let persisted_actions = match persist_res {
        Ok(list) => list,
        Err(err) => {
            let _ = conn.execute_batch("ROLLBACK;");
            return Err(err);
        }
    };
    conn.execute_batch("COMMIT;")
        .map_err(|_| unhandled_error())?;

    Ok(json!({
        "planId": plan_id,
        "planVersion": plan_version,
        "inputRiskDigest": input_risk_digest,
        "planDigest": effective_plan_digest,
        "overallRisk": overall_risk,
        "status": "MATERIALIZED",
        "actions": persisted_actions,
        "createdAt": now_iso,
    }))
}

#[cfg(test)]
mod unit_tests {
    use super::*;
    use rusqlite::Connection;
    use serde_json::json;
    use std::path::PathBuf;

    struct Scratch {
        root: PathBuf,
    }

    impl Scratch {
        fn new(tag: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "resilience-state-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).unwrap();
            Self { root }
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn severity_rank_follows_the_oracle_ladder() {
        assert_eq!(severity_rank("healthy"), 1);
        assert_eq!(severity_rank("warning"), 2);
        assert_eq!(severity_rank("degraded"), 3);
        assert_eq!(severity_rank("critical"), 4);
        assert_eq!(severity_rank("blocked"), 5);
        // Anything unrecognised is treated as healthy rather than ranked below it.
        assert_eq!(severity_rank(""), 1);
        assert_eq!(severity_rank("unknown"), 1);
        assert_eq!(severity_rank("HEALTHY"), 1);
    }

    #[test]
    fn is_open_severity_covers_every_opening_level() {
        for open in ["warning", "degraded", "critical", "blocked"] {
            assert!(is_open_severity(open), "{open} should be open");
        }
        for closed in ["healthy", "", "unknown", "WARNING"] {
            assert!(!is_open_severity(closed), "{closed} should not be open");
        }
    }

    /// `parse_python_iso` mirrors `datetime.fromisoformat(...).timestamp()`: it
    /// only yields an epoch when the text carries an explicit offset, because a
    /// naive datetime has no instant.
    #[test]
    fn parse_utc_epoch_requires_an_explicit_offset() {
        assert_eq!(
            parse_utc_epoch(Some("2024-01-01T00:00:00+00:00")),
            Some(1_704_067_200)
        );
        // A `Z` suffix is *not* accepted: `parse_time`'s offset arm only reads
        // `+` or `-`, so a Zulu timestamp yields no epoch where CPython's
        // `fromisoformat` accepts it. Asserted as-is rather than smoothed over.
        assert_eq!(parse_utc_epoch(Some("2024-01-01T00:00:00Z")), None);
        assert_eq!(
            parse_utc_epoch(Some("2024-01-01T12:30:45+02:00")),
            Some(1_704_105_045)
        );
        assert_eq!(
            parse_utc_epoch(Some("2024-01-01T00:00:00-05:00")),
            Some(1_704_085_200)
        );
        // No offset means no instant.
        assert_eq!(parse_utc_epoch(Some("2024-01-01T00:00:00")), None);
        assert_eq!(parse_utc_epoch(Some("2024-01-01")), None);
        assert_eq!(parse_utc_epoch(Some("")), None);
        assert_eq!(parse_utc_epoch(None), None);
        // An invalid calendar date is refused rather than silently clamped.
        assert_eq!(parse_utc_epoch(Some("2024-02-30T00:00:00+00:00")), None);
        assert_eq!(parse_utc_epoch(Some("not-a-date")), None);
    }

    #[test]
    fn control_and_dr_paths_are_the_oracles_dot_directories() {
        let scratch = Scratch::new("paths");
        assert_eq!(
            control_db_path(&scratch.root),
            scratch.root.join(".backup-control").join("control.sqlite3")
        );
        assert_eq!(
            dr_db_path(&scratch.root),
            scratch.root.join(".backup-dr").join("evidence.sqlite3")
        );
    }

    #[test]
    fn sha256_compact_matches_the_oracles_sorted_json_digest() {
        // Same payload the oracle hashes: sorted keys, compact separators.
        assert_eq!(
            sha256_compact(&json!({"b": 1, "a": 2})),
            sha256_compact(&json!({"a": 2, "b": 1})),
            "key order must not change the digest"
        );
        assert_ne!(
            sha256_compact(&json!({"a": 1})),
            sha256_compact(&json!({"a": 2}))
        );
        assert_eq!(sha256_compact(&json!(null)).len(), 64);
    }

    /// With no control database the readers answer with empty collections
    /// instead of failing, which is what makes the degraded path readable.
    #[test]
    fn control_readers_return_empty_without_a_connection() {
        assert_eq!(list_control_policies(None).unwrap(), Vec::<Value>::new());
        assert_eq!(list_control_targets(None).unwrap(), Vec::<Value>::new());
    }

    /// A database that exists but has never been migrated answers empty too,
    /// because `table_exists` gates the query.
    #[test]
    fn control_readers_return_empty_when_the_tables_are_absent() {
        let conn = Connection::open_in_memory().unwrap();
        assert_eq!(
            list_control_policies(Some(&conn)).unwrap(),
            Vec::<Value>::new()
        );
        assert_eq!(
            list_control_targets(Some(&conn)).unwrap(),
            Vec::<Value>::new()
        );
    }

    #[test]
    fn control_policies_are_returned_in_policy_id_order() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE control_policies (policy_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
             INSERT INTO control_policies VALUES ('p2', '{\"policyId\":\"p2\"}');
             INSERT INTO control_policies VALUES ('p1', '{\"policyId\":\"p1\"}');",
        )
        .unwrap();
        let policies = list_control_policies(Some(&conn)).unwrap();
        assert_eq!(policies.len(), 2);
        assert_eq!(policies[0], json!({"policyId": "p1"}));
        assert_eq!(policies[1], json!({"policyId": "p2"}));
    }

    #[test]
    fn a_single_control_policy_is_found_by_id_and_missing_ones_are_none() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE control_policies (policy_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
             INSERT INTO control_policies VALUES ('p1', '{\"policyId\":\"p1\",\"name\":\"one\"}');",
        )
        .unwrap();
        assert_eq!(
            get_control_policy(Some(&conn), "p1").unwrap(),
            Some(json!({"policyId": "p1", "name": "one"}))
        );
        assert_eq!(get_control_policy(Some(&conn), "absent").unwrap(), None);
        assert_eq!(get_control_policy(None, "p1").unwrap(), None);
    }

    /// `infer_absent_closure_reason` decides why a previously open subject is no
    /// longer in the authoritative snapshot. Every expected pair below was read
    /// from `deepseek_infra.infra.workspace.resilience_risk_observations`.
    #[test]
    fn absent_closure_reason_covers_every_oracle_branch() {
        let unknown = ("UNKNOWN_COVERAGE", "UNKNOWN_COVERAGE");
        let retired_scope = ("RETIRED", "SCOPE_RETIRED");
        let cleared = ("CLEARED", "HEALTHY");
        let subject = json!({"type": "capacity"});
        let complete = json!({"CAPACITY": {"complete": true}});

        // No coverage at all, coverage for another type, and coverage that is
        // present but not marked complete are all "unknown", not "retired".
        assert_eq!(infer_absent_closure_reason(&subject, None), unknown);
        assert_eq!(
            infer_absent_closure_reason(&subject, Some(&json!({"replica": {"complete": true}}))),
            unknown
        );
        assert_eq!(
            infer_absent_closure_reason(&subject, Some(&json!({"CAPACITY": {"complete": false}}))),
            unknown
        );
        assert_eq!(
            infer_absent_closure_reason(&subject, Some(&json!({"CAPACITY": {}}))),
            unknown
        );

        // A complete entry that lists nothing means the whole scope is gone.
        assert_eq!(
            infer_absent_closure_reason(&subject, Some(&complete)),
            retired_scope
        );
        // The risk type is uppercased before the lookup, so casing is irrelevant.
        assert_eq!(
            infer_absent_closure_reason(&json!({"type": "Capacity"}), Some(&complete)),
            retired_scope
        );
        assert_eq!(
            infer_absent_closure_reason(&json!({}), Some(&json!({"": {"complete": true}}))),
            retired_scope
        );

        // A policy that is no longer listed retires the subject.
        assert_eq!(
            infer_absent_closure_reason(
                &json!({"type": "capacity", "policyId": "p9"}),
                Some(&json!({"CAPACITY": {"complete": true, "policies": ["p1"]}}))
            ),
            ("RETIRED", "POLICY_DISABLED")
        );
        // A target that is no longer listed retires the subject.
        assert_eq!(
            infer_absent_closure_reason(
                &json!({"type": "capacity", "targetId": "t9"}),
                Some(&json!({"CAPACITY": {"complete": true, "targets": ["t1"]}}))
            ),
            ("RETIRED", "TARGET_REMOVED")
        );
        // A backup that is no longer listed is superseded rather than retired.
        assert_eq!(
            infer_absent_closure_reason(
                &json!({"type": "capacity", "backupId": "b9"}),
                Some(&json!({"CAPACITY": {"complete": true, "backups": ["b1"]}}))
            ),
            ("SUPERSEDED", "SUPERSEDED_BACKUP")
        );
        // The same holds when the policy survives but the backup does not.
        assert_eq!(
            infer_absent_closure_reason(
                &json!({"type": "capacity", "backupId": "b9", "policyId": "p1"}),
                Some(&json!({"CAPACITY": {"complete": true, "policies": ["p1"]}}))
            ),
            ("SUPERSEDED", "SUPERSEDED_BACKUP")
        );

        // Still listed, so nothing is wrong: the risk simply cleared.
        assert_eq!(
            infer_absent_closure_reason(
                &json!({"type": "capacity", "targetId": "t1"}),
                Some(&json!({"CAPACITY": {"complete": true, "targets": ["t1"]}}))
            ),
            cleared
        );
        // A policy with targets present but no policy list is not retired here;
        // the `POLICY_DISABLED` fallback needs every list to be empty.
        assert_eq!(
            infer_absent_closure_reason(
                &json!({"type": "capacity", "policyId": "p1"}),
                Some(&json!({"CAPACITY": {"complete": true, "targets": ["t1"]}}))
            ),
            cleared
        );
    }

    /// With no actions to coordinate, the wave is trivially satisfied and the
    /// result names the two id lists it was given.
    #[test]
    fn coordination_wave_without_actions_passes() {
        let wave = simulate_coordination_wave(None, None, &[], &[]).unwrap();
        assert_eq!(wave["passed"], true);
        assert_eq!(wave["reason"], "blast-radius-invariants-satisfied");
        assert_eq!(wave["proposedActionIds"].as_array().unwrap().len(), 0);
        assert_eq!(wave["runningActionIds"].as_array().unwrap().len(), 0);
        assert!(wave["evaluations"].as_object().unwrap().is_empty());
    }

    /// Ids are echoed back for both lists, and an action without an id is
    /// dropped from the reported ids rather than invented.
    #[test]
    fn coordination_wave_reports_the_action_ids_it_was_given() {
        let proposed = json!([
            {"actionId": "a1", "type": "CREATE_REPAIR_JOB"},
            {"type": "CREATE_REPAIR_JOB"}
        ]);
        let running = json!([{"actionId": "r1"}]);
        let wave = simulate_coordination_wave(
            None,
            None,
            proposed.as_array().unwrap(),
            running.as_array().unwrap(),
        )
        .unwrap();
        assert_eq!(wave["proposedActionIds"], json!(["a1"]));
        assert_eq!(wave["runningActionIds"], json!(["r1"]));
    }

    /// The grade boundaries are inclusive at the lower edge of each band.
    #[test]
    fn resilience_grade_bands_are_inclusive() {
        assert_eq!(calculate_grade(100), "A");
        assert_eq!(calculate_grade(90), "A");
        assert_eq!(calculate_grade(89), "B");
        assert_eq!(calculate_grade(80), "B");
        assert_eq!(calculate_grade(79), "C");
        assert_eq!(calculate_grade(70), "C");
        assert_eq!(calculate_grade(69), "D");
        assert_eq!(calculate_grade(60), "D");
        assert_eq!(calculate_grade(59), "F");
        assert_eq!(calculate_grade(0), "F");
    }

    /// An empty workspace has no drill history, no policies, no capacity
    /// history, no restore samples and no authority replicas, so every factor
    /// lands on its worst rung and the weaknesses list names each one.
    #[test]
    fn resilience_score_of_an_empty_workspace_is_bottom_of_every_factor() {
        let scratch = Scratch::new("score-empty");
        let score = calculate_resilience_score_readonly(
            &scratch.root,
            1_700_000_000,
            "2024-01-01T00:00:00Z",
        )
        .unwrap();
        assert_eq!(score["factorBreakdown"]["drDrill"]["score"], 0);
        assert_eq!(score["factorBreakdown"]["drDrill"]["status"], "critical");
        let weaknesses = score["weaknesses"].as_array().unwrap();
        assert!(
            weaknesses.iter().any(|w| w == "no-successful-dr-drills"),
            "expected the missing-drill weakness, got {weaknesses:?}"
        );
        // The total is the clamp of the summed factors, so it can never leave 0..=100.
        let total = score["score"].as_i64().unwrap();
        assert!((0..=100).contains(&total), "score out of range: {total}");
        assert_eq!(score["generatedAt"], "2024-01-01T00:00:00Z");
    }

    #[test]
    fn control_targets_are_returned_in_target_id_order() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE control_targets (target_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
             INSERT INTO control_targets VALUES ('t2', '{\"targetId\":\"t2\"}');
             INSERT INTO control_targets VALUES ('t1', '{\"targetId\":\"t1\"}');",
        )
        .unwrap();
        let targets = list_control_targets(Some(&conn)).unwrap();
        assert_eq!(
            targets,
            vec![json!({"targetId": "t1"}), json!({"targetId": "t2"})]
        );
    }
}
