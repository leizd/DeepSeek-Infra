//! Commit-marker reads for the backup catalog's write path.
//!
//! Every catalog append records the target generation it was written at, and that
//! value comes from the newest published commit under `<root>/commits/`. This module
//! is the native equivalent of `backup_publish.read_commit_markers` /
//! `latest_commit` / `commit_marker_valid`, kept separate from the catalog routes
//! because S3-S8 all need it: retention, restores, drills and DR all read the commit
//! chain before they write anything.
//!
//! # Why the details matter
//!
//! The catalog is an append-only hash chain whose entries are compared byte for byte
//! with the Python writer's. A reader that disagrees with Python about *which* marker
//! is newest would stamp a different `targetGeneration` into a new entry, and the two
//! chains would diverge permanently. Three behaviours are therefore reproduced
//! exactly rather than approximated:
//!
//! 1. **Discovery order.** Python globs `commits/*/*.json` and iterates `sorted(...)`,
//!    which is lexicographic by full path. The order decides the tie-break below.
//! 2. **Tie-break.** `max(markers, key=targetGeneration)` returns the *first* maximal
//!    element, so when two markers share a generation the lexicographically smallest
//!    path wins. Reversing that pick would be invisible in any single-marker test.
//! 3. **Tolerant reads.** An unparsable or non-object file is skipped, not fatal, and
//!    a marker without `commitHash` is not a marker at all. A reader that raised here
//!    would turn a stray file into a write outage.
//!
//! `commitGeneration` also mirrors Python's `int(marker.get("targetGeneration") or 0)`,
//! including its treatment of strings and of non-numeric values.

use std::path::{Path, PathBuf};

use deepseek_policy::core_utils::{python_truthy, text_or_empty};
use deepseek_policy::python_json::dumps_compact;
use serde_json::Value;
use sha2::{Digest, Sha256};

/// The commit directory name, matching `backup_publish.LAYOUT_DIRS`.
const COMMITS_DIR: &str = "commits";
/// Python's `GENESIS_COMMIT_HASH`, used when there is no predecessor.
pub const GENESIS_COMMIT_HASH: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

/// One parsed commit marker, with the path it was found at.
#[derive(Debug, Clone)]
pub struct CommitMarker {
    /// Relative path inside the target root, used for the deterministic tie-break.
    pub relative_path: String,
    /// The parsed marker document.
    pub document: Value,
}

impl CommitMarker {
    /// `int(marker.get("targetGeneration") or 0)`.
    ///
    /// The `or 0` is why an absent, null, empty-string or zero generation all collapse
    /// to 0, and Python's `int()` accepts a numeric string, so `"7"` counts as 7. A
    /// non-numeric string raises in Python and is treated as 0 here: the caller is a
    /// generation lookup, and refusing the whole chain over one malformed field would
    /// be worse than treating it as unversioned.
    pub fn generation(&self) -> i64 {
        match self.document.get("targetGeneration") {
            Some(Value::Number(number)) => number.as_i64().unwrap_or(0),
            Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
            Some(Value::Bool(true)) => 1,
            _ => 0,
        }
    }

    /// The marker's `commitHash`, or the empty string when absent.
    ///
    /// `value_str` alone is wrong here: it renders `None` as the literal text "None",
    /// so a missing field would become a non-empty hash. Coercion is therefore gated on
    /// `python_truthy`, which is the predicate Python's own `if not data.get(...)`
    /// performs.
    pub fn commit_hash(&self) -> String {
        text_or_empty(self.document.get("commitHash"))
    }
}

/// `backup_publish.read_commit_markers`: parse every `commits/*/*.json` that is a
/// JSON object carrying a truthy `commitHash`, in `sorted()` path order.
pub fn read_commit_markers(root: &Path) -> Vec<CommitMarker> {
    let directory = root.join(COMMITS_DIR);
    if !directory.is_dir() {
        return Vec::new();
    }
    let mut paths: Vec<PathBuf> = Vec::new();
    let Ok(buckets) = std::fs::read_dir(&directory) else {
        return Vec::new();
    };
    for bucket in buckets.flatten() {
        let bucket_path = bucket.path();
        if !bucket_path.is_dir() {
            continue;
        }
        let Ok(entries) = std::fs::read_dir(&bucket_path) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) != Some("json") || !path.is_file()
            {
                continue;
            }
            paths.push(path);
        }
    }
    // Lexicographic by the full path, which is what `sorted(commits_dir.glob("*/*.json"))`
    // does and what the tie-break below depends on.
    paths.sort();

    let mut markers = Vec::with_capacity(paths.len());
    for path in paths {
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        let Ok(document) = serde_json::from_slice::<Value>(&bytes) else {
            continue;
        };
        if !document.is_object() {
            continue;
        }
        // `data.get("commitHash")` is truthy only for a non-empty value; an empty
        // string, null or zero is falsy in Python and is not a marker.
        if !document.get("commitHash").is_some_and(python_truthy) {
            continue;
        }
        markers.push(CommitMarker {
            relative_path: relative_path(root, &path),
            document,
        });
    }
    markers
}

/// `backup_publish.latest_commit`: the marker with the greatest generation, or `None`.
///
/// `max()` keeps the first maximal element, so this scan compares with `>` rather than
/// `>=`; using `>=` would flip the winner whenever two markers share a generation.
pub fn latest_commit(root: &Path) -> Option<CommitMarker> {
    let mut best: Option<CommitMarker> = None;
    for marker in read_commit_markers(root) {
        match &best {
            Some(current) if current.generation() >= marker.generation() => {}
            _ => best = Some(marker),
        }
    }
    best
}

/// The generation a new catalog entry should record: `int(latest.get("targetGeneration") or 0)`,
/// or 0 when nothing has been published yet.
pub fn target_generation(root: &Path) -> i64 {
    latest_commit(root)
        .map(|marker| marker.generation())
        .unwrap_or(0)
}

/// `backup_publish._commit_hash`: sha256 over the marker minus its own `commitHash`.
pub fn commit_hash(marker: &Value) -> String {
    let Some(object) = marker.as_object() else {
        return String::new();
    };
    let mut body = serde_json::Map::with_capacity(object.len());
    for (key, value) in object {
        if key != "commitHash" {
            body.insert(key.clone(), value.clone());
        }
    }
    // `_stable_json` is `sort_keys=True, separators=(",", ":")` with UTF-8 output,
    // which is exactly `dumps_compact`.
    sha256_hex(dumps_compact(&Value::Object(body)).as_bytes())
}

/// `backup_publish.commit_marker_valid`: the marker's own hash matches its contents.
pub fn commit_marker_valid(marker: &Value) -> bool {
    if !marker.get("commitHash").is_some_and(python_truthy) {
        return false;
    }
    text_or_empty(marker.get("commitHash")) == commit_hash(marker)
}

/// `backup_publish.GENESIS_COMMIT_HASH` used as the predecessor when none exists.
pub fn previous_commit_hash(root: &Path) -> String {
    match latest_commit(root) {
        Some(marker) => marker.commit_hash(),
        None => GENESIS_COMMIT_HASH.to_string(),
    }
}

fn relative_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .map(|value| value.to_string_lossy().replace('\\', "/"))
        .unwrap_or_else(|_| path.to_string_lossy().to_string())
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        hex.push_str(&format!("{byte:02x}"));
    }
    hex
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn write_marker(root: &Path, bucket: &str, name: &str, generation: i64) {
        let directory = root.join(COMMITS_DIR).join(bucket);
        std::fs::create_dir_all(&directory).expect("mkdir");
        let marker =
            json!({"commitHash": format!("{bucket}/{name}"), "targetGeneration": generation});
        std::fs::write(directory.join(name), marker.to_string()).expect("write");
    }

    #[test]
    fn a_missing_commits_directory_is_not_an_error() {
        let root = tempfile::tempdir().expect("tempdir");
        assert!(read_commit_markers(root.path()).is_empty());
        assert!(latest_commit(root.path()).is_none());
        assert_eq!(target_generation(root.path()), 0);
        assert_eq!(previous_commit_hash(root.path()), GENESIS_COMMIT_HASH);
    }

    #[test]
    fn the_highest_generation_wins_across_buckets() {
        let root = tempfile::tempdir().expect("tempdir");
        write_marker(root.path(), "aa", "one.json", 3);
        write_marker(root.path(), "zz", "two.json", 9);
        assert_eq!(target_generation(root.path()), 9);
        assert_eq!(previous_commit_hash(root.path()), "zz/two.json");
    }

    /// The tie-break is the whole reason the path ordering is preserved: Python's
    /// `max()` keeps the first maximal element, so the lexicographically smallest
    /// path wins. A `>=` comparison here would silently return the last one instead.
    #[test]
    fn a_tied_generation_resolves_to_the_first_path() {
        let root = tempfile::tempdir().expect("tempdir");
        write_marker(root.path(), "aa", "bbb.json", 5);
        write_marker(root.path(), "zz", "aaa.json", 5);
        write_marker(root.path(), "mm", "ccc.json", 3);
        assert_eq!(target_generation(root.path()), 5);
        assert_eq!(previous_commit_hash(root.path()), "aa/bbb.json");
    }

    #[test]
    fn unparsable_and_non_object_markers_are_skipped() {
        let root = tempfile::tempdir().expect("tempdir");
        let bucket = root.path().join(COMMITS_DIR).join("aa");
        std::fs::create_dir_all(&bucket).expect("mkdir");
        std::fs::write(bucket.join("broken.json"), b"{not json").expect("write");
        std::fs::write(bucket.join("array.json"), b"[1,2,3]").expect("write");
        // An empty commitHash is falsy in Python and is not a marker.
        std::fs::write(
            bucket.join("empty.json"),
            br#"{"commitHash":"","targetGeneration":99}"#,
        )
        .expect("write");
        write_marker(root.path(), "aa", "real.json", 2);
        assert_eq!(read_commit_markers(root.path()).len(), 1);
        assert_eq!(target_generation(root.path()), 2);
    }

    #[test]
    fn non_json_files_are_ignored() {
        let root = tempfile::tempdir().expect("tempdir");
        let bucket = root.path().join(COMMITS_DIR).join("aa");
        std::fs::create_dir_all(&bucket).expect("mkdir");
        std::fs::write(bucket.join("notes.txt"), b"hello").expect("write");
        write_marker(root.path(), "aa", "real.json", 1);
        assert_eq!(read_commit_markers(root.path()).len(), 1);
    }

    #[test]
    fn a_generation_may_be_a_numeric_string_and_falsy_values_collapse_to_zero() {
        let root = tempfile::tempdir().expect("tempdir");
        let bucket = root.path().join(COMMITS_DIR).join("aa");
        std::fs::create_dir_all(&bucket).expect("mkdir");
        std::fs::write(
            bucket.join("text.json"),
            br#"{"commitHash":"a/text","targetGeneration":"7"}"#,
        )
        .expect("write");
        assert_eq!(target_generation(root.path()), 7);

        let nulls = tempfile::tempdir().expect("tempdir");
        let bucket = nulls.path().join(COMMITS_DIR).join("aa");
        std::fs::create_dir_all(&bucket).expect("mkdir");
        std::fs::write(
            bucket.join("null.json"),
            br#"{"commitHash":"a/null","targetGeneration":null}"#,
        )
        .expect("write");
        assert_eq!(target_generation(nulls.path()), 0);
    }

    #[test]
    fn a_valid_marker_hashes_to_its_own_contents() {
        let mut marker = json!({
            "schemaVersion": 4,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "a".repeat(64),
            "controlObjectDigest": "b".repeat(64),
            "targetGeneration": 3,
            "previousCommitHash": GENESIS_COMMIT_HASH,
        });
        assert!(!commit_marker_valid(&marker), "no commitHash yet");
        let digest = commit_hash(&marker);
        marker["commitHash"] = Value::String(digest.clone());
        assert!(commit_marker_valid(&marker));
        // Mutating any covered field breaks it.
        marker["targetGeneration"] = json!(4);
        assert!(!commit_marker_valid(&marker));
    }
}
