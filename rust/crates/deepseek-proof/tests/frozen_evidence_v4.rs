use deepseek_proof::{
    EVIDENCE_ENVELOPE_SCHEMA, EvidenceProofEnvelope, EvidenceProofError,
    parse_evidence_proof_document, validate_check, verify_evidence_proof_document,
};
use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    valid_envelope: Value,
    invalid_checks: Vec<InvalidCheck>,
}

#[derive(Debug, Deserialize)]
struct InvalidCheck {
    check_name: String,
    item: Value,
    expected_errors: Vec<String>,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v4/evidence/evidence_proof_v2_vector.json"
    )))
    .unwrap()
}

#[test]
fn rust_replays_the_frozen_python_evidence_v2_envelope() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    let bytes = serde_json::to_vec(&fixture.valid_envelope).unwrap();
    let envelope =
        verify_evidence_proof_document(&bytes, Some("native-evidence-proof-parity")).unwrap();
    assert_eq!(envelope.schema, EVIDENCE_ENVELOPE_SCHEMA);
    assert_eq!(envelope.checks.len(), 1);

    for invalid in fixture.invalid_checks {
        assert_eq!(
            validate_check(&invalid.check_name, &invalid.item),
            invalid.expected_errors
        );
    }
}

#[test]
fn document_parser_is_bounded_and_structurally_strict() {
    let oversized = vec![b' '; deepseek_proof::MAX_EVIDENCE_PROOF_BYTES + 1];
    assert_eq!(
        parse_evidence_proof_document(&oversized, None)
            .unwrap_err()
            .code(),
        "EVIDENCE_PROOF_TOO_LARGE"
    );
    assert_eq!(
        parse_evidence_proof_document(b"[]", None)
            .unwrap_err()
            .code(),
        "EVIDENCE_PROOF_MUST_BE_OBJECT"
    );
    assert_eq!(
        parse_evidence_proof_document(
            br#"{"schema":"evidence-proof-v2","checks":[],"meta":{},"scenario":"x"}"#,
            None,
        )
        .unwrap_err()
        .code(),
        "EVIDENCE_PROOF_CHECKS_REQUIRED"
    );
}

#[test]
fn unsupported_checks_and_empty_proofs_fail_closed() {
    let fixture = fixture();
    let mut envelope: EvidenceProofEnvelope =
        serde_json::from_value(fixture.valid_envelope).unwrap();
    envelope.schema = "evidence-proof-v1".to_string();
    assert_eq!(
        deepseek_proof::validate_evidence_proof(&envelope)
            .unwrap_err()
            .code(),
        "EVIDENCE_PROOF_SCHEMA_MISMATCH"
    );
    envelope.schema = EVIDENCE_ENVELOPE_SCHEMA.to_string();
    envelope.checks.clear();
    assert!(matches!(
        deepseek_proof::validate_evidence_proof(&envelope),
        Err(EvidenceProofError::SemanticInvalid(_))
    ));

    assert_eq!(
        validate_check(
            "notYetMigratedCriticalClaim",
            &serde_json::json!({"status": "PASS", "evidence": {"claimed": true}}),
        ),
        vec!["unsupported-check:notYetMigratedCriticalClaim"]
    );
}
