use super::{claim_denied, effect_unknown, wire};
use crate::managed_profile::sync_directory;
use deepseek_protocol::generated::deepseek::action::v1::{
    TargetMarkerClaim, TargetMarkerDispatchPermit, TargetMarkerPreparation,
};
use prost::Message as _;
use serde::Serialize;
use sha2::{Digest as _, Sha256};
use std::{
    fs, io,
    io::{Read as _, Write as _},
    path::{Path, PathBuf},
};
use tonic::Status;

const PREPARATION_LIMIT: usize =
    wire::MARKER_CLAIM_MAX_WIRE + wire::MARKER_INITIALIZATION_MAX_JSON + 256;

#[derive(Debug)]
pub(crate) struct MarkerJournal {
    root: PathBuf,
    legacy_installation: PathBuf,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct GenesisMarker<'a> {
    schema_version: u32,
    target_id: &'a str,
    target_nonce: String,
    incarnation_id: String,
    owner_installation_id: &'a str,
    target_generation: u64,
    latest_commit_hash: String,
    created_at: String,
}

pub(super) fn preparation_digest(prepared: &TargetMarkerPreparation) -> [u8; 32] {
    Sha256::digest(prepared.encode_to_vec()).into()
}

fn entry_name(operation: &str, epoch: u64) -> String {
    let mut hash = Sha256::new();
    hash.update(b"deepseek-infra:backup-target-marker-journal-v1\0");
    hash.update(epoch.to_le_bytes());
    hash.update(operation.as_bytes());
    format!("{:x}", hash.finalize())
}

fn derive(nonce: &[u8], domain: &[u8]) -> String {
    let mut hash = Sha256::new();
    hash.update(b"deepseek-infra:backup-target-genesis-v1\0");
    hash.update(domain);
    hash.update(nonce);
    format!("{:x}", hash.finalize())
}

fn read(path: &Path, limit: usize) -> io::Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file()
        || super::super::reject_links(path).is_err()
        || metadata.len() > limit as u64
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "backup target marker journal entry is invalid",
        ));
    }
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != metadata.len() {
        return Err(io::Error::other(
            "backup target marker journal entry changed",
        ));
    }
    Ok(bytes)
}

fn decode_exact<T: prost::Message + Default>(bytes: &[u8]) -> Result<T, Status> {
    let decoded = T::decode(bytes).map_err(|_| effect_unknown())?;
    if decoded.encode_to_vec() != bytes {
        return Err(effect_unknown());
    }
    Ok(decoded)
}

impl MarkerJournal {
    pub(super) fn new(state_root: &Path, workspace: &Path) -> io::Result<Self> {
        let root = state_root.join("backup-target-markers-v1");
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt as _;
            builder.mode(0o700);
        }
        match builder.create(&root) {
            Ok(()) => sync_directory(state_root)?,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
            Err(error) => return Err(error),
        }
        let metadata = fs::symlink_metadata(&root)?;
        if !metadata.is_dir() || super::super::reject_links(&root).is_err() {
            return Err(io::Error::other(
                "backup target marker journal root is invalid",
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt as _;
            if metadata.mode() & 0o077 != 0 || metadata.uid() != rustix::process::geteuid().as_raw()
            {
                return Err(io::Error::other(
                    "backup target marker journal is not private",
                ));
            }
        }
        Ok(Self {
            root,
            legacy_installation: workspace.join(".backup-targets").join("installation.id"),
        })
    }

    fn publish_exact(&self, path: &Path, bytes: &[u8]) -> io::Result<()> {
        let mut temporary = tempfile::NamedTempFile::new_in(&self.root)?;
        temporary.write_all(bytes)?;
        temporary.as_file().sync_all()?;
        match temporary.persist_noclobber(path) {
            Ok(persisted) => persisted.sync_all()?,
            Err(error) if error.error.kind() == io::ErrorKind::AlreadyExists => {
                if read(path, bytes.len())? != bytes {
                    return Err(io::Error::other(
                        "backup target marker journal entry conflicts",
                    ));
                }
            }
            Err(error) => return Err(error.error),
        }
        sync_directory(&self.root)
    }

    fn installation_id(&self, nonce: &[u8]) -> Result<String, Status> {
        let parse = |bytes: &[u8]| {
            let value = std::str::from_utf8(bytes)
                .map_err(|_| claim_denied())?
                .trim();
            if value.is_empty() || !value.bytes().all(|b| b.is_ascii() && !b.is_ascii_control()) {
                return Err(claim_denied());
            }
            Ok(value.to_owned())
        };
        let file = self.root.join("installation.id");
        let value = match read(&file, 128) {
            Ok(value) => return parse(&value),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                match read(&self.legacy_installation, 128) {
                    Ok(value) => parse(&value)?,
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {
                        format!("inst_{}", &derive(nonce, b"installation\0")[..16])
                    }
                    Err(_) => return Err(claim_denied()),
                }
            }
            Err(_) => return Err(claim_denied()),
        };
        self.publish_exact(&file, value.as_bytes())
            .map_err(|_| claim_denied())?;
        Ok(value)
    }

    pub(super) fn prepare(
        &self,
        claim: &TargetMarkerClaim,
    ) -> Result<TargetMarkerPreparation, Status> {
        let fence = claim.fence.as_ref().ok_or_else(claim_denied)?;
        let installation = self.installation_id(&claim.nonce)?;
        let created = time::OffsetDateTime::from_unix_timestamp(claim.issued_at_unix)
            .map_err(|_| claim_denied())?;
        let genesis = GenesisMarker {
            schema_version: 3,
            target_id: &claim.target_id,
            target_nonce: derive(&claim.nonce, b"nonce\0")[..32].to_owned(),
            incarnation_id: format!("inc_{}", &derive(&claim.nonce, b"incarnation\0")[..16]),
            owner_installation_id: &installation,
            target_generation: 0,
            latest_commit_hash: "0".repeat(64),
            created_at: created
                .format(&time::format_description::well_known::Rfc3339)
                .map_err(|_| claim_denied())?,
        };
        let mut marker = serde_json::to_vec(&genesis).map_err(|_| claim_denied())?;
        marker.push(b'\n');
        if marker.len() > wire::MARKER_INITIALIZATION_MAX_JSON {
            return Err(claim_denied());
        }
        let prepared = TargetMarkerPreparation {
            claim: Some(claim.clone()),
            directory_identity: claim.directory_identity.clone(),
            marker_json: marker,
        };
        let name = entry_name(&claim.operation_id, fence.execution_epoch);
        self.publish_exact(
            &self.root.join(format!("{name}.prepared.pb")),
            &prepared.encode_to_vec(),
        )
        .map_err(|_| claim_denied())?;
        Ok(prepared)
    }

    pub(super) fn accept(
        &self,
        claim: &TargetMarkerClaim,
        permit: &TargetMarkerDispatchPermit,
    ) -> Result<(), Status> {
        let fence = claim.fence.as_ref().ok_or_else(claim_denied)?;
        let name = entry_name(&claim.operation_id, fence.execution_epoch);
        self.publish_exact(
            &self.root.join(format!("{name}.permit.pb")),
            &permit.encode_to_vec(),
        )
        .map_err(|_| effect_unknown())
    }

    pub(super) fn dispatched(
        &self,
        operation: &str,
        epoch: u64,
        digest: &[u8],
    ) -> Result<TargetMarkerPreparation, Status> {
        let name = entry_name(operation, epoch);
        let prepared: TargetMarkerPreparation = decode_exact(
            &read(
                &self.root.join(format!("{name}.prepared.pb")),
                PREPARATION_LIMIT,
            )
            .map_err(|_| effect_unknown())?,
        )?;
        let permit: TargetMarkerDispatchPermit = decode_exact(
            &read(&self.root.join(format!("{name}.permit.pb")), 128)
                .map_err(|_| effect_unknown())?,
        )?;
        let claim = prepared.claim.as_ref().ok_or_else(effect_unknown)?;
        if !wire::valid_marker_claim(claim)
            || claim.operation_id != operation
            || claim.fence.as_ref().map(|fence| fence.execution_epoch) != Some(epoch)
            || Sha256::digest(claim.encode_to_vec()).as_slice() != digest
            || prepared.directory_identity != claim.directory_identity
            || !super::permit_binds(&permit, claim, &preparation_digest(&prepared))
        {
            return Err(effect_unknown());
        }
        Ok(prepared)
    }
}
