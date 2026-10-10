//! Production-grade atomic restore engine with isolated staging and fail-closed integrity validation.

use std::fmt;
use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::receipt::{DocumentError, validate_committed_documents};

#[derive(Debug)]
pub enum RestoreError {
    Document(DocumentError),
    IntegrityMismatch {
        expected: String,
        actual: String,
    },
    SizeMismatch {
        expected: u64,
        actual: u64,
    },
    PathTraversal(String),
    Io(io::Error),
    NotFound(String),
    #[cfg(feature = "s3")]
    Storage(crate::s3::S3Error),
}

impl fmt::Display for RestoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Document(err) => write!(f, "restore document invalid: {err:?}"),
            Self::IntegrityMismatch { expected, actual } => {
                write!(
                    f,
                    "restore integrity mismatch: expected {expected}, got {actual}"
                )
            }
            Self::SizeMismatch { expected, actual } => {
                write!(
                    f,
                    "restore size mismatch: expected {expected} bytes, got {actual} bytes"
                )
            }
            Self::PathTraversal(path) => write!(f, "forbidden path traversal in restore: {path}"),
            Self::Io(err) => write!(f, "restore I/O error: {err}"),
            Self::NotFound(key) => write!(f, "restore object not found: {key}"),
            #[cfg(feature = "s3")]
            Self::Storage(err) => write!(f, "restore storage error: {err}"),
        }
    }
}

impl std::error::Error for RestoreError {}

impl From<DocumentError> for RestoreError {
    fn from(err: DocumentError) -> Self {
        Self::Document(err)
    }
}

impl From<io::Error> for RestoreError {
    fn from(err: io::Error) -> Self {
        Self::Io(err)
    }
}

#[cfg(feature = "s3")]
impl From<crate::s3::S3Error> for RestoreError {
    fn from(err: crate::s3::S3Error) -> Self {
        Self::Storage(err)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestoreSummary {
    pub restored_objects: usize,
    pub total_bytes: u64,
    pub backup_id: String,
    pub object_set_digest: String,
}

pub fn sanitize_relative_path(path_str: &str) -> Result<PathBuf, RestoreError> {
    if path_str.is_empty()
        || path_str.starts_with('/')
        || path_str.starts_with('\\')
        || path_str.contains("..")
        || path_str.contains(':')
    {
        return Err(RestoreError::PathTraversal(path_str.to_string()));
    }
    let path = PathBuf::from(path_str);
    for comp in path.components() {
        match comp {
            Component::Normal(_) => {}
            _ => return Err(RestoreError::PathTraversal(path_str.to_string())),
        }
    }
    Ok(path)
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
    }
    s
}

#[cfg(feature = "s3")]
fn decode_hex_32(s: &str) -> Option<[u8; 32]> {
    if s.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(out)
}

pub struct RestoreEngine;

impl RestoreEngine {
    /// Restores a verified backup into `destination_dir`.
    /// 1. Validates canonical receipt and commit documents.
    /// 2. Fetches and verifies each object into an isolated staging directory.
    /// 3. If any object fails verification or read, staging is purged and destination is untouched.
    /// 4. Once all objects are verified, atomically promotes staging into `destination_dir`.
    pub fn restore_payloads<F>(
        receipt_bytes: &[u8],
        commit_bytes: &[u8],
        destination_dir: &Path,
        mut payload_fetcher: F,
    ) -> Result<RestoreSummary, RestoreError>
    where
        F: FnMut(&str) -> Result<Vec<u8>, RestoreError>,
    {
        let (receipt, _) = validate_committed_documents(receipt_bytes, commit_bytes)?;

        fs::create_dir_all(destination_dir)?;
        let pid = std::process::id();
        let staging_name = format!(".restore-staging-{}-{:08x}", pid, receipt.objects.len());
        let staging_dir = destination_dir.join(staging_name);

        if staging_dir.exists() {
            let _ = fs::remove_dir_all(&staging_dir);
        }
        fs::create_dir_all(&staging_dir)?;

        let staging_cleanup = StagingGuard {
            path: staging_dir.clone(),
            active: true,
        };

        let mut total_bytes: u64 = 0;
        let mut restored_count: usize = 0;

        for entry in &receipt.objects {
            let safe_name = sanitize_relative_path(&entry.digest)?;
            let target_file = staging_dir.join(safe_name);

            let payload = payload_fetcher(&entry.digest)?;
            if payload.len() as u64 != entry.size {
                return Err(RestoreError::SizeMismatch {
                    expected: entry.size,
                    actual: payload.len() as u64,
                });
            }

            let actual_digest = hex(&Sha256::digest(&payload));
            if actual_digest != entry.digest {
                return Err(RestoreError::IntegrityMismatch {
                    expected: entry.digest.clone(),
                    actual: actual_digest,
                });
            }

            let mut file = File::create(&target_file)?;
            file.write_all(&payload)?;
            file.flush()?;
            file.sync_all()?;

            total_bytes += payload.len() as u64;
            restored_count += 1;
        }

        // All objects verified in staging! Atomically promote into destination_dir
        for entry in &receipt.objects {
            let safe_name = sanitize_relative_path(&entry.digest)?;
            let src = staging_dir.join(&safe_name);
            let dst = destination_dir.join(&safe_name);
            fs::rename(src, dst)?;
        }

        // Remove staging directory now that files have moved
        let mut guard = staging_cleanup;
        guard.active = false;
        let _ = fs::remove_dir_all(&staging_dir);

        Ok(RestoreSummary {
            restored_objects: restored_count,
            total_bytes,
            backup_id: receipt.backup_id,
            object_set_digest: receipt.object_set_digest,
        })
    }

    #[cfg(feature = "s3")]
    pub async fn restore_from_s3(
        receipt_bytes: &[u8],
        commit_bytes: &[u8],
        destination_dir: &Path,
        transport: &crate::s3::S3Transport,
    ) -> Result<RestoreSummary, RestoreError> {
        let (receipt, _) = validate_committed_documents(receipt_bytes, commit_bytes)?;

        fs::create_dir_all(destination_dir)?;
        let pid = std::process::id();
        let staging_name = format!(".restore-s3-staging-{}-{:08x}", pid, receipt.objects.len());
        let staging_dir = destination_dir.join(staging_name);

        if staging_dir.exists() {
            let _ = fs::remove_dir_all(&staging_dir);
        }
        fs::create_dir_all(&staging_dir)?;

        let staging_cleanup = StagingGuard {
            path: staging_dir.clone(),
            active: true,
        };

        let mut total_bytes: u64 = 0;
        let mut restored_count: usize = 0;

        for entry in &receipt.objects {
            let safe_name = sanitize_relative_path(&entry.digest)?;
            let target_file = staging_dir.join(safe_name);

            let digest_bytes = match decode_hex_32(&entry.digest) {
                Some(b) => b,
                None => {
                    return Err(RestoreError::IntegrityMismatch {
                        expected: entry.digest.clone(),
                        actual: "invalid-hex".to_string(),
                    });
                }
            };

            let mut out_file = tokio::fs::File::create(&target_file).await?;
            transport
                .download_verified(&entry.digest, entry.size, digest_bytes, &mut out_file)
                .await?;
            out_file.sync_all().await?;

            total_bytes += entry.size;
            restored_count += 1;
        }

        // All objects verified! Promote into destination_dir
        for entry in &receipt.objects {
            let safe_name = sanitize_relative_path(&entry.digest)?;
            let src = staging_dir.join(&safe_name);
            let dst = destination_dir.join(&safe_name);
            fs::rename(src, dst)?;
        }

        let mut guard = staging_cleanup;
        guard.active = false;
        let _ = fs::remove_dir_all(&staging_dir);

        Ok(RestoreSummary {
            restored_objects: restored_count,
            total_bytes,
            backup_id: receipt.backup_id,
            object_set_digest: receipt.object_set_digest,
        })
    }
}

struct StagingGuard {
    path: PathBuf,
    active: bool,
}

impl Drop for StagingGuard {
    fn drop(&mut self) {
        if self.active && self.path.exists() {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}
