//! The uploaded-file cache read path, mirroring `load_cached_file` and its helpers
//! in `deepseek_infra/infra/rag/files.py`.
//!
//! Only the **read** side is here. The rest of that 1,494-line module is the
//! chunking, extraction and embedding pipeline, which the two `projects` branches do
//! not touch — so a branch that reads a cached chunk needs an id-shape check, a path
//! derivation, a JSON read and a cache, and nothing else.
//!
//! # The cache is only used without a project id
//!
//! ```python
//! if project_id:
//!     return _load_cached_file_impl_from_path(path)
//! return _load_cached_file_cached(file_id, mtime_ns)
//! ```
//!
//! So a project-scoped read always hits the disk, and a global read goes through
//! `@lru_cache(maxsize=64)` keyed on `(file_id, mtime_ns)`. The `mtime_ns` in the key
//! is what makes a changed file bypass a stale entry. [`FileCache`] reproduces the
//! bound and the move-to-front-on-hit behaviour, because an unbounded map would
//! return the same *results* while differing under mixed traffic — and the whole
//! point of this migration is that a difference is stated, not assumed harmless.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::Value;

use crate::app_error::{AppError, codes};

/// `FILE_CACHE_DIR` — `<root>/.file-cache`.
pub fn file_cache_dir(root: &Path) -> PathBuf {
    root.join(".file-cache")
}

/// `@lru_cache(maxsize=64)`.
pub const FILE_CACHE_MAX: usize = 64;

/// Mirrors `project_file_cache_dir`: `<PROJECTS_DIR>/<id>/files`, with the id shape
/// enforced so a caller cannot escape the directory.
pub fn project_file_cache_dir(root: &Path, project_id: &str) -> Result<PathBuf, AppError> {
    let safe_id = project_id.trim();
    let valid = (4..=64).contains(&safe_id.chars().count())
        && safe_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'));
    if !valid {
        return Err(AppError::invalid_payload("Invalid project id"));
    }
    Ok(root.join(".projects").join(safe_id).join("files"))
}

/// `[0-9a-f]{32}`.
fn is_file_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// A bounded, move-to-front cache standing in for `@lru_cache(maxsize=64)`.
///
/// The key is `(file_id, mtime_ns)`, so a modified file is a different key and can
/// never hit a stale entry.
#[derive(Debug, Default)]
pub struct FileCache {
    entries: Mutex<Vec<(String, Value)>>,
}

impl FileCache {
    pub fn new() -> Self {
        Self::default()
    }

    fn get(&self, key: &str) -> Option<Value> {
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let position = entries.iter().position(|(stored, _)| stored == key)?;
        // `lru_cache` promotes a hit to most-recently-used.
        let entry = entries.remove(position);
        let value = entry.1.clone();
        entries.insert(0, entry);
        Some(value)
    }

    fn put(&self, key: String, value: Value) {
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(position) = entries.iter().position(|(stored, _)| *stored == key) {
            entries.remove(position);
        }
        entries.insert(0, (key, value));
        entries.truncate(FILE_CACHE_MAX);
    }

    /// Drop everything. The oracle has no equivalent; exposed for tests.
    pub fn clear(&self) {
        self.entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clear();
    }
}

/// Mirrors `load_cached_file`.
pub fn load_cached_file(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    cache: &FileCache,
) -> Result<Value, AppError> {
    if !is_file_id(file_id) {
        return Err(AppError::invalid_payload("Invalid file id"));
    }
    // `if project_id` tests the **raw** value, while `project_file_cache_dir` is what
    // strips. So a whitespace-only project id is truthy here and then fails the id
    // shape check with a 400 — it does **not** fall back to the global path. The
    // probe caught this; the wrapper (`read_file_chunk`) strips before calling, so it
    // passes `None` for a blank id.
    let scoped = project_id.filter(|id| !id.is_empty());
    let path = match scoped {
        Some(id) => project_file_cache_dir(root, id)?.join(format!("{file_id}.json")),
        None => file_cache_dir(root).join(format!("{file_id}.json")),
    };
    if !path.exists() {
        return Err(AppError {
            message: "Uploaded file index has expired or is missing".to_string(),
            code: codes::FILE_INDEX_EXPIRED,
            status: 410,
        });
    }
    let modified = std::fs::metadata(&path)
        .and_then(|metadata| metadata.modified())
        .map_err(|_| unreadable())?;
    let mtime_ns = modified
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos() as i64)
        .unwrap_or(0);

    if scoped.is_some() {
        // A project-scoped read always re-reads.
        return read_index(&path);
    }
    let key = format!("{file_id}:{mtime_ns}");
    if let Some(cached) = cache.get(&key) {
        return Ok(cached);
    }
    let value = read_index(&path)?;
    cache.put(key, value.clone());
    Ok(value)
}

/// Mirrors `_load_cached_file_impl_from_path`.
fn read_index(path: &Path) -> Result<Value, AppError> {
    let raw = std::fs::read_to_string(path).map_err(|_| unreadable())?;
    let value: Value = serde_json::from_str(&raw).map_err(|_| unreadable())?;
    if !value.is_object() {
        return Err(AppError {
            message: "Uploaded file index is invalid".to_string(),
            code: codes::INTERNAL,
            status: 500,
        });
    }
    Ok(value)
}

fn unreadable() -> AppError {
    AppError {
        message: "Uploaded file index is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    }
}

/// Python's `int()` for the shapes a cached index holds.
///
/// Distinct from the projects store's `_safe_int`: this is the bare `int()`, so
/// `"3.7"` and `"abc"` **raise** rather than falling back, and a float truncates
/// toward zero. The oracle lets the `ValueError` escape as a 500; this reports
/// `internal`/500 with a matching message, the same documented mapping used for the
/// projects `TypeError`.
pub fn python_int(value: Option<&Value>) -> Result<i64, AppError> {
    let invalid = || AppError {
        message: format!(
            "invalid literal for int() with base 10: '{}'",
            crate::python_json::value_str(value.unwrap_or(&Value::Null))
        ),
        code: codes::INTERNAL,
        status: 500,
    };
    match value {
        None | Some(Value::Null) => Err(AppError {
            message: "int() argument must be a string or a number, not 'NoneType'".to_string(),
            code: codes::INTERNAL,
            status: 500,
        }),
        Some(Value::Bool(flag)) => Ok(i64::from(*flag)),
        Some(Value::Number(number)) => {
            if let Some(int) = number.as_i64() {
                return Ok(int);
            }
            // Python truncates a float toward zero.
            number
                .as_f64()
                .map(|float| float as i64)
                .ok_or_else(invalid)
        }
        Some(Value::String(text)) => {
            let trimmed = text.trim();
            let (sign, digits) = match trimmed.strip_prefix('-') {
                Some(rest) => (-1i64, rest),
                None => (1i64, trimmed.strip_prefix('+').unwrap_or(trimmed)),
            };
            let normalised = digits.replace('_', "");
            let parseable = !normalised.is_empty()
                && normalised.chars().all(|c| c.is_ascii_digit())
                && !digits.starts_with('_')
                && !digits.ends_with('_');
            if !parseable {
                return Err(invalid());
            }
            normalised
                .parse::<i64>()
                .map(|parsed| sign * parsed)
                .map_err(|_| invalid())
        }
        Some(_) => Err(invalid()),
    }
}

/// Mirrors the oracle's `pick(key, default)` idiom for a cached index field.
pub fn int_field(value: &Value, key: &str, default: i64) -> Result<i64, AppError> {
    let stored = value.get(key);
    // `int(chunk.get("lineStart") or 0)` — the `or` makes a falsy value the default.
    match stored {
        Some(item) if crate::projects::is_truthy(item) => python_int(Some(item)),
        _ => Ok(default),
    }
}

/// A lookup used only by tests to assert the cache bound.
pub fn cache_len(cache: &FileCache) -> usize {
    cache
        .entries
        .lock()
        .map(|entries| entries.len())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn temp_root(label: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "file-cache-test-{label}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("create temp root");
        root
    }

    fn write_index(root: &Path, file_id: &str, project: Option<&str>, body: &str) {
        let directory = match project {
            Some(id) => project_file_cache_dir(root, id).expect("project dir"),
            None => file_cache_dir(root),
        };
        std::fs::create_dir_all(&directory).expect("create cache dir");
        std::fs::write(directory.join(format!("{file_id}.json")), body).expect("write index");
    }

    #[test]
    fn the_file_id_shape_is_enforced_before_touching_disk() {
        let root = temp_root("id-shape");
        let cache = FileCache::new();
        for bad in ["", "abc", "A".repeat(32).as_str(), "g".repeat(32).as_str()] {
            let failure = load_cached_file(&root, bad, None, &cache).unwrap_err();
            assert_eq!(failure.message, "Invalid file id", "{bad}");
            assert_eq!(failure.status, 400);
        }
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_missing_index_is_a_410_not_a_404() {
        let root = temp_root("missing");
        let cache = FileCache::new();
        let failure = load_cached_file(&root, &"a".repeat(32), None, &cache).unwrap_err();
        assert_eq!(failure.code, codes::FILE_INDEX_EXPIRED);
        assert_eq!(failure.status, 410);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_malformed_index_is_a_500() {
        let root = temp_root("malformed");
        let cache = FileCache::new();
        let id = "b".repeat(32);
        write_index(&root, &id, None, "{not json");
        assert_eq!(
            load_cached_file(&root, &id, None, &cache)
                .unwrap_err()
                .message,
            "Uploaded file index is unreadable"
        );
        // A JSON scalar parses but is not an index.
        write_index(&root, &id, None, "42");
        assert_eq!(
            load_cached_file(&root, &id, None, &cache)
                .unwrap_err()
                .message,
            "Uploaded file index is invalid"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_project_scoped_read_uses_the_project_directory() {
        let root = temp_root("scoped");
        let cache = FileCache::new();
        let id = "c".repeat(32);
        // The same file id in the project directory only.
        write_index(&root, &id, Some("abcd"), r#"{"id": "inner"}"#);
        let found = load_cached_file(&root, &id, Some("abcd"), &cache).expect("scoped read");
        assert_eq!(found["id"], "inner");
        // The global path does not see it.
        assert_eq!(
            load_cached_file(&root, &id, None, &cache)
                .unwrap_err()
                .status,
            410
        );
        // An invalid project id is a 400, not a 410.
        assert_eq!(
            load_cached_file(&root, &id, Some("ab"), &cache)
                .unwrap_err()
                .status,
            400
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The cache is bounded, and a hit is promoted.
    #[test]
    fn the_cache_is_bounded_and_evicts_the_least_recently_used() {
        let cache = FileCache::new();
        for index in 0..FILE_CACHE_MAX + 10 {
            cache.put(format!("key-{index}"), json!(index));
        }
        assert_eq!(cache_len(&cache), FILE_CACHE_MAX);
        // The oldest keys are gone, the newest are present.
        assert!(cache.get("key-0").is_none());
        assert!(cache.get(&format!("key-{}", FILE_CACHE_MAX + 9)).is_some());
        cache.clear();
        assert_eq!(cache_len(&cache), 0);
    }

    /// A modified file is a different `mtime_ns`, so it cannot hit a stale entry.
    #[test]
    fn a_changed_index_bypasses_the_cache() {
        let root = temp_root("mtime");
        let cache = FileCache::new();
        let id = "d".repeat(32);
        write_index(&root, &id, None, r#"{"version": 1}"#);
        assert_eq!(
            load_cached_file(&root, &id, None, &cache).unwrap()["version"],
            1
        );
        // Rewrite with a different length so the mtime must move on any filesystem.
        std::thread::sleep(std::time::Duration::from_millis(10));
        write_index(&root, &id, None, r#"{"version": 22222}"#);
        assert_eq!(
            load_cached_file(&root, &id, None, &cache).unwrap()["version"],
            22222
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn python_int_matches_the_oracles_coercions() {
        assert_eq!(python_int(Some(&json!(5))).unwrap(), 5);
        assert_eq!(python_int(Some(&json!(-3))).unwrap(), -3);
        assert_eq!(python_int(Some(&json!(true))).unwrap(), 1);
        assert_eq!(python_int(Some(&json!(false))).unwrap(), 0);
        // A float truncates toward zero.
        assert_eq!(python_int(Some(&json!(3.7))).unwrap(), 3);
        assert_eq!(python_int(Some(&json!(-3.7))).unwrap(), -3);
        // A string parses, with the same allowances as `int()`.
        assert_eq!(python_int(Some(&json!("12"))).unwrap(), 12);
        assert_eq!(python_int(Some(&json!("  8  "))).unwrap(), 8);
        assert_eq!(python_int(Some(&json!("-4"))).unwrap(), -4);
        assert_eq!(python_int(Some(&json!("+9"))).unwrap(), 9);
        assert_eq!(python_int(Some(&json!("1_0"))).unwrap(), 10);
        // And rejects what `int()` rejects rather than falling back.
        for bad in [json!("3.7"), json!("abc"), json!(""), json!(null)] {
            assert!(python_int(Some(&bad)).is_err(), "{bad}");
        }
    }

    #[test]
    fn int_field_applies_the_or_idiom() {
        let chunk = json!({"lineStart": 4, "lineEnd": 0, "missing": null});
        assert_eq!(int_field(&chunk, "lineStart", 0).unwrap(), 4);
        // A falsy value falls back to the default rather than `int(0)`.
        assert_eq!(int_field(&chunk, "lineEnd", 7).unwrap(), 7);
        assert_eq!(int_field(&chunk, "missing", 9).unwrap(), 9);
        assert_eq!(int_field(&chunk, "absent", 11).unwrap(), 11);
    }
}
