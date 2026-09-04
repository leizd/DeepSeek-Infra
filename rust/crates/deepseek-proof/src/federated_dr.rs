use crate::federated_replica::{
    contains_secret, dedupe, digest, exact_fields, exact_integer, is_typed_digest, object_copy,
    parse_timestamp, positive_integer, string, typed_digest, validate_attestation,
    validate_peer_record, validate_transfer_record,
};
use deepseek_federation::{
    PURPOSE_DR_ATTESTATION, derive_transfer_id, validate_fleet_identity, verify_federation_document,
};
use serde_json::{Map, Value};
use sha2::{Digest as _, Sha256};
use std::collections::HashMap;

pub const FEDERATED_DR_PROOF_SCHEMA: &str = "federated-dr-proof-v1";
pub const FEDERATED_DR_PROOF_CHECKS: [&str; 10] = [
    "coldCustodyCannotClaimRecoveryReady",
    "recoveryCapablePeerRequiresPreprovisionedAgeIdentity",
    "agePrivateIdentityNeverCrossesFederationBoundary",
    "federatedDrDrillUsesProductionRestore",
    "federatedDrProofBindsTransferId",
    "federatedDrProofBindsBackupId",
    "federatedDrProofBindsObjectSetDigest",
    "federatedDrProofBindsRemoteReceiptAndCommit",
    "federatedDrProofRequiresCleanupSuccess",
    "federatedDrProofIsSemanticallyValidated",
];

const PROOF_FIELDS: [&str; 13] = [
    "schema",
    "validatedAt",
    "sourceFleetIdentity",
    "destinationFleetIdentity",
    "peerTrustRecord",
    "senderTransfer",
    "acceptedReplicaRecord",
    "drAttestation",
    "acceptedDrRecord",
    "recoveryCapability",
    "productionRestoreResult",
    "failureObservations",
    "proofDigest",
];
const ACCEPTED_REPLICA_RECORD_FIELDS: [&str; 9] = [
    "schema",
    "peerFleetId",
    "transferId",
    "sequence",
    "signerKeyId",
    "attestationDigest",
    "attestation",
    "acceptedAt",
    "revision",
];
const DR_ATTESTATION_FIELDS: [&str; 26] = [
    "schema",
    "fleetId",
    "transferId",
    "sourceFleetId",
    "destinationFleetId",
    "backupId",
    "objectSetDigest",
    "remoteTargetId",
    "remoteReceiptDigest",
    "remoteCommitDigest",
    "replicaAttestationDigest",
    "restoreId",
    "restorePath",
    "workspaceDigest",
    "sourceRevision",
    "startedAt",
    "completedAt",
    "rtoMs",
    "cleanupCompleted",
    "sequence",
    "signerCertificate",
    "signedAt",
    "expiresAt",
    "signerKeyId",
    "signatureAlgorithm",
    "signature",
];
const DR_RECORD_FIELDS: [&str; 10] = [
    "schema",
    "peerFleetId",
    "restoreId",
    "transferId",
    "sequence",
    "signerKeyId",
    "attestationDigest",
    "attestation",
    "acceptedAt",
    "revision",
];
const CAPABILITY_FIELDS: [&str; 12] = [
    "schema",
    "localFleetId",
    "peerFleetId",
    "peerRootFingerprint",
    "mode",
    "recoveryIdentityPreprovisioned",
    "ageRecipient",
    "ageRecipientDigest",
    "configuredBy",
    "configuredAt",
    "updatedAt",
    "revision",
];
const PRODUCTION_RESULT_FIELDS: [&str; 14] = [
    "schemaVersion",
    "restoreId",
    "result",
    "startedAt",
    "completedAt",
    "durationMs",
    "workspaceDigest",
    "sourceRevision",
    "cleanupCompleted",
    "chainLength",
    "components",
    "ciphertextBytes",
    "logicalBytes",
    "verifiedContributors",
];
const FAILURE_FIELDS: [&str; 7] = [
    "claim",
    "code",
    "preState",
    "preStateDigest",
    "postState",
    "postStateDigest",
    "input",
];
const FAILURE_STATE_FIELDS: [&str; 3] = ["senderTransfer", "acceptedReplica", "acceptedDrDrill"];
const FAILURE_CASES: [(&str, &str); 3] = [
    (
        "coldCustodyCannotClaimRecoveryReady",
        "FEDERATION_PEER_COLD_CUSTODY_ONLY",
    ),
    (
        "recoveryCapablePeerRequiresPreprovisionedAgeIdentity",
        "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED",
    ),
    (
        "federatedDrProofRequiresCleanupSuccess",
        "FEDERATED_DR_CLEANUP_INCOMPLETE",
    ),
];

const REPLICA_RECORD_SCHEMA: &str = "federated-replica-attestation-record-v1";
const DR_RECORD_SCHEMA: &str = "federated-dr-drill-attestation-record-v1";
const DR_ATTESTATION_SCHEMA: &str = "federated-dr-drill-attestation-v1";
const CAPABILITY_SCHEMA: &str = "federation-custody-capability-v1";
const PRODUCTION_RESTORE_PATH: &str = "backup-recovery-drill-production-v1";
const COLD_CUSTODY: &str = "COLD_CUSTODY";
const RECOVERY_CAPABLE: &str = "RECOVERY_CAPABLE";
const MAX_DR_ATTESTATION_LIFETIME_SECONDS: i64 = 300;
const MAX_DR_RTO_MS: i128 = 7 * 24 * 60 * 60 * 1_000;

pub fn federated_dr_proof_digest(proof: &Value) -> Result<String, String> {
    let fields = proof
        .as_object()
        .ok_or_else(|| "federated-dr-proof-must-be-object".to_string())?;
    let mut payload = fields.clone();
    payload.remove("proofDigest");
    digest(&Value::Object(payload))
}

pub fn validate_federated_dr_proof(value: &Value) -> Vec<String> {
    let Some(proof) = value.as_object() else {
        return vec!["federated-dr-proof-must-be-object".to_string()];
    };
    let mut errors = Vec::new();
    if !exact_fields(proof, &PROOF_FIELDS) {
        errors.push("federated-dr-proof-fields-invalid".to_string());
    }
    if string(proof, "schema") != FEDERATED_DR_PROOF_SCHEMA {
        errors.push("federated-dr-proof-schema-invalid".to_string());
    }
    if contains_secret(value) {
        errors.push("federated-dr-proof-contains-secret".to_string());
    }

    let validated_at_text = string(proof, "validatedAt").to_string();
    let validated_at = parse_timestamp(&validated_at_text);
    if validated_at.is_none() {
        errors.push("invalid-timestamp:validatedAt".to_string());
    }

    let (source, destination) = validate_identities(
        proof.get("sourceFleetIdentity"),
        proof.get("destinationFleetIdentity"),
        &mut errors,
    );
    let (_peer, pinned_metadata) =
        validate_peer_record(proof.get("peerTrustRecord"), &destination, &mut errors);

    let transfer = object_copy(proof.get("senderTransfer"), "sender-transfer", &mut errors);
    validate_sender_transfer(&transfer, &source, &destination, &mut errors);

    let replica_record = object_copy(
        proof.get("acceptedReplicaRecord"),
        "accepted-replica-record",
        &mut errors,
    );
    let mut replica_attestation = Map::new();
    if validated_at.is_some()
        && !destination.is_empty()
        && !transfer.is_empty()
        && !pinned_metadata.is_empty()
        && !replica_record.is_empty()
    {
        replica_attestation = validate_replica_record(
            &replica_record,
            &destination,
            &transfer,
            &pinned_metadata,
            &validated_at_text,
            validated_at.unwrap_or_default(),
            &mut errors,
        );
    }

    let dr_attestation = object_copy(proof.get("drAttestation"), "dr-attestation", &mut errors);
    let mut verified_dr = dr_attestation.clone();
    if validated_at.is_some()
        && !destination.is_empty()
        && !transfer.is_empty()
        && !replica_record.is_empty()
        && !dr_attestation.is_empty()
    {
        verified_dr = validate_dr_attestation(
            &dr_attestation,
            &destination,
            &transfer,
            &replica_record,
            &validated_at_text,
            validated_at.unwrap_or_default(),
            &mut errors,
        );
    }

    let dr_record = object_copy(
        proof.get("acceptedDrRecord"),
        "accepted-dr-record",
        &mut errors,
    );
    if !dr_record.is_empty() && !verified_dr.is_empty() {
        if let Some(validated_at) = validated_at {
            validate_dr_record(&dr_record, &verified_dr, validated_at, &mut errors);
        }
    }

    let capability = object_copy(
        proof.get("recoveryCapability"),
        "recovery-capability",
        &mut errors,
    );
    if !capability.is_empty() && !source.is_empty() && !destination.is_empty() {
        if let Some(validated_at) = validated_at {
            validate_capability(
                &capability,
                &source,
                &destination,
                validated_at,
                &mut errors,
            );
        }
    }

    let production_result = object_copy(
        proof.get("productionRestoreResult"),
        "production-restore-result",
        &mut errors,
    );
    if !production_result.is_empty() && !replica_attestation.is_empty() && !verified_dr.is_empty() {
        validate_production_result(
            &production_result,
            &replica_attestation,
            &verified_dr,
            &mut errors,
        );
    }

    if !source.is_empty()
        && !destination.is_empty()
        && !transfer.is_empty()
        && !replica_record.is_empty()
        && !dr_record.is_empty()
        && !replica_attestation.is_empty()
        && !verified_dr.is_empty()
    {
        let expected_state = Value::Object(Map::from_iter([
            (
                "senderTransfer".to_string(),
                Value::Object(transfer.clone()),
            ),
            (
                "acceptedReplica".to_string(),
                Value::Object(replica_record.clone()),
            ),
            (
                "acceptedDrDrill".to_string(),
                Value::Object(dr_record.clone()),
            ),
        ]));
        validate_failures(
            proof.get("failureObservations"),
            &source,
            &destination,
            &expected_state,
            &replica_attestation,
            &verified_dr,
            &mut errors,
        );
    }

    let declared = typed_digest(proof.get("proofDigest"), "proofDigest", &mut errors);
    if !declared.is_empty()
        && federated_dr_proof_digest(value).is_ok_and(|computed| computed != declared)
    {
        errors.push("proof-digest-mismatch".to_string());
    }
    dedupe(errors)
}

fn validate_identities(
    source_value: Option<&Value>,
    destination_value: Option<&Value>,
    errors: &mut Vec<String>,
) -> (Map<String, Value>, Map<String, Value>) {
    let null = Value::Null;
    let source_value = source_value.unwrap_or(&null);
    let destination_value = destination_value.unwrap_or(&null);
    let source = match validate_fleet_identity(source_value) {
        Ok(()) => source_value.as_object().cloned().unwrap_or_default(),
        Err(error) => {
            errors.push(format!("source-identity-invalid:{}", error.code()));
            Map::new()
        }
    };
    let destination = match validate_fleet_identity(destination_value) {
        Ok(()) => destination_value.as_object().cloned().unwrap_or_default(),
        Err(error) => {
            errors.push(format!("destination-identity-invalid:{}", error.code()));
            Map::new()
        }
    };
    if !source.is_empty()
        && !destination.is_empty()
        && (source.get("fleetId") == destination.get("fleetId")
            || source.get("rootFingerprint") == destination.get("rootFingerprint"))
    {
        errors.push("fleet-identities-not-distinct".to_string());
    }
    (source, destination)
}

fn validate_sender_transfer(
    transfer: &Map<String, Value>,
    source: &Map<String, Value>,
    destination: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    validate_transfer_record(transfer, "SENDER", errors);
    let state_is_committed = matches!(
        string(transfer, "state"),
        "REMOTE_COMMITTED" | "LOCAL_RECORDED" | "SUCCEEDED"
    );
    if !state_is_committed
        || transfer.get("sourceFleetId") != source.get("fleetId")
        || transfer.get("destinationFleetId") != destination.get("fleetId")
        || transfer.get("localFleetId") != source.get("fleetId")
    {
        errors.push("sender-transfer-binding-invalid".to_string());
    }
}

#[allow(clippy::too_many_arguments)]
fn validate_replica_record(
    record: &Map<String, Value>,
    identity: &Map<String, Value>,
    transfer: &Map<String, Value>,
    pinned_metadata: &Map<String, Value>,
    validated_at_text: &str,
    validated_at: i64,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    let attestation = object_copy(
        record.get("attestation"),
        "accepted-replica-attestation",
        errors,
    );
    if !exact_fields(record, &ACCEPTED_REPLICA_RECORD_FIELDS) {
        errors.push("accepted-replica-record-fields-invalid".to_string());
    }
    if !attestation.is_empty() {
        validate_attestation(
            &attestation,
            identity,
            transfer,
            pinned_metadata,
            validated_at_text,
            errors,
        );
    }
    let attestation_digest = digest(&Value::Object(attestation.clone())).unwrap_or_default();
    if string(record, "schema") != REPLICA_RECORD_SCHEMA
        || record.get("peerFleetId") != transfer.get("destinationFleetId")
        || record.get("transferId") != transfer.get("transferId")
        || record.get("sequence") != attestation.get("sequence")
        || record.get("signerKeyId") != attestation.get("signerKeyId")
        || string(record, "attestationDigest") != attestation_digest
        || record.get("attestation") != Some(&Value::Object(attestation.clone()))
    {
        errors.push("accepted-replica-record-binding-invalid".to_string());
    }
    let accepted_at = parse_timestamp(string(record, "acceptedAt"));
    if accepted_at.is_none() {
        errors.push("invalid-timestamp:acceptedReplicaRecord.acceptedAt".to_string());
    } else if accepted_at.is_some_and(|accepted_at| accepted_at > validated_at) {
        errors.push("accepted-replica-record-from-future".to_string());
    }
    if !positive_integer(record.get("revision")) {
        errors.push("accepted-replica-record-revision-invalid".to_string());
    }
    attestation
}

#[allow(clippy::too_many_arguments)]
fn validate_dr_attestation(
    attestation: &Map<String, Value>,
    identity: &Map<String, Value>,
    transfer: &Map<String, Value>,
    replica_record: &Map<String, Value>,
    validated_at_text: &str,
    validated_at: i64,
    errors: &mut Vec<String>,
) -> Map<String, Value> {
    let certificate = object_copy(
        attestation.get("signerCertificate"),
        "dr-attestation-certificate",
        errors,
    );
    if !exact_fields(attestation, &DR_ATTESTATION_FIELDS) {
        errors.push("dr-attestation-fields-invalid".to_string());
    }
    if certificate.is_empty() {
        return attestation.clone();
    }
    let document = Value::Object(attestation.clone());
    let root = Value::Object(identity.clone());
    let verified = match verify_federation_document(
        &document,
        &certificate,
        &root,
        DR_ATTESTATION_SCHEMA,
        validated_at_text,
        PURPOSE_DR_ATTESTATION,
    ) {
        Ok(verified) => verified,
        Err(error) => {
            errors.push("dr-attestation-signature-invalid".to_string());
            errors.push(format!("dr-attestation-signature-invalid:{}", error.code()));
            return attestation.clone();
        }
    };
    let verified = verified
        .as_object()
        .cloned()
        .unwrap_or_else(|| attestation.clone());
    if let Err(code) =
        dr_attestation_semantics(&verified, transfer, replica_record, validated_at, 30)
    {
        errors.push(format!("dr-attestation-semantics-invalid:{code}"));
    }
    verified
}

fn dr_attestation_semantics(
    attestation: &Map<String, Value>,
    transfer: &Map<String, Value>,
    replica_record: &Map<String, Value>,
    now: i64,
    max_future_skew_seconds: i64,
) -> Result<(), &'static str> {
    let source = string(attestation, "sourceFleetId");
    let destination = string(attestation, "destinationFleetId");
    if !is_fleet_id(source) || !is_fleet_id(destination) {
        return Err("FEDERATED_DR_FLEET_ID_INVALID");
    }
    if transfer.get("sourceFleetId") != attestation.get("sourceFleetId") {
        return Err("FEDERATED_DR_SOURCE_FLEET_MISMATCH");
    }
    if transfer.get("destinationFleetId") != attestation.get("destinationFleetId")
        || attestation.get("fleetId") != attestation.get("destinationFleetId")
    {
        return Err("FEDERATED_DR_DESTINATION_FLEET_MISMATCH");
    }
    let transfer_id = string(attestation, "transferId");
    if !is_typed_digest(transfer_id) {
        return Err("FEDERATED_DR_TRANSFER_ID_INVALID");
    }
    if transfer.get("transferId") != attestation.get("transferId") {
        return Err("FEDERATED_DR_TRANSFER_ID_MISMATCH");
    }
    let backup_id = string(attestation, "backupId");
    if !is_control_id(backup_id) {
        return Err("FEDERATED_DR_BACKUP_ID_INVALID");
    }
    if transfer.get("backupId") != attestation.get("backupId") {
        return Err("FEDERATED_DR_BACKUP_ID_MISMATCH");
    }
    let object_set_digest = string(attestation, "objectSetDigest");
    if !is_typed_digest(object_set_digest) {
        return Err("FEDERATED_DR_OBJECT_SET_DIGEST_INVALID");
    }
    if transfer.get("objectSetDigest") != attestation.get("objectSetDigest") {
        return Err("FEDERATED_DR_OBJECT_SET_DIGEST_MISMATCH");
    }
    if derive_transfer_id(source, destination, backup_id, object_set_digest)
        .map_or(true, |derived| derived != transfer_id)
    {
        return Err("FEDERATION_TRANSFER_ID_INVALID");
    }

    let replica = replica_record
        .get("attestation")
        .and_then(Value::as_object)
        .ok_or("FEDERATED_DR_REPLICA_ATTESTATION_NOT_ACCEPTED")?;
    let observed_replica_digest = digest(&Value::Object(replica.clone()))
        .map_err(|_| "FEDERATED_DR_REPLICA_ATTESTATION_RECORD_INVALID")?;
    if replica_record.get("peerFleetId") != attestation.get("destinationFleetId")
        || replica_record.get("transferId") != attestation.get("transferId")
        || string(replica_record, "attestationDigest") != observed_replica_digest
        || replica.get("fleetId") != attestation.get("destinationFleetId")
        || replica.get("sourceFleetId") != attestation.get("sourceFleetId")
        || replica.get("destinationFleetId") != attestation.get("destinationFleetId")
        || replica.get("transferId") != attestation.get("transferId")
        || replica.get("backupId") != attestation.get("backupId")
        || replica.get("objectSetDigest") != attestation.get("objectSetDigest")
    {
        return Err("FEDERATED_DR_REPLICA_ATTESTATION_RECORD_INVALID");
    }
    if attestation.get("replicaAttestationDigest") != replica_record.get("attestationDigest") {
        return Err("FEDERATED_DR_REPLICA_ATTESTATION_MISMATCH");
    }
    if attestation.get("remoteTargetId") != replica.get("remoteTargetId") {
        return Err("FEDERATED_DR_REMOTE_TARGET_MISMATCH");
    }
    if attestation.get("remoteReceiptDigest") != replica.get("remoteReceiptDigest") {
        return Err("FEDERATED_DR_REMOTE_RECEIPT_MISMATCH");
    }
    if attestation.get("remoteCommitDigest") != replica.get("remoteCommitDigest") {
        return Err("FEDERATED_DR_REMOTE_COMMIT_MISMATCH");
    }
    for (field, code) in [
        (
            "remoteReceiptDigest",
            "FEDERATED_DR_REMOTE_RECEIPT_DIGEST_INVALID",
        ),
        (
            "remoteCommitDigest",
            "FEDERATED_DR_REMOTE_COMMIT_DIGEST_INVALID",
        ),
        (
            "replicaAttestationDigest",
            "FEDERATED_DR_REPLICA_ATTESTATION_DIGEST_INVALID",
        ),
    ] {
        if !is_typed_digest(string(attestation, field)) {
            return Err(code);
        }
    }
    if !is_control_id(string(attestation, "remoteTargetId")) {
        return Err("FEDERATED_DR_REMOTE_TARGET_INVALID");
    }
    if !is_restore_id(string(attestation, "restoreId")) {
        return Err("FEDERATED_DR_RESTORE_ID_INVALID");
    }
    if string(attestation, "restorePath") != PRODUCTION_RESTORE_PATH {
        return Err("FEDERATED_DR_RESTORE_PATH_INVALID");
    }
    if !is_typed_digest(string(attestation, "workspaceDigest")) {
        return Err("FEDERATED_DR_WORKSPACE_DIGEST_INVALID");
    }
    if !is_source_revision(string(attestation, "sourceRevision")) {
        return Err("FEDERATED_DR_SOURCE_REVISION_INVALID");
    }
    if attestation.get("cleanupCompleted") != Some(&Value::Bool(true)) {
        return Err("FEDERATED_DR_CLEANUP_INCOMPLETE");
    }
    let rto_ms = valid_rto(attestation.get("rtoMs"))?;
    let started_at = dr_timestamp(attestation, "startedAt")?;
    let completed_at = dr_timestamp(attestation, "completedAt")?;
    let signed_at = dr_timestamp(attestation, "signedAt")?;
    let expires_at = dr_timestamp(attestation, "expiresAt")?;
    let committed_at = dr_timestamp(replica, "committedAt")?;
    let lifetime = expires_at - signed_at;
    if !(1..=MAX_DR_ATTESTATION_LIFETIME_SECONDS).contains(&lifetime) {
        return Err("FEDERATED_DR_ATTESTATION_LIFETIME_INVALID");
    }
    if now >= expires_at {
        return Err("FEDERATED_DR_ATTESTATION_EXPIRED");
    }
    if signed_at.saturating_sub(now) > max_future_skew_seconds {
        return Err("FEDERATED_DR_ATTESTATION_FROM_FUTURE");
    }
    if started_at < committed_at || completed_at < started_at || signed_at < completed_at {
        return Err("FEDERATED_DR_TIME_BINDING_INVALID");
    }
    let elapsed_ms = i128::from(completed_at - started_at) * 1_000;
    if (rto_ms - elapsed_ms).abs() > 1_000 {
        return Err("FEDERATED_DR_RTO_TIME_MISMATCH");
    }
    let certificate = attestation
        .get("signerCertificate")
        .and_then(Value::as_object)
        .ok_or("FEDERATED_DR_SIGNER_CERTIFICATE_INVALID")?;
    let not_before = dr_timestamp(certificate, "notBefore")?;
    let signer_expires = dr_timestamp(certificate, "expiresAt")?;
    if signed_at < not_before || expires_at > signer_expires {
        return Err("FEDERATED_DR_SIGNER_WINDOW_INVALID");
    }
    if !positive_integer(attestation.get("sequence")) {
        return Err("FEDERATED_DR_ATTESTATION_SEQUENCE_INVALID");
    }
    Ok(())
}

fn validate_dr_record(
    record: &Map<String, Value>,
    attestation: &Map<String, Value>,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    if !exact_fields(record, &DR_RECORD_FIELDS) {
        errors.push("accepted-dr-record-fields-invalid".to_string());
    }
    let attestation_digest = digest(&Value::Object(attestation.clone())).unwrap_or_default();
    if string(record, "schema") != DR_RECORD_SCHEMA
        || record.get("peerFleetId") != attestation.get("destinationFleetId")
        || record.get("restoreId") != attestation.get("restoreId")
        || record.get("transferId") != attestation.get("transferId")
        || record.get("sequence") != attestation.get("sequence")
        || record.get("signerKeyId") != attestation.get("signerKeyId")
        || string(record, "attestationDigest") != attestation_digest
        || record.get("attestation") != Some(&Value::Object(attestation.clone()))
    {
        errors.push("accepted-dr-record-binding-invalid".to_string());
    }
    let accepted_at = parse_timestamp(string(record, "acceptedAt"));
    if accepted_at.is_none() {
        errors.push("invalid-timestamp:acceptedDrRecord.acceptedAt".to_string());
    } else if accepted_at.is_some_and(|accepted_at| accepted_at > validated_at) {
        errors.push("accepted-dr-record-from-future".to_string());
    }
    if !positive_integer(record.get("revision")) {
        errors.push("accepted-dr-record-revision-invalid".to_string());
    }
}

fn validate_capability(
    capability: &Map<String, Value>,
    source: &Map<String, Value>,
    destination: &Map<String, Value>,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    if !exact_fields(capability, &CAPABILITY_FIELDS) {
        errors.push("recovery-capability-fields-invalid".to_string());
    }
    let recipient = string(capability, "ageRecipient");
    let recipient_valid = if !is_age_recipient(recipient) {
        errors.push("recovery-capability-age-recipient-invalid".to_string());
        false
    } else {
        let expected = format!("sha256:{:x}", Sha256::digest(recipient.as_bytes()));
        if string(capability, "ageRecipientDigest") != expected {
            errors.push("recovery-capability-age-recipient-digest-mismatch".to_string());
            false
        } else {
            true
        }
    };
    if string(capability, "schema") != CAPABILITY_SCHEMA
        || capability.get("localFleetId") != destination.get("fleetId")
        || capability.get("peerFleetId") != source.get("fleetId")
        || capability.get("peerRootFingerprint") != source.get("rootFingerprint")
        || string(capability, "mode") != RECOVERY_CAPABLE
        || capability.get("recoveryIdentityPreprovisioned") != Some(&Value::Bool(true))
        || !recipient_valid
    {
        errors.push("recovery-capability-invalid".to_string());
    }
    let configured_by = string(capability, "configuredBy");
    if configured_by.is_empty() || configured_by != configured_by.trim() {
        errors.push("recovery-capability-operator-invalid".to_string());
    }
    let configured_at = parse_timestamp(string(capability, "configuredAt"));
    if configured_at.is_none() {
        errors.push("invalid-timestamp:recoveryCapability.configuredAt".to_string());
    }
    let updated_at = parse_timestamp(string(capability, "updatedAt"));
    if updated_at.is_none() {
        errors.push("invalid-timestamp:recoveryCapability.updatedAt".to_string());
    }
    if configured_at
        .zip(updated_at)
        .is_some_and(|(configured_at, updated_at)| {
            updated_at < configured_at || updated_at > validated_at
        })
    {
        errors.push("recovery-capability-time-order-invalid".to_string());
    }
    if !positive_integer(capability.get("revision")) {
        errors.push("recovery-capability-revision-invalid".to_string());
    }
}

fn validate_production_result(
    result: &Map<String, Value>,
    replica_attestation: &Map<String, Value>,
    dr_attestation: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    if !PRODUCTION_RESULT_FIELDS
        .iter()
        .all(|field| result.contains_key(*field))
    {
        errors.push("production-restore-result-fields-invalid".to_string());
    }
    for field in ["chainLength", "components", "verifiedContributors"] {
        if !positive_integer(result.get(field)) {
            errors.push(format!("production-restore-metric-invalid:{field}"));
        }
    }
    for field in ["ciphertextBytes", "logicalBytes"] {
        if exact_integer(result.get(field)).is_none_or(|value| value < 0) {
            errors.push(format!("production-restore-metric-invalid:{field}"));
        }
    }
    let evidence = match drill_evidence(
        result,
        string(dr_attestation, "restoreId"),
        string(replica_attestation, "committedAt"),
        string(dr_attestation, "signedAt"),
    ) {
        Ok(evidence) => evidence,
        Err(code) => {
            errors.push(format!("production-restore-evidence-invalid:{code}"));
            return;
        }
    };
    if result.get("schemaVersion") != Some(&Value::from(1)) || string(result, "result") != "success"
    {
        errors.push("production-restore-result-invalid".to_string());
    }
    for (field, expected) in evidence {
        if dr_attestation.get(field) != Some(&expected) {
            errors.push(format!(
                "production-restore-attestation-binding-mismatch:{field}"
            ));
        }
    }
    if string(dr_attestation, "restorePath") != PRODUCTION_RESTORE_PATH {
        errors.push("production-restore-path-invalid".to_string());
    }
}

fn drill_evidence(
    result: &Map<String, Value>,
    restore_id: &str,
    committed_at: &str,
    signed_at: &str,
) -> Result<Vec<(&'static str, Value)>, &'static str> {
    if string(result, "result") != "success" {
        return Err("FEDERATED_DR_PRODUCTION_RESTORE_FAILED");
    }
    if string(result, "restoreId") != restore_id {
        return Err("FEDERATED_DR_RESTORE_ID_MISMATCH");
    }
    if result.get("cleanupCompleted") != Some(&Value::Bool(true)) {
        return Err("FEDERATED_DR_CLEANUP_INCOMPLETE");
    }
    let workspace_digest = string(result, "workspaceDigest");
    if !is_typed_digest(workspace_digest) {
        return Err("FEDERATED_DR_WORKSPACE_DIGEST_INVALID");
    }
    let source_revision = string(result, "sourceRevision");
    if !is_source_revision(source_revision) {
        return Err("FEDERATED_DR_SOURCE_REVISION_INVALID");
    }
    let started_at_text = string(result, "startedAt");
    let completed_at_text = string(result, "completedAt");
    let started_at = parse_timestamp(started_at_text).ok_or("FEDERATED_DR_TIMESTAMP_INVALID")?;
    let completed_at =
        parse_timestamp(completed_at_text).ok_or("FEDERATED_DR_TIMESTAMP_INVALID")?;
    let committed_at = parse_timestamp(committed_at).ok_or("FEDERATED_DR_TIMESTAMP_INVALID")?;
    let signed_at = parse_timestamp(signed_at).ok_or("FEDERATED_DR_TIMESTAMP_INVALID")?;
    let rto_ms = valid_rto(result.get("durationMs"))?;
    let elapsed_ms = i128::from(completed_at - started_at) * 1_000;
    if started_at < committed_at || completed_at < started_at || completed_at > signed_at {
        return Err("FEDERATED_DR_TIME_BINDING_INVALID");
    }
    if (rto_ms - elapsed_ms).abs() > 1_000 {
        return Err("FEDERATED_DR_RTO_TIME_MISMATCH");
    }
    Ok(vec![
        ("restoreId", Value::String(restore_id.to_string())),
        (
            "workspaceDigest",
            Value::String(workspace_digest.to_string()),
        ),
        ("sourceRevision", Value::String(source_revision.to_string())),
        ("startedAt", Value::String(started_at_text.to_string())),
        ("completedAt", Value::String(completed_at_text.to_string())),
        (
            "rtoMs",
            Value::from(i64::try_from(rto_ms).unwrap_or_default()),
        ),
        ("cleanupCompleted", Value::Bool(true)),
    ])
}

fn validate_failure_capability(
    capability: &Map<String, Value>,
    source: &Map<String, Value>,
    destination: &Map<String, Value>,
    expected_mode: &str,
) -> bool {
    exact_fields(capability, &CAPABILITY_FIELDS)
        && string(capability, "schema") == CAPABILITY_SCHEMA
        && capability.get("localFleetId") == destination.get("fleetId")
        && capability.get("peerFleetId") == source.get("fleetId")
        && capability.get("peerRootFingerprint") == source.get("rootFingerprint")
        && string(capability, "mode") == expected_mode
        && capability.get("recoveryIdentityPreprovisioned") == Some(&Value::Bool(false))
        && capability.get("ageRecipient") == Some(&Value::Null)
        && capability.get("ageRecipientDigest") == Some(&Value::Null)
}

#[allow(clippy::too_many_arguments)]
fn validate_failures(
    value: Option<&Value>,
    source: &Map<String, Value>,
    destination: &Map<String, Value>,
    expected_state: &Value,
    replica_attestation: &Map<String, Value>,
    dr_attestation: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    let Some(items) = value.and_then(Value::as_array) else {
        errors.push("failure-observations-must-be-list".to_string());
        return;
    };
    let mut observations = HashMap::new();
    for item in items {
        if let Some(fields) = item.as_object() {
            observations.insert(string(fields, "claim").to_string(), fields.clone());
        }
    }
    if observations.len() != FAILURE_CASES.len()
        || !FAILURE_CASES
            .iter()
            .all(|(claim, _)| observations.contains_key(*claim))
        || items.len() != FAILURE_CASES.len()
    {
        errors.push("failure-observation-inventory-mismatch".to_string());
    }
    for (claim, code) in FAILURE_CASES {
        let observation = match observations.get(claim) {
            Some(value) => value.clone(),
            None => {
                errors.push(format!("failure:{claim}-must-be-object"));
                Map::new()
            }
        };
        if observation.is_empty() {
            continue;
        }
        if !exact_fields(&observation, &FAILURE_FIELDS) {
            errors.push(format!("failure-fields-invalid:{claim}"));
        }
        if string(&observation, "code") != code {
            errors.push(format!("failure-code-mismatch:{claim}"));
        }
        let pre_state = object_copy(
            observation.get("preState"),
            &format!("{claim}.preState"),
            errors,
        );
        let post_state = object_copy(
            observation.get("postState"),
            &format!("{claim}.postState"),
            errors,
        );
        let before = typed_digest(
            observation.get("preStateDigest"),
            &format!("{claim}.preStateDigest"),
            errors,
        );
        let after = typed_digest(
            observation.get("postStateDigest"),
            &format!("{claim}.postStateDigest"),
            errors,
        );
        let pre_value = Value::Object(pre_state.clone());
        let post_value = Value::Object(post_state.clone());
        if !exact_fields(&pre_state, &FAILURE_STATE_FIELDS) || &pre_value != expected_state {
            errors.push(format!("failure-pre-state-binding-invalid:{claim}"));
        }
        if !exact_fields(&post_state, &FAILURE_STATE_FIELDS) || &post_value != expected_state {
            errors.push(format!("failure-post-state-binding-invalid:{claim}"));
        }
        if !before.is_empty() && digest(&pre_value).is_ok_and(|computed| computed != before) {
            errors.push(format!("failure-pre-state-digest-mismatch:{claim}"));
        }
        if !after.is_empty() && digest(&post_value).is_ok_and(|computed| computed != after) {
            errors.push(format!("failure-post-state-digest-mismatch:{claim}"));
        }
        if !before.is_empty() && !after.is_empty() && (before != after || pre_state != post_state) {
            errors.push(format!("failure-mutated-state:{claim}"));
        }
        let input = object_copy(
            observation.get("input"),
            &format!("failure-input:{claim}"),
            errors,
        );
        match claim {
            "coldCustodyCannotClaimRecoveryReady" => {
                let capability = object_copy(
                    input.get("capability"),
                    &format!("failure-capability:{claim}"),
                    errors,
                );
                if !validate_failure_capability(&capability, source, destination, COLD_CUSTODY) {
                    errors.push("cold-custody-evidence-invalid".to_string());
                }
            }
            "recoveryCapablePeerRequiresPreprovisionedAgeIdentity" => {
                let capability = object_copy(
                    input.get("capability"),
                    &format!("failure-capability:{claim}"),
                    errors,
                );
                if !validate_failure_capability(&capability, source, destination, RECOVERY_CAPABLE)
                {
                    errors.push("missing-recovery-identity-evidence-invalid".to_string());
                }
            }
            _ => {
                let result = object_copy(
                    input.get("productionRestoreResult"),
                    "cleanup-failure-result",
                    errors,
                );
                match drill_evidence(
                    &result,
                    string(dr_attestation, "restoreId"),
                    string(replica_attestation, "committedAt"),
                    string(dr_attestation, "signedAt"),
                ) {
                    Err(observed) if observed != code => {
                        errors.push("cleanup-failure-code-mismatch".to_string());
                    }
                    Err(_) => {}
                    Ok(_) => errors.push("cleanup-failure-evidence-invalid".to_string()),
                }
            }
        }
    }
}

fn valid_rto(value: Option<&Value>) -> Result<i128, &'static str> {
    exact_integer(value)
        .filter(|value| (0..=MAX_DR_RTO_MS).contains(value))
        .ok_or("FEDERATED_DR_RTO_INVALID")
}

fn dr_timestamp(fields: &Map<String, Value>, field: &str) -> Result<i64, &'static str> {
    parse_timestamp(string(fields, field)).ok_or("FEDERATED_DR_TIMESTAMP_INVALID")
}

fn is_fleet_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().skip(1).all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

fn is_control_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .skip(1)
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn is_restore_id(value: &str) -> bool {
    value.strip_prefix("restore_").is_some_and(|suffix| {
        (1..=64).contains(&suffix.len()) && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
    })
}

fn is_source_revision(value: &str) -> bool {
    !value.is_empty()
        && value == value.trim()
        && value.len() <= 512
        && !value
            .chars()
            .any(|character| character < '\u{20}' || character == '\u{7f}')
}

fn is_age_recipient(value: &str) -> bool {
    value.strip_prefix("age1").is_some_and(|suffix| {
        (6..=250).contains(&suffix.len())
            && suffix
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte.is_ascii_lowercase())
    })
}
