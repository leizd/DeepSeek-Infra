//! Read-only backup catalog: `GET /api/workspace/backup-catalog` (migration plan S2).
//!
//! The Rust edge reads the same append-only catalog the original
//! `backup_catalog`/`backup_scrub` modules fold
//! (`<root>/.backups/catalog/catalog-v1.jsonl`, `objects/sha256/**`,
//! `receipts/*.json`). It is a *read-only* port: the original handler resolves
//! `managed-local` through `backup_publish.resolve_target`, which `mkdir`s the
//! backup directory. This module never creates the directory, the catalog or any
//! index, so a workspace that has never run a backup answers the empty catalog
//! instead of creating one.
//!
//! Body parity is byte-exact against the Python oracle for the filesystem
//! branch, including the fold order of governance events, the chain
//! recomputation (`sort_keys=True` compact JSON), the orphan/missing scan, and
//! the scrub/unlock health evaluation. `evaluatedAt` is the only wall-clock
//! field.
//!
//! Scope of this slice: `managed-local` (the only target the React shell
//! requests) and the original 404 for an unknown `targetId`. Registered targets
//! — filesystem markers and the S3 catalog store — answer a stable
//! `NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED` refusal until their owning
//! plane is declared in `RUST_DATA_DOMAINS`; they are recorded as remaining S2
//! capability, not silently approximated.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::RawQuery;
use axum::http::{Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::{dumps_compact, value_str};
use deepseek_storage::{ObjectInventoryEntry, object_inventory_digest};
use percent_encoding::percent_decode_str;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::route_support;

const CATALOG_FILENAME: &str = "catalog-v1.jsonl";
const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";
const OBJECT_SET_V1: &str = "object-set-v1";
const UNLOCK_VERIFICATION_WARNING_DAYS: i64 = 30;
const CATALOG_CORRUPT: &str = "Backup catalog is corrupt; rebuild it from receipts";
const TARGET_NOT_MIGRATED: &str =
    "backup-catalog for a registered target is not served by the native runtime yet";
const TARGET_STORE_NOT_MIGRATED_CODE: &str = "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED";

pub fn router() -> Router {
    Router::new().route("/api/workspace/backup-catalog", any(catalog_response))
}

async fn catalog_response(method: Method, query: RawQuery) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return fallback_for(method);
    }
    let joined = tokio::task::spawn_blocking(move || catalog_body(query.0.as_deref())).await;
    match joined {
        Ok(Ok(_)) if method == Method::HEAD => StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) if method == Method::HEAD => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

/// The original `/api/{path:path}` catch-all answers POST with the flat 404 and
/// every other verb with FastAPI's 405 (`Allow: OPTIONS`).
fn fallback_for(method: Method) -> Response {
    if method == Method::OPTIONS {
        return route_support::options_response();
    }
    if method == Method::POST {
        return route_support::route_not_found();
    }
    route_support::method_not_allowed()
}

// --- request contract -------------------------------------------------------------

fn catalog_body(query: Option<&str>) -> Result<Value, AppError> {
    // `request.query_params.get("targetId") or "managed-local"`; an explicit
    // empty value is falsy and therefore also `managed-local`.
    let target_id = query_param(query, "targetId")
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "managed-local".to_string());
    let policy_id = query_param(query, "policyId").filter(|value| !value.is_empty());

    let root = catalog_root(&target_id)?;
    // The original statements evaluate in this order: list, chain, integrity,
    // health. A corrupt catalog therefore surfaces from `list_backups` first.
    let all = list_backups(&root, None)?;
    let chain_valid = verify_chain(&root)?;
    let integrity = find_orphans_and_missing(&root)?;
    // `backup_health` folds the whole catalog, not the `policyId` subset.
    let health = backup_health(&all)?;
    let entries = match policy_id.as_deref() {
        Some(policy_id) => filter_by_policy(&all, policy_id),
        None => all,
    };

    let mut body = Map::new();
    body.insert("backups".to_string(), Value::Array(entries));
    body.insert("chainValid".to_string(), Value::Bool(chain_valid));
    body.insert("integrity".to_string(), integrity);
    body.insert("health".to_string(), health);
    body.insert(
        "targetKind".to_string(),
        Value::String("filesystem".to_string()),
    );
    Ok(Value::Object(body))
}

/// Starlette's `query_params.get(key)`: the last occurrence wins, `+` decodes to
/// a space and percent escapes are decoded.
fn query_param(query: Option<&str>, name: &str) -> Option<String> {
    let mut found = None;
    for pair in query?.split('&') {
        let mut parts = pair.splitn(2, '=');
        let key = decode_component(parts.next().unwrap_or_default());
        if key != name {
            continue;
        }
        found = Some(decode_component(parts.next().unwrap_or_default()));
    }
    found
}

fn decode_component(raw: &str) -> String {
    let replaced = raw.replace('+', " ");
    percent_decode_str(&replaced)
        .decode_utf8_lossy()
        .into_owned()
}

/// `open_target_session(target_id)` for the branches this slice serves.
fn catalog_root(target_id: &str) -> Result<PathBuf, AppError> {
    if target_id == "managed-local" {
        // `backups.BACKUP_DIR`, without `resolve_target`'s `mkdir`.
        return Ok(crate::data_routes::workspace_root().join(".backups"));
    }
    registered_target(target_id)?;
    Err(AppError {
        message: TARGET_NOT_MIGRATED.to_string(),
        code: TARGET_STORE_NOT_MIGRATED_CODE,
        status: 503,
    })
}

/// `backup_targets.get_target`: the authoritative control row, then the JSON
/// projection, then the original 404. Both reads are read-only; the original
/// additionally rewrites a diverged projection, which this port must not do.
fn registered_target(target_id: &str) -> Result<Value, AppError> {
    if let Some(record) = control_target(target_id)? {
        return Ok(record);
    }
    let path = crate::data_routes::workspace_root()
        .join(".backup-targets")
        .join(format!("{target_id}.json"));
    if !path.is_file() {
        return Err(AppError {
            message: "Backup target not found".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    }
    let unreadable = || AppError {
        message: "Backup target registry is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    };
    let text = std::fs::read_to_string(&path).map_err(|_| unreadable())?;
    let value: Value = serde_json::from_str(&text).map_err(|_| unreadable())?;
    match value {
        Value::Object(fields) if python_truthy(fields.get("targetId").unwrap_or(&Value::Null)) => {
            Ok(Value::Object(fields))
        }
        _ => Err(unreadable()),
    }
}

fn control_target(target_id: &str) -> Result<Option<Value>, AppError> {
    let directory = crate::data_routes::workspace_root().join(".backup-control");
    let database = directory.join("control.sqlite3");
    if !database.is_file() {
        return Ok(None);
    }
    // Never open the control store read-write: Python's `_connect` creates the
    // directory, the schema and the WAL. A plain read-only open of a WAL database
    // that has been cleanly closed *also* creates `-wal`/`-shm`, so read the
    // database through `immutable=1` in exactly that state. When the writer's
    // side files are present they are left untouched by a read-only open.
    let side_files = directory.join("control.sqlite3-wal").exists()
        || directory.join("control.sqlite3-shm").exists();
    let connection = if side_files {
        Connection::open_with_flags(
            &database,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
    } else {
        Connection::open_with_flags(
            sqlite_immutable_uri(&database),
            OpenFlags::SQLITE_OPEN_READ_ONLY
                | OpenFlags::SQLITE_OPEN_NO_MUTEX
                | OpenFlags::SQLITE_OPEN_URI,
        )
    }
    .map_err(|_| unhandled())?;
    let _ = connection.busy_timeout(Duration::from_secs(30));
    let table: Option<String> = connection
        .query_row(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'control_targets'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled())?;
    if table.is_none() {
        return Ok(None);
    }
    let payload: Option<String> = connection
        .query_row(
            "SELECT payload_json FROM control_targets WHERE target_id = ?1",
            [target_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|_| unhandled())?;
    let Some(payload) = payload else {
        return Ok(None);
    };
    let value: Value = serde_json::from_str(&payload).map_err(|_| unhandled())?;
    if !value.is_object() {
        return Err(AppError {
            message: "storage control authority contains invalid JSON".to_string(),
            code: codes::INTERNAL,
            status: 500,
        });
    }
    Ok(Some(value))
}

/// A `file:` URI for `immutable=1`, which reads a cleanly closed WAL database
/// without creating the writer's `-wal`/`-shm` side files.
fn sqlite_immutable_uri(path: &Path) -> String {
    let text = path.to_string_lossy().replace('\\', "/");
    let mut encoded = String::with_capacity(text.len() + 20);
    for character in text.chars() {
        match character {
            '%' => encoded.push_str("%25"),
            '?' => encoded.push_str("%3F"),
            '#' => encoded.push_str("%23"),
            ' ' => encoded.push_str("%20"),
            other => encoded.push(other),
        }
    }
    if encoded.starts_with('/') {
        format!("file://{encoded}?immutable=1")
    } else {
        format!("file:///{encoded}?immutable=1")
    }
}

fn unhandled() -> AppError {
    route_support::unhandled_error()
}

// --- catalog chain -----------------------------------------------------------------

fn catalog_path(root: &Path) -> PathBuf {
    root.join("catalog").join(CATALOG_FILENAME)
}

fn catalog_payload(entry: &Map<String, Value>) -> Value {
    match entry.get("payload") {
        Some(value) if python_truthy(value) => value.clone(),
        _ => json!({}),
    }
}

/// `_read_entries`: blank lines are skipped, non-object lines are dropped, and a
/// line that is not JSON at all is the documented 409 refusal.
fn read_entries(root: &Path) -> Result<Vec<Map<String, Value>>, AppError> {
    let path = catalog_path(root);
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let text = std::fs::read_to_string(&path).map_err(|_| unhandled())?;
    let mut entries = Vec::new();
    // Python iterates the file with universal newlines, so `\r\n` and a lone `\r`
    // are both line separators.
    for line in text.replace("\r\n", "\n").replace('\r', "\n").split('\n') {
        let line = python_strip(line);
        if line.is_empty() {
            continue;
        }
        let value: Value = serde_json::from_str(line).map_err(|_| AppError {
            message: CATALOG_CORRUPT.to_string(),
            code: codes::INVALID_REQUEST,
            status: 409,
        })?;
        if let Value::Object(fields) = value {
            entries.push(fields);
        }
    }
    Ok(entries)
}

fn verify_chain(root: &Path) -> Result<bool, AppError> {
    let mut previous = GENESIS_HASH.to_string();
    for entry in read_entries(root)? {
        // The original compares the raw values, so a non-string
        // `previousEntryHash` never equals the running hash.
        if entry.get("previousEntryHash").and_then(Value::as_str) != Some(previous.as_str()) {
            return Ok(false);
        }
        let entry_type = python_str_or_empty(entry.get("type"));
        let payload = catalog_payload(&entry);
        let expected = entry_hash(&entry_type, &payload, &previous);
        if entry.get("entryHash").and_then(Value::as_str) != Some(expected.as_str()) {
            return Ok(false);
        }
        previous = python_str_or_empty(entry.get("entryHash"));
    }
    Ok(true)
}

/// `_entry_hash`: `sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")))`.
fn entry_hash(entry_type: &str, payload: &Value, previous: &str) -> String {
    let body = json!({
        "type": entry_type,
        "payload": payload,
        "previousEntryHash": previous,
    });
    sha256_hex(dumps_compact(&body).as_bytes())
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        hex.push_str(&format!("{byte:02x}"));
    }
    hex
}

/// `catalog_state`: fold the chain into per-backup records, preserving the
/// insertion order the original dict has.
fn catalog_state(root: &Path) -> Result<Map<String, Value>, AppError> {
    let mut state: Map<String, Value> = Map::new();
    for entry in read_entries(root)? {
        let payload_value = catalog_payload(&entry);
        // A truthy non-mapping payload is the original's unhandled
        // `AttributeError`; it stays an unhandled 500 here.
        let Some(payload) = payload_value.as_object() else {
            return Err(unhandled());
        };
        let backup_id = python_str_or_empty(payload.get("backupId"));
        let entry_type = python_str_or_empty(entry.get("type"));
        if entry_type == "receipt" {
            let mut record = payload.clone();
            record.insert("pinned".to_string(), Value::Bool(false));
            record.insert("ciphertextScrubbedAt".to_string(), Value::Null);
            record.insert("scrubOk".to_string(), Value::Null);
            record.insert("userUnlockVerifiedAt".to_string(), Value::Null);
            record.insert("trashed".to_string(), Value::Bool(false));
            record.insert("deleted".to_string(), Value::Bool(false));
            state.insert(backup_id, Value::Object(record));
            continue;
        }
        let Some(Value::Object(record)) = state.get_mut(&backup_id) else {
            continue;
        };
        match entry_type.as_str() {
            "pin" => {
                record.insert(
                    "pinned".to_string(),
                    Value::Bool(python_truthy(payload.get("pinned").unwrap_or(&Value::Null))),
                );
            }
            "scrub" => {
                record.insert(
                    "ciphertextScrubbedAt".to_string(),
                    payload.get("scrubbedAt").cloned().unwrap_or(Value::Null),
                );
                record.insert(
                    "scrubOk".to_string(),
                    Value::Bool(python_truthy(payload.get("ok").unwrap_or(&Value::Null))),
                );
            }
            "unlock-verified" => {
                record.insert(
                    "userUnlockVerifiedAt".to_string(),
                    payload
                        .get("userUnlockVerifiedAt")
                        .cloned()
                        .unwrap_or(Value::Null),
                );
            }
            "trash" => {
                record.insert("trashed".to_string(), Value::Bool(true));
                record.insert(
                    "trashedAt".to_string(),
                    payload.get("trashedAt").cloned().unwrap_or(Value::Null),
                );
            }
            "restore-trash" => {
                record.insert("trashed".to_string(), Value::Bool(false));
                record.shift_remove("trashedAt");
            }
            "delete" => {
                record.insert("deleted".to_string(), Value::Bool(true));
                record.insert(
                    "deletedAt".to_string(),
                    payload.get("deletedAt").cloned().unwrap_or(Value::Null),
                );
            }
            _ => {}
        }
    }
    Ok(state)
}

/// `state_sorted`: stable descending sort on `str(createdAt or "")`.
fn state_sorted(state: &Map<String, Value>) -> Vec<Value> {
    let mut records: Vec<Value> = state.values().cloned().collect();
    records.sort_by_key(|record| std::cmp::Reverse(sort_key(record)));
    records
}

fn sort_key(record: &Value) -> String {
    python_str_or_empty(record.get("createdAt"))
}

fn list_backups(root: &Path, policy_id: Option<&str>) -> Result<Vec<Value>, AppError> {
    let state = catalog_state(root)?;
    let records = state_sorted(&state);
    let mut result = Vec::with_capacity(records.len());
    for record in records {
        if python_truthy(record.get("deleted").unwrap_or(&Value::Null)) {
            continue;
        }
        if let Some(policy_id) = policy_id {
            if record.get("policyId") != Some(&Value::String(policy_id.to_string())) {
                continue;
            }
        }
        result.push(record);
    }
    Ok(result)
}

fn filter_by_policy(records: &[Value], policy_id: &str) -> Vec<Value> {
    let wanted = Value::String(policy_id.to_string());
    records
        .iter()
        .filter(|record| record.get("policyId") == Some(&wanted))
        .cloned()
        .collect()
}

// --- integrity --------------------------------------------------------------------

fn find_orphans_and_missing(root: &Path) -> Result<Value, AppError> {
    let state = catalog_state(root)?;
    let records: Vec<Value> = state
        .values()
        .filter(|record| !python_truthy(record.get("deleted").unwrap_or(&Value::Null)))
        .cloned()
        .collect();

    let mut referenced: HashSet<PathBuf> = HashSet::new();
    for record in &records {
        for candidate in backup_file_candidates(root, record) {
            referenced.insert(candidate);
        }
    }
    let receipts_dir = root.join("receipts");
    let mut names = collect_entries(&receipts_dir);
    names.retain(|path| has_name_suffix(path, ".json"));
    names.sort();
    for path in names {
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(value) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        if value.is_object() {
            for candidate in backup_file_candidates(root, &value) {
                referenced.insert(candidate);
            }
        }
    }

    let mut on_disk: Vec<PathBuf> = Vec::new();
    for bucket in collect_entries(&root.join("objects").join("sha256")) {
        on_disk.extend(
            collect_entries(&bucket)
                .into_iter()
                .filter(|path| has_name_suffix(path, ".age")),
        );
    }
    on_disk.extend(
        collect_entries(&root.join("backups"))
            .into_iter()
            .filter(|path| has_name_part(path, ".dsibackup")),
    );

    let mut orphans: Vec<String> = on_disk
        .iter()
        .filter(|path| !referenced.contains(*path))
        .filter_map(|path| {
            path.file_name()
                .map(|name| name.to_string_lossy().into_owned())
        })
        .collect();
    orphans.sort();

    let mut missing: Vec<String> = Vec::new();
    for record in &records {
        if python_truthy(record.get("trashed").unwrap_or(&Value::Null)) {
            continue;
        }
        let candidates = backup_file_candidates(root, record);
        // `str(record.get("filename") or record.get("backupId"))`: two operands and
        // no `or ""`, so a record with neither renders `"None"`.
        let label = || value_str(python_or(record.get("filename"), record.get("backupId")));
        if candidates.is_empty() {
            if is_object_set(record) {
                missing.push(label());
            }
            continue;
        }
        let unavailable = if is_object_set(record) {
            !candidates.iter().all(|candidate| candidate.is_file())
        } else {
            !candidates.iter().any(|candidate| candidate.is_file())
        };
        if unavailable {
            missing.push(label());
        }
    }
    missing.sort();

    Ok(json!({"orphans": orphans, "missing": missing}))
}

fn has_name_suffix(path: &Path, suffix: &str) -> bool {
    path.file_name()
        .map(|name| name.to_string_lossy().ends_with(suffix))
        .unwrap_or(false)
}

/// The directory entries of `dir`, or an empty list when it does not exist. The
/// original uses `Path.glob`, which yields nothing for a missing directory and —
/// unlike the `glob` module — does **not** skip names that begin with a dot.
fn collect_entries(dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    entries.flatten().map(|entry| entry.path()).collect()
}

fn has_name_part(path: &Path, part: &str) -> bool {
    path.file_name()
        .map(|name| name.to_string_lossy().contains(part))
        .unwrap_or(false)
}

fn is_object_set(record: &Value) -> bool {
    python_str_or_empty(record.get("storageProtocol")) == OBJECT_SET_V1
}

fn object_path(root: &Path, digest: &str) -> PathBuf {
    // `digest[:2]` is a character slice in the original; build it from chars so a
    // legacy record with 64 multi-byte characters cannot panic here.
    let prefix: String = digest.chars().take(2).collect();
    root.join("objects")
        .join("sha256")
        .join(prefix)
        .join(format!("{digest}.age"))
}

/// `backup_publish.backup_file_candidates`: the object-set commitment members,
/// then the legacy ciphertext object plus the legacy package filename.
fn backup_file_candidates(root: &Path, record: &Value) -> Vec<PathBuf> {
    if is_object_set(record) {
        let Ok(inventory) = committed_object_inventory(record) else {
            return Vec::new();
        };
        return inventory
            .iter()
            .map(|entry| object_path(root, &entry.digest))
            .collect();
    }
    let mut candidates = Vec::new();
    // `record.get("objectDigest") or record.get("ciphertextSha256") or ""`.
    let digest = python_str_or_empty(Some(python_or(
        record.get("objectDigest"),
        record.get("ciphertextSha256"),
    )));
    if digest.chars().count() == 64 {
        candidates.push(object_path(root, &digest));
    }
    let filename = python_str_or_empty(record.get("filename"));
    if !filename.is_empty() && !filename.contains('/') && !filename.contains('\\') {
        candidates.push(root.join("backups").join(filename));
    }
    candidates
}

/// `backup_object_set.committed_object_inventory`: the frozen `object-set-v1`
/// commitment is validated before any member is used, and any rejection makes
/// the record contribute no candidates at all.
fn committed_object_inventory(record: &Value) -> Result<Vec<ObjectInventoryEntry>, ()> {
    let Some(Value::Array(raw_objects)) = record.get("objects") else {
        return Err(());
    };
    let mut inventory: Vec<ObjectInventoryEntry> = Vec::with_capacity(raw_objects.len());
    for item in raw_objects {
        let Some(fields) = item.as_object() else {
            return Err(());
        };
        // A boolean, a float, a negative integer or a missing size is rejected
        // by the original commitment validation.
        let Some(size) = fields.get("size").and_then(Value::as_u64) else {
            return Err(());
        };
        inventory.push(ObjectInventoryEntry {
            digest: python_str_or_empty(fields.get("digest")),
            size,
        });
    }
    let Ok(expected) = object_inventory_digest(&inventory) else {
        return Err(());
    };
    if expected != python_str_or_empty(record.get("objectSetDigest")) {
        return Err(());
    }
    let control = python_str_or_empty(record.get("controlObjectDigest"));
    if control.chars().count() != 64 || !inventory.iter().any(|entry| entry.digest == control) {
        return Err(());
    }
    inventory.sort_by(|left, right| {
        left.digest
            .cmp(&right.digest)
            .then(left.size.cmp(&right.size))
    });
    Ok(inventory)
}

// --- health -----------------------------------------------------------------------

/// `backup_scrub.backup_health` over the already-folded, unfiltered catalog.
fn backup_health(records: &[Value]) -> Result<Value, AppError> {
    let now = unix_seconds();
    let mut entries = Vec::with_capacity(records.len());
    let mut worst = "ok";
    for record in records {
        let mut issues: Vec<&str> = Vec::new();
        if matches!(record.get("scrubOk"), Some(Value::Bool(false))) {
            issues.push("scrub-failed");
        }
        let verified = record.get("userUnlockVerifiedAt");
        if !python_truthy(verified.unwrap_or(&Value::Null)) {
            issues.push("unlock-verification-missing");
        } else {
            let text = python_str_or_empty(verified).replace('Z', "+00:00");
            match parse_python_iso(&text) {
                Ok(Some(instant)) => {
                    if now as i64 - instant > UNLOCK_VERIFICATION_WARNING_DAYS * 86_400 {
                        issues.push("unlock-verification-overdue");
                    }
                }
                // A timezone-naive value is the original's unhandled `TypeError`.
                Ok(None) => return Err(unhandled()),
                Err(()) => issues.push("unlock-verification-missing"),
            }
        }
        let status = if issues.contains(&"scrub-failed") {
            worst = "error";
            "error"
        } else if !issues.is_empty() {
            if worst != "error" {
                worst = "warning";
            }
            "warning"
        } else {
            "ok"
        };
        // `record["backupId"]` indexes the folded record: a receipt payload with
        // no `backupId` is the original's unhandled `KeyError`.
        let Some(backup_id) = record.get("backupId").cloned() else {
            return Err(unhandled());
        };
        entries.push(json!({
            "backupId": backup_id,
            "status": status,
            "issues": issues,
            "creationVerified": python_truthy(
                record.get("creationVerified").unwrap_or(&Value::Null)
            ),
            "ciphertextScrubbedAt": record
                .get("ciphertextScrubbedAt")
                .cloned()
                .unwrap_or(Value::Null),
            "userUnlockVerifiedAt": verified.cloned().unwrap_or(Value::Null),
        }));
    }
    Ok(json!({
        "status": worst,
        "backups": entries,
        "evaluatedAt": utc_iso_from_unix(now),
    }))
}

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0)
}

/// `datetime.now(timezone.utc).isoformat(timespec="seconds")` with `Z`.
fn utc_iso_from_unix(unix_secs: u64) -> String {
    let days = (unix_secs / 86_400) as i64;
    let second_of_day = unix_secs % 86_400;
    let (year, month, day) = civil_from_days(days);
    let hour = second_of_day / 3_600;
    let minute = (second_of_day % 3_600) / 60;
    let second = second_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

/// Howard Hinnant's `civil_from_days`.
fn civil_from_days(mut z: i64) -> (i64, u32, u32) {
    z += 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = (z - era * 146_097) as u64;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era as i64 + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = if month_prime < 10 {
        month_prime + 3
    } else {
        month_prime - 9
    };
    (year + i64::from(month <= 2), month as u32, day as u32)
}

/// `datetime.fromisoformat(text)` for the shapes a catalog timestamp takes,
/// returning seconds since the epoch. `Ok(None)` is a timezone-naive value (the
/// original raises `TypeError` when subtracting it); `Err(())` is its `ValueError`.
///
/// Scope: the writers store `...Z`/`...+00:00`. This parser additionally accepts
/// the CPython 3.11+ extended forms a hand-edited catalog can contain — any
/// single date/time separator character, `+HH`, `+HHMM`, offset seconds, a comma
/// fraction. CPython's *compact* forms (`YYYYMMDD`, `HHMMSS`, ISO week dates)
/// are not accepted; they classify as `unlock-verification-missing` where the
/// original would parse them. That narrowing is recorded in the slice manifest
/// because no writer emits those shapes.
fn parse_python_iso(text: &str) -> Result<Option<i64>, ()> {
    let bytes = text.as_bytes();
    let (year, month, day, rest) = parse_date(bytes)?;
    let (hour, minute, second, offset) = if rest.is_empty() {
        (0, 0, 0, None)
    } else {
        // CPython 3.11+ accepts any single character as the date/time separator.
        parse_time(&rest[1..])?
    };
    let seconds = days_from_civil(i64::from(year), month, day) * 86_400
        + i64::from(hour) * 3_600
        + i64::from(minute) * 60
        + i64::from(second);
    match offset {
        Some(offset) => Ok(Some(seconds - offset)),
        None => Ok(None),
    }
}

fn parse_date(bytes: &[u8]) -> Result<(u32, u32, u32, &[u8]), ()> {
    if bytes.len() < 10 || bytes[4] != b'-' || bytes[7] != b'-' {
        return Err(());
    }
    let year = parse_digits(&bytes[0..4])?;
    let month = parse_digits(&bytes[5..7])?;
    let day = parse_digits(&bytes[8..10])?;
    if !(1..=12).contains(&month) || day == 0 || day > days_in_month(year, month) {
        return Err(());
    }
    Ok((year, month, day, &bytes[10..]))
}

fn parse_time(bytes: &[u8]) -> Result<(u32, u32, u32, Option<i64>), ()> {
    if bytes.len() < 2 {
        return Err(());
    }
    let hour = parse_digits(&bytes[0..2])?;
    if hour > 23 {
        return Err(());
    }
    let mut rest = &bytes[2..];
    let mut minute = 0;
    let mut second = 0;
    if !rest.is_empty() {
        if rest[0] != b':' || rest.len() < 3 {
            return Err(());
        }
        minute = parse_digits(&rest[1..3])?;
        if minute > 59 {
            return Err(());
        }
        rest = &rest[3..];
    }
    if !rest.is_empty() && rest[0] == b':' {
        if rest.len() < 3 {
            return Err(());
        }
        second = parse_digits(&rest[1..3])?;
        if second > 59 {
            return Err(());
        }
        rest = &rest[3..];
    }
    if !rest.is_empty() && (rest[0] == b'.' || rest[0] == b',') {
        let digits = rest[1..]
            .iter()
            .take_while(|byte| byte.is_ascii_digit())
            .count();
        if digits == 0 {
            return Err(());
        }
        rest = &rest[1 + digits..];
    }
    if rest.is_empty() {
        return Ok((hour, minute, second, None));
    }
    let sign = match rest[0] {
        b'+' => 1_i64,
        b'-' => -1_i64,
        _ => return Err(()),
    };
    let rest = &rest[1..];
    if rest.len() < 2 {
        return Err(());
    }
    let offset_hours = parse_digits(&rest[0..2])?;
    let mut rest = &rest[2..];
    let mut offset_minutes = 0;
    let mut offset_seconds = 0;
    if !rest.is_empty() && rest[0] == b':' {
        if rest.len() < 3 {
            return Err(());
        }
        offset_minutes = parse_digits(&rest[1..3])?;
        rest = &rest[3..];
        if !rest.is_empty() && rest[0] == b':' {
            if rest.len() < 3 {
                return Err(());
            }
            offset_seconds = parse_digits(&rest[1..3])?;
            rest = &rest[3..];
            if !rest.is_empty() && rest[0] == b'.' {
                let digits = rest[1..]
                    .iter()
                    .take_while(|byte| byte.is_ascii_digit())
                    .count();
                if digits == 0 {
                    return Err(());
                }
                rest = &rest[1 + digits..];
            }
        }
    } else if !rest.is_empty() {
        // `+HHMM` without the colon.
        if rest.len() < 2 {
            return Err(());
        }
        offset_minutes = parse_digits(&rest[0..2])?;
        rest = &rest[2..];
    }
    if !rest.is_empty() {
        return Err(());
    }
    if offset_hours > 23 || offset_minutes > 59 || offset_seconds > 59 {
        return Err(());
    }
    Ok((
        hour,
        minute,
        second,
        Some(
            sign * (i64::from(offset_hours) * 3_600
                + i64::from(offset_minutes) * 60
                + i64::from(offset_seconds)),
        ),
    ))
}

fn parse_digits(bytes: &[u8]) -> Result<u32, ()> {
    let mut value = 0_u32;
    for byte in bytes {
        if !byte.is_ascii_digit() {
            return Err(());
        }
        value = value * 10 + u32::from(*byte - b'0');
    }
    Ok(value)
}

fn days_in_month(year: u32, month: u32) -> u32 {
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        _ => 0,
    }
}

fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let mut year = year;
    let month = i64::from(month);
    if month <= 2 {
        year -= 1;
    }
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let month_prime = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * month_prime + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

// --- Python value helpers ----------------------------------------------------------

/// `str.strip()`: Unicode whitespace plus the four information separators
/// CPython also treats as whitespace.
fn python_strip(text: &str) -> &str {
    text.trim_matches(|character: char| {
        character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
    })
}

/// `a or b` with Python truthiness, where the fallback may itself be falsy.
fn python_or<'a>(left: Option<&'a Value>, right: Option<&'a Value>) -> &'a Value {
    match left {
        Some(value) if python_truthy(value) => value,
        _ => right.unwrap_or(&Value::Null),
    }
}

/// `str(value or "")`.
fn python_str_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(value) if python_truthy(value) => value_str(value),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::{
        GENESIS_HASH, days_from_civil, entry_hash, parse_python_iso, python_strip, query_param,
        sha256_hex, utc_iso_from_unix,
    };

    #[test]
    fn query_parsing_matches_starlette_last_value_wins() {
        assert_eq!(
            query_param(Some("targetId=fs-1"), "targetId").as_deref(),
            Some("fs-1")
        );
        assert_eq!(
            query_param(Some("targetId=a&targetId=b"), "targetId").as_deref(),
            Some("b")
        );
        assert_eq!(
            query_param(Some("targetId"), "targetId").as_deref(),
            Some("")
        );
        assert_eq!(
            query_param(Some("policyId=a%2Fb"), "policyId").as_deref(),
            Some("a/b")
        );
        assert_eq!(
            query_param(Some("policyId=a+b"), "policyId").as_deref(),
            Some("a b")
        );
        assert_eq!(query_param(Some("other=1"), "targetId"), None);
        assert_eq!(query_param(None, "targetId"), None);
    }

    #[test]
    fn entry_hash_is_the_frozen_compact_sorted_document() {
        let expected = sha256_hex(
            format!(
                "{{\"payload\":{{\"backupId\":\"b\",\"pinned\":true}},\"previousEntryHash\":\"{GENESIS_HASH}\",\"type\":\"pin\"}}"
            )
            .as_bytes(),
        );
        assert_eq!(
            entry_hash(
                "pin",
                &serde_json::json!({"pinned": true, "backupId": "b"}),
                GENESIS_HASH
            ),
            expected
        );
    }

    #[test]
    fn strip_matches_python_str_strip() {
        assert_eq!(python_strip("  a  "), "a");
        assert_eq!(python_strip("\t\r\n a \u{1c}"), "a");
        assert_eq!(python_strip("\u{a0}a\u{3000}"), "a");
        assert_eq!(python_strip(""), "");
    }

    #[test]
    fn iso_parsing_matches_the_original_acceptance() {
        assert_eq!(
            parse_python_iso("2026-10-04T00:00:00+00:00"),
            Ok(Some(days_from_civil(2026, 10, 4) * 86_400))
        );
        assert_eq!(
            parse_python_iso("2026-10-04T00:00:00+05:00"),
            Ok(Some(days_from_civil(2026, 10, 4) * 86_400 - 18_000))
        );
        assert_eq!(
            parse_python_iso("2026-10-04T00:00:00.123456+00:00"),
            Ok(Some(days_from_civil(2026, 10, 4) * 86_400))
        );
        assert_eq!(parse_python_iso("2026-10-04"), Ok(None));
        assert_eq!(parse_python_iso("2026-10-04T00:00:00"), Ok(None));
        assert_eq!(parse_python_iso("2026-13-01T00:00:00+00:00"), Err(()));
        assert_eq!(parse_python_iso("garbage"), Err(()));
        assert_eq!(parse_python_iso(""), Err(()));
        // CPython 3.11+ forms the writers never emit but a hand-edited value can.
        let base = days_from_civil(2026, 10, 4) * 86_400;
        assert_eq!(
            parse_python_iso("2026-10-04 00:00:00+00:00"),
            Ok(Some(base))
        );
        assert_eq!(
            parse_python_iso("2026-10-04X00:00:00+00:00"),
            Ok(Some(base))
        );
        assert_eq!(parse_python_iso("2026-10-04T00:00:00+0000"), Ok(Some(base)));
        assert_eq!(parse_python_iso("2026-10-04T00:00:00+00"), Ok(Some(base)));
        assert_eq!(
            parse_python_iso("2026-10-04T00:00:00,5+00:00"),
            Ok(Some(base))
        );
        assert_eq!(
            parse_python_iso("2026-10-04T00:00:00+00:00:30"),
            Ok(Some(base - 30))
        );
        // Refusals the original also raises.
        assert_eq!(parse_python_iso("2026-10-04T24:00:00+00:00"), Err(()));
        assert_eq!(parse_python_iso("2026-02-30T00:00:00+00:00"), Err(()));
        assert_eq!(parse_python_iso("2026-10-04T00:00:00z"), Err(()));
        assert_eq!(parse_python_iso("2026-10-04T00:00:00+00:00 "), Err(()));
        assert_eq!(parse_python_iso("2026-10-04T00:00:00+00:00junk"), Err(()));
        assert_eq!(parse_python_iso("2026-10-04T00:00:00+24:00"), Err(()));
        // `fromisoformat` discounts a +00:60 offset into +01:00; this port keeps
        // the same arithmetic instead of rejecting it.
        assert_eq!(
            parse_python_iso("2026-10-04T00:00:00+00:60"),
            Ok(Some(base - 3_600))
        );
    }

    #[test]
    fn utc_iso_matches_known_unix_instants() {
        assert_eq!(utc_iso_from_unix(0), "1970-01-01T00:00:00Z");
        assert_eq!(utc_iso_from_unix(86_400), "1970-01-02T00:00:00Z");
        assert_eq!(utc_iso_from_unix(1_700_000_000), "2023-11-14T22:13:20Z");
    }
}
