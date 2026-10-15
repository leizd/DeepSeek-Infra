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
    signer.verify_retained_node_outcome(&first).unwrap();
    let mut changed = first.clone();
    changed.signature[0] ^= 1;
    assert!(signer.verify_retained_node_outcome(&changed).is_err());
    changed = first.clone();
    changed
        .evidence
        .as_mut()
        .unwrap()
        .request
        .as_mut()
        .unwrap()
        .sha256 = "f".repeat(64);
    assert!(signer.verify_retained_node_outcome(&changed).is_err());
    assert_eq!(first, signer.sign_node_outcome(evidence.clone()).unwrap());
    drop(signer);
    let mut signer = reopen(root.path());
    signer.verify_retained_node_outcome(&first).unwrap();
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
    assert!(signer.verify_retained_node_outcome(&first).is_err());
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
    assert!(signer.verify_retained_node_outcome(&first).is_err());
}

fn synthesis_test_custody(root: &Path) -> ControlSigner {
    fs::create_dir_all(root.join("rust-worker")).unwrap();
    let password = b"isolated-synthesis-password-32-bytes";
    let bundle_path = root.join("fixture-custody.json");
    let bundle: Value = if bundle_path.exists() {
        serde_json::from_slice(&fs::read(&bundle_path).unwrap()).unwrap()
    } else {
        let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
        fs::write(&bundle_path, serde_json::to_vec(&bundle).unwrap()).unwrap();
        bundle
    };
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let binding = String::from_utf8(canonical_json_bytes(&bundle["binding"]).unwrap()).unwrap();
    ControlSigner {
        key: load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap(),
        journal: SignatureJournal::open(root, &binding).unwrap(),
    }
}

#[test]
fn synthesis_outcome_signing_requires_its_role_and_counters_and_recovers_after_restart() {
    use deepseek_protocol::generated::deepseek::agent::v1::AgentTaskOutputMetadata;
    let root = tempfile::tempdir().unwrap();
    let evidence = AgentNodeOutcomeEvidence {
        schema_version: 1,
        fence: Some(ActionFence {
            action_id: "synthesis".into(),
            execution_epoch: 2,
        }),
        operation_id: format!("agent-node-{}", "a".repeat(64)),
        run_id: "run".into(),
        node_id: "synthesizer".into(),
        phase: 3,
        request: Some(AgentArtifactReference {
            sha256: "b".repeat(64),
            length: 20,
        }),
        plan_digest: "c".repeat(64),
        metadata_index: 6,
        metadata_epoch: 1,
        writer_fencing_token: 2,
        created_at: "2026-10-12T00:00:00Z".into(),
        confirmed_at: "2026-10-12T00:00:01Z".into(),
        outcome_sha256: "d".repeat(64),
        outcome_length: 50,
        public_event: Some(AgentArtifactReference {
            sha256: "e".repeat(64),
            length: 30,
        }),
        provider_plan: vec![],
        task_output: Some(AgentTaskOutputMetadata {
            schema_version: 1,
            prompt_tokens: 3,
            completion_tokens: 1,
            latency_ms: 40,
            failed: false,
        }),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
    };
    let mut signer = synthesis_test_custody(root.path());
    let first = signer.sign_node_outcome(evidence.clone()).unwrap();
    assert_eq!(first.signature.len(), 64);
    assert_eq!(first, signer.sign_node_outcome(evidence.clone()).unwrap());
    drop(signer);
    let mut signer = synthesis_test_custody(root.path());
    assert_eq!(first, signer.sign_node_outcome(evidence.clone()).unwrap());
    for case in 0..11 {
        let mut invalid = evidence.clone();
        match case {
            0 => invalid.node_id = "coder".into(),
            1 => invalid.task_output = None,
            2 => invalid.task_output.as_mut().unwrap().schema_version = 2,
            3 => invalid.task_output.as_mut().unwrap().prompt_tokens = u64::MAX,
            4 => invalid.task_output.as_mut().unwrap().completion_tokens = u64::MAX,
            5 => invalid.task_output.as_mut().unwrap().latency_ms = -1,
            6 => invalid.task_output.as_mut().unwrap().failed = true,
            7 => invalid.phase = 4,
            8 => invalid.provider_plan.push(
                deepseek_protocol::generated::deepseek::agent::v1::AgentRunPlanNode {
                    id: "coder".into(),
                    task: "write".into(),
                    depends_on: vec![],
                },
            ),
            9 => invalid.environment = "foreign".into(),
            _ => invalid.fence.as_mut().unwrap().execution_epoch = 0,
        }
        assert_eq!(
            signer.sign_node_outcome(invalid).unwrap_err().message(),
            "AGENT_NODE_OUTCOME_INVALID",
            "case {case}"
        );
    }
}

#[test]
fn synthesis_final_projection_has_its_own_domain_and_retains_signature_on_restart() {
    use deepseek_protocol::generated::deepseek::agent::v1::AgentSynthesisProjectionEvidence;
    let root = tempfile::tempdir().unwrap();
    let evidence = AgentSynthesisProjectionEvidence {
        schema_version: 1,
        source_receipt_sha256: "a".repeat(64),
        settlement_fence: Some(ActionFence {
            action_id: "synthesis".into(),
            execution_epoch: 2,
        }),
        settlement_writer_token: 3,
        run_id: "run".into(),
        metadata_epoch: 1,
        expected_index: 6,
        content_body: Some(AgentArtifactReference {
            sha256: "b".repeat(64),
            length: 30,
        }),
        done_body: Some(AgentArtifactReference {
            sha256: "c".repeat(64),
            length: 15,
        }),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
    };
    let mut signer = synthesis_test_custody(root.path());
    let first = signer.sign_synthesis_projection(evidence.clone()).unwrap();
    assert_eq!(first.signature.len(), 64);
    assert_eq!(
        first,
        signer.sign_synthesis_projection(evidence.clone()).unwrap()
    );
    drop(signer);
    let mut signer = synthesis_test_custody(root.path());
    assert_eq!(
        first,
        signer.sign_synthesis_projection(evidence.clone()).unwrap()
    );
    for case in 0..11 {
        let mut invalid = evidence.clone();
        match case {
            0 => invalid.schema_version = 0,
            1 => invalid.source_receipt_sha256 = "bad".into(),
            2 => invalid.settlement_fence = None,
            3 => invalid.settlement_writer_token = 0,
            4 => invalid.run_id.clear(),
            5 => invalid.metadata_epoch = 0,
            6 => invalid.expected_index = i64::MAX,
            7 => invalid.content_body = None,
            8 => invalid.done_body.as_mut().unwrap().length = 0,
            9 => invalid.fleet_id = "foreign".into(),
            _ => invalid.environment = "foreign".into(),
        }
        assert_eq!(
            signer
                .sign_synthesis_projection(invalid)
                .unwrap_err()
                .message(),
            "AGENT_SYNTHESIS_PROJECTION_INVALID",
            "case {case}"
        );
    }
}
