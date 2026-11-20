use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_federation::{create_control_signer_bundle, load_control_signer};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRecoveryProgressContext, AgentRecoveryProgressEvidence,
    AgentRunRecoveryEffectSource,
};
use ed25519_dalek::{Signature, VerifyingKey};
use prost::Message as _;

#[test]
fn recovery_progress_has_distinct_custody_and_binds_every_progress_decision() {
    let password = b"isolated-recovery-progress-password-32-bytes";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let key = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
    let reference = || {
        Some(AgentArtifactReference {
            sha256: "a".repeat(64),
            length: 40,
        })
    };
    let evidence = AgentRecoveryProgressEvidence {
        schema_version: 1,
        context: Some(AgentRecoveryProgressContext {
            schema_version: 1,
            run_id: "run".into(),
            generation: 2,
            metadata_epoch: 3,
            expected_index: 12,
            runtime_revision: 4,
            request: reference(),
            recovery_context_sha256: "b".repeat(64),
            task_action_id: "original-task".into(),
            task_action_revision: 7,
            task_metadata_index: 12,
            role: "critic".into(),
            synthesize: true,
            outputs: vec![AgentRunRecoveryEffectSource::default()],
        }),
        note_body: reference(),
        next_body: reference(),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
    };
    let bytes: [u8; 32] = URL_SAFE_NO_PAD.decode(public).unwrap().try_into().unwrap();
    let verifier = VerifyingKey::from_bytes(&bytes).unwrap();
    let signature =
        Signature::from_slice(&key.sign_agent_recovery_progress(&evidence).unwrap()).unwrap();
    let signed = |domain: &[u8], value: &AgentRecoveryProgressEvidence| {
        let mut bytes = domain.to_vec();
        bytes.extend(value.encode_to_vec());
        bytes
    };
    verifier
        .verify_strict(
            &signed(
                b"deepseek-infra:agent-recovery-progress-proof-v1\0",
                &evidence,
            ),
            &signature,
        )
        .unwrap();
    assert!(
        verifier
            .verify_strict(
                &signed(b"deepseek-infra:agent-node-outcome-proof-v1\0", &evidence),
                &signature
            )
            .is_err()
    );
    for change in [
        "synthesize",
        "task-revision",
        "role",
        "note",
        "next",
        "source",
    ] {
        let mut different = evidence.clone();
        match change {
            "synthesize" => different.context.as_mut().unwrap().synthesize = false,
            "task-revision" => different.context.as_mut().unwrap().task_action_revision += 1,
            "role" => different.context.as_mut().unwrap().role = "coder".into(),
            "note" => different.note_body.as_mut().unwrap().length += 1,
            "next" => different.next_body.as_mut().unwrap().length += 1,
            "source" => {
                different.context.as_mut().unwrap().outputs[0].settlement_metadata_index += 1
            }
            _ => unreachable!(),
        }
        assert!(
            verifier
                .verify_strict(
                    &signed(
                        b"deepseek-infra:agent-recovery-progress-proof-v1\0",
                        &different
                    ),
                    &signature
                )
                .is_err()
        );
    }
    for change in [
        "context",
        "schema",
        "generation",
        "epoch",
        "cursor",
        "runtime",
        "request",
        "digest",
        "action",
        "revision",
        "task-cursor",
        "role",
        "outputs",
        "note",
        "next",
        "fleet",
        "environment",
        "size",
    ] {
        let mut bad = evidence.clone();
        let context = bad.context.as_mut().unwrap();
        match change {
            "context" => bad.context = None,
            "schema" => context.schema_version = 0,
            "generation" => context.generation = 0,
            "epoch" => context.metadata_epoch = 0,
            "cursor" => context.expected_index = i64::MAX,
            "runtime" => context.runtime_revision = 1,
            "request" => context.request = None,
            "digest" => context.recovery_context_sha256 = "bad".into(),
            "action" => context.task_action_id.clear(),
            "revision" => context.task_action_revision = 0,
            "task-cursor" => context.task_metadata_index += 1,
            "role" => context.role = "leader".into(),
            "outputs" => context.outputs.clear(),
            "note" => bad.note_body = None,
            "next" => bad.next_body = None,
            "fleet" => bad.fleet_id = "foreign".into(),
            "environment" => bad.environment = "foreign".into(),
            "size" => context.run_id = "x".repeat(1024 * 1024),
            _ => unreachable!(),
        }
        assert!(key.sign_agent_recovery_progress(&bad).is_err(), "{change}");
    }
}
