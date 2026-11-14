//! Purpose-bound signatures over Rust-read target observations, never grants.
use super::*;
use deepseek_protocol::{
    backup_target_inspection as wire,
    generated::deepseek::action::v1::{
        FilesystemBackupTargetLocationObservation, FilesystemBackupTargetObservation,
    },
};
use prost::Message as _;
use sha2::{Digest as _, Sha256};

impl ControlSigningKey {
    pub fn sign_backup_target_location(
        &self,
        observation: &FilesystemBackupTargetLocationObservation,
    ) -> Result<Vec<u8>, FederationIdentityError> {
        if !wire::valid_location_observation_shape(observation)
            || self.binding["fleetId"].as_str() != Some(observation.fleet_id.as_str())
            || self.binding["environment"].as_str() != Some(observation.environment.as_str())
        {
            return Err(error("BACKUP_TARGET_LOCATION_OBSERVATION_INVALID"));
        }
        let mut message = wire::LOCATION_SIGNATURE_DOMAIN.to_vec();
        message.extend(observation.encode_to_vec());
        Ok(self.key.sign(&message).to_bytes().to_vec())
    }

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
mod location_tests {
    use super::*;
    use deepseek_protocol::{
        ActionFence, generated::deepseek::action::v1::BackupTargetLocationState,
    };
    use ed25519_dalek::{Signature, Verifier};

    #[test]
    fn target_location_signer_binds_state_path_fence_deployment_and_its_own_purpose() {
        let password = b"isolated-location-proof-signature-fixture";
        let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
        let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
        let key = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
        let observation = FilesystemBackupTargetLocationObservation {
            schema_version: 1,
            fence: Some(ActionFence {
                action_id: "target-inspect-location".into(),
                execution_epoch: 3,
            }),
            operation_id: "target-inspect-location".into(),
            writer_fencing_token: 7,
            requested_path: std::env::temp_dir()
                .join("old-registered-location")
                .to_str()
                .unwrap()
                .into(),
            state: BackupTargetLocationState::Exists as i32,
            observed_at_unix: 1794445200,
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            canonical_path: String::new(),
        };
        let signature = key.sign_backup_target_location(&observation).unwrap();
        let parsed = Signature::from_slice(&signature).unwrap();
        let mut message = wire::LOCATION_SIGNATURE_DOMAIN.to_vec();
        message.extend(observation.encode_to_vec());
        key.key.verifying_key().verify(&message, &parsed).unwrap();
        for domain in [
            wire::SIGNATURE_DOMAIN,
            b"deepseek-infra:storage-operation-grant-v1\0".as_slice(),
        ] {
            let mut other = domain.to_vec();
            other.extend(observation.encode_to_vec());
            assert!(key.key.verifying_key().verify(&other, &parsed).is_err());
        }
        let mut missing = observation.clone();
        missing.state = BackupTargetLocationState::Missing as i32;
        assert_ne!(
            key.sign_backup_target_location(&missing).unwrap(),
            signature
        );
        let mut resolved = observation.clone();
        resolved.canonical_path = observation.requested_path.clone();
        resolved.state = BackupTargetLocationState::Unspecified as i32;
        assert_ne!(
            key.sign_backup_target_location(&resolved).unwrap(),
            signature
        );
        let mutations: [fn(&mut FilesystemBackupTargetLocationObservation); 15] = [
            |o| o.schema_version = 2,
            |o| o.fence = None,
            |o| o.fence.as_mut().unwrap().execution_epoch = 0,
            |o| o.fence.as_mut().unwrap().execution_epoch = u64::MAX,
            |o| o.operation_id = "different-operation".into(),
            |o| o.writer_fencing_token = 0,
            |o| o.writer_fencing_token = u64::MAX,
            |o| o.requested_path = "relative".into(),
            |o| o.state = BackupTargetLocationState::Unspecified as i32,
            |o| o.observed_at_unix = 0,
            |o| o.fleet_id = "other-fleet".into(),
            |o| o.environment = "other-environment".into(),
            |o| o.canonical_path = "relative".into(),
            |o| o.canonical_path = format!("{}\0", o.requested_path),
            |o| o.canonical_path = format!("{}/{}", o.requested_path, "x".repeat(32769)),
        ];
        for change in mutations {
            let mut bad = observation.clone();
            change(&mut bad);
            assert!(key.sign_backup_target_location(&bad).is_err());
        }
        let mut mutated_message = wire::LOCATION_SIGNATURE_DOMAIN.to_vec();
        mutated_message.extend(missing.encode_to_vec());
        assert!(
            key.key
                .verifying_key()
                .verify(&mutated_message, &parsed)
                .is_err()
        );
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
            full_containment_checked: false,
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
