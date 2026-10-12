use super::*;
use deepseek_federation::create_control_signer_bundle;
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentNodeOutcomeEvidence,
};
use prost::Message as _;

#[test]
fn plan_projection_journal_preserves_derived_signature_after_restart() {
    use deepseek_protocol::generated::deepseek::agent::v1::{
        AgentPlanProjectionEvidence, AgentRunPlanNode,
    };
    let root = tempfile::tempdir().unwrap();
    fs::create_dir(root.path().join("rust-worker")).unwrap();
    let password = b"isolated-projection-password-32-bytes";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let bound = String::from_utf8(canonical_json_bytes(&bundle["binding"]).unwrap()).unwrap();
    let reopen = || ControlSigner {
        key: load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap(),
        journal: SignatureJournal::open(root.path(), &bound).unwrap(),
    };
    let evidence = AgentPlanProjectionEvidence {
        schema_version: 1,
        source_receipt_sha256: "a".repeat(64),
        settlement_fence: Some(ActionFence {
            action_id: "planner".into(),
            execution_epoch: 2,
        }),
        settlement_writer_token: 3,
        run_id: "run".into(),
        metadata_epoch: 1,
        expected_index: 2,
        normalized_plan: vec![AgentRunPlanNode {
            id: "coder".into(),
            task: "write".into(),
            depends_on: vec![],
        }],
        next_status: "running".into(),
        plan_body: Some(AgentArtifactReference {
            sha256: "b".repeat(64),
            length: 20,
        }),
        status_body: Some(AgentArtifactReference {
            sha256: "c".repeat(64),
            length: 30,
        }),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        planning_body: None,
        planning_required: false,
    };
    let mut signer = reopen();
    let first = signer.sign_plan_projection(evidence.clone()).unwrap();
    assert_eq!(first.signature.len(), 64);
    assert_eq!(
        first,
        signer.sign_plan_projection(evidence.clone()).unwrap()
    );
    drop(signer);
    let mut signer = reopen();
    assert_eq!(
        first,
        signer.sign_plan_projection(evidence.clone()).unwrap()
    );
    let mut invalid = evidence.clone();
    invalid.environment = "foreign".into();
    assert_eq!(
        signer.sign_plan_projection(invalid).unwrap_err().message(),
        "AGENT_PLAN_PROJECTION_INVALID"
    );
    let mut invalid = evidence.clone();
    invalid.next_status = "done".into();
    assert_eq!(
        signer.sign_plan_projection(invalid).unwrap_err().message(),
        "AGENT_PLAN_PROJECTION_INVALID"
    );
}

#[test]
fn outcome_signature_journal_is_idempotent_after_restart_and_refuses_corrupt_receipts() {
    let root = tempfile::tempdir().unwrap();
    fs::create_dir(root.path().join("rust-worker")).unwrap();
    let password = b"isolated-control-password-32-bytes";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let bound = String::from_utf8(canonical_json_bytes(&bundle["binding"]).unwrap()).unwrap();
    let reopen = |directory: &Path| ControlSigner {
        key: load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap(),
        journal: SignatureJournal::open(directory, &bound).unwrap(),
    };
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
        task_output: None,
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
    };
    let mut signer = reopen(root.path());
    let first = signer.sign_node_outcome(evidence.clone()).unwrap();
    assert_eq!(first, signer.sign_node_outcome(evidence.clone()).unwrap());
    drop(signer);
    let mut signer = reopen(root.path());
    assert_eq!(first, signer.sign_node_outcome(evidence.clone()).unwrap());
    let mut wrong = evidence.clone();
    wrong.environment = "foreign".into();
    assert_eq!(
        signer.sign_node_outcome(wrong).unwrap_err().message(),
        "AGENT_NODE_OUTCOME_INVALID"
    );
    let corrupted = tempfile::tempdir().unwrap();
    fs::create_dir(corrupted.path().join("rust-worker")).unwrap();
    let mut signer = reopen(corrupted.path());
    let payload = evidence.encode_to_vec();
    let mut identity = b"deepseek-infra:agent-node-outcome-receipt-v1:request\0".to_vec();
    identity.extend(&payload);
    let mut nonce = b"deepseek-infra:agent-node-outcome-receipt-v1:nonce\0".to_vec();
    nonce.extend(&payload);
    let request_id = format!("{:x}", Sha256::digest(&identity));
    let nonce = format!("{:x}", Sha256::digest(&nonce));
    signer
        .journal
        .record(&request_id, &nonce, b"corrupted protobuf receipt")
        .unwrap();
    assert_eq!(
        signer.sign_node_outcome(evidence).unwrap_err().message(),
        "AGENT_NODE_OUTCOME_RECEIPT_CORRUPT"
    );
    assert_eq!(
        signer.journal.existing(&request_id).unwrap().unwrap(),
        b"corrupted protobuf receipt"
    );
}
