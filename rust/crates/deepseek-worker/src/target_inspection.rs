//! Read-only filesystem target inspection. Registry writes remain exclusively Go.
use crate::WorkerRpcService;
use deepseek_protocol::{
    backup_target_inspection as wire,
    generated::deepseek::action::v1::{
        BackupTargetLocationState, FilesystemBackupTargetInspectionReceipt,
        FilesystemBackupTargetLocationObservation, FilesystemBackupTargetLocationReceipt,
        FilesystemBackupTargetObservation, InspectFilesystemBackupTargetRequest,
        ProbeFilesystemBackupTargetLocationRequest,
        backup_target_inspection_server::BackupTargetInspection,
    },
};
use sha2::{Digest as _, Sha256};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
};
use tonic::{Request, Response, Status};

const MARKER_NAME: &str = ".deepseek-infra-backup-target.json";
const MAX_MARKER_BYTES: usize = wire::MAX_MARKER_BYTES;

#[path = "target_path_resolution.rs"]
mod path_resolution;

#[derive(Debug)]
pub(crate) struct LocationPolicy {
    protected: Vec<PathBuf>,
    readers: Arc<tokio::sync::Semaphore>,
}
#[derive(Debug)]
struct Inspection {
    path: PathBuf,
    marker: Option<Vec<u8>>,
}

impl LocationPolicy {
    fn new(protected: &[PathBuf]) -> Result<Self, Status> {
        if protected.is_empty() || protected.iter().any(|p| !p.is_absolute()) {
            return Err(Status::failed_precondition(
                "BACKUP_TARGET_LOCATION_POLICY_INVALID",
            ));
        }
        let protected = protected
            .iter()
            .map(|p| {
                fs::canonicalize(p).map_err(|_| {
                    Status::failed_precondition("BACKUP_TARGET_LOCATION_POLICY_INVALID")
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            protected,
            readers: Arc::new(tokio::sync::Semaphore::new(1)),
        })
    }
    fn inspect(&self, raw: &str) -> Result<Inspection, Status> {
        let requested = Path::new(raw);
        if !requested.is_absolute() {
            return Err(Status::invalid_argument(
                "Backup target path must be absolute",
            ));
        }
        if raw.len() > 32768 || !requested.is_dir() {
            return Err(Status::invalid_argument(
                "Backup target directory does not exist",
            ));
        }
        reject_links(requested)?;
        let path = fs::canonicalize(requested).map_err(|_| invalid_marker())?;
        if self.protected.iter().any(|base| contained(&path, base)) {
            return Err(Status::invalid_argument(
                "Unsafe backup target: directory is inside a protected runtime, package or temporary root",
            ));
        }
        let marker_path = path.join(MARKER_NAME);
        let before = match fs::symlink_metadata(&marker_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                reject_links(requested)?;
                if fs::canonicalize(requested).map_err(|_| invalid_marker())? != path {
                    return Err(invalid_marker());
                }
                return Ok(Inspection { path, marker: None });
            }
            Err(_) => return Err(invalid_marker()),
        };
        reject_links(&marker_path)?;
        if !before.is_file() || before.len() > MAX_MARKER_BYTES as u64 {
            return Err(invalid_marker());
        }
        let file = fs::File::open(&marker_path).map_err(|_| invalid_marker())?;
        if !same_file(&before, &file.metadata().map_err(|_| invalid_marker())?) {
            return Err(invalid_marker());
        }
        let mut bytes = Vec::new();
        file.take(MAX_MARKER_BYTES as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| invalid_marker())?;
        if bytes.len() > MAX_MARKER_BYTES || bytes.len() as u64 != before.len() {
            return Err(invalid_marker());
        }
        let document: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| invalid_marker())?;
        if !document
            .as_object()
            .and_then(|m| m.get("targetId"))
            .is_some_and(python_truthy)
        {
            return Err(invalid_marker());
        }
        reject_links(requested)?;
        reject_links(&marker_path)?;
        if fs::canonicalize(requested).map_err(|_| invalid_marker())? != path
            || !same_file(
                &before,
                &fs::metadata(&marker_path).map_err(|_| invalid_marker())?,
            )
        {
            return Err(invalid_marker());
        }
        Ok(Inspection {
            path,
            marker: Some(bytes),
        })
    }
}
fn contained(path: &Path, base: &Path) -> bool {
    #[cfg(not(windows))]
    {
        path.starts_with(base)
    }
    #[cfg(windows)]
    {
        let normalize = |p: &Path| {
            p.to_string_lossy()
                .replace('\\', "/")
                .trim_end_matches('/')
                .to_lowercase()
        };
        let p = normalize(path);
        let b = normalize(base);
        p == b || p.strip_prefix(&b).is_some_and(|tail| tail.starts_with('/'))
    }
}
fn python_truthy(value: &serde_json::Value) -> bool {
    use serde_json::Value;
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|n| n != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn reject_links(path: &Path) -> Result<(), Status> {
    for prefix in path.ancestors() {
        let metadata = fs::symlink_metadata(prefix).map_err(|_| invalid_marker())?;
        let linked = metadata.file_type().is_symlink();
        #[cfg(windows)]
        let linked = {
            use std::os::windows::fs::MetadataExt;
            linked || metadata.file_attributes() & 0x400 != 0
        };
        if linked {
            return Err(Status::invalid_argument(
                "Backup target must not sit behind a symlink or reparse point",
            ));
        }
    }
    Ok(())
}
fn same_file(a: &fs::Metadata, b: &fs::Metadata) -> bool {
    let common =
        a.is_file() == b.is_file() && a.len() == b.len() && a.modified().ok() == b.modified().ok();
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        common && a.dev() == b.dev() && a.ino() == b.ino()
    }
    #[cfg(not(unix))]
    {
        common
    }
}
fn invalid_marker() -> Status {
    Status::invalid_argument("Backup target marker is unreadable or invalid")
}

// Match the retained Path.exists clone check. This reads metadata only: it
// follows an old registered link, but never opens its marker or initializes it.
fn probe_location(path: &str) -> Result<BackupTargetLocationState, Status> {
    if path.len() > 32768 || path.contains('\0') || !Path::new(path).is_absolute() {
        return Err(Status::invalid_argument("BACKUP_TARGET_LOCATION_INVALID"));
    }
    location_state(fs::metadata(path))
}

fn location_state(
    result: std::io::Result<fs::Metadata>,
) -> Result<BackupTargetLocationState, Status> {
    match result {
        Ok(_) => Ok(BackupTargetLocationState::Exists),
        Err(error) if location_missing_error(&error) => Ok(BackupTargetLocationState::Missing),
        Err(_) => Err(Status::unavailable("BACKUP_TARGET_LOCATION_UNRESOLVED")),
    }
}

fn location_missing_error(error: &std::io::Error) -> bool {
    if matches!(
        error.kind(),
        std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
    ) {
        return true;
    }
    // CPython 3.10 pathlib._ignore_error also ignores EBADF and ELOOP, and
    // Windows NOT_READY / INVALID_NAME / CANT_RESOLVE_FILENAME. All other
    // errors remain unresolved; permission or I/O failure is never absence.
    #[cfg(unix)]
    {
        let eloop = if cfg!(target_os = "macos") { 62 } else { 40 };
        error.raw_os_error() == Some(9) || error.raw_os_error() == Some(eloop)
    }
    #[cfg(windows)]
    {
        matches!(error.raw_os_error(), Some(21 | 123 | 1921))
    }
    #[cfg(not(any(unix, windows)))]
    {
        false
    }
}

impl WorkerRpcService {
    fn target_inspection_caller<T>(&self, request: &Request<T>) -> Result<(), Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|e| Status::unauthenticated(e.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied(
                "BACKUP_TARGET_INSPECTION_CALLER_DENIED",
            ));
        }
        Ok(())
    }

    pub fn with_backup_target_inspection(
        mut self,
        workspace: &Path,
        package: &Path,
    ) -> Result<Self, Status> {
        let mut protected = vec![
            fs::canonicalize(workspace).map_err(|_| invalid_marker())?,
            package.to_path_buf(),
            std::env::temp_dir(),
        ];
        // Source builds retain the source-repository exclusion. Packaged builds
        // additionally exclude their actual executable/package directory.
        let source = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .ok_or_else(invalid_marker)?;
        if source.is_dir() {
            protected.push(source.to_path_buf());
        }
        self.target_inspection = Some(Arc::new(LocationPolicy::new(&protected)?));
        Ok(self)
    }
}

#[tonic::async_trait]
impl BackupTargetInspection for WorkerRpcService {
    async fn inspect(
        &self,
        request: Request<InspectFilesystemBackupTargetRequest>,
    ) -> Result<Response<FilesystemBackupTargetInspectionReceipt>, Status> {
        let caller = || self.target_inspection_caller(&request);
        caller()?;
        let input = request.get_ref();
        let fence = input
            .fence
            .as_ref()
            .ok_or_else(|| Status::invalid_argument("FENCE_MISMATCH"))?;
        deepseek_protocol::validate_fence(fence)
            .map_err(|_| Status::invalid_argument("FENCE_MISMATCH"))?;
        if input.schema_version != 1
            || input.operation_id != fence.action_id
            || !input.operation_id.starts_with("target-inspect-")
            || input.operation_id.len() > 128
        {
            return Err(Status::invalid_argument("BACKUP_TARGET_INSPECTION_INVALID"));
        }
        let policy = self
            .target_inspection
            .as_ref()
            .ok_or_else(|| Status::unavailable("BACKUP_TARGET_INSPECTION_UNAVAILABLE"))?
            .clone();
        let config = self.worker.lock().await.agent_node_authority()?;
        if input.writer_fencing_token != config.fencing_token as u64 {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let path = input.path.clone();
        let permit = policy
            .readers
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("BACKUP_TARGET_INSPECTION_BUSY"))?;
        let inspected = tokio::task::spawn_blocking(move || {
            // Cancellation of the RPC must not release the bound while its
            // blocking reader still owns memory and the filesystem handle.
            let _permit = permit;
            policy.inspect(&path)
        })
        .await
        .map_err(|_| invalid_marker())??;
        let marker_present = inspected.marker.is_some();
        let marker_json = inspected.marker.unwrap_or_default();
        let marker_sha256 = if marker_present {
            Sha256::digest(&marker_json).to_vec()
        } else {
            Vec::new()
        };
        let observation = FilesystemBackupTargetObservation {
            schema_version: 1,
            fence: Some(fence.clone()),
            operation_id: input.operation_id.clone(),
            writer_fencing_token: input.writer_fencing_token,
            requested_path: input.path.clone(),
            canonical_path: inspected.path.to_str().ok_or_else(invalid_marker)?.into(),
            marker_present,
            marker_json,
            marker_sha256,
            observed_at_unix: crate::service::unix_now_seconds().map_err(|_| invalid_marker())?,
            fleet_id: config.fleet_id,
            environment: config.environment,
        };
        // Match the established worker -> signer lock order and recheck the
        // expiring transport credential before issuing the read-only witness.
        caller()?;
        let worker = self.worker.lock().await;
        let authority = worker.agent_node_authority()?;
        if authority.fencing_token as u64 != observation.writer_fencing_token {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(|| Status::unavailable("CONTROL_SIGNER_UNAVAILABLE"))?
            .lock()
            .await;
        if signer.key.public_key() != authority.signer_public_key {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let signature = signer
            .key
            .sign_backup_target_observation(&observation)
            .map_err(|_| invalid_marker())?;
        Ok(Response::new(FilesystemBackupTargetInspectionReceipt {
            schema_version: 1,
            observation: Some(observation),
            signature,
            signer_public_key: signer.key.public_key(),
        }))
    }

    async fn probe_location(
        &self,
        request: Request<ProbeFilesystemBackupTargetLocationRequest>,
    ) -> Result<Response<FilesystemBackupTargetLocationReceipt>, Status> {
        self.target_inspection_caller(&request)?;
        let input = request.get_ref();
        let fence = input
            .fence
            .as_ref()
            .ok_or_else(|| Status::invalid_argument("FENCE_MISMATCH"))?;
        deepseek_protocol::validate_fence(fence)
            .map_err(|_| Status::invalid_argument("FENCE_MISMATCH"))?;
        if input.schema_version != 1
            || input.operation_id != fence.action_id
            || !input.operation_id.starts_with("target-inspect-")
            || input.operation_id.len() > 128
            || fence.execution_epoch > i64::MAX as u64
        {
            return Err(Status::invalid_argument("BACKUP_TARGET_LOCATION_INVALID"));
        }
        let policy = self
            .target_inspection
            .as_ref()
            .ok_or_else(|| Status::unavailable("BACKUP_TARGET_INSPECTION_UNAVAILABLE"))?
            .clone();
        let config = self.worker.lock().await.agent_node_authority()?;
        if input.writer_fencing_token != config.fencing_token as u64 {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let path = input.path.clone();
        let permit = policy
            .readers
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("BACKUP_TARGET_INSPECTION_BUSY"))?;
        let resolve_path = input.resolve_path;
        let (state, canonical_path) = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            let (state, canonical) = if resolve_path {
                (
                    BackupTargetLocationState::Unspecified,
                    path_resolution::resolve(&path)?,
                )
            } else {
                (probe_location(&path)?, String::new())
            };
            Ok::<_, Status>((state, canonical))
        })
        .await
        .map_err(|_| Status::unavailable("BACKUP_TARGET_LOCATION_UNRESOLVED"))??;
        let observation = FilesystemBackupTargetLocationObservation {
            schema_version: 1,
            fence: Some(fence.clone()),
            operation_id: input.operation_id.clone(),
            writer_fencing_token: input.writer_fencing_token,
            requested_path: input.path.clone(),
            state: state as i32,
            observed_at_unix: crate::service::unix_now_seconds()
                .map_err(|_| Status::unavailable("BACKUP_TARGET_LOCATION_UNRESOLVED"))?,
            fleet_id: config.fleet_id,
            environment: config.environment,
            canonical_path,
        };
        self.target_inspection_caller(&request)?;
        let worker = self.worker.lock().await;
        let authority = worker.agent_node_authority()?;
        if authority.fencing_token as u64 != observation.writer_fencing_token
            || authority.fleet_id != observation.fleet_id
            || authority.environment != observation.environment
        {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(|| Status::unavailable("CONTROL_SIGNER_UNAVAILABLE"))?
            .lock()
            .await;
        if signer.key.public_key() != authority.signer_public_key {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let signature = signer
            .key
            .sign_backup_target_location(&observation)
            .map_err(|_| Status::invalid_argument("BACKUP_TARGET_LOCATION_INVALID"))?;
        Ok(Response::new(FilesystemBackupTargetLocationReceipt {
            schema_version: 1,
            observation: Some(observation),
            signature,
            signer_public_key: signer.key.public_key(),
        }))
    }
}

#[cfg(test)]
#[path = "target_inspection_tests.rs"]
mod tests;
