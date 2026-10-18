use deepseek_federation::{create_control_signer_bundle, load_control_signer};
use serde_json::json;

#[test]
fn node_outcome_signature_binds_typed_scope_and_fixed_domain() {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use deepseek_protocol::ActionFence;
    use deepseek_protocol::generated::deepseek::agent::v1::{
        AgentArtifactReference, AgentNodeOutcomeEvidence,
    };
    use ed25519_dalek::{Signature, VerifyingKey};
    use prost::Message as _;
    let password = b"isolated-control-password-32-bytes";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let key = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
    let evidence = AgentNodeOutcomeEvidence {
        schema_version: 1,
        fence: Some(ActionFence {
            action_id: "agent-plan".into(),
            execution_epoch: 2,
        }),
        operation_id: format!("agent-node-{}", "a".repeat(64)),
        run_id: "run".into(),
        node_id: "leader".into(),
        phase: 1,
        request: Some(AgentArtifactReference {
            sha256: "b".repeat(64),
            length: 1,
        }),
        plan_digest: "c".repeat(64),
        metadata_index: 4,
        metadata_epoch: 3,
        writer_fencing_token: 7,
        created_at: "2026-09-04T00:01:00Z".into(),
        confirmed_at: "2026-09-04T00:01:01Z".into(),
        outcome_sha256: "d".repeat(64),
        outcome_length: 100,
        public_event: Some(AgentArtifactReference {
            sha256: "e".repeat(64),
            length: 1,
        }),
        provider_plan: vec![],
        failure: None,
        task_output: None,
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
    };
    let signature = key.sign_agent_node_outcome(&evidence).unwrap();
    assert_eq!(signature, key.sign_agent_node_outcome(&evidence).unwrap());
    let bytes: [u8; 32] = URL_SAFE_NO_PAD.decode(public).unwrap().try_into().unwrap();
    let verifier = VerifyingKey::from_bytes(&bytes).unwrap();
    let signature = Signature::from_slice(&signature).unwrap();
    let mut signed = b"deepseek-infra:agent-node-outcome-proof-v1\0".to_vec();
    signed.extend(evidence.encode_to_vec());
    verifier.verify_strict(&signed, &signature).unwrap();
    assert!(
        verifier
            .verify_strict(&evidence.encode_to_vec(), &signature)
            .is_err()
    );
    let mut wrong_domain = b"deepseek-infra:control-agent-execution-grant-v1\0".to_vec();
    wrong_domain.extend(evidence.encode_to_vec());
    assert!(verifier.verify_strict(&wrong_domain, &signature).is_err());
    for field in [
        "fleet",
        "environment",
        "node",
        "digest",
        "length",
        "request",
        "writer",
        "time",
        "payload",
    ] {
        let mut invalid = evidence.clone();
        match field {
            "fleet" => invalid.fleet_id = "foreign".into(),
            "environment" => invalid.environment = "foreign".into(),
            "node" => invalid.node_id = "coder".into(),
            "digest" => invalid.outcome_sha256 = "D".repeat(64),
            "length" => invalid.outcome_length = 1024 * 1024 + 1,
            "request" => invalid.request = None,
            "writer" => invalid.writer_fencing_token = 0,
            "time" => invalid.created_at.clear(),
            _ => invalid.provider_plan.push(
                deepseek_protocol::generated::deepseek::agent::v1::AgentRunPlanNode {
                    id: "coder".into(),
                    task: "x".repeat(1024 * 1024),
                    depends_on: vec![],
                },
            ),
        }
        assert!(
            key.sign_agent_node_outcome(&invalid).is_err(),
            "signed invalid {field}"
        );
    }
}

#[test]
fn encrypted_control_key_is_random_bound_and_secret_free() {
    let password = b"isolated-control-password-32-bytes";
    let first = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let second = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    assert_ne!(
        first["binding"]["signerPublicKey"],
        second["binding"]["signerPublicKey"]
    );
    let public = first["binding"]["signerPublicKey"].as_str().unwrap();
    let signer = load_control_signer(&first, password, "fleet-a", "test", public).unwrap();
    assert_eq!(signer.public_key(), public);
    let debug = format!("{signer:?}");
    assert!(!debug.contains("password"));
    assert!(!debug.contains("ciphertext"));
    assert!(
        signer
            .sign_control_document(&json!({"schema":"control-authority-request-v1"}))
            .is_ok()
    );
    assert!(
        signer
            .sign_control_document(&json!({"schema":"control-storage-operation-grant-v1"}))
            .is_ok()
    );
    assert!(
        signer
            .sign_control_document(&json!({"schema":"fleet-identity-v1"}))
            .is_err()
    );
    for (fleet, environment, key) in [
        ("fleet-b", "test", public),
        ("fleet-a", "prod", public),
        ("fleet-a", "test", "invalid"),
    ] {
        assert!(load_control_signer(&first, password, fleet, environment, key).is_err());
    }
    assert!(
        load_control_signer(
            &first,
            b"different-control-password",
            "fleet-a",
            "test",
            public
        )
        .is_err()
    );
    assert!(create_control_signer_bundle(b"short", "fleet-a", "test").is_err());
    assert!(create_control_signer_bundle(password, "../fleet", "test").is_err());
    let mut tampered = first.clone();
    tampered["privateKeyEnvelope"]["bindingDigest"] = json!("sha256:wrong");
    assert!(load_control_signer(&tampered, password, "fleet-a", "test", public).is_err());
}
