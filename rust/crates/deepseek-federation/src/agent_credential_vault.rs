//! Immutable encrypted credential objects. Rust is the only writer; a handle
//! and ciphertext digest are references, not execution or decryption authority.
use crate::canonical::{canonical_bytes, sha256_hex};
use crate::custody::{FederationIdentityError, error};
use crate::{
    AgentCredentialScope, AgentProviderCredentials, ControlSigningKey, SealedAgentCredentials,
};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

const MAX_OBJECT_BYTES: u64 = 65_536;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentCredentialObject {
    pub id: String,
    pub sha256: String,
    pub length: u64,
}

#[derive(Debug)]
pub struct AgentCredentialVault {
    root: PathBuf,
}

fn unavailable() -> FederationIdentityError {
    error("AGENT_PROVIDER_CREDENTIAL_STORE_UNAVAILABLE")
}

fn checked_path(path: &Path) -> Result<(), FederationIdentityError> {
    if !path.is_absolute()
        || path
            .components()
            .any(|part| matches!(part, Component::ParentDir | Component::CurDir))
    {
        return Err(unavailable());
    }
    for parent in path.ancestors() {
        match fs::symlink_metadata(parent) {
            Ok(info) if info.file_type().is_symlink() => return Err(unavailable()),
            Ok(_) => {}
            Err(io_error) if io_error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(unavailable()),
        }
    }
    Ok(())
}

fn object_id(scope: &AgentCredentialScope) -> Result<String, FederationIdentityError> {
    let mut bytes = b"deepseek-infra:agent-credential-object-v1\0".to_vec();
    bytes.extend(canonical_bytes(scope).map_err(|_| unavailable())?);
    Ok(sha256_hex(&bytes))
}

impl AgentCredentialVault {
    pub fn open(state_root: &Path) -> Result<Self, FederationIdentityError> {
        checked_path(state_root)?;
        let root = state_root.join("agent-provider-credentials-v1");
        checked_path(&root)?;
        fs::create_dir_all(&root).map_err(|_| unavailable())?;
        checked_path(&root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .map_err(|_| unavailable())?;
        }
        Ok(Self { root })
    }

    pub fn put(
        &self,
        signer: &ControlSigningKey,
        scope: &AgentCredentialScope,
        credentials: &AgentProviderCredentials,
    ) -> Result<AgentCredentialObject, FederationIdentityError> {
        let sealed = signer.seal_agent_credentials(scope, credentials)?;
        let bytes = serde_json::to_vec(&sealed).map_err(|_| unavailable())?;
        if bytes.len() as u64 > MAX_OBJECT_BYTES {
            return Err(unavailable());
        }
        let id = object_id(scope)?;
        let path = self.root.join(format!("{id}.json"));
        checked_path(&path)?;
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&path) {
            Ok(mut file) => {
                // Preserve partial writes on failure. Replay must not overwrite
                // them or claim custody was complete without verifying the file.
                file.write_all(&bytes).map_err(|_| unavailable())?;
                file.sync_all().map_err(|_| unavailable())?;
                #[cfg(unix)]
                fs::File::open(&self.root)
                    .and_then(|directory| directory.sync_all())
                    .map_err(|_| unavailable())?;
                Ok(AgentCredentialObject {
                    id,
                    sha256: sha256_hex(&bytes),
                    length: bytes.len() as u64,
                })
            }
            Err(io_error) if io_error.kind() == std::io::ErrorKind::AlreadyExists => {
                let bytes = self.read_bytes(&path)?;
                let sealed: SealedAgentCredentials =
                    serde_json::from_slice(&bytes).map_err(|_| unavailable())?;
                let retained = signer.open_agent_credentials(scope, &sealed)?;
                if retained.api_key() != credentials.api_key()
                    || retained.tavily_api_key() != credentials.tavily_api_key()
                {
                    return Err(error("AGENT_PROVIDER_CREDENTIAL_REPLAY_CONFLICT"));
                }
                OpenOptions::new()
                    .read(true)
                    .write(true)
                    .open(&path)
                    .and_then(|file| file.sync_all())
                    .map_err(|_| unavailable())?;
                #[cfg(unix)]
                fs::File::open(&self.root)
                    .and_then(|directory| directory.sync_all())
                    .map_err(|_| unavailable())?;
                Ok(AgentCredentialObject {
                    id,
                    sha256: sha256_hex(&bytes),
                    length: bytes.len() as u64,
                })
            }
            Err(_) => Err(unavailable()),
        }
    }

    fn read_bytes(&self, path: &Path) -> Result<Vec<u8>, FederationIdentityError> {
        checked_path(path)?;
        let metadata = fs::symlink_metadata(path).map_err(|_| unavailable())?;
        if !metadata.is_file() || metadata.len() == 0 || metadata.len() > MAX_OBJECT_BYTES {
            return Err(unavailable());
        }
        let mut bytes = Vec::new();
        fs::File::open(path)
            .map_err(|_| unavailable())?
            .take(MAX_OBJECT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| unavailable())?;
        if bytes.len() as u64 != metadata.len() {
            return Err(unavailable());
        }
        Ok(bytes)
    }

    pub fn get(
        &self,
        signer: &ControlSigningKey,
        scope: &AgentCredentialScope,
        object: &AgentCredentialObject,
    ) -> Result<AgentProviderCredentials, FederationIdentityError> {
        if object.id != object_id(scope)? || !(1..=MAX_OBJECT_BYTES).contains(&object.length) {
            return Err(error("AGENT_PROVIDER_CREDENTIAL_SCOPE_INVALID"));
        }
        let path = self.root.join(format!("{}.json", object.id));
        let bytes = self.read_bytes(&path)?;
        if bytes.len() as u64 != object.length || sha256_hex(&bytes) != object.sha256 {
            return Err(error("AGENT_PROVIDER_CREDENTIAL_INVALID"));
        }
        let sealed = serde_json::from_slice(&bytes).map_err(|_| unavailable())?;
        signer.open_agent_credentials(scope, &sealed)
    }
}
