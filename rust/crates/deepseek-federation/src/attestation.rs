use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct FailureDomainMetadata {
    pub jurisdiction: String,
    pub provider: String,
    pub region: String,
    pub site_class: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ReplicaAttestation {
    pub backup_id: String,
    pub committed_at: String,
    pub destination_fleet_id: String,
    pub expires_at: String,
    pub failure_domain: FailureDomainMetadata,
    pub fleet_id: String,
    pub object_set_digest: String,
    pub remote_commit_digest: String,
    pub remote_receipt_digest: String,
    pub remote_target_id: String,
    pub schema: String,
    pub sequence: u64,
    pub signature: String,
    pub signature_algorithm: String,
    pub signed_at: String,
    pub signer_certificate: String,
    pub signer_key_id: String,
    pub source_fleet_id: String,
    pub transfer_id: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attestation_matches_wire_format() {
        let att = ReplicaAttestation {
            backup_id: "bk-1".to_string(),
            committed_at: "2026-09-03T00:00:00Z".to_string(),
            destination_fleet_id: "fleet-b".to_string(),
            expires_at: "2026-09-04T00:00:00Z".to_string(),
            failure_domain: FailureDomainMetadata {
                jurisdiction: "us".to_string(),
                provider: "minio".to_string(),
                region: "us-east".to_string(),
                site_class: "region".to_string(),
            },
            fleet_id: "fleet-a".to_string(),
            object_set_digest: "sha256:obj".to_string(),
            remote_commit_digest: "sha256:cmt".to_string(),
            remote_receipt_digest: "sha256:rcp".to_string(),
            remote_target_id: "tgt-2".to_string(),
            schema: "replica-attestation-v1".to_string(),
            sequence: 1,
            signature: "sig-ed25519".to_string(),
            signature_algorithm: "ed25519".to_string(),
            signed_at: "2026-09-03T00:00:00Z".to_string(),
            signer_certificate: "cert-data".to_string(),
            signer_key_id: "key-1".to_string(),
            source_fleet_id: "fleet-a".to_string(),
            transfer_id: "tx-1".to_string(),
        };

        let val = serde_json::to_value(&att).unwrap();
        let map = val.as_object().unwrap();
        assert!(map.contains_key("backupId"));
        assert!(map.contains_key("destinationFleetId"));
        assert!(map.contains_key("failureDomain"));
        assert!(map.contains_key("signerKeyId"));
        assert!(map.contains_key("sourceFleetId"));
        assert!(map.contains_key("transferId"));
    }
}
