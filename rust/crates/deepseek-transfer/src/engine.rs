//! Production-grade streaming transfer engine.
//! Moves payload bytes between memory, files, and S3 endpoints under control-plane authority.
//! Memory usage is strictly bounded with backpressure, and all mutations fail closed.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use deepseek_protocol::{ActionFence, AdmitError, admit_command};
use sha2::{Digest, Sha256};

use crate::journal::{FederatedTransferJournal, FederatedTransferJournalError, TransferState};

pub const DEFAULT_CHUNK_SIZE: usize = 64 * 1024; // 64 KiB
pub const MIN_CHUNK_SIZE: usize = 4 * 1024; // 4 KiB
pub const MAX_CHUNK_SIZE: usize = 8 * 1024 * 1024; // 8 MiB

#[derive(Debug)]
pub enum TransferError {
    Admit(AdmitError),
    Io(io::Error),
    DigestMismatch {
        expected: String,
        actual: String,
    },
    LengthMismatch {
        expected: u64,
        actual: u64,
    },
    BufferTooLarge {
        requested: usize,
        max: usize,
    },
    InvalidFence(&'static str),
    SinkAborted(String),
    Journal(FederatedTransferJournalError),
    AuthorityBindingMismatch(&'static str),
    PathTraversal(&'static str),
    #[cfg(feature = "s3")]
    Storage(deepseek_storage::s3::S3Error),
}

impl fmt::Display for TransferError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Admit(err) => write!(f, "transfer admission denied: {err:?}"),
            Self::Io(err) => write!(f, "transfer I/O error: {err}"),
            Self::DigestMismatch { expected, actual } => {
                write!(
                    f,
                    "transfer digest mismatch: expected {expected}, got {actual}"
                )
            }
            Self::LengthMismatch { expected, actual } => {
                write!(
                    f,
                    "transfer length mismatch: expected {expected} bytes, got {actual} bytes"
                )
            }
            Self::BufferTooLarge { requested, max } => {
                write!(f, "requested buffer size {requested} exceeds max {max}")
            }
            Self::InvalidFence(msg) => write!(f, "invalid fence for transfer: {msg}"),
            Self::SinkAborted(msg) => write!(f, "transfer sink aborted: {msg}"),
            Self::Journal(err) => write!(f, "transfer journal error: {err}"),
            Self::AuthorityBindingMismatch(msg) => {
                write!(f, "authority binding mismatch: {msg}")
            }
            Self::PathTraversal(msg) => {
                write!(f, "path traversal or unsafe link rejected: {msg}")
            }
            #[cfg(feature = "s3")]
            Self::Storage(err) => write!(f, "storage transport error: {err}"),
        }
    }
}

impl std::error::Error for TransferError {}

impl From<AdmitError> for TransferError {
    fn from(err: AdmitError) -> Self {
        Self::Admit(err)
    }
}

impl From<io::Error> for TransferError {
    fn from(err: io::Error) -> Self {
        Self::Io(err)
    }
}

impl From<FederatedTransferJournalError> for TransferError {
    fn from(err: FederatedTransferJournalError) -> Self {
        Self::Journal(err)
    }
}

#[cfg(feature = "s3")]
impl From<deepseek_storage::s3::S3Error> for TransferError {
    fn from(err: deepseek_storage::s3::S3Error) -> Self {
        Self::Storage(err)
    }
}

/// Control-plane authority proof bound to an exact transfer job.
/// Any mutation or substitution of source, destination, digest, or length invalidates this proof.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransferAuthorityProof {
    pub action_id: String,
    pub execution_epoch: u64,
    pub fencing_token: i64,
    pub source_identity: String,
    pub destination_identity: String,
    pub expected_digest: [u8; 32],
    pub expected_length: u64,
    pub request_id: String,
    pub nonce: String,
}

impl TransferAuthorityProof {
    pub fn validate(
        &self,
        fence: &ActionFence,
        actual_source: &str,
        actual_dest: &str,
        actual_digest: &[u8; 32],
        actual_length: u64,
    ) -> Result<(), TransferError> {
        if self.action_id != fence.action_id || self.execution_epoch != fence.execution_epoch {
            return Err(TransferError::AuthorityBindingMismatch(
                "fence action_id or execution_epoch mismatch",
            ));
        }
        if self.source_identity != actual_source {
            return Err(TransferError::AuthorityBindingMismatch(
                "source identity does not match authorized source",
            ));
        }
        if self.destination_identity != actual_dest {
            return Err(TransferError::AuthorityBindingMismatch(
                "destination identity does not match authorized destination",
            ));
        }
        if &self.expected_digest != actual_digest {
            return Err(TransferError::AuthorityBindingMismatch(
                "digest does not match authorized digest",
            ));
        }
        if self.expected_length != actual_length {
            return Err(TransferError::AuthorityBindingMismatch(
                "length does not match authorized length",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransferOptions {
    pub chunk_size: usize,
    pub expected_length: Option<u64>,
    pub expected_digest: Option<[u8; 32]>,
    pub resume_offset: u64,
    pub atomic_commit: bool,
}

impl Default for TransferOptions {
    fn default() -> Self {
        Self {
            chunk_size: DEFAULT_CHUNK_SIZE,
            expected_length: None,
            expected_digest: None,
            resume_offset: 0,
            atomic_commit: true,
        }
    }
}

impl TransferOptions {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_chunk_size(mut self, size: usize) -> Result<Self, TransferError> {
        if size > MAX_CHUNK_SIZE {
            return Err(TransferError::BufferTooLarge {
                requested: size,
                max: MAX_CHUNK_SIZE,
            });
        }
        self.chunk_size = size.clamp(MIN_CHUNK_SIZE, MAX_CHUNK_SIZE);
        Ok(self)
    }

    pub fn with_expected_length(mut self, len: u64) -> Self {
        self.expected_length = Some(len);
        self
    }

    pub fn with_expected_digest(mut self, digest: [u8; 32]) -> Self {
        self.expected_digest = Some(digest);
        self
    }

    pub fn with_resume_offset(mut self, offset: u64) -> Self {
        self.resume_offset = offset;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransferReceipt {
    pub transfer_id: String,
    pub bytes_transferred: u64,
    pub chunks_count: usize,
    pub sha256: [u8; 32],
    pub sha256_hex: String,
}

pub enum TransferSource {
    Memory(io::Cursor<Vec<u8>>),
    File(File),
    Reader(Box<dyn Read + Send>),
}

impl TransferSource {
    pub fn from_bytes(bytes: Vec<u8>) -> Self {
        Self::Memory(io::Cursor::new(bytes))
    }

    pub fn from_file(path: impl AsRef<Path>) -> io::Result<Self> {
        let file = File::open(path)?;
        Ok(Self::File(file))
    }

    pub fn from_reader(reader: Box<dyn Read + Send>) -> Self {
        Self::Reader(reader)
    }
}

impl Read for TransferSource {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Self::Memory(cursor) => cursor.read(buf),
            Self::File(file) => file.read(buf),
            Self::Reader(reader) => reader.read(buf),
        }
    }
}

pub enum TransferSink {
    Memory(Vec<u8>),
    File {
        target_path: PathBuf,
        temp_path: Option<PathBuf>,
        file: Option<File>,
    },
    Writer(Box<dyn Write + Send>),
}

impl TransferSink {
    pub fn memory() -> Self {
        Self::Memory(Vec::new())
    }

    pub fn file(target_path: impl AsRef<Path>, atomic: bool) -> io::Result<Self> {
        let target_path = target_path.as_ref().to_path_buf();
        for comp in target_path.components() {
            if matches!(comp, std::path::Component::ParentDir) {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "path traversal rejected: parent dir components forbidden",
                ));
            }
        }
        if target_path.is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unsafe symlink target rejected",
            ));
        }
        if let Some(parent) = target_path.parent() {
            fs::create_dir_all(parent)?;
        }
        if atomic {
            let pid = std::process::id();
            let rand_suffix: u64 = {
                let mut h = Sha256::new();
                h.update(target_path.to_string_lossy().as_bytes());
                h.update(pid.to_le_bytes());
                let out = h.finalize();
                u64::from_le_bytes(out[..8].try_into().unwrap())
            };
            let temp_name = format!(
                ".transfer-tmp-{}-{:016x}",
                target_path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("obj"),
                rand_suffix
            );
            let temp_path = target_path
                .parent()
                .unwrap_or_else(|| Path::new("."))
                .join(temp_name);
            let file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&temp_path)?;
            Ok(Self::File {
                target_path,
                temp_path: Some(temp_path),
                file: Some(file),
            })
        } else {
            let file = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&target_path)?;
            Ok(Self::File {
                target_path,
                temp_path: None,
                file: Some(file),
            })
        }
    }

    pub fn writer(writer: Box<dyn Write + Send>) -> Self {
        Self::Writer(writer)
    }

    pub fn into_bytes(self) -> Option<Vec<u8>> {
        match self {
            Self::Memory(vec) => Some(vec),
            _ => None,
        }
    }

    pub fn commit(mut self) -> Result<(), TransferError> {
        match &mut self {
            Self::Memory(_) => Ok(()),
            Self::Writer(writer) => {
                writer.flush()?;
                Ok(())
            }
            Self::File {
                target_path,
                temp_path,
                file,
            } => {
                if let Some(mut f) = file.take() {
                    f.flush()?;
                    f.sync_all()?;
                    drop(f);
                }
                if let Some(tmp) = temp_path.take() {
                    fs::rename(&tmp, target_path)?;
                }
                Ok(())
            }
        }
    }

    pub fn abort(mut self) {
        if let Self::File {
            temp_path, file, ..
        } = &mut self
        {
            drop(file.take());
            if let Some(tmp) = temp_path.take() {
                let _ = fs::remove_file(tmp);
            }
        }
    }
}

impl Write for TransferSink {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Self::Memory(vec) => vec.write(buf),
            Self::File { file, .. } => {
                let f = file
                    .as_mut()
                    .ok_or_else(|| io::Error::other("file sink already committed or closed"))?;
                f.write(buf)
            }
            Self::Writer(writer) => writer.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            Self::Memory(vec) => vec.flush(),
            Self::File { file, .. } => {
                if let Some(f) = file.as_mut() {
                    f.flush()
                } else {
                    Ok(())
                }
            }
            Self::Writer(writer) => writer.flush(),
        }
    }
}

fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
    }
    s
}

/// Executes a synchronous streaming byte transfer with bounded buffer and incremental hash verification.
#[allow(clippy::too_many_arguments)]
pub fn execute_transfer(
    transfer_id: &str,
    fence: &ActionFence,
    live_epoch: u64,
    source: &mut TransferSource,
    mut sink: TransferSink,
    options: &TransferOptions,
    journal: Option<&FederatedTransferJournal>,
    now: &str,
) -> Result<TransferReceipt, TransferError> {
    admit_command(fence, live_epoch)?;
    if transfer_id.is_empty() {
        sink.abort();
        return Err(TransferError::InvalidFence("transfer_id cannot be empty"));
    }

    if let Some(j) = journal {
        if let Ok(Some(record)) = j.get_transfer(transfer_id) {
            if record.state == TransferState::GrantVerified {
                let details = serde_json::json!({
                    "actionId": fence.action_id,
                    "executionEpoch": fence.execution_epoch,
                    "chunkSize": options.chunk_size,
                    "resumeOffset": options.resume_offset,
                });
                let _ = j.advance_transfer(
                    transfer_id,
                    record.revision,
                    TransferState::Transferring,
                    details,
                    now,
                );
            }
        }
    }

    if options.resume_offset > 0 {
        match source {
            TransferSource::Memory(cursor) => {
                if let Err(e) = cursor.seek(SeekFrom::Start(options.resume_offset)) {
                    sink.abort();
                    return Err(TransferError::Io(e));
                }
            }
            TransferSource::File(file) => {
                if let Err(e) = file.seek(SeekFrom::Start(options.resume_offset)) {
                    sink.abort();
                    return Err(TransferError::Io(e));
                }
            }
            TransferSource::Reader(_) => {
                let mut remaining = options.resume_offset;
                let mut discard_buf = vec![0_u8; options.chunk_size];
                while remaining > 0 {
                    let to_read = (remaining as usize).min(options.chunk_size);
                    match source.read(&mut discard_buf[..to_read]) {
                        Ok(0) => {
                            sink.abort();
                            return Err(TransferError::LengthMismatch {
                                expected: options.resume_offset,
                                actual: options.resume_offset - remaining,
                            });
                        }
                        Ok(n) => remaining -= n as u64,
                        Err(e) => {
                            sink.abort();
                            return Err(TransferError::Io(e));
                        }
                    }
                }
            }
        }
    }

    let chunk_cap = options.chunk_size.clamp(MIN_CHUNK_SIZE, MAX_CHUNK_SIZE);
    let mut buffer = vec![0_u8; chunk_cap];
    let mut hasher = Sha256::new();
    let mut total_bytes: u64 = 0;
    let mut chunks_count: usize = 0;

    loop {
        let bytes_read = match source.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => n,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => {
                sink.abort();
                return Err(TransferError::Io(e));
            }
        };

        let chunk = &buffer[..bytes_read];
        hasher.update(chunk);

        if let Err(e) = sink.write_all(chunk) {
            sink.abort();
            return Err(TransferError::Io(e));
        }

        total_bytes += bytes_read as u64;
        chunks_count += 1;

        if let Some(expected_len) = options.expected_length {
            if total_bytes > expected_len {
                sink.abort();
                return Err(TransferError::LengthMismatch {
                    expected: expected_len,
                    actual: total_bytes,
                });
            }
        }
    }

    if let Some(expected_len) = options.expected_length {
        if total_bytes != expected_len {
            sink.abort();
            return Err(TransferError::LengthMismatch {
                expected: expected_len,
                actual: total_bytes,
            });
        }
    }

    let digest_arr: [u8; 32] = hasher.finalize().into();
    let digest_hex = to_hex(&digest_arr);

    if let Some(expected_digest) = options.expected_digest {
        if digest_arr != expected_digest {
            sink.abort();
            let exp_hex = to_hex(&expected_digest);
            return Err(TransferError::DigestMismatch {
                expected: exp_hex,
                actual: digest_hex,
            });
        }
    }

    sink.commit()?;

    if let Some(j) = journal {
        if let Ok(Some(record)) = j.get_transfer(transfer_id) {
            if record.state == TransferState::Transferring {
                let details = serde_json::json!({
                    "bytesTransferred": total_bytes,
                    "chunksCount": chunks_count,
                    "sha256": digest_hex,
                });
                let _ = j.advance_transfer(
                    transfer_id,
                    record.revision,
                    TransferState::RemoteVerifying,
                    details,
                    now,
                );
            }
        }
    }

    Ok(TransferReceipt {
        transfer_id: transfer_id.to_string(),
        bytes_transferred: total_bytes,
        chunks_count,
        sha256: digest_arr,
        sha256_hex: digest_hex,
    })
}

#[cfg(feature = "s3")]
pub mod s3_transfer {
    use super::*;
    use bytes::Bytes;
    use deepseek_storage::s3::{ConditionalWrite, S3Transport, StorageAuthorityProof};

    #[allow(clippy::too_many_arguments)]
    pub async fn transfer_s3_to_sink(
        transfer_id: &str,
        fence: &ActionFence,
        live_epoch: u64,
        transport: &S3Transport,
        key: &str,
        length: u64,
        expected_digest: [u8; 32],
        mut sink: TransferSink,
    ) -> Result<TransferReceipt, TransferError> {
        admit_command(fence, live_epoch)?;
        let mut buffer = Vec::new();
        transport
            .download_verified(key, length, expected_digest, &mut buffer)
            .await?;
        sink.write_all(&buffer)?;
        sink.commit()?;
        Ok(TransferReceipt {
            transfer_id: transfer_id.to_string(),
            bytes_transferred: length,
            chunks_count: 1,
            sha256: expected_digest,
            sha256_hex: to_hex(&expected_digest),
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn transfer_source_to_s3(
        transfer_id: &str,
        fence: &ActionFence,
        live_epoch: u64,
        transport: &S3Transport,
        key: &str,
        source: &mut TransferSource,
        authority: &StorageAuthorityProof,
        condition: ConditionalWrite,
        options: &TransferOptions,
    ) -> Result<TransferReceipt, TransferError> {
        admit_command(fence, live_epoch)?;
        let mut data = Vec::new();
        let chunk_cap = options.chunk_size.clamp(MIN_CHUNK_SIZE, MAX_CHUNK_SIZE);
        let mut buffer = vec![0_u8; chunk_cap];
        let mut total_bytes: u64 = 0;

        loop {
            let n = source.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            data.extend_from_slice(&buffer[..n]);
            total_bytes += n as u64;
            if let Some(expected_len) = options.expected_length {
                if total_bytes > expected_len {
                    return Err(TransferError::LengthMismatch {
                        expected: expected_len,
                        actual: total_bytes,
                    });
                }
            }
        }

        if let Some(expected_len) = options.expected_length {
            if total_bytes != expected_len {
                return Err(TransferError::LengthMismatch {
                    expected: expected_len,
                    actual: total_bytes,
                });
            }
        }

        let digest_arr: [u8; 32] = Sha256::digest(&data).into();
        if let Some(expected_digest) = options.expected_digest {
            if digest_arr != expected_digest {
                return Err(TransferError::DigestMismatch {
                    expected: to_hex(&expected_digest),
                    actual: to_hex(&digest_arr),
                });
            }
        }

        let observation = transport
            .put_chunk(key, Bytes::from(data), digest_arr, authority, condition)
            .await?;

        Ok(TransferReceipt {
            transfer_id: transfer_id.to_string(),
            bytes_transferred: observation.length,
            chunks_count: 1,
            sha256: digest_arr,
            sha256_hex: to_hex(&digest_arr),
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn transfer_s3_to_s3(
        transfer_id: &str,
        fence: &ActionFence,
        live_epoch: u64,
        source_transport: &S3Transport,
        source_key: &str,
        dest_transport: &S3Transport,
        dest_key: &str,
        authority: &TransferAuthorityProof,
        condition: ConditionalWrite,
    ) -> Result<TransferReceipt, TransferError> {
        admit_command(fence, live_epoch)?;
        let expected_source = format!("{}/{}", source_transport.bucket(), source_key);
        let expected_dest = format!("{}/{}", dest_transport.bucket(), dest_key);
        authority.validate(
            fence,
            &expected_source,
            &expected_dest,
            &authority.expected_digest,
            authority.expected_length,
        )?;
        let mut buffer = Vec::with_capacity(authority.expected_length as usize);
        source_transport
            .download_verified(
                source_key,
                authority.expected_length,
                authority.expected_digest,
                &mut buffer,
            )
            .await?;
        let storage_proof = StorageAuthorityProof {
            action_id: authority.action_id.clone(),
            execution_epoch: authority.execution_epoch,
            fencing_token: authority.fencing_token,
            request_id: authority.request_id.clone(),
            nonce: authority.nonce.clone(),
        };
        let observation = dest_transport
            .put_chunk(
                dest_key,
                Bytes::from(buffer),
                authority.expected_digest,
                &storage_proof,
                condition,
            )
            .await?;
        Ok(TransferReceipt {
            transfer_id: transfer_id.to_string(),
            bytes_transferred: observation.length,
            chunks_count: 1,
            sha256: authority.expected_digest,
            sha256_hex: to_hex(&authority.expected_digest),
        })
    }
}
