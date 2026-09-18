//! The file index behind [`crate::attachment_context::FileContextDeps`] — `load_cached_file`,
//! and an honest hole where the vector index would be.
//!
//! # What is here
//!
//! `load_cached_file` reads one cached index document, with the oracle's four failure modes
//! kept apart because each carries its own code and message, and the message **is** rendered
//! (`build_attachment_context` writes `[文件索引读取失败：...]` into the prompt):
//!
//! - a `file_id` that is not 32 lowercase hex characters -> `INVALID_PAYLOAD`, 400;
//! - no such file -> `FILE_INDEX_EXPIRED`, 410;
//! - a `stat` that fails -> `INTERNAL`, 500;
//! - unreadable or non-object JSON -> `INTERNAL`, 500.
//!
//! `project_id` reaches `project_file_cache_dir` only when it is **truthy**, matching
//! `if project_id:`, and that path validates `[a-zA-Z0-9_-]{4,64}` first. When the project is
//! empty the oracle memoises on `(file_id, mtime_ns)`; so does this, and the eviction order
//! of that memo is not observable — a changed file changes the key.
//!
//! # What is not here, on purpose
//!
//! `local_rag.search_file_chunks` — the other half of `FileContextDeps` — is not a store. It
//! resolves through `local_rag.search`, which is the RAG retrieval subsystem: the vector
//! table, the embedding store, the ranking pipeline, ~1,200 lines plus its own schema. That is
//! a **milestone**, not a slice, and it is not faked here.
//!
//! Until it lands, callers must fail closed. An empty `Vec` from a not-implemented vector
//! index is indistinguishable from "no hits" and would silently cost every attachment its
//! indexed chunks — the failure would be invisible in the answer. [`vector_index_not_ready`]
//! names the condition and the code to refuse with.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;

use serde_json::Value;

use crate::app_error::{AppError, codes};

/// `ErrorCode`-style code for a store that is not there yet: the assembly must refuse rather
/// than serve a file context with no indexed chunks.
pub const VECTOR_INDEX_NOT_READY: &str = "NATIVE_FILE_VECTOR_INDEX_NOT_READY";

/// The refusal to hand back when `search_file_chunks` has no implementation.
pub fn vector_index_not_ready() -> AppError {
    AppError {
        message: "The file vector index is not implemented yet, so indexed chunks cannot be \
                  selected; refusing rather than serving a file context without them."
            .to_string(),
        code: VECTOR_INDEX_NOT_READY,
        status: 501,
    }
}

/// A file cache bound to the two directories the oracle reads from.
pub struct FileStore {
    file_cache_dir: PathBuf,
    projects_dir: PathBuf,
    /// `lru_cache(maxsize=64)` on `(file_id, mtime_ns)`; the capacity is not observable.
    memo: Mutex<HashMap<(String, i64), Value>>,
}

impl FileStore {
    pub fn new(file_cache_dir: impl Into<PathBuf>, projects_dir: impl Into<PathBuf>) -> Self {
        Self {
            file_cache_dir: file_cache_dir.into(),
            projects_dir: projects_dir.into(),
            memo: Mutex::new(HashMap::new()),
        }
    }

    /// Mirrors `project_file_cache_dir`, including its own rejection.
    pub fn project_file_cache_dir(&self, project_id: Option<&str>) -> Result<PathBuf, AppError> {
        let safe_id = project_id.unwrap_or("").trim();
        if !is_valid_project_id(safe_id) {
            return Err(AppError {
                message: "Invalid project id".to_string(),
                code: codes::INVALID_PAYLOAD,
                status: 400,
            });
        }
        Ok(self.projects_dir.join(safe_id).join("files"))
    }

    /// Mirrors `load_cached_file`.
    pub fn load_cached_file(
        &self,
        file_id: &str,
        project_id: Option<&str>,
    ) -> Result<Value, AppError> {
        if !is_valid_file_id(file_id) {
            return Err(AppError {
                message: "Invalid file id".to_string(),
                code: codes::INVALID_PAYLOAD,
                status: 400,
            });
        }
        let project = project_id.unwrap_or("");
        let path = if project.is_empty() {
            self.file_cache_dir.join(format!("{file_id}.json"))
        } else {
            self.project_file_cache_dir(Some(project))?
                .join(format!("{file_id}.json"))
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
            .map_err(|_| AppError {
                message: "Uploaded file index is unreadable".to_string(),
                code: codes::INTERNAL,
                status: 500,
            })?;
        let mtime_ns = modified
            .duration_since(UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos() as i64)
            .unwrap_or(0);

        if !project.is_empty() {
            return read_index_document(&path);
        }
        let key = (file_id.to_string(), mtime_ns);
        if let Some(cached) = self
            .memo
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(&key)
        {
            return Ok(cached.clone());
        }
        let document = read_index_document(&path)?;
        let mut memo = self
            .memo
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if memo.len() >= 64 {
            memo.clear();
        }
        memo.insert(key, document.clone());
        Ok(document)
    }
}

/// The read the two branches share: a missing or malformed document is `INTERNAL`, and a
/// document that is not an object says so separately.
fn read_index_document(path: &Path) -> Result<Value, AppError> {
    let text = std::fs::read_to_string(path).map_err(|_| AppError {
        message: "Uploaded file index is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    })?;
    let document: Value = serde_json::from_str(&text).map_err(|_| AppError {
        message: "Uploaded file index is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    })?;
    if !document.is_object() {
        return Err(AppError {
            message: "Uploaded file index is invalid".to_string(),
            code: codes::INTERNAL,
            status: 500,
        });
    }
    Ok(document)
}

/// `re.fullmatch(r"[0-9a-f]{32}", file_id)`.
fn is_valid_file_id(file_id: &str) -> bool {
    file_id.len() == 32
        && file_id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// `re.fullmatch(r"[a-zA-Z0-9_-]{4,64}", project_id)`.
fn is_valid_project_id(project_id: &str) -> bool {
    (4..=64).contains(&project_id.chars().count())
        && project_id.chars().all(|character| {
            character.is_ascii_alphanumeric() || character == '_' || character == '-'
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Scratch {
        root: PathBuf,
        cache: PathBuf,
        projects: PathBuf,
    }

    impl Scratch {
        fn new(tag: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "file-store-test-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&root);
            let cache = root.join("cache");
            let projects = root.join("projects");
            std::fs::create_dir_all(&cache).expect("a cache directory");
            Self {
                root,
                cache,
                projects,
            }
        }

        fn store(&self) -> FileStore {
            FileStore::new(&self.cache, &self.projects)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn a_file_id_must_be_thirty_two_lowercase_hex_characters() {
        let scratch = Scratch::new("id");
        let store = scratch.store();
        for bad in [
            "A".repeat(32),
            "a".repeat(31),
            "a".repeat(33),
            "g".repeat(32),
            String::new(),
            format!("{}B", "a".repeat(31)),
        ] {
            let error = store
                .load_cached_file(&bad, None)
                .expect_err("the id is rejected");
            assert_eq!(error.message, "Invalid file id", "for {bad:?}");
            assert_eq!(error.code, codes::INVALID_PAYLOAD);
            assert_eq!(error.status, 400);
        }
    }

    #[test]
    fn the_three_read_failures_keep_their_own_codes_and_messages() {
        let scratch = Scratch::new("failures");
        let store = scratch.store();

        let missing = store
            .load_cached_file(&"b".repeat(32), None)
            .expect_err("no such file");
        assert_eq!(
            missing.message,
            "Uploaded file index has expired or is missing"
        );
        assert_eq!(missing.code, codes::FILE_INDEX_EXPIRED);
        assert_eq!(missing.status, 410);

        std::fs::write(
            scratch.cache.join(format!("{}.json", "c".repeat(32))),
            "not json",
        )
        .expect("the malformed document writes");
        let malformed = store
            .load_cached_file(&"c".repeat(32), None)
            .expect_err("not json");
        assert_eq!(malformed.message, "Uploaded file index is unreadable");
        assert_eq!(malformed.code, codes::INTERNAL);

        std::fs::write(
            scratch.cache.join(format!("{}.json", "d".repeat(32))),
            "[1, 2]",
        )
        .expect("the array document writes");
        let invalid = store
            .load_cached_file(&"d".repeat(32), None)
            .expect_err("not an object");
        assert_eq!(invalid.message, "Uploaded file index is invalid");
        assert_eq!(invalid.status, 500);
    }

    #[test]
    fn a_project_id_is_validated_before_its_path_is_built() {
        let scratch = Scratch::new("project");
        let store = scratch.store();
        let file_id = "a".repeat(32);

        let error = store
            .load_cached_file(&file_id, Some("ab"))
            .expect_err("too short");
        assert_eq!(error.message, "Invalid project id");
        assert_eq!(error.code, codes::INVALID_PAYLOAD);

        // A valid project reads from its own directory, and an empty one reads the global
        // cache rather than being rejected.
        let project_dir = scratch.projects.join("proj1").join("files");
        std::fs::create_dir_all(&project_dir).expect("the project directory");
        std::fs::write(
            project_dir.join(format!("{file_id}.json")),
            serde_json::to_string(&json!({"id": file_id, "projectId": "proj1"})).unwrap(),
        )
        .expect("the project document writes");
        std::fs::write(
            scratch.cache.join(format!("{file_id}.json")),
            serde_json::to_string(&json!({"id": file_id, "name": "全局"})).unwrap(),
        )
        .expect("the global document writes");

        assert_eq!(
            store.load_cached_file(&file_id, Some("proj1")).unwrap()["projectId"],
            "proj1"
        );
        assert_eq!(
            store.load_cached_file(&file_id, Some("")).unwrap()["name"],
            "全局"
        );
        assert_eq!(
            store.load_cached_file(&file_id, None).unwrap()["name"],
            "全局"
        );
    }

    #[test]
    fn a_rewritten_document_is_what_comes_back() {
        let scratch = Scratch::new("rewrite");
        let store = scratch.store();
        let file_id = "a".repeat(32);
        let path = scratch.cache.join(format!("{file_id}.json"));
        std::fs::write(
            &path,
            serde_json::to_string(&json!({"name": "v1"})).unwrap(),
        )
        .unwrap();
        assert_eq!(
            store.load_cached_file(&file_id, None).unwrap()["name"],
            "v1"
        );

        std::fs::write(
            &path,
            serde_json::to_string(&json!({"name": "v2"})).unwrap(),
        )
        .unwrap();
        assert_eq!(
            store.load_cached_file(&file_id, None).unwrap()["name"],
            "v2"
        );
    }

    #[test]
    fn the_vector_index_refuses_rather_than_returning_nothing() {
        let error = vector_index_not_ready();
        assert_eq!(error.code, VECTOR_INDEX_NOT_READY);
        assert_eq!(error.status, 501);
    }
}
