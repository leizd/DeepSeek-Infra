use deepseek_federation::{
    CurrentSignerAuthorization, FailureDomainMetadata, ReplicaAttestation, ReplicaTransferBinding,
    ReplicaVerificationContext, attestation_digest, derive_transfer_id,
    failure_domain_from_metadata, verify_replica_attestation, verify_replica_attestation_document,
};
use deepseek_storage::{CommitV4, ReceiptV4};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    now: String,
    max_future_skew_seconds: u64,
    root_identity: Value,
    signer_authorization: CurrentSignerAuthorization,
    pinned_metadata: FailureDomainMetadata,
    transfer: ReplicaTransferBinding,
    source_receipt: ReceiptV4,
    remote_receipt: ReceiptV4,
    remote_commit: CommitV4,
    attestation: ReplicaAttestation,
    attestation_digest: String,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v3/federation/replica_attestation_vector.json"
    )))
    .unwrap()
}

impl Fixture {
    fn context<'a>(
        &'a self,
        remote_receipt_bytes: &'a [u8],
        remote_commit_bytes: &'a [u8],
    ) -> ReplicaVerificationContext<'a> {
        ReplicaVerificationContext {
            root_identity: &self.root_identity,
            signer_authorization: &self.signer_authorization,
            pinned_metadata: &self.pinned_metadata,
            transfer: &self.transfer,
            source_receipt: &self.source_receipt,
            remote_receipt_bytes,
            remote_commit_bytes,
            now: &self.now,
            max_future_skew_seconds: self.max_future_skew_seconds,
        }
    }
}

#[test]
fn rust_verifies_the_python_4_8_0_replica_attestation() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        failure_domain_from_metadata(&fixture.pinned_metadata).unwrap(),
        fixture.attestation.failure_domain
    );
    assert_eq!(
        derive_transfer_id(
            &fixture.transfer.source_fleet_id,
            &fixture.transfer.destination_fleet_id,
            &fixture.transfer.backup_id,
            &fixture.transfer.object_set_digest,
        )
        .unwrap(),
        fixture.transfer.transfer_id
    );
    assert_eq!(
        attestation_digest(&fixture.attestation).unwrap(),
        fixture.attestation_digest
    );

    let remote_receipt_bytes = fixture.remote_receipt.canonical_bytes().unwrap();
    let remote_commit_bytes = fixture.remote_commit.canonical_bytes().unwrap();
    let context = fixture.context(&remote_receipt_bytes, &remote_commit_bytes);
    verify_replica_attestation(&fixture.attestation, &context).unwrap();
    let document = serde_json::to_vec(&fixture.attestation).unwrap();
    assert_eq!(
        verify_replica_attestation_document(&document, &context).unwrap(),
        fixture.attestation
    );
}

#[test]
fn verifier_fails_closed_on_signature_time_domain_and_remote_bytes() {
    let fixture = fixture();
    let remote_receipt_bytes = fixture.remote_receipt.canonical_bytes().unwrap();
    let remote_commit_bytes = fixture.remote_commit.canonical_bytes().unwrap();
    let mut bad_signature = fixture.attestation.clone();
    bad_signature.signature.replace_range(..1, "A");
    let error = verify_replica_attestation(
        &bad_signature,
        &fixture.context(&remote_receipt_bytes, &remote_commit_bytes),
    )
    .unwrap_err();
    assert_eq!(error.code(), "FEDERATION_DOCUMENT_SIGNATURE_INVALID");

    let mut wrong_schema = fixture.attestation.clone();
    wrong_schema.schema = "federated-replica-attestation-v2".to_string();
    let error = verify_replica_attestation(
        &wrong_schema,
        &fixture.context(&remote_receipt_bytes, &remote_commit_bytes),
    )
    .unwrap_err();
    assert_eq!(error.code(), "FEDERATION_DOCUMENT_SCHEMA_INVALID");

    let error = verify_replica_attestation(
        &fixture.attestation,
        &ReplicaVerificationContext {
            now: &fixture.attestation.expires_at,
            ..fixture.context(&remote_receipt_bytes, &remote_commit_bytes)
        },
    )
    .unwrap_err();
    assert_eq!(error.code(), "FEDERATION_REPLICA_ATTESTATION_EXPIRED");

    let wrong_metadata = FailureDomainMetadata {
        region: "different-region".to_string(),
        ..fixture.pinned_metadata.clone()
    };
    let error = verify_replica_attestation(
        &fixture.attestation,
        &ReplicaVerificationContext {
            pinned_metadata: &wrong_metadata,
            ..fixture.context(&remote_receipt_bytes, &remote_commit_bytes)
        },
    )
    .unwrap_err();
    assert_eq!(
        error.code(),
        "FEDERATION_REPLICA_ATTESTATION_FAILURE_DOMAIN_MISMATCH"
    );

    let mut changed_commit = fixture.remote_commit.clone();
    changed_commit.target_generation = 2;
    changed_commit.commit_hash = changed_commit.compute_hash().unwrap();
    let changed_commit_bytes = changed_commit.canonical_bytes().unwrap();
    let error = verify_replica_attestation(
        &fixture.attestation,
        &fixture.context(&remote_receipt_bytes, &changed_commit_bytes),
    )
    .unwrap_err();
    assert_eq!(
        error.code(),
        "FEDERATION_REPLICA_ATTESTATION_REMOTE_COMMIT_DIGEST_MISMATCH"
    );
}

#[test]
fn verifier_requires_the_current_pinned_signer_and_certificate_chain() {
    let fixture = fixture();
    let remote_receipt_bytes = fixture.remote_receipt.canonical_bytes().unwrap();
    let remote_commit_bytes = fixture.remote_commit.canonical_bytes().unwrap();

    let mut revoked = fixture.signer_authorization.clone();
    revoked.active = false;
    let error = verify_replica_attestation(
        &fixture.attestation,
        &ReplicaVerificationContext {
            signer_authorization: &revoked,
            ..fixture.context(&remote_receipt_bytes, &remote_commit_bytes)
        },
    )
    .unwrap_err();
    assert_eq!(error.code(), "FEDERATION_SIGNER_REVOKED");

    let mut conflicting = fixture.signer_authorization.clone();
    conflicting.certificate_digest = format!("sha256:{}", "f".repeat(64));
    let error = verify_replica_attestation(
        &fixture.attestation,
        &ReplicaVerificationContext {
            signer_authorization: &conflicting,
            ..fixture.context(&remote_receipt_bytes, &remote_commit_bytes)
        },
    )
    .unwrap_err();
    assert_eq!(error.code(), "FEDERATION_SIGNER_CERTIFICATE_CONFLICT");

    let mut bad_certificate = fixture.attestation.clone();
    bad_certificate
        .signer_certificate
        .insert("rootSignature".to_string(), Value::String("A".repeat(86)));
    let error = verify_replica_attestation(
        &bad_certificate,
        &fixture.context(&remote_receipt_bytes, &remote_commit_bytes),
    )
    .unwrap_err();
    assert_eq!(
        error.code(),
        "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID"
    );

    let mut secret_bearing = fixture.attestation.clone();
    secret_bearing.signer_certificate.insert(
        "privateKey".to_string(),
        Value::String("must-never-cross-the-wire".to_string()),
    );
    let error = verify_replica_attestation(
        &secret_bearing,
        &fixture.context(&remote_receipt_bytes, &remote_commit_bytes),
    )
    .unwrap_err();
    assert_eq!(error.code(), "FEDERATION_DOCUMENT_CONTAINS_SECRET");
}

#[test]
fn attestation_rejects_unknown_top_level_fields() {
    let fixture = fixture();
    let remote_receipt_bytes = fixture.remote_receipt.canonical_bytes().unwrap();
    let remote_commit_bytes = fixture.remote_commit.canonical_bytes().unwrap();
    let context = fixture.context(&remote_receipt_bytes, &remote_commit_bytes);
    let mut value = serde_json::to_value(&fixture.attestation).unwrap();
    value["unexpected"] = serde_json::json!(true);
    assert!(serde_json::from_value::<ReplicaAttestation>(value.clone()).is_err());
    assert_eq!(
        verify_replica_attestation_document(&serde_json::to_vec(&value).unwrap(), &context)
            .unwrap_err()
            .code(),
        "FEDERATION_REPLICA_ATTESTATION_FIELDS_INVALID"
    );
}

#[test]
fn document_entrypoint_bounds_input_before_json_parsing() {
    let fixture = fixture();
    let remote_receipt_bytes = fixture.remote_receipt.canonical_bytes().unwrap();
    let remote_commit_bytes = fixture.remote_commit.canonical_bytes().unwrap();
    let context = fixture.context(&remote_receipt_bytes, &remote_commit_bytes);
    assert_eq!(
        verify_replica_attestation_document(b"not-json", &context)
            .unwrap_err()
            .code(),
        "FEDERATION_REPLICA_ATTESTATION_INVALID"
    );
    let oversized = vec![b' '; deepseek_federation::MAX_REPLICA_ATTESTATION_BYTES + 1];
    assert_eq!(
        verify_replica_attestation_document(&oversized, &context)
            .unwrap_err()
            .code(),
        "FEDERATION_REPLICA_ATTESTATION_TOO_LARGE"
    );
}

#[test]
fn verifier_rejects_an_invalid_transfer_policy_id() {
    let mut fixture = fixture();
    let remote_receipt_bytes = fixture.remote_receipt.canonical_bytes().unwrap();
    let remote_commit_bytes = fixture.remote_commit.canonical_bytes().unwrap();
    fixture.transfer.policy_id = "policy/escape".to_string();
    assert_eq!(
        verify_replica_attestation(
            &fixture.attestation,
            &fixture.context(&remote_receipt_bytes, &remote_commit_bytes),
        )
        .unwrap_err()
        .code(),
        "FEDERATION_TRANSFER_POLICY_ID_INVALID"
    );
}
