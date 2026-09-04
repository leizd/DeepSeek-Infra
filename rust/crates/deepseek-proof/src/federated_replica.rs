use base64::Engine as _;
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use deepseek_federation::{
    CurrentSignerAuthorization, FailureDomainMetadata, PURPOSE_INGRESS_GRANT,
    PURPOSE_REPLICA_ATTESTATION, ReplicaAttestation, ReplicaTransferBinding,
    ReplicaVerificationContext, derive_transfer_id, failure_domain_from_metadata,
    validate_fleet_identity, verify_federation_document, verify_replica_attestation_for_proof,
    verify_replica_remote_documents,
};
use deepseek_storage::ReceiptV4;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

pub const FEDERATED_REPLICA_PROOF_SCHEMA: &str = "federated-replica-proof-v1";
pub const FEDERATED_REPLICA_PROOF_CHECKS: [&str; 32] = [
    "ingressGrantIsReceiverSigned",
    "ingressGrantBindsSourceFleet",
    "ingressGrantBindsDestinationFleet",
    "ingressGrantBindsBackupId",
    "ingressGrantBindsObjectSetDigest",
    "ingressGrantBindsTransferId",
    "expiredIngressGrantCannotWrite",
    "ingressGrantCannotEscapeObjectPrefix",
    "ingressGrantCannotExceedMaxBytes",
    "sameTransferIdSameDigestIsIdempotent",
    "sameTransferIdDifferentDigestFailsClosed",
    "receiverRestartResumesExistingTransfer",
    "federatedReplicaUsesExistingObjectSetV1",
    "federatedReplicaCreatesReceiptV4",
    "federatedReplicaCreatesCommitV4",
    "federatedReplicaAttestationBindsReceiptDigest",
    "federatedReplicaAttestationBindsCommitDigest",
    "federatedReplicaAttestationBindsObjectSetDigest",
    "remoteCopyRecordedOnlyAfterAttestationVerification",
    "federatedCopyDoesNotReduceLocalMinCommittedCopies",
    "federatedCopyDoesNotReduceLocalMinFailureDomains",
    "federatedCopyCannotAuthorizePrimaryPromotion",
    "federatedTransferNeverDeletesLocalReplica",
    "peerFailureDomainIsCheckedAgainstPinnedMetadata",
    "replayedIngressGrantFailsClosed",
    "tamperedReplicaAttestationFailsClosed",
    "objectSetV1WireFormatUnchanged",
    "receiptV4Unchanged",
    "commitV4Unchanged",
    "fastCdcV3Unchanged",
    "randomizedAgeUnchanged",
    "federatedReplicaProofIsSemanticallyValidated",
];

const PROOF_FIELDS: [&str; 20] = [
    "schema",
    "validatedAt",
    "destinationFleetIdentity",
    "peerTrustRecord",
    "ingressGrant",
    "receiverTransfer",
    "senderTransfer",
    "objectSetDeclaration",
    "sourceReceipt",
    "remoteReceiptBytesBase64",
    "remoteCommitBytesBase64",
    "replicaAttestation",
    "acceptedReplicaRecord",
    "federatedCopyRecord",
    "localDurabilityBefore",
    "localDurabilityAfter",
    "federatedDurabilityStatus",
    "failureObservations",
    "wireContracts",
    "proofDigest",
];
const PINNED_METADATA_FIELDS: [&str; 4] = ["provider", "region", "jurisdiction", "siteClass"];
const PEER_RECORD_FIELDS: [&str; 17] = [
    "schema",
    "peerFleetId",
    "rootKeyId",
    "rootFingerprint",
    "fleetIdentity",
    "pinnedMetadata",
    "metadataDigest",
    "state",
    "pinnedBy",
    "stateReason",
    "pinnedAt",
    "verifiedAt",
    "activatedAt",
    "suspendedAt",
    "revokedAt",
    "revision",
    "updatedAt",
];
const TRANSFER_RECORD_FIELDS: [&str; 16] = [
    "schema",
    "transferId",
    "identityDigest",
    "localFleetId",
    "role",
    "sourceFleetId",
    "destinationFleetId",
    "policyId",
    "backupId",
    "objectSetDigest",
    "state",
    "statePayloadDigest",
    "stateDetails",
    "createdAt",
    "updatedAt",
    "revision",
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
const COPY_RECORD_FIELDS: [&str; 22] = [
    "schema",
    "status",
    "transferId",
    "sourceFleetId",
    "destinationFleetId",
    "policyId",
    "backupId",
    "objectSetDigest",
    "remoteTargetId",
    "remoteReceiptDigest",
    "remoteCommitDigest",
    "attestationDigest",
    "attestationSequence",
    "signerKeyId",
    "failureDomain",
    "peerMetadata",
    "committedAt",
    "attestationAcceptedAt",
    "recordedAt",
    "localDurabilityCredit",
    "recordDigest",
    "revision",
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
const FAILURE_CASES: [(&str, &str); 6] = [
    (
        "expiredIngressGrantCannotWrite",
        "FEDERATION_INGRESS_GRANT_EXPIRED",
    ),
    (
        "ingressGrantCannotEscapeObjectPrefix",
        "FEDERATION_INGRESS_OBJECT_PREFIX_VIOLATION",
    ),
    (
        "ingressGrantCannotExceedMaxBytes",
        "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED",
    ),
    (
        "sameTransferIdDifferentDigestFailsClosed",
        "FEDERATION_TRANSFER_IDENTITY_CONFLICT",
    ),
    (
        "replayedIngressGrantFailsClosed",
        "FEDERATION_INGRESS_GRANT_NONCE_REPLAY",
    ),
    (
        "tamperedReplicaAttestationFailsClosed",
        "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
    ),
];
const INGRESS_GRANT_FIELDS: [&str; 19] = [
    "schema",
    "fleetId",
    "grantId",
    "sourceFleetId",
    "destinationFleetId",
    "transferId",
    "policyId",
    "backupId",
    "objectSetDigest",
    "allowedObjectPrefix",
    "maxBytes",
    "issuedAt",
    "expiresAt",
    "nonce",
    "sessionNonceDigest",
    "signerCertificate",
    "signerKeyId",
    "signatureAlgorithm",
    "signature",
];
const SECRET_FIELD_NAMES: [&str; 13] = [
    "accesskey",
    "ageidentity",
    "ageprivateidentity",
    "credential",
    "credentialref",
    "credentialreference",
    "passphrase",
    "password",
    "privatekey",
    "privatekeyenvelope",
    "secret",
    "secretkey",
    "sessiontoken",
];
const SECRET_VALUE_MARKERS: [&str; 3] = [
    "age-secret-key-",
    "begin private key",
    "begin openssh private key",
];
const SENSITIVE_STATE_MARKERS: [&str; 6] = [
    "credential",
    "password",
    "privatekey",
    "secretkey",
    "accesskey",
    "agesecretkey",
];

pub fn federated_replica_proof_digest(proof: &Value) -> Result<String, String> {
    let fields = proof
        .as_object()
        .ok_or_else(|| "federated-replica-proof-must-be-object".to_string())?;
    let mut payload = fields.clone();
    payload.remove("proofDigest");
    digest(&Value::Object(payload))
}

pub fn validate_federated_replica_check(check_name: &str, evidence: &Value) -> Vec<String> {
    if evidence.get("schema").and_then(Value::as_str) == Some(FEDERATED_REPLICA_PROOF_SCHEMA) {
        return validate_federated_replica_proof(evidence);
    }
    match check_name {
        "receiptV4Unchanged" | "commitV4Unchanged" => {
            crate::storage_evidence::validate_autonomous_storage_bytes_proof(evidence)
        }
        "objectSetV1WireFormatUnchanged" => {
            validate_legacy_wire_value(evidence, "objectSetVersion", &json!("object-set-v1"))
        }
        "fastCdcV3Unchanged" => {
            validate_legacy_wire_value(evidence, "cdcVersion", &json!("fastcdc-v3"))
        }
        "randomizedAgeUnchanged" => {
            validate_legacy_wire_value(evidence, "ageRandomized", &Value::Bool(true))
        }
        _ if FEDERATED_REPLICA_PROOF_CHECKS.contains(&check_name) => {
            validate_federated_replica_proof(evidence)
        }
        _ => vec![format!(
            "unsupported-federated-replica-proof-check:{check_name}"
        )],
    }
}

pub fn validate_federated_replica_proof(value: &Value) -> Vec<String> {
    let Some(proof) = value.as_object() else {
        return vec!["federated-replica-proof-must-be-object".to_string()];
    };
    let mut errors = Vec::new();
    if !exact_fields(proof, &PROOF_FIELDS) {
        errors.push("federated-replica-proof-fields-invalid".to_string());
    }
    if string(proof, "schema") != FEDERATED_REPLICA_PROOF_SCHEMA {
        errors.push("federated-replica-proof-schema-invalid".to_string());
    }
    if contains_secret(value) {
        errors.push("federated-replica-proof-contains-secret".to_string());
    }
    let validated_at_text = string(proof, "validatedAt").to_string();
    let validated_at = parse_timestamp(&validated_at_text);
    if validated_at.is_none() {
        errors.push("invalid-timestamp:validatedAt".to_string());
    }

    let identity_value = proof
        .get("destinationFleetIdentity")
        .cloned()
        .unwrap_or(Value::Null);
    let identity = identity_value.as_object().cloned().unwrap_or_default();
    let identity_valid = validate_fleet_identity(&identity_value)
        .map_err(|error| {
            errors.push(format!("destination-identity-invalid:{}", error.code()));
        })
        .is_ok();
    let (_peer, pinned_metadata) =
        validate_peer_record(proof.get("peerTrustRecord"), &identity, &mut errors);
    let receiver = object_copy(
        proof.get("receiverTransfer"),
        "receiver-transfer",
        &mut errors,
    );
    let sender = object_copy(proof.get("senderTransfer"), "sender-transfer", &mut errors);
    validate_transfer_pair(&receiver, &sender, &mut errors);
    let grant = object_copy(proof.get("ingressGrant"), "ingress-grant", &mut errors);
    let declaration = object_copy(
        proof.get("objectSetDeclaration"),
        "object-set-declaration",
        &mut errors,
    );
    let source_receipt = object_copy(proof.get("sourceReceipt"), "source-receipt", &mut errors);
    let attestation = object_copy(
        proof.get("replicaAttestation"),
        "replica-attestation",
        &mut errors,
    );
    let accepted = object_copy(
        proof.get("acceptedReplicaRecord"),
        "accepted-replica-record",
        &mut errors,
    );
    let copy_record = object_copy(
        proof.get("federatedCopyRecord"),
        "federated-copy-record",
        &mut errors,
    );
    let before = object_copy(
        proof.get("localDurabilityBefore"),
        "local-durability-before",
        &mut errors,
    );
    let after = object_copy(
        proof.get("localDurabilityAfter"),
        "local-durability-after",
        &mut errors,
    );
    let status = object_copy(
        proof.get("federatedDurabilityStatus"),
        "federated-durability-status",
        &mut errors,
    );
    let receipt_bytes = decode_document(
        proof.get("remoteReceiptBytesBase64"),
        "remoteReceiptBytesBase64",
        &mut errors,
    );
    let commit_bytes = decode_document(
        proof.get("remoteCommitBytesBase64"),
        "remoteCommitBytesBase64",
        &mut errors,
    );

    if validated_at.is_some() && identity_valid && !receiver.is_empty() && !grant.is_empty() {
        validate_grant(
            &grant,
            &identity,
            &receiver,
            &validated_at_text,
            validated_at.unwrap_or_default(),
            &mut errors,
        );
    }
    if !declaration.is_empty()
        && !source_receipt.is_empty()
        && !receipt_bytes.is_empty()
        && !commit_bytes.is_empty()
        && !sender.is_empty()
        && !attestation.is_empty()
    {
        validate_storage(
            &declaration,
            &source_receipt,
            &receipt_bytes,
            &commit_bytes,
            &sender,
            &attestation,
            &mut errors,
        );
    }
    let verified_attestation = attestation.clone();
    if validated_at.is_some()
        && identity_valid
        && !sender.is_empty()
        && !pinned_metadata.is_empty()
        && !attestation.is_empty()
    {
        validate_attestation(
            &attestation,
            &identity,
            &sender,
            &pinned_metadata,
            &source_receipt,
            &receipt_bytes,
            &commit_bytes,
            &validated_at_text,
            &mut errors,
        );
    }
    if validated_at.is_some()
        && !accepted.is_empty()
        && !copy_record.is_empty()
        && !verified_attestation.is_empty()
        && !sender.is_empty()
        && !pinned_metadata.is_empty()
    {
        validate_records(
            &accepted,
            &copy_record,
            &verified_attestation,
            &sender,
            &pinned_metadata,
            validated_at.unwrap_or_default(),
            &mut errors,
        );
    }
    if !before.is_empty() && !after.is_empty() && !status.is_empty() && !sender.is_empty() {
        validate_durability(&before, &after, &status, &sender, &mut errors);
    }
    let wires = object_copy(proof.get("wireContracts"), "wire-contracts", &mut errors);
    if Value::Object(wires)
        != json!({
            "objectSet": "object-set-v1",
            "receiptVersion": 4,
            "commitVersion": 4,
            "fastCdc": "fastcdc-v3",
            "randomizedAge": true,
        })
    {
        errors.push("frozen-wire-contract-mismatch".to_string());
    }
    if validated_at.is_some() && !grant.is_empty() && !receiver.is_empty() && identity_valid {
        validate_failures(
            proof.get("failureObservations"),
            &grant,
            &receiver,
            &identity,
            &validated_at_text,
            validated_at.unwrap_or_default(),
            &mut errors,
        );
    }
    let declared = typed_digest(proof.get("proofDigest"), "proofDigest", &mut errors);
    if !declared.is_empty()
        && federated_replica_proof_digest(value).is_ok_and(|computed| computed != declared)
    {
        errors.push("proof-digest-mismatch".to_string());
    }
    dedupe(errors)
}

fn validate_peer_record(
    value: Option<&Value>,
    identity: &Map<String, Value>,
    errors: &mut Vec<String>,
) -> (Map<String, Value>, Map<String, Value>) {
    let peer = object_copy(value, "peer-trust-record", errors);
    if peer.is_empty() {
        return (Map::new(), Map::new());
    }
    if !exact_fields(&peer, &PEER_RECORD_FIELDS) {
        errors.push("peer-trust-record-fields-invalid".to_string());
    }
    if string(&peer, "schema") != "federation-peer-trust-record-v1" {
        errors.push("peer-trust-record-schema-invalid".to_string());
    }
    if string(&peer, "state") != "ACTIVE" {
        errors.push("peer-trust-not-active".to_string());
    }
    if peer.get("peerFleetId") != identity.get("fleetId")
        || peer.get("fleetIdentity") != Some(&Value::Object(identity.clone()))
    {
        errors.push("peer-trust-identity-binding-invalid".to_string());
    }
    if peer.get("rootKeyId") != identity.get("rootKeyId")
        || peer.get("rootFingerprint") != identity.get("rootFingerprint")
    {
        errors.push("peer-trust-root-binding-invalid".to_string());
    }
    let metadata = object_copy(peer.get("pinnedMetadata"), "pinned-metadata", errors);
    if !exact_fields(&metadata, &PINNED_METADATA_FIELDS)
        || metadata.values().any(|value| {
            value
                .as_str()
                .is_none_or(|item| item.is_empty() || item != item.trim())
        })
    {
        errors.push("pinned-metadata-invalid".to_string());
    } else if peer.get("metadataDigest").and_then(Value::as_str)
        != digest(&Value::Object(metadata.clone())).ok().as_deref()
    {
        errors.push("pinned-metadata-digest-mismatch".to_string());
    }
    if string(&peer, "pinnedBy").is_empty() {
        errors.push("peer-trust-operator-pin-missing".to_string());
    }
    for field in ["pinnedAt", "verifiedAt", "activatedAt", "updatedAt"] {
        if parse_timestamp(string(&peer, field)).is_none() {
            errors.push(format!("invalid-timestamp:peerTrustRecord.{field}"));
        }
    }
    if !positive_integer(peer.get("revision")) {
        errors.push("peer-trust-revision-invalid".to_string());
    }
    if peer.get("revokedAt").is_some_and(|value| !value.is_null()) {
        errors.push("peer-trust-revoked".to_string());
    }
    (peer, metadata)
}

fn validate_transfer_record(
    record: &Map<String, Value>,
    expected_role: &str,
    errors: &mut Vec<String>,
) {
    let label = expected_role.to_ascii_lowercase();
    if !exact_fields(record, &TRANSFER_RECORD_FIELDS) {
        errors.push(format!("{label}-transfer-fields-invalid"));
    }
    if string(record, "schema") != "federated-transfer-journal-record-v1" {
        errors.push(format!("{label}-transfer-schema-invalid"));
    }
    if string(record, "role") != expected_role {
        errors.push(format!("{label}-transfer-role-invalid"));
    }
    let expected_local = if expected_role == "RECEIVER" {
        record.get("destinationFleetId")
    } else {
        record.get("sourceFleetId")
    };
    if record.get("localFleetId") != expected_local {
        errors.push(format!("{label}-transfer-local-fleet-invalid"));
    }
    let details = record
        .get("stateDetails")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let state_result = transfer_state_payload(
        &python_text(record.get("transferId")),
        &python_text(record.get("state")),
        &details,
    );
    if let Ok(state_digest) = state_result {
        let binding = json!({
            "schema": "federated-transfer-binding-v1",
            "transferId": python_text(record.get("transferId")),
            "sourceFleetId": python_text(record.get("sourceFleetId")),
            "destinationFleetId": python_text(record.get("destinationFleetId")),
            "policyId": python_text(record.get("policyId")),
            "backupId": python_text(record.get("backupId")),
            "objectSetDigest": python_text(record.get("objectSetDigest")),
        });
        let identity_digest = digest(&binding).unwrap_or_default();
        if string(record, "identityDigest") != identity_digest {
            errors.push(format!("{label}-transfer-identity-digest-mismatch"));
        }
        if string(record, "statePayloadDigest") != state_digest {
            errors.push(format!("{label}-transfer-state-digest-mismatch"));
        }
    } else {
        errors.push(format!("{label}-transfer-record-invalid"));
    }
    let created = parse_timestamp(string(record, "createdAt"));
    if created.is_none() {
        errors.push(format!("invalid-timestamp:{label}Transfer.createdAt"));
    }
    let updated = parse_timestamp(string(record, "updatedAt"));
    if updated.is_none() {
        errors.push(format!("invalid-timestamp:{label}Transfer.updatedAt"));
    }
    if created
        .zip(updated)
        .is_some_and(|(created, updated)| updated < created)
    {
        errors.push(format!("{label}-transfer-time-order-invalid"));
    }
    if !positive_integer(record.get("revision")) {
        errors.push(format!("{label}-transfer-revision-invalid"));
    }
}

fn validate_transfer_pair(
    receiver: &Map<String, Value>,
    sender: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    validate_transfer_record(receiver, "RECEIVER", errors);
    validate_transfer_record(sender, "SENDER", errors);
    for field in [
        "transferId",
        "sourceFleetId",
        "destinationFleetId",
        "policyId",
        "backupId",
        "objectSetDigest",
    ] {
        if receiver.get(field) != sender.get(field) {
            errors.push(format!("transfer-role-binding-mismatch:{field}"));
        }
    }
    let object_set = typed_digest(
        receiver.get("objectSetDigest"),
        "receiverTransfer.objectSetDigest",
        errors,
    );
    let transfer_id = typed_digest(
        receiver.get("transferId"),
        "receiverTransfer.transferId",
        errors,
    );
    if !object_set.is_empty()
        && !transfer_id.is_empty()
        && derive_transfer_id(
            string(receiver, "sourceFleetId"),
            string(receiver, "destinationFleetId"),
            string(receiver, "backupId"),
            &object_set,
        ) != Ok(transfer_id.clone())
    {
        errors.push("transfer-identity-invalid".to_string());
    }
    if string(receiver, "state") != "REMOTE_COMMITTED" {
        errors.push("receiver-transfer-not-committed".to_string());
    }
    if string(sender, "state") != "SUCCEEDED" {
        errors.push("sender-transfer-not-succeeded".to_string());
    }
}

fn validate_grant(
    grant: &Map<String, Value>,
    identity: &Map<String, Value>,
    transfer: &Map<String, Value>,
    validated_at_text: &str,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    let certificate = object_copy(
        grant.get("signerCertificate"),
        "ingress-grant-certificate",
        errors,
    );
    if !certificate.is_empty() {
        let document = Value::Object(grant.clone());
        if let Err(error) = verify_federation_document(
            &document,
            &certificate,
            &Value::Object(identity.clone()),
            "federation-ingress-grant-v1",
            validated_at_text,
            PURPOSE_INGRESS_GRANT,
        ) {
            errors.push(format!("ingress-grant-signature-invalid:{}", error.code()));
        }
    }
    if let Err(code) = grant_semantics(grant, transfer, validated_at) {
        errors.push(format!("ingress-grant-invalid:{code}"));
    }
}

fn grant_semantics(
    grant: &Map<String, Value>,
    transfer: &Map<String, Value>,
    now: i64,
) -> Result<(), &'static str> {
    if !exact_fields(grant, &INGRESS_GRANT_FIELDS) {
        return Err("FEDERATION_INGRESS_GRANT_FIELDS_INVALID");
    }
    if string(grant, "schema") != "federation-ingress-grant-v1" {
        return Err("FEDERATION_INGRESS_GRANT_SCHEMA_INVALID");
    }
    if !is_grant_id(string(grant, "grantId")) {
        return Err("FEDERATION_INGRESS_GRANT_ID_INVALID");
    }
    let source = string(grant, "sourceFleetId");
    let destination = string(grant, "destinationFleetId");
    if !is_fleet_id(source) || !is_fleet_id(destination) {
        return Err("FEDERATION_INGRESS_FLEET_ID_INVALID");
    }
    if source != string(transfer, "sourceFleetId") {
        return Err("FEDERATION_INGRESS_SOURCE_FLEET_MISMATCH");
    }
    if string(grant, "fleetId") != destination
        || destination != string(transfer, "destinationFleetId")
    {
        return Err("FEDERATION_INGRESS_DESTINATION_FLEET_MISMATCH");
    }
    if source == destination {
        return Err("FEDERATION_INGRESS_REFLECTION_REJECTED");
    }
    let transfer_id = string(grant, "transferId");
    if !is_typed_digest(transfer_id) {
        return Err("FEDERATION_INGRESS_TRANSFER_ID_INVALID");
    }
    if transfer_id != string(transfer, "transferId") {
        return Err("FEDERATION_INGRESS_TRANSFER_ID_MISMATCH");
    }
    let policy = string(grant, "policyId");
    if !is_control_id(policy) {
        return Err("FEDERATION_INGRESS_POLICY_ID_INVALID");
    }
    if policy != string(transfer, "policyId") {
        return Err("FEDERATION_INGRESS_POLICY_ID_MISMATCH");
    }
    let backup = string(grant, "backupId");
    if !is_control_id(backup) {
        return Err("FEDERATION_INGRESS_BACKUP_ID_INVALID");
    }
    if backup != string(transfer, "backupId") {
        return Err("FEDERATION_INGRESS_BACKUP_ID_MISMATCH");
    }
    let object_set = string(grant, "objectSetDigest");
    if !is_typed_digest(object_set) {
        return Err("FEDERATION_INGRESS_OBJECT_SET_DIGEST_INVALID");
    }
    if object_set != string(transfer, "objectSetDigest") {
        return Err("FEDERATION_INGRESS_OBJECT_SET_DIGEST_MISMATCH");
    }
    let derived = derive_transfer_id(source, destination, backup, object_set)
        .map_err(|error| error.code())?;
    if transfer_id != derived {
        return Err("FEDERATION_TRANSFER_ID_INVALID");
    }
    if !is_object_prefix(string(grant, "allowedObjectPrefix")) {
        return Err("FEDERATION_INGRESS_OBJECT_PREFIX_INVALID");
    }
    if !positive_bounded_i64(grant.get("maxBytes")) {
        return Err("FEDERATION_INGRESS_MAX_BYTES_INVALID");
    }
    if !is_nonce(string(grant, "nonce")) {
        return Err("FEDERATION_INGRESS_GRANT_NONCE_INVALID");
    }
    if !is_typed_digest(string(grant, "sessionNonceDigest")) {
        return Err("FEDERATION_INGRESS_SESSION_NONCE_DIGEST_INVALID");
    }
    let issued_at =
        parse_timestamp(string(grant, "issuedAt")).ok_or("FEDERATION_INGRESS_TIMESTAMP_INVALID")?;
    let expires_at = parse_timestamp(string(grant, "expiresAt"))
        .ok_or("FEDERATION_INGRESS_TIMESTAMP_INVALID")?;
    let lifetime = expires_at.saturating_sub(issued_at);
    if !(1..=300).contains(&lifetime) {
        return Err("FEDERATION_INGRESS_GRANT_LIFETIME_INVALID");
    }
    if issued_at.saturating_sub(now) > 30 {
        return Err("FEDERATION_INGRESS_GRANT_FROM_FUTURE");
    }
    if now >= expires_at {
        return Err("FEDERATION_INGRESS_GRANT_EXPIRED");
    }
    let certificate = grant
        .get("signerCertificate")
        .and_then(Value::as_object)
        .ok_or("FEDERATION_INGRESS_SIGNER_CERTIFICATE_INVALID")?;
    let not_before = parse_timestamp(string(certificate, "notBefore"))
        .ok_or("FEDERATION_INGRESS_TIMESTAMP_INVALID")?;
    let certificate_expires = parse_timestamp(string(certificate, "expiresAt"))
        .ok_or("FEDERATION_INGRESS_TIMESTAMP_INVALID")?;
    if issued_at < not_before || expires_at > certificate_expires {
        return Err("FEDERATION_INGRESS_SIGNER_WINDOW_INVALID");
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn validate_storage(
    declaration: &Map<String, Value>,
    source_receipt: &Map<String, Value>,
    receipt_bytes: &[u8],
    commit_bytes: &[u8],
    transfer: &Map<String, Value>,
    attestation: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    if string(declaration, "storageProtocol") != "object-set-v1"
        || declaration.get("objectSetDigest") != transfer.get("objectSetDigest")
        || declaration.get("backupId") != transfer.get("backupId")
        || declaration.get("policyId") != transfer.get("policyId")
        || declaration
            .get("objects")
            .and_then(Value::as_array)
            .is_none_or(Vec::is_empty)
    {
        errors.push("object-set-declaration-invalid".to_string());
    }
    let result = replica_context_parts(source_receipt, transfer, attestation).and_then(
        |(source, replica, metadata, binding, authorization, root)| {
            let context = ReplicaVerificationContext {
                root_identity: &root,
                signer_authorization: &authorization,
                pinned_metadata: &metadata,
                transfer: &binding,
                source_receipt: &source,
                remote_receipt_bytes: receipt_bytes,
                remote_commit_bytes: commit_bytes,
                now: "1970-01-01T00:00:00Z",
                max_future_skew_seconds: 30,
            };
            verify_replica_remote_documents(&replica, &context).map_err(|error| error.code())
        },
    );
    if let Err(code) = result {
        errors.push("remote-storage-documents-invalid".to_string());
        errors.push(format!("remote-storage-documents-invalid:{code}"));
    }
}

#[allow(clippy::too_many_arguments)]
fn validate_attestation(
    attestation: &Map<String, Value>,
    identity: &Map<String, Value>,
    transfer: &Map<String, Value>,
    pinned_metadata: &Map<String, Value>,
    source_receipt: &Map<String, Value>,
    receipt_bytes: &[u8],
    commit_bytes: &[u8],
    validated_at: &str,
    errors: &mut Vec<String>,
) {
    let certificate = object_copy(
        attestation.get("signerCertificate"),
        "replica-attestation-certificate",
        errors,
    );
    if certificate.is_empty() {
        return;
    }
    let result = replica_context_parts_with(
        source_receipt,
        transfer,
        attestation,
        pinned_metadata,
        identity,
    )
    .and_then(
        |(source, replica, metadata, binding, authorization, root)| {
            let context = ReplicaVerificationContext {
                root_identity: &root,
                signer_authorization: &authorization,
                pinned_metadata: &metadata,
                transfer: &binding,
                source_receipt: &source,
                remote_receipt_bytes: receipt_bytes,
                remote_commit_bytes: commit_bytes,
                now: validated_at,
                max_future_skew_seconds: 30,
            };
            verify_replica_attestation_for_proof(&replica, &context).map_err(|error| error.code())
        },
    );
    match result {
        Ok(()) => {}
        Err(code) => {
            errors.push(format!("replica-attestation-invalid:{code}"));
        }
    }
}

fn validate_records(
    accepted: &Map<String, Value>,
    copy_record: &Map<String, Value>,
    attestation: &Map<String, Value>,
    transfer: &Map<String, Value>,
    pinned_metadata: &Map<String, Value>,
    validated_at: i64,
    errors: &mut Vec<String>,
) {
    let attestation_digest = digest(&Value::Object(attestation.clone())).unwrap_or_default();
    if !exact_fields(accepted, &ACCEPTED_REPLICA_RECORD_FIELDS) {
        errors.push("accepted-replica-record-fields-invalid".to_string());
    }
    if string(accepted, "schema") != "federated-replica-attestation-record-v1"
        || accepted.get("peerFleetId") != transfer.get("destinationFleetId")
        || accepted.get("transferId") != transfer.get("transferId")
        || accepted.get("sequence") != attestation.get("sequence")
        || accepted.get("signerKeyId") != attestation.get("signerKeyId")
        || string(accepted, "attestationDigest") != attestation_digest
        || accepted.get("attestation") != Some(&Value::Object(attestation.clone()))
    {
        errors.push("accepted-replica-record-binding-invalid".to_string());
    }
    let accepted_at = parse_timestamp(string(accepted, "acceptedAt"));
    if accepted_at.is_none() {
        errors.push("invalid-timestamp:acceptedReplicaRecord.acceptedAt".to_string());
    }
    let committed_at = parse_timestamp(string(attestation, "committedAt"));
    if committed_at.is_none() {
        errors.push("invalid-timestamp:replicaAttestation.committedAt".to_string());
    }
    if accepted_at
        .zip(committed_at)
        .is_some_and(|(accepted_at, committed_at)| {
            accepted_at < committed_at || accepted_at > validated_at
        })
    {
        errors.push("accepted-replica-record-time-order-invalid".to_string());
    }
    if !positive_integer(accepted.get("revision")) {
        errors.push("accepted-replica-record-revision-invalid".to_string());
    }
    if !exact_fields(copy_record, &COPY_RECORD_FIELDS) {
        errors.push("federated-copy-record-fields-invalid".to_string());
    }
    let mut record_identity = copy_record.clone();
    for field in ["recordDigest", "recordedAt", "revision"] {
        record_identity.remove(field);
    }
    if copy_record.get("recordDigest").and_then(Value::as_str)
        != digest(&Value::Object(record_identity)).ok().as_deref()
    {
        errors.push("federated-copy-record-digest-mismatch".to_string());
    }
    let recorded_at = parse_timestamp(string(copy_record, "recordedAt"));
    if recorded_at.is_none() {
        errors.push("invalid-timestamp:federatedCopyRecord.recordedAt".to_string());
    }
    if recorded_at
        .zip(accepted_at)
        .is_some_and(|(recorded_at, accepted_at)| {
            recorded_at < accepted_at || recorded_at > validated_at
        })
    {
        errors.push("federated-copy-record-time-order-invalid".to_string());
    }
    if !positive_integer(copy_record.get("revision")) {
        errors.push("federated-copy-record-revision-invalid".to_string());
    }
    for field in [
        "transferId",
        "sourceFleetId",
        "destinationFleetId",
        "policyId",
        "backupId",
        "objectSetDigest",
    ] {
        if copy_record.get(field) != transfer.get(field) {
            errors.push(format!("federated-copy-transfer-binding-mismatch:{field}"));
        }
    }
    let failure_domain = metadata_failure_domain(pinned_metadata).unwrap_or_default();
    if string(copy_record, "status") != "FEDERATED_COMMITTED"
        || copy_record.get("localDurabilityCredit") != Some(&Value::Bool(false))
        || string(copy_record, "attestationDigest") != attestation_digest
        || copy_record.get("remoteTargetId") != attestation.get("remoteTargetId")
        || copy_record.get("remoteReceiptDigest") != attestation.get("remoteReceiptDigest")
        || copy_record.get("remoteCommitDigest") != attestation.get("remoteCommitDigest")
        || copy_record.get("committedAt") != attestation.get("committedAt")
        || copy_record.get("attestationSequence") != attestation.get("sequence")
        || copy_record.get("signerKeyId") != attestation.get("signerKeyId")
        || copy_record.get("attestationAcceptedAt") != accepted.get("acceptedAt")
        || copy_record.get("peerMetadata") != Some(&Value::Object(pinned_metadata.clone()))
        || string(copy_record, "failureDomain") != failure_domain
    {
        errors.push("federated-copy-semantic-binding-invalid".to_string());
    }
}

fn validate_durability(
    before: &Map<String, Value>,
    after: &Map<String, Value>,
    status: &Map<String, Value>,
    transfer: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    if before != after {
        errors.push("local-durability-regressed".to_string());
    }
    for field in ["minCommittedCopies", "minFailureDomains"] {
        if !positive_integer(before.get(field)) || after.get(field) != before.get(field) {
            errors.push(format!("local-durability-objective-invalid:{field}"));
        }
    }
    let credited = status
        .get("creditedTransferIds")
        .and_then(Value::as_array)
        .is_some_and(|items| {
            items
                .iter()
                .any(|item| Some(item) == transfer.get("transferId"))
        });
    if string(status, "schema") != "federated-durability-status-v1"
        || status.get("satisfied") != Some(&Value::Bool(true))
        || !python_zero(status.get("localDurabilityCredit"))
        || status.get("objectSetDigest") != transfer.get("objectSetDigest")
        || status.get("backupId") != transfer.get("backupId")
        || !credited
    {
        errors.push("federated-durability-status-invalid".to_string());
    }
}

fn validate_failures(
    value: Option<&Value>,
    grant: &Map<String, Value>,
    transfer: &Map<String, Value>,
    identity: &Map<String, Value>,
    validated_at_text: &str,
    _validated_at: i64,
    errors: &mut Vec<String>,
) {
    let Some(items) = value.and_then(Value::as_array) else {
        errors.push("failure-observations-must-be-list".to_string());
        return;
    };
    let observed_claims: Vec<String> = items
        .iter()
        .filter_map(Value::as_object)
        .map(|item| python_text(item.get("claim")))
        .collect();
    let expected: HashSet<&str> = FAILURE_CASES.iter().map(|(claim, _)| *claim).collect();
    let observed: HashSet<&str> = observed_claims.iter().map(String::as_str).collect();
    if expected != observed || items.len() != FAILURE_CASES.len() {
        errors.push("failure-observation-inventory-mismatch".to_string());
    }
    for (claim, code) in FAILURE_CASES {
        let observation_value = items
            .iter()
            .filter_map(Value::as_object)
            .find(|item| python_text(item.get("claim")) == claim)
            .map(|item| Value::Object(item.clone()));
        let observation = object_copy(
            observation_value.as_ref(),
            &format!("failure:{claim}"),
            errors,
        );
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
        if !before.is_empty()
            && !pre_state.is_empty()
            && digest(&Value::Object(pre_state.clone())).is_ok_and(|digest| digest != before)
        {
            errors.push(format!("failure-pre-state-digest-mismatch:{claim}"));
        }
        if !after.is_empty()
            && !post_state.is_empty()
            && digest(&Value::Object(post_state.clone())).is_ok_and(|digest| digest != after)
        {
            errors.push(format!("failure-post-state-digest-mismatch:{claim}"));
        }
        if !before.is_empty() && !after.is_empty() && (before != after || pre_state != post_state) {
            errors.push(format!("failure-mutated-state:{claim}"));
        }
        let evidence = object_copy(
            observation.get("input"),
            &format!("failure-input:{claim}"),
            errors,
        );
        match claim {
            "expiredIngressGrantCannotWrite" => {
                let attempted = parse_timestamp(string(&evidence, "attemptedAt"));
                if attempted.is_none() {
                    errors.push("invalid-timestamp:expiredGrant.attemptedAt".to_string());
                }
                let expires = parse_timestamp(string(grant, "expiresAt"));
                if expires.is_none() {
                    errors.push("invalid-timestamp:ingressGrant.expiresAt".to_string());
                }
                if evidence.get("grant") != Some(&Value::Object(grant.clone()))
                    || attempted
                        .zip(expires)
                        .is_none_or(|(attempted, expires)| attempted < expires)
                {
                    errors.push("expired-grant-evidence-invalid".to_string());
                }
            }
            "ingressGrantCannotEscapeObjectPrefix" => {
                let key = evidence.get("objectKey").and_then(Value::as_str);
                if evidence.get("grant") != Some(&Value::Object(grant.clone()))
                    || key.is_none_or(|key| key.starts_with(string(grant, "allowedObjectPrefix")))
                {
                    errors.push("prefix-escape-evidence-invalid".to_string());
                }
            }
            "ingressGrantCannotExceedMaxBytes" => {
                let byte_count = exact_integer(evidence.get("byteCount"));
                let max_bytes = exact_integer(grant.get("maxBytes"));
                if evidence.get("grant") != Some(&Value::Object(grant.clone()))
                    || byte_count
                        .zip(max_bytes)
                        .is_none_or(|(count, maximum)| count <= maximum)
                {
                    errors.push("max-bytes-evidence-invalid".to_string());
                }
            }
            "sameTransferIdDifferentDigestFailsClosed" => {
                let conflict = typed_digest(
                    evidence.get("conflictingObjectSetDigest"),
                    "conflictingObjectSetDigest",
                    errors,
                );
                let mut invalid = evidence.get("transfer")
                    != Some(&Value::Object(transfer.clone()))
                    || conflict.is_empty()
                    || conflict == string(transfer, "objectSetDigest");
                if !invalid {
                    invalid = derive_transfer_id(
                        string(transfer, "sourceFleetId"),
                        string(transfer, "destinationFleetId"),
                        string(transfer, "backupId"),
                        &conflict,
                    )
                    .map_or(true, |derived| derived == string(transfer, "transferId"));
                }
                if invalid {
                    errors.push("transfer-conflict-evidence-invalid".to_string());
                }
            }
            "replayedIngressGrantFailsClosed" => {
                if evidence.get("grant") != Some(&Value::Object(grant.clone()))
                    || evidence.get("replayedGrantId") != grant.get("grantId")
                {
                    errors.push("grant-replay-evidence-invalid".to_string());
                }
            }
            "tamperedReplicaAttestationFailsClosed" => {
                let tampered =
                    object_copy(evidence.get("attestation"), "tampered-attestation", errors);
                let certificate = object_copy(
                    tampered.get("signerCertificate"),
                    "tampered-attestation-certificate",
                    errors,
                );
                if tampered.is_empty()
                    || certificate.is_empty()
                    || verify_federation_document(
                        &Value::Object(tampered),
                        &certificate,
                        &Value::Object(identity.clone()),
                        "federated-replica-attestation-v1",
                        validated_at_text,
                        PURPOSE_REPLICA_ATTESTATION,
                    )
                    .is_ok()
                {
                    errors.push("tampered-attestation-evidence-invalid".to_string());
                }
            }
            _ => {}
        }
    }
}

type ReplicaParts = (
    ReceiptV4,
    ReplicaAttestation,
    FailureDomainMetadata,
    ReplicaTransferBinding,
    CurrentSignerAuthorization,
    Value,
);

fn replica_context_parts(
    source_receipt: &Map<String, Value>,
    transfer: &Map<String, Value>,
    attestation: &Map<String, Value>,
) -> Result<ReplicaParts, &'static str> {
    let mut metadata = Map::new();
    for (field, value) in [
        ("provider", "placeholder"),
        ("region", "placeholder"),
        ("jurisdiction", "placeholder"),
        ("siteClass", "placeholder"),
    ] {
        metadata.insert(field.to_string(), Value::String(value.to_string()));
    }
    replica_context_parts_with(
        source_receipt,
        transfer,
        attestation,
        &metadata,
        &Map::new(),
    )
}

fn replica_context_parts_with(
    source_receipt: &Map<String, Value>,
    transfer: &Map<String, Value>,
    attestation: &Map<String, Value>,
    pinned_metadata: &Map<String, Value>,
    identity: &Map<String, Value>,
) -> Result<ReplicaParts, &'static str> {
    let source: ReceiptV4 = serde_json::from_value(Value::Object(source_receipt.clone()))
        .map_err(|_| "FEDERATION_REPLICA_SOURCE_RECEIPT_V4_INVALID")?;
    let mut normalized_attestation = attestation.clone();
    if normalized_attestation
        .get("signerCertificate")
        .and_then(Value::as_object)
        .is_none()
    {
        normalized_attestation.insert("signerCertificate".to_string(), Value::Object(Map::new()));
    }
    let replica: ReplicaAttestation = serde_json::from_value(Value::Object(normalized_attestation))
        .map_err(|_| "FEDERATION_REPLICA_ATTESTATION_INVALID")?;
    let metadata: FailureDomainMetadata =
        serde_json::from_value(Value::Object(pinned_metadata.clone()))
            .map_err(|_| "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_METADATA_INVALID")?;
    let binding = ReplicaTransferBinding {
        backup_id: string(transfer, "backupId").to_string(),
        destination_fleet_id: string(transfer, "destinationFleetId").to_string(),
        object_set_digest: string(transfer, "objectSetDigest").to_string(),
        policy_id: string(transfer, "policyId").to_string(),
        source_fleet_id: string(transfer, "sourceFleetId").to_string(),
        transfer_id: string(transfer, "transferId").to_string(),
    };
    let certificate = replica.signer_certificate.clone();
    let authorization = CurrentSignerAuthorization {
        active: true,
        certificate_digest: digest(&Value::Object(certificate)).unwrap_or_default(),
        signer_key_id: replica.signer_key_id.clone(),
    };
    Ok((
        source,
        replica,
        metadata,
        binding,
        authorization,
        Value::Object(identity.clone()),
    ))
}

fn metadata_failure_domain(metadata: &Map<String, Value>) -> Result<String, String> {
    let typed: FailureDomainMetadata = serde_json::from_value(Value::Object(metadata.clone()))
        .map_err(|error| error.to_string())?;
    failure_domain_from_metadata(&typed).map_err(|error| error.to_string())
}

fn validate_legacy_wire_value(evidence: &Value, field: &str, expected: &Value) -> Vec<String> {
    let Some(fields) = evidence.as_object() else {
        return vec!["not-a-dict".to_string()];
    };
    let mut errors = Vec::new();
    if fields
        .get(field)
        .is_none_or(|value| value.is_null() || value.as_str() == Some(""))
    {
        errors.push(format!("missing-field:{field}"));
    }
    if fields.contains_key(field) && fields.get(field) != Some(expected) {
        errors.push(format!("frozen-wire-value-mismatch:{field}"));
    }
    errors
}

fn object_copy(value: Option<&Value>, field: &str, errors: &mut Vec<String>) -> Map<String, Value> {
    match value.and_then(Value::as_object) {
        Some(value) => value.clone(),
        None => {
            errors.push(format!("{field}-must-be-object"));
            Map::new()
        }
    }
}

fn exact_fields(fields: &Map<String, Value>, expected: &[&str]) -> bool {
    fields.len() == expected.len() && expected.iter().all(|field| fields.contains_key(*field))
}

fn string<'a>(fields: &'a Map<String, Value>, field: &str) -> &'a str {
    fields.get(field).and_then(Value::as_str).unwrap_or("")
}

fn python_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(value)) => value.clone(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::Number(value)) if value.as_f64() == Some(0.0) => String::new(),
        Some(Value::Number(value)) => value.to_string(),
        _ => String::new(),
    }
}

fn canonical_bytes(value: &Value) -> Result<Vec<u8>, String> {
    let mut normalized = value.clone();
    normalized.sort_all_objects();
    serde_json::to_vec(&normalized).map_err(|error| error.to_string())
}

fn digest(value: &Value) -> Result<String, String> {
    Ok(format!(
        "sha256:{:x}",
        Sha256::digest(canonical_bytes(value)?)
    ))
}

fn typed_digest(value: Option<&Value>, field: &str, errors: &mut Vec<String>) -> String {
    match value
        .and_then(Value::as_str)
        .filter(|value| is_typed_digest(value))
    {
        Some(value) => value.to_string(),
        None => {
            errors.push(format!("invalid-sha256:{field}"));
            String::new()
        }
    }
}

fn is_typed_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn decode_document(value: Option<&Value>, field: &str, errors: &mut Vec<String>) -> Vec<u8> {
    let Some(encoded) = value.and_then(Value::as_str) else {
        errors.push(format!("invalid-base64:{field}"));
        return Vec::new();
    };
    match STANDARD.decode(encoded) {
        Ok(decoded) if decoded.is_empty() => {
            errors.push(format!("empty-bytes:{field}"));
            decoded
        }
        Ok(decoded) => decoded,
        Err(_) => {
            errors.push(format!("invalid-base64:{field}"));
            Vec::new()
        }
    }
}

fn positive_integer(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_u64)
        .is_some_and(|value| value >= 1)
}

fn exact_integer(value: Option<&Value>) -> Option<i128> {
    value.and_then(|value| {
        value
            .as_i64()
            .map(i128::from)
            .or_else(|| value.as_u64().map(i128::from))
    })
}

fn positive_bounded_i64(value: Option<&Value>) -> bool {
    exact_integer(value).is_some_and(|value| (1..=i128::from(i64::MAX)).contains(&value))
}

fn python_zero(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Bool(false)))
        || value
            .and_then(Value::as_f64)
            .is_some_and(|number| number == 0.0)
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

fn is_grant_id(value: &str) -> bool {
    value.strip_prefix("grant-").is_some_and(|suffix| {
        suffix.len() == 32
            && suffix
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn is_nonce(value: &str) -> bool {
    !value.is_empty()
        && !value.contains('=')
        && URL_SAFE_NO_PAD
            .decode(value)
            .is_ok_and(|decoded| decoded.len() == 32 && URL_SAFE_NO_PAD.encode(decoded) == value)
}

fn is_object_prefix(value: &str) -> bool {
    if !value.starts_with("federation/") || !value.ends_with('/') {
        return false;
    }
    let lowered = value.to_ascii_lowercase();
    !value.contains('\\')
        && !value.contains("//")
        && !value
            .chars()
            .any(|character| character < '\u{20}' || character == '\u{7f}')
        && !["%2e", "%2f", "%5c"]
            .iter()
            .any(|encoded| lowered.contains(encoded))
        && !value
            .split('/')
            .filter(|segment| !segment.is_empty())
            .any(|segment| matches!(segment, "." | ".."))
}

fn transfer_state_payload(
    transfer_id: &str,
    state: &str,
    details: &Map<String, Value>,
) -> Result<String, ()> {
    if contains_sensitive_state(&Value::Object(details.clone())) {
        return Err(());
    }
    let details_value = Value::Object(details.clone());
    if canonical_bytes(&details_value).map_err(|_| ())?.len() > 64 * 1024 {
        return Err(());
    }
    digest(&json!({
        "schema": "federated-transfer-state-v1",
        "transferId": transfer_id,
        "state": state,
        "details": details,
    }))
    .map_err(|_| ())
}

fn contains_sensitive_state(value: &Value) -> bool {
    match value {
        Value::Object(fields) => fields.iter().any(|(key, value)| {
            let normalized: String = key
                .chars()
                .flat_map(char::to_lowercase)
                .filter(char::is_ascii_alphanumeric)
                .collect();
            SENSITIVE_STATE_MARKERS
                .iter()
                .any(|marker| normalized.contains(marker))
                || contains_sensitive_state(value)
        }),
        Value::Array(items) => items.iter().any(contains_sensitive_state),
        _ => false,
    }
}

fn contains_secret(value: &Value) -> bool {
    match value {
        Value::Object(fields) => fields.iter().any(|(key, value)| {
            let normalized: String = key
                .chars()
                .flat_map(char::to_lowercase)
                .filter(char::is_ascii_alphanumeric)
                .collect();
            SECRET_FIELD_NAMES.contains(&normalized.as_str()) || contains_secret(value)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        Value::String(value) => {
            let lowered = value.to_lowercase();
            SECRET_VALUE_MARKERS
                .iter()
                .any(|marker| lowered.contains(marker))
        }
        _ => false,
    }
}

fn parse_timestamp(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() != 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'Z'
    {
        return None;
    }
    let year = decimal(bytes, 0, 4)? as i64;
    let month = decimal(bytes, 5, 2)? as i64;
    let day = decimal(bytes, 8, 2)? as i64;
    let hour = decimal(bytes, 11, 2)? as i64;
    let minute = decimal(bytes, 14, 2)? as i64;
    let second = decimal(bytes, 17, 2)? as i64;
    if year == 0 || !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    if !(1..=days_in_month).contains(&day) {
        return None;
    }
    let adjusted_year = year - i64::from(month <= 2);
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    Some(days * 86_400 + hour * 3_600 + minute * 60 + second)
}

fn decimal(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    bytes
        .get(start..start + length)?
        .iter()
        .try_fold(0_u32, |value, byte| {
            byte.is_ascii_digit()
                .then(|| value * 10 + u32::from(*byte - b'0'))
        })
}

fn dedupe(errors: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    errors
        .into_iter()
        .filter(|error| seen.insert(error.clone()))
        .collect()
}
