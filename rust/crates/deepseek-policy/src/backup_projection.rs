//! Projected restore planning, incremental delta chain materialization, and preflight sizing.
//!
//! Mirrors `backup_projection.py`, `backup_incremental.py` (Merkle & FastCDC v2/v3),
//! `backup_incremental_restore.py`, `backup_verified_plan.py`, `backup_recovery_preflight.py`,
//! and `backup_object_set.py` inventory/control verification without Python fallbacks.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid_simple_hex::UuidHex;

use crate::app_error::{AppError, codes};
use crate::backup_mirror::sha256_file;
use crate::backup_unlock::{
    PACK_INDEX_PATH, ParsedPackIndex, is_plain_sha256, parse_pack_index_file, parse_payload_ref,
    validate_archive_path,
};
use crate::core_utils::python_truthy;
use crate::entropy::{Entropy, SystemEntropy};
use crate::python_json::{dumps_compact, value_str};

pub const PROJECTS_CONTRIBUTOR: &str = "projects";
pub const FRONTEND_CONTRIBUTOR: &str = "frontend";
pub const STATELESS_MCP_CONTRIBUTOR: &str = "stateless-mcp";
pub const MIGRATION_PATH_PREFIX: &str = "migration/";
pub const NETWORK_SELECTIVITY_REASON: &str = "whole-age-object";
pub const PLANNER_SCHEMA_VERSION: i64 = 1;
pub const VERIFIED_PLAN_NAME: &str = "verified-plan.json";
pub const OBJECT_SET_V1: &str = "object-set-v1";

pub const CDC_ALGORITHM_V2: &str = "fastcdc-gear-v2";
pub const CDC_ALGORITHM_V3: &str = "fastcdc-gear-v3";
pub const CDC_MIN_CHUNK: usize = 512 * 1024;
pub const CDC_AVG_CHUNK: usize = 2 * 1024 * 1024;
pub const CDC_MAX_CHUNK: usize = 8 * 1024 * 1024;
pub const DEFAULT_DISK_RESERVE_BYTES: u64 = 1024 * 1024 * 1024;
const COPY_BUFFER_BYTES: usize = 1024 * 1024;
const SAFETY_ARCHIVE_FIXED_OVERHEAD_BYTES: u64 = 1024 * 1024;
const SAFETY_ARCHIVE_OVERHEAD_PERCENT: u64 = 1;
const CRYPTO_QUEUE_COMPONENTS: usize = 2;

mod uuid_simple_hex {
    use super::*;

    pub(crate) struct UuidHex;

    impl UuidHex {
        pub(crate) fn generate() -> String {
            SystemEntropy.new_file_id().unwrap_or_else(|_| {
                let nanos = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|elapsed| elapsed.as_nanos())
                    .unwrap_or(0);
                format!("{nanos:032x}")
            })
        }
    }
}

fn invalid_payload(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

fn conflict_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status: 409,
    }
}

fn internal_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INTERNAL,
        status: 500,
    }
}

pub fn sha256_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn stable_json_bytes(value: &Value) -> Vec<u8> {
    dumps_compact(value).into_bytes()
}

/// Canonical frozen contributor/project selection for projected restores.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RestoreSelection {
    pub contributors: Vec<String>,
    pub project_ids: Vec<String>,
}

impl RestoreSelection {
    pub fn canonical_value(&self) -> Value {
        let mut contributors = self.contributors.clone();
        contributors.sort();
        let mut project_ids = self.project_ids.clone();
        project_ids.sort();
        json!({
            "contributors": contributors,
            "projectIds": project_ids,
        })
    }

    pub fn digest(&self) -> String {
        sha256_bytes(&stable_json_bytes(&self.canonical_value()))
    }
}

/// Coerce an API selection value into a canonical, sorted `RestoreSelection`.
pub fn normalize_selection(value: Option<&Value>) -> Result<Option<RestoreSelection>, AppError> {
    let Some(val) = value else {
        return Ok(None);
    };
    if val.is_null() {
        return Ok(None);
    }
    let Some(obj) = val.as_object() else {
        return Err(conflict_error("Restore selection must be an object"));
    };
    let Some(raw_contributors) = obj.get("contributors").and_then(Value::as_array) else {
        return Err(conflict_error(
            "Restore selection requires at least one contributor",
        ));
    };
    if raw_contributors.is_empty() {
        return Err(conflict_error(
            "Restore selection requires at least one contributor",
        ));
    }
    let raw_projects = match obj.get("projectIds") {
        None | Some(Value::Null) => &[] as &[Value],
        Some(Value::Array(arr)) => arr.as_slice(),
        Some(_) => {
            return Err(conflict_error(
                "Restore selection projectIds must be a list",
            ));
        }
    };
    let contributors: BTreeSet<String> = raw_contributors
        .iter()
        .map(value_str)
        .filter(|s| !s.is_empty())
        .collect();
    if contributors.is_empty() {
        return Err(conflict_error(
            "Restore selection requires at least one contributor",
        ));
    }
    let project_ids: BTreeSet<String> = raw_projects
        .iter()
        .map(value_str)
        .filter(|s| !s.is_empty())
        .collect();
    Ok(Some(RestoreSelection {
        contributors: contributors.into_iter().collect(),
        project_ids: project_ids.into_iter().collect(),
    }))
}

/// Logical file entry recorded inside a snapshot manifest/delta.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileRecord {
    pub contributor_id: String,
    pub logical_path: String,
    pub size: u64,
    pub sha256: String,
}

impl FileRecord {
    pub fn leaf_digest(&self) -> String {
        let payload = json!({
            "contributorId": self.contributor_id,
            "logicalPath": self.logical_path,
            "sha256": self.sha256,
            "size": self.size,
        });
        let mut hasher = Sha256::new();
        hasher.update(b"\x00");
        hasher.update(stable_json_bytes(&payload));
        format!("{:x}", hasher.finalize())
    }
}

pub fn merkle_root(leaves: &[String]) -> String {
    if leaves.is_empty() {
        return sha256_bytes(b"\x00");
    }
    let mut level: Vec<String> = leaves.to_vec();
    level.sort();
    while level.len() > 1 {
        let mut next = Vec::with_capacity(level.len().div_ceil(2));
        for pair in level.chunks(2) {
            let left = &pair[0];
            let right = pair.get(1).unwrap_or(left);
            let mut hasher = Sha256::new();
            hasher.update(b"\x01");
            hasher.update(left.as_bytes());
            hasher.update(right.as_bytes());
            next.push(format!("{:x}", hasher.finalize()));
        }
        level = next;
    }
    level.into_iter().next().unwrap_or_default()
}

pub fn snapshot_root(files: &[FileRecord]) -> String {
    let leaves: Vec<String> = files.iter().map(FileRecord::leaf_digest).collect();
    merkle_root(&leaves)
}

pub fn manifest_files(manifest: &Value) -> Result<Vec<FileRecord>, AppError> {
    let Some(items) = manifest.get("files").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut records = Vec::with_capacity(items.len());
    for item in items {
        let Some(obj) = item.as_object() else {
            return Err(invalid_payload("Chain package file inventory is invalid"));
        };
        let path = obj
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_payload("Chain package file inventory is invalid"))?;
        records.push(FileRecord {
            contributor_id: obj
                .get("contributorId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            logical_path: path.to_string(),
            size: obj.get("size").and_then(Value::as_u64).unwrap_or(0),
            sha256: obj
                .get("sha256")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        });
    }
    Ok(records)
}

pub fn apply_delta_ops(current_files: &[FileRecord], delta: &Value) -> Vec<FileRecord> {
    let mut result = current_files.to_vec();
    let mut index_by_key: HashMap<(String, String), usize> = result
        .iter()
        .enumerate()
        .map(|(idx, item)| {
            (
                (item.contributor_id.clone(), item.logical_path.clone()),
                idx,
            )
        })
        .collect();

    if let Some(puts) = delta.get("put").and_then(Value::as_array) {
        for put in puts {
            let Some(path) = put.get("path").and_then(Value::as_str) else {
                continue;
            };
            let record = FileRecord {
                contributor_id: put
                    .get("contributorId")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                logical_path: path.to_string(),
                size: put.get("size").and_then(Value::as_u64).unwrap_or(0),
                sha256: put
                    .get("sha256")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            };
            let key = (record.contributor_id.clone(), record.logical_path.clone());
            if let Some(&idx) = index_by_key.get(&key) {
                result[idx] = record;
            } else {
                index_by_key.insert(key, result.len());
                result.push(record);
            }
        }
    }

    if let Some(deletes) = delta.get("delete").and_then(Value::as_array) {
        let delete_keys: HashSet<(String, String)> = deletes
            .iter()
            .filter_map(|del| {
                let path = del.get("path").and_then(Value::as_str)?;
                let contrib = del
                    .get("contributorId")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                Some((contrib.to_string(), path.to_string()))
            })
            .collect();
        if !delete_keys.is_empty() {
            result.retain(|item| {
                !delete_keys.contains(&(item.contributor_id.clone(), item.logical_path.clone()))
            });
        }
    }

    result
}

/// Metadata-only view of one decrypted chain member (no payload bytes).
#[derive(Clone, Debug)]
pub struct ChainPackage {
    pub snapshot_kind: String,
    pub files: Vec<FileRecord>,
    pub root_digest: String,
    pub operations: Option<Value>,
    pub pack_index: Option<Value>,
    pub frontend: bool,
    pub contributor_ids: BTreeSet<String>,
    pub external_mcp: bool,
    pub manifest: Value,
}

/// Verified projection plan and dependency closure over an ancestor chain.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProjectionPlan {
    pub selection: RestoreSelection,
    pub selection_digest: String,
    pub output_files: BTreeSet<String>,
    pub support_files: BTreeSet<String>,
    pub needed_full_entries: BTreeSet<String>,
    pub needed_packs: BTreeSet<String>,
    pub needed_blobs: BTreeSet<String>,
    pub needed_standalone: BTreeSet<String>,
    pub needed_after_layer: Vec<BTreeSet<String>>,
    pub produced_by_layer: Vec<BTreeSet<String>>,
    pub requires_frontend_apply: bool,
    pub requires_external_mcp: bool,
    #[serde(skip)]
    pub report: Value,
}

pub fn chain_roots(chain: &[ChainPackage]) -> Result<Vec<Vec<FileRecord>>, AppError> {
    if chain.is_empty() {
        return Err(invalid_payload(
            "Restore projection requires a non-empty chain",
        ));
    }
    let mut states = Vec::with_capacity(chain.len());
    let mut current = chain[0].files.clone();
    if !chain[0].root_digest.is_empty() && snapshot_root(&current) != chain[0].root_digest {
        return Err(invalid_payload("Full baseline Merkle root mismatch"));
    }
    states.push(current.clone());
    for (index, package) in chain.iter().enumerate().skip(1) {
        let Some(ops) = package.operations.as_ref().filter(|v| v.is_object()) else {
            return Err(invalid_payload(format!(
                "Incremental chain member {index} has no operations"
            )));
        };
        let parent = ops
            .get("parentRootDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !parent.is_empty() && snapshot_root(&current) != parent {
            return Err(invalid_payload(format!(
                "Incremental chain parent root mismatch at {index}"
            )));
        }
        current = apply_delta_ops(&current, ops);
        let expected = ops
            .get("rootDigest")
            .and_then(Value::as_str)
            .unwrap_or(&package.root_digest);
        if !expected.is_empty() && snapshot_root(&current) != expected {
            return Err(invalid_payload(format!(
                "Incremental chain Merkle root mismatch at {index}"
            )));
        }
        states.push(current.clone());
    }
    Ok(states)
}

fn under_selected_project(logical_path: &str, project_ids: &HashSet<String>) -> bool {
    let mut parts = logical_path.split('/');
    matches!(
        (parts.next(), parts.next(), parts.next()),
        (Some("payload"), Some("projects"), Some(project_id)) if project_ids.contains(project_id)
    )
}

fn known_contributors(chain: &[ChainPackage], final_files: &[FileRecord]) -> BTreeSet<String> {
    let mut known: BTreeSet<String> = final_files
        .iter()
        .map(|r| r.contributor_id.clone())
        .collect();
    if let Some(target) = chain.last() {
        known.extend(target.contributor_ids.iter().cloned());
        if target.frontend {
            known.insert(FRONTEND_CONTRIBUTOR.to_string());
        }
        if target.external_mcp {
            known.insert(STATELESS_MCP_CONTRIBUTOR.to_string());
        }
    }
    known
}

fn project_ids_in_final(final_files: &[FileRecord]) -> BTreeSet<String> {
    let mut result = BTreeSet::new();
    for record in final_files {
        let mut parts = record.logical_path.split('/');
        if let (Some("payload"), Some("projects"), Some(project_id)) =
            (parts.next(), parts.next(), parts.next())
        {
            if !project_id.is_empty() {
                result.insert(project_id.to_string());
            }
        }
    }
    result
}

pub fn validate_selection(
    selection: &RestoreSelection,
    chain: &[ChainPackage],
    final_files: &[FileRecord],
) -> Vec<String> {
    let mut errors = Vec::new();
    if selection.contributors.is_empty() {
        errors.push("selection is empty".to_string());
    }
    let known = known_contributors(chain, final_files);
    let unknown: Vec<String> = selection
        .contributors
        .iter()
        .filter(|c| !known.contains(*c))
        .cloned()
        .collect();
    if !unknown.is_empty() {
        errors.push(format!(
            "selection includes unsupported contributors: {}",
            unknown.join(", ")
        ));
    }
    let selected_set: HashSet<&str> = selection.contributors.iter().map(String::as_str).collect();
    if !selection.project_ids.is_empty() && !selected_set.contains(PROJECTS_CONTRIBUTOR) {
        errors.push("selection projectIds require the projects contributor".to_string());
    }
    if selected_set.contains(PROJECTS_CONTRIBUTOR) && !selection.project_ids.is_empty() {
        let available = project_ids_in_final(final_files);
        let missing: Vec<String> = selection
            .project_ids
            .iter()
            .filter(|pid| !available.contains(*pid))
            .cloned()
            .collect();
        if !missing.is_empty() {
            errors.push(format!(
                "selection includes unknown project ids: {}",
                missing.join(", ")
            ));
        }
    }
    errors
}

fn selectable_files(final_files: &[FileRecord], selection: &RestoreSelection) -> BTreeSet<String> {
    let selected: HashSet<&str> = selection.contributors.iter().map(String::as_str).collect();
    let project_ids: HashSet<String> = selection.project_ids.iter().cloned().collect();
    let mut output = BTreeSet::new();
    for record in final_files {
        if !selected.contains(record.contributor_id.as_str()) {
            continue;
        }
        if record.logical_path.starts_with(MIGRATION_PATH_PREFIX) {
            continue;
        }
        if record.contributor_id == PROJECTS_CONTRIBUTOR
            && !project_ids.is_empty()
            && !under_selected_project(&record.logical_path, &project_ids)
        {
            continue;
        }
        output.insert(record.logical_path.clone());
    }
    output
}

fn put_parent_dependencies(put: &Value) -> BTreeSet<String> {
    let mut deps = BTreeSet::new();
    let storage = put
        .get("storage")
        .and_then(Value::as_str)
        .unwrap_or("whole");
    if storage == "parent-file" {
        if let Some(parent) = put
            .get("parentPath")
            .and_then(Value::as_str)
            .filter(|p| !p.is_empty())
        {
            deps.insert(parent.to_string());
        }
    } else if storage == "cdc" {
        let path = put.get("path").and_then(Value::as_str).unwrap_or("");
        if let Some(chunks) = put.get("chunks").and_then(Value::as_array) {
            for chunk in chunks {
                let source = chunk.get("source").and_then(Value::as_str).unwrap_or("");
                if source == "parent" && !path.is_empty() {
                    deps.insert(path.to_string());
                } else if source == "parent-range" {
                    if let Some(parent) = chunk
                        .get("parentPath")
                        .and_then(Value::as_str)
                        .filter(|p| !p.is_empty())
                    {
                        deps.insert(parent.to_string());
                    }
                }
            }
        }
    }
    deps
}

type ClosureResult = (Vec<BTreeSet<String>>, Vec<BTreeSet<String>>);

fn backward_closure(
    chain: &[ChainPackage],
    output_set: &BTreeSet<String>,
) -> Result<ClosureResult, AppError> {
    let layer_count = chain.len();
    let mut needed_after = vec![BTreeSet::new(); layer_count];
    needed_after[layer_count - 1] = output_set.clone();
    let mut produced_by_layer = vec![BTreeSet::new(); layer_count];

    for k in (1..layer_count).rev() {
        let ops = chain[k].operations.as_ref();
        let mut puts_by_path: HashMap<String, &Value> = HashMap::new();
        if let Some(puts) = ops.and_then(|o| o.get("put")).and_then(Value::as_array) {
            for item in puts {
                if let Some(path) = item.get("path").and_then(Value::as_str) {
                    puts_by_path.insert(path.to_string(), item);
                }
            }
        }
        let mut deleted = HashSet::new();
        if let Some(dels) = ops.and_then(|o| o.get("delete")).and_then(Value::as_array) {
            for item in dels {
                if let Some(path) = item.get("path").and_then(Value::as_str) {
                    deleted.insert(path.to_string());
                }
            }
        }
        let mut next_needed = BTreeSet::new();
        for path in &needed_after[k] {
            if let Some(put) = puts_by_path.get(path) {
                next_needed.extend(put_parent_dependencies(put));
                produced_by_layer[k].insert(path.clone());
            } else if deleted.contains(path) {
                return Err(invalid_payload(format!(
                    "Selected or required file is deleted by a later snapshot: {path}"
                )));
            } else {
                next_needed.insert(path.clone());
            }
        }
        needed_after[k - 1] = next_needed;
    }

    let baseline_paths: HashSet<&str> = chain[0]
        .files
        .iter()
        .map(|r| r.logical_path.as_str())
        .collect();
    let missing: Vec<String> = needed_after[0]
        .iter()
        .filter(|path| !baseline_paths.contains(path.as_str()))
        .cloned()
        .collect();
    if !missing.is_empty() {
        return Err(invalid_payload(format!(
            "Projection depends on baseline files that are not in the snapshot: {}",
            missing.join(", ")
        )));
    }
    Ok((needed_after, produced_by_layer))
}

/// Normalize a v1/v2 pack index JSON object into a blobId -> pack mapping.
fn pack_for_blob_from_json(index: Option<&Value>, blob_id: &str) -> Option<String> {
    let idx = index?;
    let schema = idx.get("schemaVersion").and_then(Value::as_i64)?;
    if schema == 1 {
        return idx
            .get("entries")?
            .get(blob_id)?
            .get("pack")
            .and_then(Value::as_str)
            .map(str::to_string);
    }
    if schema == 2 {
        let ordinal_str = blob_id.strip_prefix("blob_")?;
        let ordinal: usize = ordinal_str.parse().ok()?;
        let pack_ordinal = idx
            .get("entries")?
            .as_array()?
            .get(ordinal)?
            .as_array()?
            .first()?
            .as_u64()? as usize;
        return idx
            .get("packs")?
            .as_array()?
            .get(pack_ordinal)?
            .get("path")
            .and_then(Value::as_str)
            .map(str::to_string);
    }
    None
}

pub type LayerPayloads = (BTreeSet<String>, BTreeSet<String>, BTreeSet<String>);

/// Returns `(packs, blobs, standalone)` needed to materialize `layer`.
pub fn layer_needed_payloads(
    chain: &[ChainPackage],
    produced_by_layer: &[BTreeSet<String>],
    layer: usize,
) -> Result<LayerPayloads, AppError> {
    let mut packs = BTreeSet::new();
    let mut blobs = BTreeSet::new();
    let mut standalone = BTreeSet::new();
    if layer == 0 || layer >= chain.len() {
        return Ok((packs, blobs, standalone));
    }
    let ops = chain[layer].operations.as_ref();
    let index = chain[layer].pack_index.as_ref();
    let produced = &produced_by_layer[layer];
    let Some(puts) = ops.and_then(|o| o.get("put")).and_then(Value::as_array) else {
        return Ok((packs, blobs, standalone));
    };
    for put in puts {
        let Some(path) = put.get("path").and_then(Value::as_str) else {
            continue;
        };
        if !produced.contains(path) {
            continue;
        }
        let storage = put
            .get("storage")
            .and_then(Value::as_str)
            .unwrap_or("whole");
        if storage == "cdc" {
            if let Some(chunks) = put.get("chunks").and_then(Value::as_array) {
                for chunk in chunks {
                    if chunk.get("source").and_then(Value::as_str) == Some("payload") {
                        let (kind, locator) = parse_payload_ref(chunk.get("payloadRef"))?;
                        if kind == "pack-range" {
                            if let Some(pack) =
                                pack_for_blob_from_json(index, &locator).filter(|p| !p.is_empty())
                            {
                                packs.insert(pack);
                            }
                            blobs.insert(locator);
                        } else if kind == "standalone" {
                            standalone.insert(locator);
                        }
                    }
                }
            }
        } else if storage == "whole" {
            let (kind, locator) = parse_payload_ref(put.get("payloadRef"))?;
            if kind == "pack-range" {
                if let Some(pack) =
                    pack_for_blob_from_json(index, &locator).filter(|p| !p.is_empty())
                {
                    packs.insert(pack);
                }
                blobs.insert(locator);
            } else if kind == "standalone" {
                standalone.insert(locator);
            }
        }
    }
    Ok((packs, blobs, standalone))
}

fn count_parent_ranges(chain: &[ChainPackage], produced_by_layer: &[BTreeSet<String>]) -> usize {
    let mut total = 0;
    for (k, package) in chain.iter().enumerate().skip(1) {
        let Some(puts) = package
            .operations
            .as_ref()
            .and_then(|o| o.get("put"))
            .and_then(Value::as_array)
        else {
            continue;
        };
        let produced = &produced_by_layer[k];
        for put in puts {
            let Some(path) = put.get("path").and_then(Value::as_str) else {
                continue;
            };
            if !produced.contains(path) || put.get("storage").and_then(Value::as_str) != Some("cdc")
            {
                continue;
            }
            if let Some(chunks) = put.get("chunks").and_then(Value::as_array) {
                total += chunks
                    .iter()
                    .filter(|c| c.get("source").and_then(Value::as_str) == Some("parent-range"))
                    .count();
            }
        }
    }
    total
}

/// Build a verified projection plan and compute exact logical & dependency byte counts.
pub fn plan_projection(
    selection: &RestoreSelection,
    chain: &[ChainPackage],
    ciphertext_download_bytes: u64,
    selection_digest_override: Option<&str>,
) -> Result<ProjectionPlan, AppError> {
    let states = chain_roots(chain)?;
    let final_files = states
        .last()
        .ok_or_else(|| invalid_payload("Restore projection requires a non-empty chain"))?;
    let errors = validate_selection(selection, chain, final_files);
    if !errors.is_empty() {
        return Err(conflict_error(errors.join("; ")));
    }
    let output_set = selectable_files(final_files, selection);
    if output_set.is_empty() {
        return Err(conflict_error(
            "Restore selection matches no restorable files in this snapshot",
        ));
    }
    let (needed_after, produced_by_layer) = backward_closure(chain, &output_set)?;
    let mut packs = BTreeSet::new();
    let mut blobs = BTreeSet::new();
    let mut standalone = BTreeSet::new();
    for k in 1..chain.len() {
        let (lp, lb, ls) = layer_needed_payloads(chain, &produced_by_layer, k)?;
        packs.extend(lp);
        blobs.extend(lb);
        standalone.extend(ls);
    }
    let mut all_needed = BTreeSet::new();
    for level in &needed_after {
        all_needed.extend(level.iter().cloned());
    }
    let support_files: BTreeSet<String> = all_needed.difference(&output_set).cloned().collect();
    let final_by_path: HashMap<&str, &FileRecord> = final_files
        .iter()
        .map(|r| (r.logical_path.as_str(), r))
        .collect();
    let selected_logical_bytes: u64 = output_set
        .iter()
        .filter_map(|p| final_by_path.get(p.as_str()).map(|r| r.size))
        .sum();
    let dependency_bytes: u64 = support_files
        .iter()
        .filter_map(|p| final_by_path.get(p.as_str()).map(|r| r.size))
        .sum();
    let estimated_materialized = selected_logical_bytes + dependency_bytes;
    let selection_digest = match selection_digest_override {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => selection.digest(),
    };
    let requires_frontend = selection
        .contributors
        .iter()
        .any(|c| c == FRONTEND_CONTRIBUTOR);
    let requires_external = selection
        .contributors
        .iter()
        .any(|c| c == STATELESS_MCP_CONTRIBUTOR);
    let report = json!({
        "selectionDigest": selection_digest,
        "selection": selection.canonical_value(),
        "selected": {
            "contributors": selection.contributors.len(),
            "projects": selection.project_ids.len(),
            "files": output_set.len(),
        },
        "dependencies": {
            "supportFiles": support_files.len(),
            "parentRanges": count_parent_ranges(chain, &produced_by_layer),
            "packs": packs.len(),
        },
        "bytes": {
            "selectedLogicalBytes": selected_logical_bytes,
            "dependencyBytes": dependency_bytes,
            "ciphertextDownloadBytes": ciphertext_download_bytes,
            "estimatedMaterializedBytes": estimated_materialized,
        },
        "networkSelective": false,
        "selectiveFetchSupported": false,
        "networkSelectivityReason": NETWORK_SELECTIVITY_REASON,
        "requiresFrontendApply": requires_frontend,
        "requiresExternalMcp": requires_external,
    });
    Ok(ProjectionPlan {
        selection: selection.clone(),
        selection_digest,
        output_files: output_set,
        support_files,
        needed_full_entries: needed_after[0].clone(),
        needed_packs: packs,
        needed_blobs: blobs,
        needed_standalone: standalone,
        needed_after_layer: needed_after,
        produced_by_layer,
        requires_frontend_apply: requires_frontend,
        requires_external_mcp: requires_external,
        report,
    })
}

pub type ControlComponentInventory = (BTreeMap<String, String>, BTreeMap<String, Value>);

/// Validate `component-map.json`, `payload-index.json`, and member remote object commitments.
pub fn validate_control_component_inventory(
    member: &Value,
    component_map: &Value,
    payload_index: &Value,
) -> Result<ControlComponentInventory, AppError> {
    let valid_headers = component_map.get("schemaVersion").and_then(Value::as_i64) == Some(1)
        && payload_index.get("schemaVersion").and_then(Value::as_i64) == Some(1)
        && payload_index.get("storageProtocol").and_then(Value::as_str) == Some(OBJECT_SET_V1);
    if !valid_headers {
        return Err(invalid_payload("Object-set control metadata is invalid"));
    }
    let Some(raw_paths) = component_map.get("paths").and_then(Value::as_object) else {
        return Err(invalid_payload("Object-set component map is invalid"));
    };
    let Some(raw_components) = component_map.get("components").and_then(Value::as_object) else {
        return Err(invalid_payload("Object-set component map is invalid"));
    };
    let Some(raw_descriptors) = payload_index
        .get("payloadComponents")
        .and_then(Value::as_object)
    else {
        return Err(invalid_payload("Object-set component map is invalid"));
    };
    let comp_keys: BTreeSet<&String> = raw_components.keys().collect();
    let desc_keys: BTreeSet<&String> = raw_descriptors.keys().collect();
    if comp_keys != desc_keys {
        return Err(invalid_payload(
            "Object-set component inventory is inconsistent",
        ));
    }

    let mut normalized_paths = BTreeMap::<String, String>::new();
    for (component_id, raw_entries) in raw_components {
        let Some(entries) = raw_entries.as_array() else {
            return Err(invalid_payload("Object-set component path map is invalid"));
        };
        if component_id.is_empty() || entries.is_empty() {
            return Err(invalid_payload("Object-set component path map is invalid"));
        }
        for raw_path in entries {
            let Some(path_str) = raw_path.as_str() else {
                return Err(invalid_payload("Object-set component path map is invalid"));
            };
            let path = validate_archive_path(path_str)?;
            if normalized_paths.contains_key(&path) {
                return Err(invalid_payload("Object-set component paths overlap"));
            }
            normalized_paths.insert(path, component_id.clone());
        }
    }
    if raw_paths.len() != normalized_paths.len() {
        return Err(invalid_payload(
            "Object-set component path map is inconsistent",
        ));
    }
    for (raw_path, comp_val) in raw_paths {
        let Some(comp_id) = comp_val.as_str() else {
            return Err(invalid_payload("Object-set component path map is invalid"));
        };
        let path = validate_archive_path(raw_path)?;
        if normalized_paths.get(&path).map(String::as_str) != Some(comp_id) {
            return Err(invalid_payload(
                "Object-set component path map is inconsistent",
            ));
        }
    }
    let reachable_comps: BTreeSet<&String> = normalized_paths.values().collect();
    if reachable_comps != comp_keys {
        return Err(invalid_payload(
            "Object-set component inventory contains an unreachable payload",
        ));
    }

    let mut remote_by_digest = HashMap::<String, &Value>::new();
    if let Some(objs) = member.get("objects").and_then(Value::as_array) {
        for item in objs {
            if let Some(d) = item.get("digest").and_then(Value::as_str) {
                remote_by_digest.insert(d.to_string(), item);
            }
        }
    }
    let control_digest = member
        .get("controlObjectDigest")
        .and_then(Value::as_str)
        .unwrap_or("");
    let expected_payload_digests: BTreeSet<String> = remote_by_digest
        .keys()
        .filter(|d| d.as_str() != control_digest)
        .cloned()
        .collect();
    let mut descriptors = BTreeMap::new();
    let mut descriptor_digests = BTreeSet::new();
    for (component_id, descriptor) in raw_descriptors {
        if component_id.is_empty() || !descriptor.is_object() {
            return Err(invalid_payload("Object-set payload descriptor is invalid"));
        }
        let digest = descriptor
            .get("ciphertextDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        let size = descriptor.get("ciphertextSize").and_then(Value::as_u64);
        let plaintext_size = descriptor.get("plaintextSize").and_then(Value::as_u64);
        let plaintext_digest = descriptor
            .get("plaintextSha256")
            .and_then(Value::as_str)
            .unwrap_or("");
        let remote = remote_by_digest.get(digest);
        let valid = is_plain_sha256(digest)
            && !descriptor_digests.contains(digest)
            && digest != control_digest
            && size.is_some()
            && plaintext_size.is_some()
            && is_plain_sha256(plaintext_digest)
            && remote.and_then(|r| r.get("size").and_then(Value::as_u64)) == size;
        if !valid {
            return Err(invalid_payload(
                "Object-set control references a foreign component or invalid commitment",
            ));
        }
        descriptor_digests.insert(digest.to_string());
        descriptors.insert(component_id.clone(), descriptor.clone());
    }
    if descriptor_digests != expected_payload_digests {
        return Err(invalid_payload(
            "Object-set receipt contains a foreign component",
        ));
    }
    Ok((normalized_paths, descriptors))
}

static CDC_GEARS: LazyLock<[u64; 256]> = LazyLock::new(|| {
    let mut table = [0_u64; 256];
    let mut seed: u64 = 0x9E37_79B9_7F4A_7C15;
    for slot in &mut table {
        seed = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = seed;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        *slot = z;
    }
    table
});

/// Split a file into exact `(offset, length)` FastCDC ranges matching `fastcdc-gear-v2/v3`.
pub fn chunk_file_ranges(path: &Path, protocol: &str) -> Result<Vec<(u64, u64)>, AppError> {
    if protocol != CDC_ALGORITHM_V2 && protocol != CDC_ALGORITHM_V3 {
        return Err(invalid_payload(format!(
            "Unsupported CDC chunk protocol: {protocol}"
        )));
    }
    let meta = fs::metadata(path).map_err(|_| invalid_payload("Delta parent file is missing"))?;
    let file_size = meta.len();
    let (early_bits, late_bits) = if protocol == CDC_ALGORITHM_V2 || file_size <= 16 * 1024 * 1024 {
        (13_u32, 22_u32)
    } else {
        (22_u32, 20_u32)
    };
    let early_mask = (1_u64 << early_bits) - 1;
    let late_mask = (1_u64 << late_bits) - 1;
    let gears = &*CDC_GEARS;

    let mut file = BufReader::new(
        File::open(path).map_err(|_| invalid_payload("Delta parent file is unreadable"))?,
    );
    let mut ranges = Vec::new();
    let mut chunk_len = 0_usize;
    let mut chunk_start = 0_u64;
    let mut offset = 0_u64;
    let mut fp = 0_u64;
    let mut buf = vec![0_u8; COPY_BUFFER_BYTES];

    loop {
        let n = file
            .read(&mut buf)
            .map_err(|_| invalid_payload("Delta parent file is unreadable"))?;
        if n == 0 {
            break;
        }
        for &byte in &buf[..n] {
            chunk_len += 1;
            let emit = if chunk_len >= CDC_MAX_CHUNK {
                true
            } else if chunk_len >= CDC_MIN_CHUNK {
                fp = (fp << 1).wrapping_add(gears[byte as usize]);
                let mask = if chunk_len >= CDC_AVG_CHUNK {
                    late_mask
                } else {
                    early_mask
                };
                (fp & mask) == 0
            } else {
                false
            };
            if emit {
                ranges.push((chunk_start, chunk_len as u64));
                chunk_len = 0;
                fp = 0;
                chunk_start = offset + 1;
            }
            offset += 1;
        }
    }

    if chunk_len > 0 {
        if protocol == CDC_ALGORITHM_V3 && ranges.is_empty() && chunk_len >= CDC_AVG_CHUNK {
            let midpoint = (chunk_len / 2) as u64;
            ranges.push((chunk_start, midpoint));
            ranges.push((chunk_start + midpoint, chunk_len as u64 - midpoint));
        } else {
            ranges.push((chunk_start, chunk_len as u64));
        }
    }
    Ok(ranges)
}

fn target_from_relative(root: &Path, relative: &str) -> Result<PathBuf, AppError> {
    let normalized = validate_archive_path(relative)?;
    let mut path = root.to_path_buf();
    for segment in normalized.split('/') {
        path.push(segment);
    }
    Ok(path)
}

fn copy_verified_range(
    source_path: &Path,
    output: &mut BufWriter<File>,
    offset: u64,
    expected_length: u64,
    expected_sha256: &str,
    label: &str,
) -> Result<(), AppError> {
    let meta = fs::metadata(source_path)
        .map_err(|_| invalid_payload(format!("Delta payload range is missing: {label}")))?;
    if offset
        .checked_add(expected_length)
        .is_none_or(|end| end > meta.len())
    {
        return Err(invalid_payload(format!(
            "Delta CDC chunk length mismatch: {label}"
        )));
    }
    let mut reader = File::open(source_path)
        .map_err(|_| invalid_payload(format!("Delta payload range is missing: {label}")))?;
    reader
        .seek(SeekFrom::Start(offset))
        .map_err(|_| invalid_payload(format!("Delta payload range is missing: {label}")))?;
    let mut remaining = expected_length;
    let mut buf = vec![0_u8; COPY_BUFFER_BYTES.min(expected_length as usize).max(1)];
    let mut hasher = Sha256::new();
    while remaining > 0 {
        let to_read = (remaining as usize).min(buf.len());
        let n = reader
            .read(&mut buf[..to_read])
            .map_err(|_| invalid_payload(format!("Delta CDC chunk length mismatch: {label}")))?;
        if n == 0 {
            return Err(invalid_payload(format!(
                "Delta CDC chunk length mismatch: {label}"
            )));
        }
        output
            .write_all(&buf[..n])
            .map_err(|err| internal_error(err.to_string()))?;
        hasher.update(&buf[..n]);
        remaining -= n as u64;
    }
    if !expected_sha256.is_empty() && format!("{:x}", hasher.finalize()) != expected_sha256 {
        return Err(invalid_payload(format!(
            "Delta CDC chunk checksum mismatch: {label}"
        )));
    }
    Ok(())
}

struct LazyPackVerifier {
    package_root: PathBuf,
    index: ParsedPackIndex,
    verified: HashSet<String>,
}

impl LazyPackVerifier {
    fn open(package_root: &Path) -> Result<Self, AppError> {
        let index = parse_pack_index_file(package_root)?;
        Ok(Self {
            package_root: package_root.to_path_buf(),
            index,
            verified: HashSet::new(),
        })
    }

    fn verify_and_path(
        &mut self,
        blob_id: &str,
    ) -> Result<(PathBuf, u64, u64, Option<String>), AppError> {
        let entry = self
            .index
            .entries
            .get(blob_id)
            .ok_or_else(|| invalid_payload(format!("Delta pack blob is missing: {blob_id}")))?
            .clone();
        if !self.verified.contains(&entry.pack) {
            let spec = self.index.packs.get(&entry.pack).ok_or_else(|| {
                invalid_payload(format!(
                    "Delta pack is missing from the index: {}",
                    entry.pack
                ))
            })?;
            let pack_path = target_from_relative(&self.package_root, &spec.path)?;
            let ok = pack_path.is_file()
                && fs::metadata(&pack_path).map(|m| m.len()).ok() == Some(spec.size)
                && sha256_file(&pack_path).ok().as_deref() == Some(spec.sha256.as_str());
            if !ok {
                return Err(invalid_payload(format!(
                    "Delta pack failed checksum: {}",
                    spec.path
                )));
            }
            self.verified.insert(entry.pack.clone());
        }
        let pack_path = target_from_relative(&self.package_root, &entry.pack)?;
        Ok((pack_path, entry.offset, entry.length, entry.sha256))
    }
}

fn copy_payload_source(
    package_root: &Path,
    raw_ref: Option<&Value>,
    pack_cache: Option<&mut LazyPackVerifier>,
    output: &mut BufWriter<File>,
    expected_sha256: &str,
    expected_length: u64,
) -> Result<(), AppError> {
    let (kind, locator) = parse_payload_ref(raw_ref)?;
    if kind == "pack-range" {
        let cache = pack_cache.ok_or_else(|| invalid_payload("Delta pack index is missing"))?;
        let (pack_path, offset, length, indexed_sha) = cache.verify_and_path(&locator)?;
        if length != expected_length
            || indexed_sha
                .as_deref()
                .is_some_and(|d| !expected_sha256.is_empty() && d != expected_sha256)
        {
            return Err(invalid_payload(format!(
                "Delta pack blob metadata mismatch: {locator}"
            )));
        }
        copy_verified_range(
            &pack_path,
            output,
            offset,
            length,
            expected_sha256,
            &locator,
        )
    } else {
        let standalone = target_from_relative(package_root, &locator)?;
        copy_verified_range(
            &standalone,
            output,
            0,
            expected_length,
            expected_sha256,
            &locator,
        )
    }
}

fn materialize_cdc(
    package_root: &Path,
    parent_root: &Path,
    target: &Path,
    put: &Value,
    chunk_protocol: &str,
    mut pack_cache: Option<&mut LazyPackVerifier>,
) -> Result<(), AppError> {
    let logical_path = put
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_payload("Delta put entry is invalid"))?;
    let Some(chunks) = put.get("chunks").and_then(Value::as_array) else {
        return Err(invalid_payload(format!(
            "Delta CDC file has no chunk list: {logical_path}"
        )));
    };
    let tmp = target.with_file_name(format!(".tmp.{}.restore", UuidHex::generate()));
    let write_res = (|| -> Result<(), AppError> {
        let file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|err| internal_error(err.to_string()))?;
        let mut output = BufWriter::new(file);
        for (index, chunk) in chunks.iter().enumerate() {
            if !chunk.is_object() {
                return Err(invalid_payload(format!(
                    "Delta CDC file has an invalid chunk: {logical_path}"
                )));
            }
            let length = chunk.get("length").and_then(Value::as_u64).unwrap_or(0);
            let expected = chunk.get("sha256").and_then(Value::as_str).unwrap_or("");
            let label = format!("{logical_path}#{index}");
            let source_kind = chunk.get("source").and_then(Value::as_str).unwrap_or("");
            match source_kind {
                "parent" => {
                    let parent_path = target_from_relative(parent_root, logical_path)?;
                    if !parent_path.is_file() {
                        return Err(invalid_payload(format!(
                            "Delta references a missing parent file: {logical_path}"
                        )));
                    }
                    let ranges = chunk_file_ranges(&parent_path, chunk_protocol)?;
                    let ordinal = chunk
                        .get("parentOrdinal")
                        .and_then(Value::as_u64)
                        .and_then(|v| usize::try_from(v).ok())
                        .ok_or_else(|| {
                            invalid_payload(format!(
                                "Delta CDC references an invalid parent chunk: {logical_path}"
                            ))
                        })?;
                    let &(offset, chunk_length) = ranges.get(ordinal).ok_or_else(|| {
                        invalid_payload(format!(
                            "Delta CDC references an invalid parent chunk: {logical_path}"
                        ))
                    })?;
                    if chunk_length != length {
                        return Err(invalid_payload(format!(
                            "Delta CDC parent chunk length mismatch: {logical_path}"
                        )));
                    }
                    copy_verified_range(
                        &parent_path,
                        &mut output,
                        offset,
                        length,
                        expected,
                        &label,
                    )?;
                }
                "parent-range" => {
                    let rel = chunk
                        .get("parentPath")
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let parent_path = target_from_relative(parent_root, rel)?;
                    if !parent_path.is_file() {
                        return Err(invalid_payload(format!(
                            "Delta references a missing parent file: {rel}"
                        )));
                    }
                    let Some(offset) = chunk.get("offset").and_then(Value::as_u64) else {
                        return Err(invalid_payload(format!(
                            "Delta CDC references an invalid parent range: {logical_path}"
                        )));
                    };
                    if length == 0 {
                        return Err(invalid_payload(format!(
                            "Delta CDC references an invalid parent range: {logical_path}"
                        )));
                    }
                    let psize = fs::metadata(&parent_path)
                        .map(|m| m.len())
                        .map_err(|err| internal_error(err.to_string()))?;
                    if offset.checked_add(length).is_none_or(|end| end > psize) {
                        return Err(invalid_payload(format!(
                            "Delta CDC parent range exceeds its file: {logical_path}"
                        )));
                    }
                    copy_verified_range(
                        &parent_path,
                        &mut output,
                        offset,
                        length,
                        expected,
                        &label,
                    )?;
                }
                _ => {
                    copy_payload_source(
                        package_root,
                        chunk.get("payloadRef"),
                        pack_cache.as_deref_mut(),
                        &mut output,
                        expected,
                        length,
                    )?;
                }
            }
        }
        output
            .flush()
            .map_err(|err| internal_error(err.to_string()))?;
        let inner = output
            .into_inner()
            .map_err(|err| internal_error(err.to_string()))?;
        inner
            .sync_all()
            .map_err(|err| internal_error(err.to_string()))?;
        Ok(())
    })();
    if let Err(err) = write_res {
        let _ = fs::remove_file(&tmp);
        return Err(err);
    }
    fs::rename(&tmp, target).map_err(|err| {
        let _ = fs::remove_file(&tmp);
        internal_error(err.to_string())
    })
}

fn materialize_put(
    package_root: &Path,
    parent_root: &Path,
    target: &Path,
    put: &Value,
    chunk_protocol: &str,
    pack_cache: Option<&mut LazyPackVerifier>,
) -> Result<(), AppError> {
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
    }
    let storage = put
        .get("storage")
        .and_then(Value::as_str)
        .unwrap_or("whole");
    let logical_path = put.get("path").and_then(Value::as_str).unwrap_or("");
    let expected = put.get("sha256").and_then(Value::as_str).unwrap_or("");
    let expected_size = put.get("size").and_then(Value::as_u64).unwrap_or(0);
    match storage {
        "cdc" => materialize_cdc(
            package_root,
            parent_root,
            target,
            put,
            chunk_protocol,
            pack_cache,
        )?,
        "whole" => {
            let file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(target)
                .map_err(|err| internal_error(err.to_string()))?;
            let mut writer = BufWriter::new(file);
            copy_payload_source(
                package_root,
                put.get("payloadRef"),
                pack_cache,
                &mut writer,
                expected,
                expected_size,
            )?;
            writer
                .flush()
                .map_err(|err| internal_error(err.to_string()))?;
        }
        "parent-file" => {
            let rel = put.get("parentPath").and_then(Value::as_str).unwrap_or("");
            let parent_path = target_from_relative(parent_root, rel)?;
            if !parent_path.is_file() {
                return Err(invalid_payload(format!(
                    "Delta references a missing parent file: {rel}"
                )));
            }
            let file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(target)
                .map_err(|err| internal_error(err.to_string()))?;
            let mut writer = BufWriter::new(file);
            copy_verified_range(&parent_path, &mut writer, 0, expected_size, expected, rel)?;
            writer
                .flush()
                .map_err(|err| internal_error(err.to_string()))?;
        }
        other => {
            return Err(invalid_payload(format!(
                "Unsupported delta storage: {other}"
            )));
        }
    }
    if !expected.is_empty() && sha256_file(target).ok().as_deref() != Some(expected) {
        return Err(invalid_payload(format!(
            "Delta file failed checksum after restore: {logical_path}"
        )));
    }
    Ok(())
}

fn read_json_dict(path: &Path, message: &str) -> Result<Value, AppError> {
    let raw = fs::read_to_string(path).map_err(|_| invalid_payload(message))?;
    let val: Value = serde_json::from_str(&raw).map_err(|_| invalid_payload(message))?;
    if !val.is_object() {
        return Err(invalid_payload(message));
    }
    Ok(val)
}

fn copy_dir_recursive(src: &Path, dst: &Path) -> Result<(), AppError> {
    if !src.is_dir() {
        return Ok(());
    }
    fs::create_dir_all(dst).map_err(|err| internal_error(err.to_string()))?;
    for entry in fs::read_dir(src).map_err(|err| internal_error(err.to_string()))? {
        let entry = entry.map_err(|err| internal_error(err.to_string()))?;
        let path = entry.path();
        let target = dst.join(entry.file_name());
        if path.is_dir() {
            copy_dir_recursive(&path, &target)?;
        } else if path.is_file() {
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
            }
            fs::copy(&path, &target).map_err(|err| internal_error(err.to_string()))?;
        }
    }
    Ok(())
}

fn copy_full_tree(package_root: &Path, output_root: &Path) -> Result<(), AppError> {
    for top in ["payload", "migration", "frontend"] {
        let src = package_root.join(top);
        if src.is_dir() {
            copy_dir_recursive(&src, &output_root.join(top))?;
        }
    }
    Ok(())
}

fn chunk_protocol_for_manifest(manifest: &Value) -> String {
    let from_snapshot = manifest
        .get("snapshot")
        .and_then(|s| s.get("chunkProtocol"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if !from_snapshot.is_empty() {
        return from_snapshot.to_string();
    }
    let from_top = manifest
        .get("chunkProtocol")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !from_top.is_empty() {
        return from_top.to_string();
    }
    CDC_ALGORITHM_V2.to_string()
}

/// Reconstruct the complete or projected workspace tree from `[F0, I1, ..., In]`
/// verifying every layer's domain-separated Merkle transition.
pub fn materialize_chain(
    package_roots: &[PathBuf],
    output_root: &Path,
    projection: Option<&ProjectionPlan>,
) -> Result<Value, AppError> {
    if package_roots.is_empty() {
        return Err(invalid_payload("Incremental restore chain is empty"));
    }
    fs::create_dir_all(output_root).map_err(|err| internal_error(err.to_string()))?;

    let mut current: Vec<FileRecord> = Vec::new();
    let mut final_manifest = Value::Null;

    let work_root = if projection.is_some() {
        let scratch = output_root.join(format!(".projection-support-{}", UuidHex::generate()));
        fs::create_dir_all(&scratch).map_err(|err| internal_error(err.to_string()))?;
        scratch
    } else {
        output_root.to_path_buf()
    };

    let run_res = (|| -> Result<Value, AppError> {
        for (index, package_root) in package_roots.iter().enumerate() {
            let manifest_path = package_root.join("manifest.json");
            if !manifest_path.is_file() {
                return Err(invalid_payload("Chain member is missing manifest.json"));
            }
            let manifest = read_json_dict(&manifest_path, "Chain package manifest is invalid")?;
            final_manifest = manifest.clone();
            let files = manifest_files(&manifest)?;
            if index == 0 {
                if manifest.get("snapshotKind").and_then(Value::as_str) == Some("incremental")
                    || package_root.join("delta").join("operations.json").is_file()
                {
                    return Err(invalid_payload(
                        "Incremental restore chain is missing its full baseline",
                    ));
                }
                if let Some(proj) = projection {
                    for rel in &proj.needed_full_entries {
                        let src = target_from_relative(package_root, rel)?;
                        if !src.is_file() {
                            return Err(invalid_payload(format!(
                                "Projection baseline file is missing: {rel}"
                            )));
                        }
                        let dst = target_from_relative(&work_root, rel)?;
                        if let Some(parent) = dst.parent() {
                            fs::create_dir_all(parent)
                                .map_err(|err| internal_error(err.to_string()))?;
                        }
                        fs::copy(&src, &dst).map_err(|err| internal_error(err.to_string()))?;
                    }
                } else {
                    copy_full_tree(package_root, &work_root)?;
                }
                current = files;
                let expected_root = manifest
                    .get("snapshot")
                    .and_then(|s| s.get("rootDigest"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if !expected_root.is_empty() && snapshot_root(&current) != expected_root {
                    return Err(invalid_payload("Full baseline Merkle root mismatch"));
                }
                continue;
            }

            let ops_path = package_root.join("delta").join("operations.json");
            if !ops_path.is_file() {
                return Err(invalid_payload(
                    "Incremental chain member is missing its operations manifest",
                ));
            }
            let ops = read_json_dict(
                &ops_path,
                "Incremental chain operations manifest is invalid",
            )?;
            if ops.get("put").is_none() || ops.get("delete").is_none() {
                return Err(invalid_payload(
                    "Incremental chain operations manifest is invalid",
                ));
            }
            let parent_root_digest = ops
                .get("parentRootDigest")
                .and_then(Value::as_str)
                .unwrap_or("");
            if parent_root_digest != snapshot_root(&current) {
                return Err(invalid_payload(format!(
                    "Incremental chain parent root mismatch at {index}"
                )));
            }

            let prepared_root = work_root.join(format!(".delta-prepared-{}", UuidHex::generate()));
            fs::create_dir_all(&prepared_root).map_err(|err| internal_error(err.to_string()))?;
            let chunk_protocol = chunk_protocol_for_manifest(&manifest);
            let mut pack_cache = if target_from_relative(package_root, PACK_INDEX_PATH)
                .map(|p| p.is_file())
                .unwrap_or(false)
            {
                Some(LazyPackVerifier::open(package_root)?)
            } else {
                None
            };

            let produced = projection.map(|p| &p.produced_by_layer[index]);
            let needed_after = projection.map(|p| &p.needed_after_layer[index]);

            let layer_res = (|| -> Result<(), AppError> {
                let Some(puts) = ops.get("put").and_then(Value::as_array) else {
                    return Err(invalid_payload("Delta put list is invalid"));
                };
                for put in puts {
                    if !put.is_object() {
                        return Err(invalid_payload("Delta put entry is invalid"));
                    }
                    let path = put
                        .get("path")
                        .and_then(Value::as_str)
                        .ok_or_else(|| invalid_payload("Delta put entry is invalid"))?;
                    if produced.is_some_and(|prod| !prod.contains(path)) {
                        continue;
                    }
                    let staged = target_from_relative(&prepared_root, path)?;
                    materialize_put(
                        package_root,
                        &work_root,
                        &staged,
                        put,
                        &chunk_protocol,
                        pack_cache.as_mut(),
                    )?;
                }

                let Some(deletes) = ops.get("delete").and_then(Value::as_array) else {
                    return Err(invalid_payload("Delta delete list is invalid"));
                };
                for del in deletes {
                    if !del.is_object() {
                        return Err(invalid_payload("Delta delete entry is invalid"));
                    }
                    let del_path = del
                        .get("path")
                        .and_then(Value::as_str)
                        .ok_or_else(|| invalid_payload("Delta delete entry is invalid"))?;
                    if needed_after.is_some_and(|needed| needed.contains(del_path)) {
                        return Err(invalid_payload(format!(
                            "Projection required file is deleted: {del_path}"
                        )));
                    }
                    let target = target_from_relative(&work_root, del_path)?;
                    if target.is_file() {
                        fs::remove_file(&target).map_err(|err| internal_error(err.to_string()))?;
                    } else if target.is_dir() {
                        fs::remove_dir_all(&target)
                            .map_err(|err| internal_error(err.to_string()))?;
                    }
                }

                for put in puts {
                    let Some(path) = put.get("path").and_then(Value::as_str) else {
                        continue;
                    };
                    if produced.is_some_and(|prod| !prod.contains(path)) {
                        continue;
                    }
                    let src = target_from_relative(&prepared_root, path)?;
                    let dst = target_from_relative(&work_root, path)?;
                    if let Some(parent) = dst.parent() {
                        fs::create_dir_all(parent)
                            .map_err(|err| internal_error(err.to_string()))?;
                    }
                    fs::rename(&src, &dst).map_err(|err| internal_error(err.to_string()))?;
                }
                Ok(())
            })();
            let _ = fs::remove_dir_all(&prepared_root);
            layer_res?;

            current = apply_delta_ops(&current, &ops);
            let expected_root = ops.get("rootDigest").and_then(Value::as_str).unwrap_or("");
            if !expected_root.is_empty() && snapshot_root(&current) != expected_root {
                return Err(invalid_payload(format!(
                    "Incremental chain Merkle root mismatch at {index}"
                )));
            }
            if projection.is_none() {
                for rec in &current {
                    let file_path = target_from_relative(&work_root, &rec.logical_path)?;
                    if !file_path.is_file() {
                        return Err(invalid_payload(format!(
                            "Restored tree is missing declared file: {}",
                            rec.logical_path
                        )));
                    }
                    if sha256_file(&file_path).ok().as_deref() != Some(rec.sha256.as_str()) {
                        return Err(invalid_payload(format!(
                            "Restored file failed checksum: {}",
                            rec.logical_path
                        )));
                    }
                }
            }
        }

        if let Some(proj) = projection {
            let final_by_path: HashMap<&str, &FileRecord> = current
                .iter()
                .map(|r| (r.logical_path.as_str(), r))
                .collect();
            let last_needed = proj
                .needed_after_layer
                .last()
                .ok_or_else(|| invalid_payload("Invalid projection closure"))?;
            for entry_path in last_needed {
                let record = final_by_path.get(entry_path.as_str()).ok_or_else(|| {
                    invalid_payload(format!("Projected file failed checksum: {entry_path}"))
                })?;
                let entry_target = target_from_relative(&work_root, entry_path)?;
                let ok = entry_target.is_file()
                    && sha256_file(&entry_target).ok().as_deref() == Some(record.sha256.as_str());
                if !ok {
                    return Err(invalid_payload(format!(
                        "Projected file failed checksum: {entry_path}"
                    )));
                }
            }
            for entry_path in &proj.output_files {
                let src = target_from_relative(&work_root, entry_path)?;
                let dst = target_from_relative(output_root, entry_path)?;
                if let Some(parent) = dst.parent() {
                    fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
                }
                fs::rename(&src, &dst).map_err(|err| internal_error(err.to_string()))?;
            }
        }
        Ok(final_manifest)
    })();

    if projection.is_some() {
        let _ = fs::remove_dir_all(&work_root);
    }
    run_res
}

/// Resolve `[full, ..., parent, target]` strictly from target receipt/catalog records.
pub fn resolve_lineage_from_receipts(
    catalog: &HashMap<String, Value>,
    backup_id: &str,
) -> Result<Vec<Value>, AppError> {
    let mut chain = Vec::new();
    let mut seen = HashSet::new();
    let mut current = backup_id.to_string();
    while !current.is_empty() {
        if !seen.insert(current.clone()) {
            return Err(conflict_error("incremental chain cycle detected"));
        }
        let record = catalog
            .get(&current)
            .ok_or_else(|| conflict_error(format!("missing parent snapshot in chain: {current}")))?
            .clone();
        let next = match record.get("parentBackupId") {
            Some(v) if python_truthy(v) => value_str(v),
            _ => String::new(),
        };
        chain.push(record);
        if next.is_empty() {
            break;
        }
        current = next;
    }
    chain.reverse();
    Ok(chain)
}

fn dir_metadata_digest(dir: &Path) -> String {
    if !dir.is_dir() {
        return String::new();
    }
    fn collect(
        base: &Path,
        current: &Path,
        out: &mut Vec<(String, PathBuf)>,
    ) -> Result<(), AppError> {
        for entry in fs::read_dir(current).map_err(|err| internal_error(err.to_string()))? {
            let entry = entry.map_err(|err| internal_error(err.to_string()))?;
            let path = entry.path();
            if path.is_dir() {
                collect(base, &path, out)?;
            } else if path.is_file() {
                let rel = path
                    .strip_prefix(base)
                    .map_err(|err| internal_error(err.to_string()))?
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((rel, path));
            }
        }
        Ok(())
    }
    let mut files = Vec::new();
    if collect(dir, dir, &mut files).is_err() || files.is_empty() {
        return String::new();
    }
    files.sort_by(|a, b| a.0.cmp(&b.0));
    let mut records = Vec::with_capacity(files.len());
    for (rel, path) in files {
        let Ok(digest) = sha256_file(&path) else {
            return String::new();
        };
        let Ok(meta) = fs::metadata(&path) else {
            return String::new();
        };
        records.push(json!({
            "path": rel,
            "sha256": digest,
            "size": meta.len(),
        }));
    }
    sha256_bytes(&stable_json_bytes(&Value::Array(records)))
}

fn verified_plan_bindings(base: &Path, session: &Value) -> Value {
    let chain = session
        .get("chain")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let object_sets: Vec<String> = chain
        .iter()
        .map(|m| {
            m.get("objectSetDigest")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        })
        .collect();
    let controls: Vec<String> = chain
        .iter()
        .map(|m| {
            let from_member = m.get("controlObjectDigest").and_then(Value::as_str);
            let from_ctrl = m
                .get("control")
                .and_then(|c| c.get("objectDigest"))
                .and_then(Value::as_str);
            from_member.or(from_ctrl).unwrap_or("").to_string()
        })
        .collect();
    let mut required = Vec::new();
    for member in chain {
        if let Some(reqs) = member.get("requiredComponents").and_then(Value::as_array) {
            for comp in reqs {
                if !comp.is_object() {
                    continue;
                }
                let priority = comp.get("priority").and_then(Value::as_i64).unwrap_or(2);
                required.push(json!({
                    "componentId": comp.get("componentId").and_then(Value::as_str).unwrap_or(""),
                    "expectedBytes": comp.get("expectedBytes").and_then(Value::as_u64).unwrap_or(0),
                    "objectDigest": comp.get("objectDigest").and_then(Value::as_str).unwrap_or(""),
                    "plaintextSha256": comp.get("plaintextSha256").and_then(Value::as_str).unwrap_or(""),
                    "plaintextSize": comp.get("plaintextSize").and_then(Value::as_u64).unwrap_or(0),
                    "priority": priority,
                }));
            }
        }
    }
    let metadata_digests: Vec<String> = (0..chain.len())
        .map(|idx| dir_metadata_digest(&base.join(format!("metadata-{idx}"))))
        .collect();
    let chain_digest = sha256_bytes(&stable_json_bytes(&json!(object_sets)));
    let selection_digest = match session.get("selectionDigest") {
        Some(v) if python_truthy(v) => value_str(v),
        _ => String::new(),
    };
    json!({
        "chainDigest": chain_digest,
        "controlDigests": controls,
        "metadataDigests": metadata_digests,
        "objectSetDigests": object_sets,
        "requiredComponents": required,
        "selectionDigest": selection_digest,
    })
}

pub fn write_verified_plan(
    base: &Path,
    session: &Value,
    projection: Option<&ProjectionPlan>,
    report: &Value,
) -> Result<PathBuf, AppError> {
    let serialized_proj = projection.map(|p| {
        json!({
            "neededAfterLayer": p.needed_after_layer.iter().map(|s| s.iter().cloned().collect::<Vec<_>>()).collect::<Vec<_>>(),
            "neededBlobs": p.needed_blobs.iter().cloned().collect::<Vec<_>>(),
            "neededFullEntries": p.needed_full_entries.iter().cloned().collect::<Vec<_>>(),
            "neededPacks": p.needed_packs.iter().cloned().collect::<Vec<_>>(),
            "neededStandalone": p.needed_standalone.iter().cloned().collect::<Vec<_>>(),
            "outputFiles": p.output_files.iter().cloned().collect::<Vec<_>>(),
            "producedByLayer": p.produced_by_layer.iter().map(|s| s.iter().cloned().collect::<Vec<_>>()).collect::<Vec<_>>(),
            "requiresExternalMcp": p.requires_external_mcp,
            "requiresFrontendApply": p.requires_frontend_apply,
            "selection": p.selection.canonical_value(),
            "selectionDigest": p.selection_digest,
            "supportFiles": p.support_files.iter().cloned().collect::<Vec<_>>(),
        })
    });
    let body = json!({
        "bindings": verified_plan_bindings(base, session),
        "plannerSchemaVersion": PLANNER_SCHEMA_VERSION,
        "projection": serialized_proj,
        "report": report,
    });
    let body_sha256 = sha256_bytes(&stable_json_bytes(&body));
    let payload = json!({
        "body": body,
        "bodySha256": body_sha256,
    });
    let path = base.join(VERIFIED_PLAN_NAME);
    fs::create_dir_all(base).map_err(|err| internal_error(err.to_string()))?;
    let tmp = base.join(format!(".{VERIFIED_PLAN_NAME}.{}.tmp", UuidHex::generate()));
    let formatted =
        serde_json::to_string_pretty(&payload).map_err(|err| internal_error(err.to_string()))?;
    let write_res = (|| -> Result<(), AppError> {
        let mut f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|err| internal_error(err.to_string()))?;
        f.write_all(formatted.as_bytes())
            .and_then(|_| f.write_all(b"\n"))
            .and_then(|_| f.flush())
            .and_then(|_| f.sync_all())
            .map_err(|err| internal_error(err.to_string()))?;
        Ok(())
    })();
    if let Err(err) = write_res {
        let _ = fs::remove_file(&tmp);
        return Err(err);
    }
    fs::rename(&tmp, &path).map_err(|err| {
        let _ = fs::remove_file(&tmp);
        internal_error(err.to_string())
    })?;
    Ok(path)
}

pub fn load_verified_plan(base: &Path, session: &Value) -> Option<(Option<ProjectionPlan>, Value)> {
    let raw = fs::read_to_string(base.join(VERIFIED_PLAN_NAME)).ok()?;
    let payload: Value = serde_json::from_str(&raw).ok()?;
    let body = payload.get("body")?;
    let body_digest = payload.get("bodySha256")?.as_str()?;
    if !body.is_object() || body_digest != sha256_bytes(&stable_json_bytes(body)) {
        return None;
    }
    if body.get("plannerSchemaVersion")?.as_i64() != Some(PLANNER_SCHEMA_VERSION) {
        return None;
    }
    let current_bindings = verified_plan_bindings(base, session);
    if body.get("bindings") != Some(&current_bindings) {
        return None;
    }
    let meta_digests = current_bindings.get("metadataDigests")?.as_array()?;
    if meta_digests
        .iter()
        .any(|d| d.as_str().is_none_or(str::is_empty))
    {
        return None;
    }
    let report = body.get("report")?.clone();
    if !report.is_object() {
        return None;
    }
    let selection_digest = current_bindings
        .get("selectionDigest")
        .and_then(Value::as_str)
        .unwrap_or("");
    let raw_proj = body.get("projection")?;
    let projection = if raw_proj.is_null() {
        None
    } else {
        let mut plan: ProjectionPlan = serde_json::from_value(raw_proj.clone()).ok()?;
        plan.report = report.clone();
        if plan.selection_digest != selection_digest
            || report
                .get("selectionDigest")
                .and_then(Value::as_str)
                .unwrap_or("")
                != selection_digest
        {
            return None;
        }
        Some(plan)
    };
    Some((projection, report))
}

pub fn estimate_safety_backup_peak(live_logical_bytes: u64) -> Map<String, Value> {
    let proportional = (live_logical_bytes * SAFETY_ARCHIVE_OVERHEAD_PERCENT).div_ceil(100);
    let archive_bytes =
        live_logical_bytes.saturating_add(SAFETY_ARCHIVE_FIXED_OVERHEAD_BYTES.max(proportional));
    let peak_bytes = (2 * live_logical_bytes).saturating_add(2 * archive_bytes);
    let mut out = Map::new();
    out.insert("liveLogicalBytes".into(), json!(live_logical_bytes));
    out.insert("archiveBytes".into(), json!(archive_bytes));
    out.insert("estimatedPeakBytes".into(), json!(peak_bytes));
    out
}

pub fn capacity_report(
    materialized_tree_bytes: u64,
    safety_backup_peak_bytes: u64,
    uncached_ciphertext_bytes: u64,
    plaintext_component_bytes: &[u64],
    free_disk_bytes: Option<u64>,
    reserve_bytes: u64,
) -> Value {
    let mut sorted = plaintext_component_bytes.to_vec();
    sorted.sort_unstable_by(|a, b| b.cmp(a));
    let crypto_plaintext: u64 = sorted.into_iter().take(CRYPTO_QUEUE_COMPONENTS).sum();
    let scratch_bytes = materialized_tree_bytes
        .saturating_add(uncached_ciphertext_bytes)
        .saturating_add(crypto_plaintext);
    let required = scratch_bytes
        .saturating_add(safety_backup_peak_bytes)
        .saturating_add(reserve_bytes);
    let sufficient = free_disk_bytes.is_some_and(|free| free >= required);
    json!({
        "scratch": {
            "materializedTreeBytes": materialized_tree_bytes,
            "uncachedCiphertextBytes": uncached_ciphertext_bytes,
            "boundedCryptoPlaintextBytes": crypto_plaintext,
            "estimatedPeakBytes": scratch_bytes,
        },
        "disk": {
            "freeBytes": free_disk_bytes,
            "reserveBytes": reserve_bytes,
            "requiredBytes": required,
            "sufficient": sufficient,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn selection_normalization_and_digest_match_oracle() {
        assert!(normalize_selection(None).unwrap().is_none());
        let raw =
            json!({"contributors": ["memory", "projects", "memory"], "projectIds": ["p2", "p1"]});
        let sel = normalize_selection(Some(&raw)).unwrap().unwrap();
        assert_eq!(sel.contributors, vec!["memory", "projects"]);
        assert_eq!(sel.project_ids, vec!["p1", "p2"]);
        let expected =
            sha256_bytes(br#"{"contributors":["memory","projects"],"projectIds":["p1","p2"]}"#);
        assert_eq!(sel.digest(), expected);
        assert_eq!(
            normalize_selection(Some(&json!("bad"))).unwrap_err().status,
            409
        );
    }

    #[test]
    fn merkle_and_cdc_ranges_are_deterministic_and_covering() {
        let f1 = FileRecord {
            contributor_id: "projects".into(),
            logical_path: "payload/projects/p1/a.txt".into(),
            size: 5,
            sha256: sha256_bytes(b"hello"),
        };
        let root_a = snapshot_root(std::slice::from_ref(&f1));
        assert_eq!(root_a.len(), 64);
        assert_eq!(snapshot_root(&[]), sha256_bytes(b"\x00"));

        let dir = tempdir().unwrap();
        let big = dir.path().join("big.bin");
        fs::write(&big, vec![0x5a_u8; 600_000]).unwrap();
        let ranges = chunk_file_ranges(&big, CDC_ALGORITHM_V3).unwrap();
        let total: u64 = ranges.iter().map(|(_, l)| *l).sum();
        assert_eq!(total, 600_000);
        assert_eq!(ranges[0].0, 0);
    }

    #[test]
    fn projection_and_materialize_chain_round_trip() {
        let dir = tempdir().unwrap();
        let f0 = dir.path().join("f0");
        let i1 = dir.path().join("i1");
        let out = dir.path().join("out");
        fs::create_dir_all(f0.join("payload/projects/p1")).unwrap();
        fs::create_dir_all(f0.join("payload/memory")).unwrap();
        fs::create_dir_all(i1.join("delta")).unwrap();
        fs::create_dir_all(i1.join("payload/blobs")).unwrap();

        let a_bytes = b"alpha-v0";
        let m_bytes = b"mem-v0";
        let a1_bytes = b"alpha-v1";
        fs::write(f0.join("payload/projects/p1/a.txt"), a_bytes).unwrap();
        fs::write(f0.join("payload/memory/m.json"), m_bytes).unwrap();
        fs::write(i1.join("payload/blobs/a1.bin"), a1_bytes).unwrap();

        let r0_a = FileRecord {
            contributor_id: "projects".into(),
            logical_path: "payload/projects/p1/a.txt".into(),
            size: a_bytes.len() as u64,
            sha256: sha256_bytes(a_bytes),
        };
        let r0_m = FileRecord {
            contributor_id: "memory".into(),
            logical_path: "payload/memory/m.json".into(),
            size: m_bytes.len() as u64,
            sha256: sha256_bytes(m_bytes),
        };
        let root0 = snapshot_root(&[r0_a.clone(), r0_m.clone()]);
        let r1_a = FileRecord {
            contributor_id: "projects".into(),
            logical_path: "payload/projects/p1/a.txt".into(),
            size: a1_bytes.len() as u64,
            sha256: sha256_bytes(a1_bytes),
        };
        let root1 = snapshot_root(&[r1_a.clone(), r0_m.clone()]);

        let m0 = json!({
            "snapshotKind": "full",
            "snapshot": {"rootDigest": root0},
            "contributors": [{"id": "projects", "schemaVersion": 1}, {"id": "memory", "schemaVersion": 1}],
            "files": [
                {"contributorId": "projects", "path": "payload/projects/p1/a.txt", "size": r0_a.size, "sha256": r0_a.sha256},
                {"contributorId": "memory", "path": "payload/memory/m.json", "size": r0_m.size, "sha256": r0_m.sha256},
            ]
        });
        fs::write(f0.join("manifest.json"), serde_json::to_vec(&m0).unwrap()).unwrap();

        let ops1 = json!({
            "parentRootDigest": root0,
            "rootDigest": root1,
            "put": [{
                "contributorId": "projects",
                "path": "payload/projects/p1/a.txt",
                "size": r1_a.size,
                "sha256": r1_a.sha256,
                "storage": "whole",
                "payloadRef": {"kind": "standalone", "path": "payload/blobs/a1.bin"}
            }],
            "delete": []
        });
        let m1 = json!({
            "snapshotKind": "incremental",
            "snapshot": {"rootDigest": root1},
            "contributors": [{"id": "projects", "schemaVersion": 1}, {"id": "memory", "schemaVersion": 1}],
            "files": [
                {"contributorId": "projects", "path": "payload/projects/p1/a.txt", "size": r1_a.size, "sha256": r1_a.sha256},
                {"contributorId": "memory", "path": "payload/memory/m.json", "size": r0_m.size, "sha256": r0_m.sha256},
            ]
        });
        fs::write(
            i1.join("delta/operations.json"),
            serde_json::to_vec(&ops1).unwrap(),
        )
        .unwrap();
        fs::write(i1.join("manifest.json"), serde_json::to_vec(&m1).unwrap()).unwrap();

        let pkgs = vec![
            ChainPackage {
                snapshot_kind: "full".into(),
                files: vec![r0_a, r0_m.clone()],
                root_digest: root0,
                operations: None,
                pack_index: None,
                frontend: false,
                contributor_ids: ["projects".into(), "memory".into()].into_iter().collect(),
                external_mcp: false,
                manifest: m0,
            },
            ChainPackage {
                snapshot_kind: "incremental".into(),
                files: vec![r1_a, r0_m],
                root_digest: root1,
                operations: Some(ops1),
                pack_index: None,
                frontend: false,
                contributor_ids: ["projects".into(), "memory".into()].into_iter().collect(),
                external_mcp: false,
                manifest: m1,
            },
        ];
        let sel = RestoreSelection {
            contributors: vec!["projects".into()],
            project_ids: vec!["p1".into()],
        };
        let plan = plan_projection(&sel, &pkgs, 1024, None).unwrap();
        assert_eq!(plan.output_files.len(), 1);
        materialize_chain(&[f0, i1], &out, Some(&plan)).unwrap();
        assert_eq!(
            fs::read(out.join("payload/projects/p1/a.txt")).unwrap(),
            a1_bytes
        );
        assert!(!out.join("payload/memory/m.json").exists());
    }
}
