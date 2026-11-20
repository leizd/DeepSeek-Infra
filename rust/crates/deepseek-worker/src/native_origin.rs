use crate::{
    WorkerRpcService,
    managed_profile::{publish, read, sync_directory},
};
use deepseek_federation::{
    ControlSigningKey, verify_native_origin, verify_native_origin_publication,
};
use deepseek_protocol::generated::deepseek::action::v1::{
    NativeOriginPublication, NativeOriginReceipt, NativeOriginState, PublishNativeOriginRequest,
    ReadNativeOriginRequest, native_origin_custody_server::NativeOriginCustody as OriginRpc,
};
use prost::Message as _;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
};
use tonic::{Request, Response, Status};

fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "managed native origin is inconsistent",
    )
}
pub(crate) fn decode_origin(bytes: &[u8]) -> io::Result<NativeOriginReceipt> {
    let origin = NativeOriginReceipt::decode(bytes).map_err(|_| invalid())?;
    if origin.encode_to_vec() != bytes {
        return Err(invalid());
    }
    verify_native_origin(&origin).map_err(|_| invalid())?;
    Ok(origin)
}
pub(crate) struct NativeOriginCustody {
    root: PathBuf,
    origin: NativeOriginReceipt,
}
impl NativeOriginCustody {
    pub(crate) fn load(root: &Path, key: &ControlSigningKey) -> io::Result<Option<Self>> {
        let manifest: serde_json::Value =
            serde_json::from_slice(&read(&root.join("custody/manifest.json"), 64 * 1024)?)
                .map_err(|_| invalid())?;
        if manifest["schema"] == "native-managed-rust-custody-v1" {
            // Upgrading an existing custody bundle cannot manufacture provenance.
            if root.join("custody/native-origin.pb").exists() {
                return Err(invalid());
            }
            return Ok(None);
        }
        if manifest["schema"] != "native-managed-rust-custody-v2" {
            return Err(invalid());
        }
        let origin = decode_origin(&read(&root.join("custody/native-origin.pb"), 16 * 1024)?)?;
        if origin
            .evidence
            .as_ref()
            .ok_or_else(invalid)?
            .signer_public_key
            != key.public_key()
        {
            return Err(invalid());
        }
        let custody = Self {
            root: root.into(),
            origin,
        };
        custody.state()?;
        Ok(Some(custody))
    }
    fn state(&self) -> io::Result<NativeOriginState> {
        let path = self.root.join("native-origin-publication.pb");
        let publication = match fs::symlink_metadata(&path) {
            Ok(_) => {
                let bytes = read(&path, 16 * 1024)?;
                let receipt =
                    NativeOriginPublication::decode(bytes.as_slice()).map_err(|_| invalid())?;
                if receipt.encode_to_vec() != bytes {
                    return Err(invalid());
                }
                verify_native_origin_publication(&self.origin, &receipt).map_err(|_| invalid())?;
                Some(receipt)
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => return Err(e),
        };
        Ok(NativeOriginState {
            schema_version: 1,
            origin: Some(self.origin.clone()),
            publication,
        })
    }
    fn publish(&self, key: &ControlSigningKey) -> io::Result<NativeOriginState> {
        let retained = self.state()?;
        if retained.publication.is_some() {
            return Ok(retained);
        }
        let publication = key
            .sign_native_origin_publication(&self.origin)
            .map_err(|_| invalid())?;
        let bytes = publication.encode_to_vec();
        let pending = self.root.join(".native-origin-publication.pending");
        match fs::symlink_metadata(&pending) {
            Ok(info) => {
                if !info.is_file() || info.len() > bytes.len() as u64 {
                    return Err(invalid());
                }
                let prefix = fs::read(&pending)?;
                if !bytes.starts_with(&prefix) {
                    return Err(invalid());
                }
                // A torn local staging write has no remote effect and contains
                // only a prefix of this deterministic original receipt.
                let mut file = fs::OpenOptions::new()
                    .write(true)
                    .truncate(true)
                    .open(&pending)?;
                file.write_all(&bytes)?;
                file.sync_all()?;
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => publish(&pending, &bytes)?,
            Err(e) => return Err(e),
        }
        sync_directory(&self.root)?;
        fs::rename(&pending, self.root.join("native-origin-publication.pb"))?;
        sync_directory(&self.root)?;
        self.state()
    }
}

impl WorkerRpcService {
    async fn native_origin_state(
        &self,
        metadata: &tonic::metadata::MetadataMap,
        input: ReadNativeOriginRequest,
        publish_sha: Option<String>,
    ) -> Result<NativeOriginState, Status> {
        let identity = self
            .authenticator
            .authenticate(metadata)
            .map_err(|e| Status::unauthenticated(e.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("NATIVE_ORIGIN_CALLER_DENIED"));
        }
        let worker = self.worker.lock().await;
        let authority = worker
            .authority
            .as_ref()
            .ok_or_else(|| Status::failed_precondition("NATIVE_ORIGIN_UNAVAILABLE"))?;
        if input.schema_version != 1
            || input.writer_fencing_token > i64::MAX as u64
            || input.writer_fencing_token as i64 != authority.fencing_token
            || input.expected_signer_public_key != authority.signer_public_key
            || authority.fleet_id != format!("native-{}", input.profile_id)
            || authority.environment != "local"
            || !input.fence.as_ref().is_some_and(|f| {
                f.action_id == format!("native-origin:{}", input.profile_id)
                    && f.execution_epoch == 1
            })
        {
            return Err(Status::failed_precondition(
                "NATIVE_ORIGIN_BINDING_MISMATCH",
            ));
        }
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(|| Status::unavailable("NATIVE_ORIGIN_UNAVAILABLE"))?
            .lock()
            .await;
        let Some(custody) = &signer.native_origin else {
            if publish_sha.is_some() {
                return Err(Status::failed_precondition("NATIVE_ORIGIN_UNAVAILABLE"));
            }
            return Ok(NativeOriginState {
                schema_version: 1,
                origin: None,
                publication: None,
            });
        };
        if let Some(sha) = publish_sha {
            if sha != format!("{:x}", Sha256::digest(custody.origin.encode_to_vec())) {
                return Err(Status::failed_precondition(
                    "NATIVE_ORIGIN_BINDING_MISMATCH",
                ));
            }
            custody
                .publish(&signer.key)
                .map_err(|_| Status::data_loss("NATIVE_ORIGIN_CUSTODY_INCONSISTENT"))
        } else {
            custody
                .state()
                .map_err(|_| Status::data_loss("NATIVE_ORIGIN_CUSTODY_INCONSISTENT"))
        }
    }
}
#[tonic::async_trait]
impl OriginRpc for WorkerRpcService {
    async fn read(
        &self,
        request: Request<ReadNativeOriginRequest>,
    ) -> Result<Response<NativeOriginState>, Status> {
        let (metadata, _, input) = request.into_parts();
        Ok(Response::new(
            self.native_origin_state(&metadata, input, None).await?,
        ))
    }
    async fn publish(
        &self,
        request: Request<PublishNativeOriginRequest>,
    ) -> Result<Response<NativeOriginState>, Status> {
        let (metadata, _, input) = request.into_parts();
        let context = ReadNativeOriginRequest {
            schema_version: input.schema_version,
            fence: input.fence,
            profile_id: input.profile_id,
            expected_signer_public_key: input.expected_signer_public_key,
            writer_fencing_token: input.writer_fencing_token,
        };
        Ok(Response::new(
            self.native_origin_state(&metadata, context, Some(input.origin_sha256))
                .await?,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(root: &Path) -> (ControlSigningKey, NativeOriginCustody) {
        let profile = "0123456789abcdef0123456789abcdef";
        let password = b"isolated-native-origin-publication-passphrase";
        let fleet = format!("native-{profile}");
        let bundle =
            deepseek_federation::create_control_signer_bundle(password, &fleet, "local").unwrap();
        let key = deepseek_federation::load_control_signer(
            &bundle,
            password,
            &fleet,
            "local",
            bundle["binding"]["signerPublicKey"].as_str().unwrap(),
        )
        .unwrap();
        let origin = key
            .create_native_origin(profile, &"a".repeat(64), "2026-10-21T01:00:00Z")
            .unwrap();
        (
            key,
            NativeOriginCustody {
                root: root.into(),
                origin,
            },
        )
    }
    #[test]
    fn native_publication_retains_original_receipt_after_response_loss_and_restart() {
        let root = tempfile::tempdir().unwrap();
        let (key, custody) = fixture(root.path());
        assert!(custody.state().unwrap().publication.is_none());
        assert!(fs::read_dir(root.path()).unwrap().next().is_none()); // Read is pure.
        let committed = custody.publish(&key).unwrap();
        let original = read(&root.path().join("native-origin-publication.pb"), 16384).unwrap();
        let restarted = NativeOriginCustody {
            root: root.path().into(),
            origin: custody.origin.clone(),
        };
        assert_eq!(restarted.state().unwrap(), committed);
        assert_eq!(restarted.publish(&key).unwrap(), committed);
        assert_eq!(
            read(&root.path().join("native-origin-publication.pb"), 16384).unwrap(),
            original
        );
        assert_eq!(
            committed
                .origin
                .unwrap()
                .evidence
                .unwrap()
                .fence
                .unwrap()
                .execution_epoch,
            1
        );
    }
    #[test]
    fn native_publication_recovers_only_a_matching_torn_staging_write() {
        let root = tempfile::tempdir().unwrap();
        let (key, custody) = fixture(root.path());
        let expected = key
            .sign_native_origin_publication(&custody.origin)
            .unwrap()
            .encode_to_vec();
        fs::write(
            root.path().join(".native-origin-publication.pending"),
            &expected[..expected.len() / 2],
        )
        .unwrap();
        assert!(custody.state().unwrap().publication.is_none());
        custody.publish(&key).unwrap();
        assert_eq!(
            read(&root.path().join("native-origin-publication.pb"), 16384).unwrap(),
            expected
        );
        let other = tempfile::tempdir().unwrap();
        let (key, custody) = fixture(other.path());
        fs::write(
            other.path().join(".native-origin-publication.pending"),
            b"foreign staging bytes",
        )
        .unwrap();
        assert!(custody.publish(&key).is_err());
        assert!(!other.path().join("native-origin-publication.pb").exists());
    }
    #[test]
    fn native_publication_corruption_never_causes_reissuance() {
        let root = tempfile::tempdir().unwrap();
        let (key, custody) = fixture(root.path());
        custody.publish(&key).unwrap();
        let path = root.path().join("native-origin-publication.pb");
        let mut original = fs::read(&path).unwrap();
        original[0] ^= 1;
        fs::write(&path, &original).unwrap();
        assert!(custody.state().is_err());
        assert!(custody.publish(&key).is_err());
        assert_eq!(fs::read(path).unwrap(), original);
    }
}
