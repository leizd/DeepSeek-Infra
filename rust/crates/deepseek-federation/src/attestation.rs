use crate::canonical::{
    assert_secret_free, canonical_bytes, is_failure_domain, is_typed_sha256, parse_timestamp,
    sha256_hex, typed_sha256, validate_control_id, validate_fleet_id,
};
use crate::identity::{
    PURPOSE_REPLICA_ATTESTATION, VerifiedSigner, verify_attestation_signature,
    verify_federation_document,
};
use deepseek_storage::{DocumentError, ReceiptError, ReceiptV4, validate_committed_documents};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::fmt;

pub const REPLICA_ATTESTATION_SCHEMA: &str = "federated-replica-attestation-v1";
pub const MAX_REPLICA_ATTESTATION_LIFETIME_SECONDS: i64 = 300;
pub const MAX_REPLICA_ATTESTATION_BYTES: usize = 256 * 1024;
pub const MAX_REMOTE_RECEIPT_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_REMOTE_COMMIT_BYTES: usize = 128 * 1024;

const TRANSFER_IDENTITY_SCHEMA: &str = "federated-transfer-identity-v1";
const TRANSFER_ID_DOMAIN: &[u8] = b"deepseek-infra:federated-transfer-identity-v1\0";
const REPLICA_ATTESTATION_FIELDS: [&str; 19] = [
    "backupId",
    "committedAt",
    "destinationFleetId",
    "expiresAt",
    "failureDomain",
    "fleetId",
    "objectSetDigest",
    "remoteCommitDigest",
    "remoteReceiptDigest",
    "remoteTargetId",
    "schema",
    "sequence",
    "signature",
    "signatureAlgorithm",
    "signedAt",
    "signerCertificate",
    "signerKeyId",
    "sourceFleetId",
    "transferId",
];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct FailureDomainMetadata {
    pub jurisdiction: String,
    pub provider: String,
    pub region: String,
    pub site_class: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReplicaAttestation {
    pub backup_id: String,
    pub committed_at: String,
    pub destination_fleet_id: String,
    pub expires_at: String,
    pub failure_domain: String,
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
    pub signer_certificate: Map<String, Value>,
    pub signer_key_id: String,
    pub source_fleet_id: String,
    pub transfer_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReplicaTransferBinding {
    pub backup_id: String,
    pub destination_fleet_id: String,
    pub object_set_digest: String,
    pub policy_id: String,
    pub source_fleet_id: String,
    pub transfer_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CurrentSignerAuthorization {
    pub active: bool,
    pub certificate_digest: String,
    pub signer_key_id: String,
}

pub struct ReplicaVerificationContext<'a> {
    pub root_identity: &'a Value,
    pub signer_authorization: &'a CurrentSignerAuthorization,
    pub pinned_metadata: &'a FailureDomainMetadata,
    pub transfer: &'a ReplicaTransferBinding,
    pub source_receipt: &'a ReceiptV4,
    pub remote_receipt_bytes: &'a [u8],
    pub remote_commit_bytes: &'a [u8],
    pub now: &'a str,
    pub max_future_skew_seconds: u64,
}

/// Public, read-only inputs required to verify a replica attestation's signed
/// semantics without re-reading the Receipt v4 / Commit v4 documents.
pub struct ReplicaProofVerificationContext<'a> {
    pub root_identity: &'a Value,
    pub pinned_metadata: &'a FailureDomainMetadata,
    pub transfer: &'a ReplicaTransferBinding,
    pub now: &'a str,
    pub max_future_skew_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttestationError {
    code: &'static str,
}

impl AttestationError {
    pub(crate) const fn new(code: &'static str) -> Self {
        Self { code }
    }

    pub const fn code(&self) -> &'static str {
        self.code
    }
}

impl fmt::Display for AttestationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for AttestationError {}

pub fn failure_domain_from_metadata(
    metadata: &FailureDomainMetadata,
) -> Result<String, AttestationError> {
    for value in [
        metadata.jurisdiction.as_str(),
        metadata.provider.as_str(),
        metadata.region.as_str(),
        metadata.site_class.as_str(),
    ] {
        if value.is_empty()
            || value != value.trim()
            || value.chars().count() > 256
            || value
                .chars()
                .any(|character| character < '\u{20}' || character == '\u{7f}')
        {
            return Err(error(
                "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID",
            ));
        }
    }
    Ok(format!(
        "federation-peer-domain:sha256:{}",
        sha256_hex(&canonical_bytes(metadata)?)
    ))
}

pub fn derive_transfer_id(
    source_fleet_id: &str,
    destination_fleet_id: &str,
    backup_id: &str,
    object_set_digest: &str,
) -> Result<String, AttestationError> {
    if !validate_fleet_id(source_fleet_id) || !validate_fleet_id(destination_fleet_id) {
        return Err(error("FEDERATION_TRANSFER_FLEET_ID_INVALID"));
    }
    if source_fleet_id == destination_fleet_id {
        return Err(error("FEDERATION_TRANSFER_REFLECTION_REJECTED"));
    }
    if !validate_control_id(backup_id) {
        return Err(error("FEDERATION_TRANSFER_BACKUP_ID_INVALID"));
    }
    if !is_typed_sha256(object_set_digest) {
        return Err(error("FEDERATION_TRANSFER_OBJECT_SET_DIGEST_INVALID"));
    }
    let identity = serde_json::json!({
        "schema": TRANSFER_IDENTITY_SCHEMA,
        "sourceFleetId": source_fleet_id,
        "destinationFleetId": destination_fleet_id,
        "backupId": backup_id,
        "objectSetDigest": object_set_digest,
    });
    let mut message = TRANSFER_ID_DOMAIN.to_vec();
    message.extend(canonical_bytes(&identity)?);
    Ok(typed_sha256(&message))
}

pub fn attestation_digest(attestation: &ReplicaAttestation) -> Result<String, AttestationError> {
    Ok(typed_sha256(&canonical_bytes(attestation)?))
}

pub fn verify_replica_attestation_document(
    document: &[u8],
    context: &ReplicaVerificationContext<'_>,
) -> Result<ReplicaAttestation, AttestationError> {
    if document.is_empty() {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_INVALID"));
    }
    if document.len() > MAX_REPLICA_ATTESTATION_BYTES {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_TOO_LARGE"));
    }
    let value: Value = serde_json::from_slice(document)
        .map_err(|_| error("FEDERATION_REPLICA_ATTESTATION_INVALID"))?;
    let fields = value
        .as_object()
        .ok_or_else(|| error("FEDERATION_REPLICA_ATTESTATION_INVALID"))?;
    if fields.len() != REPLICA_ATTESTATION_FIELDS.len()
        || !fields
            .keys()
            .all(|field| REPLICA_ATTESTATION_FIELDS.contains(&field.as_str()))
    {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_FIELDS_INVALID"));
    }
    let attestation: ReplicaAttestation = serde_json::from_slice(document)
        .map_err(|_| error("FEDERATION_REPLICA_ATTESTATION_INVALID"))?;
    verify_replica_attestation(&attestation, context)?;
    Ok(attestation)
}

pub fn verify_replica_attestation(
    attestation: &ReplicaAttestation,
    context: &ReplicaVerificationContext<'_>,
) -> Result<(), AttestationError> {
    assert_secret_free(attestation)?;
    if canonical_bytes(attestation)?.len() > MAX_REPLICA_ATTESTATION_BYTES {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_TOO_LARGE"));
    }
    let now = parse_timestamp(context.now)
        .ok_or_else(|| error("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"))?;
    let signer = verify_attestation_signature(
        attestation,
        context.root_identity,
        context.signer_authorization,
        now,
    )?;
    let proof_context = ReplicaProofVerificationContext {
        root_identity: context.root_identity,
        pinned_metadata: context.pinned_metadata,
        transfer: context.transfer,
        now: context.now,
        max_future_skew_seconds: context.max_future_skew_seconds,
    };
    verify_semantics(attestation, &proof_context, &signer, now)?;
    verify_remote_documents(attestation, context)?;
    Ok(())
}

pub fn verify_replica_attestation_for_proof(
    attestation: &ReplicaAttestation,
    context: &ReplicaProofVerificationContext<'_>,
) -> Result<(), AttestationError> {
    assert_secret_free(attestation)?;
    if canonical_bytes(attestation)?.len() > MAX_REPLICA_ATTESTATION_BYTES {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_TOO_LARGE"));
    }
    let now = parse_timestamp(context.now)
        .ok_or_else(|| error("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"))?;
    let document = serde_json::to_value(attestation)
        .map_err(|_| error("FEDERATION_REPLICA_ATTESTATION_CANONICAL_PAYLOAD_INVALID"))?;
    verify_federation_document(
        &document,
        &attestation.signer_certificate,
        context.root_identity,
        REPLICA_ATTESTATION_SCHEMA,
        context.now,
        PURPOSE_REPLICA_ATTESTATION,
    )?;
    let signer = VerifiedSigner {
        signer_key_id: attestation.signer_key_id.clone(),
        not_before: attestation
            .signer_certificate
            .get("notBefore")
            .and_then(Value::as_str)
            .and_then(parse_timestamp)
            .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID"))?,
        expires_at: attestation
            .signer_certificate
            .get("expiresAt")
            .and_then(Value::as_str)
            .and_then(parse_timestamp)
            .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID"))?,
    };
    verify_semantics(attestation, context, &signer, now)
}

pub fn verify_replica_remote_documents(
    attestation: &ReplicaAttestation,
    context: &ReplicaVerificationContext<'_>,
) -> Result<(), AttestationError> {
    verify_remote_documents(attestation, context)
}

fn verify_semantics(
    attestation: &ReplicaAttestation,
    context: &ReplicaProofVerificationContext<'_>,
    signer: &VerifiedSigner,
    now: i64,
) -> Result<(), AttestationError> {
    let transfer = context.transfer;
    if attestation.schema != REPLICA_ATTESTATION_SCHEMA {
        return Err(error("FEDERATION_DOCUMENT_SCHEMA_INVALID"));
    }
    if !validate_control_id(&transfer.policy_id) {
        return Err(error("FEDERATION_TRANSFER_POLICY_ID_INVALID"));
    }
    for fleet_id in [
        attestation.fleet_id.as_str(),
        attestation.source_fleet_id.as_str(),
        attestation.destination_fleet_id.as_str(),
        transfer.source_fleet_id.as_str(),
        transfer.destination_fleet_id.as_str(),
    ] {
        if !validate_fleet_id(fleet_id) {
            return Err(error("FEDERATION_REPLICA_ATTESTATION_FLEET_ID_INVALID"));
        }
    }
    if attestation.source_fleet_id != transfer.source_fleet_id {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_SOURCE_FLEET_MISMATCH",
        ));
    }
    if attestation.destination_fleet_id != transfer.destination_fleet_id
        || attestation.fleet_id != transfer.destination_fleet_id
    {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_DESTINATION_FLEET_MISMATCH",
        ));
    }
    if !is_typed_sha256(&attestation.transfer_id) {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_TRANSFER_ID_INVALID"));
    }
    if attestation.transfer_id != transfer.transfer_id {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_TRANSFER_ID_MISMATCH"));
    }
    if !validate_control_id(&attestation.backup_id) {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_BACKUP_ID_INVALID"));
    }
    if attestation.backup_id != transfer.backup_id {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_BACKUP_ID_MISMATCH"));
    }
    if !is_typed_sha256(&attestation.object_set_digest) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_OBJECT_SET_DIGEST_INVALID",
        ));
    }
    if attestation.object_set_digest != transfer.object_set_digest {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_OBJECT_SET_DIGEST_MISMATCH",
        ));
    }
    let derived_transfer_id = derive_transfer_id(
        &transfer.source_fleet_id,
        &transfer.destination_fleet_id,
        &transfer.backup_id,
        &transfer.object_set_digest,
    )?;
    if transfer.transfer_id != derived_transfer_id {
        return Err(error("FEDERATION_TRANSFER_ID_INVALID"));
    }
    if !validate_control_id(&attestation.remote_target_id) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_INVALID",
        ));
    }
    if !is_typed_sha256(&attestation.remote_receipt_digest) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_INVALID",
        ));
    }
    if !is_typed_sha256(&attestation.remote_commit_digest) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_INVALID",
        ));
    }
    if !is_failure_domain(&attestation.failure_domain) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_INVALID",
        ));
    }
    if attestation.failure_domain != failure_domain_from_metadata(context.pinned_metadata)? {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_MISMATCH",
        ));
    }
    if attestation.sequence == 0 {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_SEQUENCE_INVALID"));
    }
    let signed_at = attestation_timestamp(&attestation.signed_at)?;
    let expires_at = attestation_timestamp(&attestation.expires_at)?;
    let committed_at = attestation_timestamp(&attestation.committed_at)?;
    let lifetime = expires_at - signed_at;
    if !(1..=MAX_REPLICA_ATTESTATION_LIFETIME_SECONDS).contains(&lifetime) {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_LIFETIME_INVALID"));
    }
    if now >= expires_at {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_EXPIRED"));
    }
    let allowed_future_skew = i64::try_from(context.max_future_skew_seconds).unwrap_or(i64::MAX);
    if signed_at.saturating_sub(now) > allowed_future_skew {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_FROM_FUTURE"));
    }
    if committed_at > signed_at {
        return Err(error("FEDERATION_REPLICA_ATTESTATION_COMMIT_TIME_INVALID"));
    }
    if signed_at < signer.not_before || expires_at > signer.expires_at {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID",
        ));
    }
    Ok(())
}

fn verify_remote_documents(
    attestation: &ReplicaAttestation,
    context: &ReplicaVerificationContext<'_>,
) -> Result<(), AttestationError> {
    if context.remote_receipt_bytes.is_empty()
        || context.remote_receipt_bytes.len() > MAX_REMOTE_RECEIPT_BYTES
    {
        return Err(error("FEDERATION_REPLICA_REMOTE_RECEIPT_INVALID"));
    }
    if context.remote_commit_bytes.is_empty()
        || context.remote_commit_bytes.len() > MAX_REMOTE_COMMIT_BYTES
    {
        return Err(error("FEDERATION_REPLICA_REMOTE_COMMIT_INVALID"));
    }
    let (remote_receipt, _remote_commit) =
        validate_committed_documents(context.remote_receipt_bytes, context.remote_commit_bytes)
            .map_err(map_storage_error)?;
    if attestation.remote_receipt_digest != typed_sha256(context.remote_receipt_bytes) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_MISMATCH",
        ));
    }
    if attestation.remote_commit_digest != typed_sha256(context.remote_commit_bytes) {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_MISMATCH",
        ));
    }

    let source = context.source_receipt;
    source
        .validate()
        .map_err(|_| error("FEDERATION_REPLICA_SOURCE_RECEIPT_V4_INVALID"))?;
    if !source.creation_verified
        || source.size == 0
        || source.backup_id != context.transfer.backup_id
        || source.policy_id != context.transfer.policy_id
        || format!("sha256:{}", source.object_set_digest) != context.transfer.object_set_digest
    {
        return Err(error("FEDERATION_REPLICA_SOURCE_RECEIPT_BINDING_INVALID"));
    }
    if remote_receipt.target_id != attestation.remote_target_id {
        return Err(error(
            "FEDERATION_REPLICA_ATTESTATION_REMOTE_TARGET_MISMATCH",
        ));
    }
    let transfer_hex = context
        .transfer
        .transfer_id
        .strip_prefix("sha256:")
        .ok_or_else(|| error("FEDERATION_TRANSFER_ID_INVALID"))?;
    let expected_run_id = format!("fed-replica-{transfer_hex}");
    let expected_schedule_slot = format!("federation/replica/{}", context.transfer.transfer_id);
    if remote_receipt.backup_id != context.transfer.backup_id
        || remote_receipt.policy_id != context.transfer.policy_id
        || remote_receipt.run_id != expected_run_id
        || remote_receipt.schedule_slot != expected_schedule_slot
        || !remote_receipt.creation_verified
        || remote_receipt.pinned
        || remote_receipt.size != source.size
        || remote_receipt.object_set_digest != source.object_set_digest
        || remote_receipt.control_object_digest != source.control_object_digest
        || remote_receipt.objects != source.objects
        || remote_receipt.snapshot_kind != source.snapshot_kind
        || remote_receipt.lineage_id != source.lineage_id
        || remote_receipt.parent_backup_id != source.parent_backup_id
        || remote_receipt.base_backup_id != source.base_backup_id
        || remote_receipt.chain_depth != source.chain_depth
        || remote_receipt.chunk_protocol != source.chunk_protocol
    {
        return Err(error("FEDERATION_REPLICA_REMOTE_RECEIPT_BINDING_INVALID"));
    }
    let remote_created_at = attestation_timestamp(&remote_receipt.created_at)
        .map_err(|_| error("FEDERATION_REPLICA_REMOTE_RECEIPT_TIMESTAMP_INVALID"))?;
    let committed_at = attestation_timestamp(&attestation.committed_at)?;
    if remote_created_at > committed_at {
        return Err(error("FEDERATION_REPLICA_REMOTE_RECEIPT_TIMESTAMP_INVALID"));
    }
    Ok(())
}

fn map_storage_error(storage_error: DocumentError) -> AttestationError {
    match storage_error {
        DocumentError::InvalidReceiptJson => error("FEDERATION_REPLICA_REMOTE_RECEIPT_INVALID"),
        DocumentError::ReceiptEncodingMismatch => {
            error("FEDERATION_REPLICA_REMOTE_RECEIPT_ENCODING_INVALID")
        }
        DocumentError::InvalidCommitJson => error("FEDERATION_REPLICA_REMOTE_COMMIT_INVALID"),
        DocumentError::CommitEncodingMismatch => {
            error("FEDERATION_REPLICA_REMOTE_COMMIT_ENCODING_INVALID")
        }
        DocumentError::ReceiptInvalid(ReceiptError::InvalidCreatedAt) => {
            error("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID")
        }
        DocumentError::ReceiptInvalid(_) => {
            error("FEDERATION_REPLICA_REMOTE_RECEIPT_BINDING_INVALID")
        }
        DocumentError::CommitInvalid(_) | DocumentError::Serialization => {
            error("FEDERATION_REPLICA_REMOTE_COMMIT_BINDING_INVALID")
        }
    }
}

fn attestation_timestamp(value: &str) -> Result<i64, AttestationError> {
    parse_timestamp(value).ok_or_else(|| error("FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"))
}

fn error(code: &'static str) -> AttestationError {
    AttestationError::new(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failure_domain_is_derived_from_exact_pinned_metadata() {
        let metadata = FailureDomainMetadata {
            jurisdiction: "cn".to_string(),
            provider: "minio".to_string(),
            region: "cn-south-1".to_string(),
            site_class: "region".to_string(),
        };
        assert_eq!(
            failure_domain_from_metadata(&metadata).unwrap(),
            "federation-peer-domain:sha256:551cc7d4afd831263e06f9dc4370ba4ffddd81dc2ecac9fa9242969b9bcc4fc1"
        );
        let mut invalid = metadata;
        invalid.region = " cn-south-1".to_string();
        assert_eq!(
            failure_domain_from_metadata(&invalid).unwrap_err().code(),
            "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID"
        );
    }

    #[test]
    fn transfer_identity_is_domain_separated_and_reflection_safe() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let transfer = derive_transfer_id("fleet-a", "fleet-b", "backup-1", &digest).unwrap();
        assert!(is_typed_sha256(&transfer));
        assert_eq!(
            derive_transfer_id("fleet-a", "fleet-a", "backup-1", &digest)
                .unwrap_err()
                .code(),
            "FEDERATION_TRANSFER_REFLECTION_REJECTED"
        );
    }

    #[test]
    fn invalid_remote_receipt_timestamp_preserves_the_frozen_error_code() {
        assert_eq!(
            map_storage_error(DocumentError::ReceiptInvalid(
                ReceiptError::InvalidCreatedAt
            ))
            .code(),
            "FEDERATION_REPLICA_ATTESTATION_TIMESTAMP_INVALID"
        );
    }
}
