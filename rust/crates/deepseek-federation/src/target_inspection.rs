//! Purpose-bound signatures over Rust-read target observations, never grants.
use super::*;
use deepseek_protocol::{
    backup_target_inspection as wire,
    generated::deepseek::action::v1::FilesystemBackupTargetObservation,
};
use prost::Message as _;
use sha2::{Digest as _, Sha256};

impl ControlSigningKey {
    pub fn sign_backup_target_observation(
        &self,
        observation: &FilesystemBackupTargetObservation,
    ) -> Result<Vec<u8>, FederationIdentityError> {
        if !wire::valid_observation_shape(observation)
            || (observation.marker_present
                && observation.marker_sha256 != Sha256::digest(&observation.marker_json).as_slice())
            || self.binding["fleetId"].as_str() != Some(observation.fleet_id.as_str())
            || self.binding["environment"].as_str() != Some(observation.environment.as_str())
        {
            return Err(error("BACKUP_TARGET_OBSERVATION_INVALID"));
        }
        let mut message = wire::SIGNATURE_DOMAIN.to_vec();
        message.extend(observation.encode_to_vec());
        Ok(self.key.sign(&message).to_bytes().to_vec())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_protocol::ActionFence;
    use ed25519_dalek::{Signature, Verifier};

    #[test]
    fn target_inspection_signer_binds_read_shape_digest_fence_and_deployment() {
        let password = b"isolated-target-observation-signature-fixture";
        let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
        let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
        let key = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
        let bytes = br#"{"targetId":"target_original"}"#.to_vec();
        let observation = FilesystemBackupTargetObservation {
            schema_version: 1,
            fence: Some(ActionFence {
                action_id: "target-inspect-one".into(),
                execution_epoch: 4,
            }),
            operation_id: "target-inspect-one".into(),
            writer_fencing_token: 8,
            requested_path: "/outside/target".into(),
            canonical_path: "/outside/target".into(),
            marker_present: true,
            marker_sha256: Sha256::digest(&bytes).to_vec(),
            marker_json: bytes,
            observed_at_unix: 1794445200,
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
        };
        let signature = key.sign_backup_target_observation(&observation).unwrap();
        let mut message = wire::SIGNATURE_DOMAIN.to_vec();
        message.extend(observation.encode_to_vec());
        key.key
            .verifying_key()
            .verify(&message, &Signature::from_slice(&signature).unwrap())
            .unwrap();
        let mut wrong = observation.clone();
        wrong.fleet_id = "fleet-b".into();
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong = observation.clone();
        wrong.environment = "other".into();
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong = observation.clone();
        wrong.marker_sha256[0] ^= 1;
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong = observation.clone();
        wrong.fence.as_mut().unwrap().execution_epoch = 0;
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong = observation.clone();
        wrong.operation_id = "different-operation".into();
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong = observation.clone();
        wrong.schema_version = 2;
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong = observation.clone();
        wrong.marker_present = false;
        assert!(key.sign_backup_target_observation(&wrong).is_err());
        wrong.marker_json.clear();
        wrong.marker_sha256.clear();
        assert!(key.sign_backup_target_observation(&wrong).is_ok());
        // The inspection signature does not verify under an execution domain.
        let mut execution_message = b"deepseek-infra:storage-operation-grant-v1\0".to_vec();
        execution_message.extend(observation.encode_to_vec());
        assert!(
            key.key
                .verifying_key()
                .verify(
                    &execution_message,
                    &Signature::from_slice(&signature).unwrap()
                )
                .is_err()
        );
    }
}
