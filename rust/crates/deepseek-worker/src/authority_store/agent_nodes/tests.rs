use super::*;
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::generated::deepseek::agent::v1::AgentArtifactReference;
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

mod recovery_custody;
mod synthesis_executor;
mod task_executor;

const NOW: &str = "2026-09-04T00:01:00Z";

struct Fixture {
    root: tempfile::TempDir,
    config: WorkerAuthorityConfig,
    response: BeginAgentNodeDispatchResponse,
    grant: Vec<u8>,
    epoch: Vec<u8>,
}

fn signed(mut document: Value, domain: &[u8]) -> Vec<u8> {
    // Published RFC 8032 fixture, the same non-runtime key as v7/v31 corpora.
    let seed_hex = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
    let mut seed = [0_u8; 32];
    for (index, pair) in seed_hex.as_bytes().chunks_exact(2).enumerate() {
        seed[index] = u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap();
    }
    let object = document.as_object_mut().unwrap();
    object.remove("signature");
    object.remove("digest");
    document["payloadDigest"] = format!(
        "sha256:{}",
        agent_node_effect::digest(
            &authority_request::canonical_json_bytes(&document["payload"]).unwrap()
        )
    )
    .into();
    document["digest"] = format!(
        "sha256:{}",
        agent_node_effect::digest(&authority_request::canonical_json_bytes(&document).unwrap())
    )
    .into();
    let mut bytes = domain.to_vec();
    bytes.extend(authority_request::canonical_json_bytes(&document).unwrap());
    document["signature"] = URL_SAFE_NO_PAD
        .encode(SigningKey::from_bytes(&seed).sign(&bytes).to_bytes())
        .into();
    authority_request::canonical_json_bytes(&document).unwrap()
}

impl Fixture {
    fn new() -> Self {
        let corpus: Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
        )))
        .unwrap();
        let seconds = authority_request::parse_utc_z_str(NOW).unwrap();
        let fence = ActionFence {
            action_id: format!(
                "agent-exec-{:x}",
                Sha256::digest(b"agent-run-execution-v1\nrun_rpc\ntasks\n4\n")
            ),
            execution_epoch: 4,
        };
        let config = WorkerAuthorityConfig {
            signer_public_key: corpus["signer_public_key"].as_str().unwrap().into(),
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 4,
            now: Some(NOW.into()),
        };
        let claim = AgentRunExecutionClaim {
            fence: Some(fence.clone()),
            run_id: "run_rpc".into(),
            phase: 2,
            request: Some(AgentArtifactReference {
                sha256: "b".repeat(64),
                length: 100,
            }),
            plan_digest: "a".repeat(64),
            metadata_index: 4,
            metadata_epoch: 3,
            owner: "rust-agent-worker".into(),
            claim_token: "fixture-claim-token".into(),
            lease_until: seconds + 120,
            claim_revision: 1,
            writer_fencing_token: 4,
            writer_lease_until: seconds + 300,
            state: "EXECUTING".into(),
            reconciliation_required: false,
        };
        let mut intent = AgentNodeDispatchIntent {
            schema_version: 1,
            operation_id: String::new(),
            fence: Some(fence.clone()),
            run_id: claim.run_id.clone(),
            node_id: "coder".into(),
            phase: claim.phase,
            task: "write <&> 代码\u{2028}\u{2029}".into(),
            depends_on: vec![],
            request: claim.request.clone(),
            plan_digest: claim.plan_digest.clone(),
            metadata_index: claim.metadata_index,
            metadata_epoch: claim.metadata_epoch,
            state: "INTENT_RECORDED".into(),
            recorded_at: seconds,
        };
        intent.operation_id = agent_node_effect::node_operation_id(&intent).unwrap();
        let mut epoch: Value =
            serde_json::from_str(corpus["canonical_request"].as_str().unwrap()).unwrap();
        epoch["actionId"] = fence.action_id.clone().into();
        let epoch = signed(epoch, b"deepseek-infra:control-authority-request-v1\0");
        let payload = crate::agent_execution_grant::claim_payload(&claim).unwrap();
        let grant = signed(
            json!({"schema": crate::AGENT_EXECUTION_GRANT_SCHEMA,"schemaVersion":1,"domain":"action",
            "operation":"execute-agent-phase","runtime":"go","mode":"authoritative","fleetId":"fleet-a","environment":"test",
            "role":"control-plane","fencingToken":4,"actionId":fence.action_id,"executionEpoch":4,"revision":1,
            "issuedAt":"2026-09-04T00:00:30Z","expiresAt":"2026-09-04T00:02:00Z","requestId":"3".repeat(64),"nonce":"4".repeat(64),
            "payload":payload,"signerKeyId":corpus["signer_key_id"],"signatureAlgorithm":"Ed25519"}),
            b"deepseek-infra:control-agent-execution-grant-v1\0",
        );
        Self {
            root: tempfile::tempdir().unwrap(),
            config,
            response: BeginAgentNodeDispatchResponse {
                fresh: true,
                intent: Some(intent),
                claim: Some(claim),
            },
            grant,
            epoch,
        }
    }

    fn worker(&self, install: bool) -> crate::Worker {
        let mut worker =
            crate::Worker::open_with_authority(self.config.clone(), self.root.path()).unwrap();
        if install {
            worker
                .authority_store
                .as_mut()
                .unwrap()
                .install(
                    worker.authority.as_ref().unwrap(),
                    self.response
                        .claim
                        .as_ref()
                        .unwrap()
                        .fence
                        .as_ref()
                        .unwrap(),
                    &self.epoch,
                )
                .unwrap();
        }
        worker
    }

    fn begin(
        &self,
        worker: &mut crate::Worker,
        fresh: bool,
    ) -> Result<AgentNodeEffectStart, Status> {
        let mut response = self.response.clone();
        response.fresh = fresh;
        worker.authority_store.as_mut().unwrap().begin_agent_node(
            &response,
            &self.grant,
            &self.config,
            NOW,
        )
    }

    fn database(&self) -> Connection {
        Connection::open(self.root.path().join("rust-worker/authority.sqlite3")).unwrap()
    }
}

fn started(result: AgentNodeEffectStart) -> AgentNodeEffectPermit {
    match result {
        AgentNodeEffectStart::Started(permit) => permit,
        other => panic!("not a fresh Rust permit: {other:?}"),
    }
}

fn planner_fixture() -> Fixture {
    let mut fixture = Fixture::new();
    let claim = fixture.response.claim.as_mut().unwrap();
    claim.phase = 1;
    claim.fence.as_mut().unwrap().action_id = format!(
        "agent-exec-{:x}",
        Sha256::digest(b"agent-run-execution-v1\nrun_rpc\nplan\n4\n")
    );
    let intent = fixture.response.intent.as_mut().unwrap();
    intent.fence = claim.fence.clone();
    intent.phase = 1;
    intent.node_id = "leader".into();
    intent.task = "plan".into();
    intent.operation_id = agent_node_effect::node_operation_id(intent).unwrap();
    let mut epoch: Value = serde_json::from_slice(&fixture.epoch).unwrap();
    epoch["actionId"] = claim.fence.as_ref().unwrap().action_id.clone().into();
    fixture.epoch = signed(epoch, b"deepseek-infra:control-authority-request-v1\0");
    let mut grant: Value = serde_json::from_slice(&fixture.grant).unwrap();
    grant["actionId"] = claim.fence.as_ref().unwrap().action_id.clone().into();
    grant["payload"] = crate::agent_execution_grant::claim_payload(claim).unwrap();
    fixture.grant = signed(grant, b"deepseek-infra:control-agent-execution-grant-v1\0");
    fixture
}

#[test]
fn outcome_evidence_comes_from_confirmed_journal_and_exact_sanitized_projection() {
    let fixture = planner_fixture();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let query = crate::QueryAgentNodeEffect {
        operation_id: permit.operation_id.clone(),
        fence: permit.fence.clone(),
        run_id: "run_rpc".into(),
        node_id: "leader".into(),
    };
    let event = json!({"type":"agent_plan","plan":{"tasks":[{"role":"coder","task":"implement","depends_on":["researcher"]}]}});
    let mut artifacts = deepseek_policy::agent_run_data::ArtifactStore::open_native(
        fixture.root.path(),
        "python_disabled",
    )
    .unwrap();
    let different_bytes = artifacts.put(&event).unwrap();
    let output =
        authority_request::canonical_json_bytes(&json!({"runId":"run_rpc","publicEvent":event}))
            .unwrap();
    let stored: Value = serde_json::from_slice(&output).unwrap();
    let reference = artifacts.put(&stored["publicEvent"]).unwrap();
    let public = AgentArtifactReference {
        sha256: reference.sha256,
        length: reference.length,
    };
    let store = worker.authority_store.as_mut().unwrap();
    assert!(
        store
            .agent_node_outcome_evidence(&query, public.clone(), &fixture.config)
            .is_err()
    );

    let outcome = store.confirm_agent_node(&permit, &output, NOW).unwrap();
    assert_ne!(different_bytes.sha256, public.sha256);
    assert!(
        store
            .agent_node_outcome_evidence(
                &query,
                AgentArtifactReference {
                    sha256: different_bytes.sha256,
                    length: different_bytes.length,
                },
                &fixture.config
            )
            .is_err()
    );
    let evidence = store
        .agent_node_outcome_evidence(&query, public.clone(), &fixture.config)
        .unwrap();
    assert_eq!(
        evidence.request,
        fixture.response.intent.as_ref().unwrap().request
    );
    assert_eq!(
        evidence.metadata_epoch,
        fixture.response.intent.as_ref().unwrap().metadata_epoch
    );
    assert_eq!(evidence.writer_fencing_token, fixture.config.fencing_token);
    assert_eq!(evidence.outcome_sha256, outcome.output_sha256);
    assert_eq!(evidence.outcome_length, output.len() as u64);
    assert_eq!(evidence.provider_plan[0].id, "coder");
    assert_eq!(evidence.provider_plan[0].depends_on, ["researcher"]);
    let mut rebound = public.clone();
    rebound.length += 1;
    assert!(
        store
            .agent_node_outcome_evidence(&query, rebound, &fixture.config)
            .is_err()
    );
    let mut rebound = public.clone();
    rebound.sha256 = "f".repeat(64);
    assert!(
        store
            .agent_node_outcome_evidence(&query, rebound, &fixture.config)
            .is_err()
    );
    drop(worker);
    let recovered = fixture.worker(false);
    assert_eq!(
        recovered
            .authority_store
            .as_ref()
            .unwrap()
            .agent_node_outcome_evidence(&query, public, &fixture.config)
            .unwrap(),
        evidence
    );
}

#[test]
fn malformed_confirmed_projection_never_produces_outcome_evidence() {
    for output in [
        json!({}),
        json!({"runId":"foreign","publicEvent":{"type":"agent_plan"}}),
        json!({"runId":"run_rpc","publicEvent":{"type":"agent_chunk"}}),
    ] {
        let fixture = planner_fixture();
        let mut worker = fixture.worker(true);
        let permit = started(fixture.begin(&mut worker, true).unwrap());
        let query = crate::QueryAgentNodeEffect {
            operation_id: permit.operation_id.clone(),
            fence: permit.fence.clone(),
            run_id: "run_rpc".into(),
            node_id: "leader".into(),
        };
        let store = worker.authority_store.as_mut().unwrap();
        store
            .confirm_agent_node(
                &permit,
                &authority_request::canonical_json_bytes(&output).unwrap(),
                NOW,
            )
            .unwrap();
        assert!(
            store
                .agent_node_outcome_evidence(
                    &query,
                    AgentArtifactReference {
                        sha256: "a".repeat(64),
                        length: 1
                    },
                    &fixture.config
                )
                .is_err()
        );
    }
}

#[test]
fn provider_send_requires_the_live_stored_grant_and_dispatch_state() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let store = worker.authority_store.as_mut().unwrap();
    assert_eq!(
        store
            .agent_node_send_deadline(&permit, &fixture.config, NOW)
            .unwrap(),
        authority_request::parse_utc_z_str("2026-09-04T00:02:00Z").unwrap()
    );
    for at in [
        "2026-09-04T00:00:59Z",
        "2026-09-04T00:02:00Z",
        "2026-09-04T00:07:00Z",
    ] {
        assert!(
            store
                .agent_node_send_deadline(&permit, &fixture.config, at)
                .is_err(),
            "send admitted at {at}"
        );
    }
    let mut foreign = fixture.config.clone();
    foreign.fencing_token += 1;
    assert!(
        store
            .agent_node_send_deadline(&permit, &foreign, NOW)
            .is_err()
    );
    store.mark_agent_node_unknown(&permit, NOW).unwrap();
    assert!(
        store
            .agent_node_send_deadline(&permit, &fixture.config, NOW)
            .is_err()
    );
    let selector = crate::QueryAgentNodeEffect {
        operation_id: permit.operation_id.clone(),
        fence: permit.fence.clone(),
        run_id: fixture.response.claim.as_ref().unwrap().run_id.clone(),
        node_id: "coder".into(),
    };
    assert!(store.agent_node_outcome(&selector).unwrap().is_none());
    let output = authority_request::canonical_json_bytes(&json!({"answer":"confirmed"})).unwrap();
    let known = store.confirm_agent_node(&permit, &output, NOW).unwrap();
    assert!(
        store
            .agent_node_send_deadline(&permit, &fixture.config, NOW)
            .is_err()
    );
    assert_eq!(store.agent_node_outcome(&selector).unwrap(), Some(known));
    let wrong_run = crate::QueryAgentNodeEffect {
        run_id: "other".into(),
        ..selector
    };
    assert!(store.agent_node_outcome(&wrong_run).is_err());
}

#[test]
fn provider_send_refuses_a_successor_epoch() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let mut epoch: Value = serde_json::from_slice(&fixture.epoch).unwrap();
    epoch["executionEpoch"] = 5.into();
    epoch["requestId"] = "5".repeat(64).into();
    epoch["nonce"] = "6".repeat(64).into();
    let update = signed(epoch, b"deepseek-infra:control-authority-request-v1\0");
    let fence = ActionFence {
        execution_epoch: 5,
        ..permit.fence.clone()
    };
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .install(worker.authority.as_ref().unwrap(), &fence, &update)
        .unwrap();
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .agent_node_send_deadline(&permit, &fixture.config, NOW)
            .is_err()
    );
}

#[test]
fn local_epoch_and_signed_scope_are_required_without_implicit_installation() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(false);
    assert!(fixture.begin(&mut worker, true).is_err());
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT COUNT(*) FROM epoch_installs", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .install(
            worker.authority.as_ref().unwrap(),
            fixture
                .response
                .claim
                .as_ref()
                .unwrap()
                .fence
                .as_ref()
                .unwrap(),
            &fixture.epoch,
        )
        .unwrap();
    let mut grant = fixture.grant.clone();
    let last = grant.len() - 3;
    grant[last] ^= 1;
    assert!(
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .begin_agent_node(&fixture.response, &grant, &fixture.config, NOW)
            .is_err()
    );
    assert!(matches!(
        fixture.begin(&mut worker, true).unwrap(),
        AgentNodeEffectStart::Started(_)
    ));
}

#[test]
fn fresh_go_decision_is_single_use_and_restart_retains_unknown() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    assert_eq!(
        permit.operation_id,
        fixture.response.intent.as_ref().unwrap().operation_id
    );
    assert!(matches!(
        fixture.begin(&mut worker, true).unwrap(),
        AgentNodeEffectStart::EffectUnknown
    ));
    drop(worker);
    let mut worker = fixture.worker(false);
    assert!(matches!(
        fixture.begin(&mut worker, false).unwrap(),
        AgentNodeEffectStart::EffectUnknown
    ));
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT state FROM agent_node_effects", [], |row| row
                .get::<_, String>(0))
            .unwrap(),
        "EFFECT_UNKNOWN"
    );
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn missing_local_record_and_replayed_go_intent_never_authorize_a_call() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    assert!(matches!(
        fixture.begin(&mut worker, false).unwrap(),
        AgentNodeEffectStart::EffectUnknown
    ));
    assert!(matches!(
        fixture.begin(&mut worker, true).unwrap(),
        AgentNodeEffectStart::EffectUnknown
    ));
}

#[test]
fn contended_agent_node_transaction_denies_without_record_or_duplicate_dispatch() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    // Immediate timeout makes contention deterministic without sleeps or retries.
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .connection
        .busy_timeout(std::time::Duration::ZERO)
        .unwrap();
    let mut blocking = fixture.database();
    let lock = blocking
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let error = fixture.begin(&mut worker, true).unwrap_err();
    assert_eq!(error.code(), tonic::Code::FailedPrecondition);
    assert_eq!(error.message(), "AGENT_NODE_EFFECT_DENIED");
    assert_eq!(
        lock.query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    lock.rollback().unwrap();
    assert!(matches!(
        fixture.begin(&mut worker, true).unwrap(),
        AgentNodeEffectStart::Started(_)
    ));
    assert!(matches!(
        fixture.begin(&mut worker, true).unwrap(),
        AgentNodeEffectStart::EffectUnknown
    ));
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn independent_rust_handles_cannot_dispatch_the_same_fresh_decision_twice() {
    let fixture = Fixture::new();
    drop(fixture.worker(true));
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let mut threads = vec![];
    for _ in 0..2 {
        let root = fixture.root.path().to_path_buf();
        let config = fixture.config.clone();
        let response = fixture.response.clone();
        let grant = fixture.grant.clone();
        let barrier = barrier.clone();
        threads.push(std::thread::spawn(move || {
            let mut worker = crate::Worker::open_with_authority(config.clone(), &root).unwrap();
            // This test proves serialization, not the production lock-wait limit.
            // Allow bounded CI scheduling/fsync delay while keeping both real handles.
            worker
                .authority_store
                .as_mut()
                .unwrap()
                .connection
                .busy_timeout(std::time::Duration::from_secs(5))
                .unwrap();
            barrier.wait();
            worker
                .authority_store
                .as_mut()
                .unwrap()
                .begin_agent_node(&response, &grant, &config, NOW)
                .unwrap()
        }));
    }
    let results: Vec<_> = threads
        .into_iter()
        .map(|thread| thread.join().unwrap())
        .collect();
    assert_eq!(
        results
            .iter()
            .filter(|r| matches!(r, AgentNodeEffectStart::Started(_)))
            .count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter(|r| matches!(r, AgentNodeEffectStart::EffectUnknown))
            .count(),
        1
    );
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn known_response_survives_restart_and_replay_cannot_change_it() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let body = authority_request::canonical_json_bytes(
        &json!({"content":"known response","usage":{"total_tokens":12}}),
    )
    .unwrap();
    let store = worker.authority_store.as_mut().unwrap();
    let first = store.confirm_agent_node(&permit, &body, NOW).unwrap();
    assert_eq!(
        store.confirm_agent_node(&permit, &body, NOW).unwrap(),
        first
    );
    assert!(store.confirm_agent_node(&permit, b"{}", NOW).is_err());
    assert!(store.mark_agent_node_unknown(&permit, NOW).is_err());
    drop(worker);
    let mut worker = fixture.worker(false);
    match fixture.begin(&mut worker, false).unwrap() {
        AgentNodeEffectStart::Confirmed(outcome) => assert_eq!(outcome, first),
        other => panic!("lost known outcome {other:?}"),
    }
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT state FROM agent_node_effects", [], |row| row
                .get::<_, String>(0))
            .unwrap(),
        "CONFIRMED"
    );
}

#[test]
fn expired_execution_leases_do_not_hide_historical_known_outcomes() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .confirm_agent_node(&permit, b"{}", NOW)
        .unwrap();
    drop(worker);
    let successor = WorkerAuthorityConfig {
        fencing_token: 5,
        now: Some("2026-09-04T01:00:00Z".into()),
        ..fixture.config.clone()
    };
    let worker = crate::Worker::open_with_authority(successor, fixture.root.path()).unwrap();
    let store = worker.authority_store.as_ref().unwrap();
    let mut query = crate::QueryAgentNodeEffect {
        operation_id: permit.operation_id.clone(),
        fence: permit.fence.clone(),
        run_id: "run_rpc".into(),
        node_id: "coder".into(),
    };
    let known = store.agent_node_snapshot(&query).unwrap().unwrap();
    assert_eq!(known.state, "CONFIRMED");
    assert_eq!(known.output_length, Some(2));
    query.node_id = "critic".into();
    assert!(store.agent_node_snapshot(&query).is_err());
    query.node_id = "coder".into();
    query.operation_id = "missing-operation".into();
    assert!(store.agent_node_snapshot(&query).unwrap().is_none());
}

#[test]
fn stale_writer_refuses_dispatch_and_confirmation() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let newer = WorkerAuthorityConfig {
        fencing_token: 5,
        ..fixture.config.clone()
    };
    let successor = crate::Worker::open_with_authority(newer, fixture.root.path()).unwrap();
    assert!(fixture.begin(&mut worker, true).is_err());
    assert!(
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .confirm_agent_node(&permit, b"{}", NOW)
            .is_err()
    );
    assert!(
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .mark_agent_node_unknown(&permit, NOW)
            .is_err()
    );
    drop(successor);
}

#[test]
fn newer_installed_epoch_cannot_reuse_a_prior_node_permit() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let mut epoch: Value = serde_json::from_slice(&fixture.epoch).unwrap();
    epoch["executionEpoch"] = 5.into();
    epoch["requestId"] = "5".repeat(64).into();
    epoch["nonce"] = "6".repeat(64).into();
    let signed = signed(epoch, b"deepseek-infra:control-authority-request-v1\0");
    let fence = ActionFence {
        execution_epoch: 5,
        ..permit.fence.clone()
    };
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .install(worker.authority.as_ref().unwrap(), &fence, &signed)
        .unwrap();
    assert!(fixture.begin(&mut worker, true).is_err());
    assert!(
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .confirm_agent_node(&permit, b"{}", NOW)
            .is_err()
    );
}

#[test]
fn changed_task_scope_and_expired_grant_write_no_new_node() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let mut response = fixture.response.clone();
    response.intent.as_mut().unwrap().task = "a rebound task".into();
    assert!(
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .begin_agent_node(&response, &fixture.grant, &fixture.config, NOW)
            .is_err()
    );
    assert!(
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .begin_agent_node(
                &fixture.response,
                &fixture.grant,
                &fixture.config,
                "2026-09-04T00:02:00Z"
            )
            .is_err()
    );
    assert_eq!(
        fixture
            .database()
            .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn malformed_provider_body_is_rolled_back_before_becoming_known() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let store = worker.authority_store.as_mut().unwrap();
    assert!(store.confirm_agent_node(&permit, b"not json", NOW).is_err());
    assert!(
        store
            .confirm_agent_node(&permit, b"{\"z\":1,\"a\":2}", NOW)
            .is_err()
    );
    store.mark_agent_node_unknown(&permit, NOW).unwrap();
    let confirmed = store.confirm_agent_node(&permit, b"{}", NOW).unwrap();
    assert_eq!(confirmed.output, b"{}");
}

#[test]
fn journal_identity_replace_delete_and_terminal_changes_are_denied() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let database = fixture.database();
    for sql in [
        "DELETE FROM agent_node_effects",
        "UPDATE agent_node_effects SET node_id='critic'",
        "UPDATE agent_node_effects SET grant=x'00'",
        "UPDATE agent_node_effects SET rowid=rowid+1",
        "INSERT OR REPLACE INTO agent_node_effects SELECT * FROM agent_node_effects",
    ] {
        assert!(database.execute(sql, []).is_err(), "{sql}");
    }
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .confirm_agent_node(&permit, b"{}", NOW)
        .unwrap();
    assert!(
        database
            .execute("UPDATE agent_node_effects SET output=x'00'", [])
            .is_err()
    );
    assert!(
        database
            .execute("UPDATE agent_node_effects SET state='DISPATCHING'", [])
            .is_err()
    );
}

#[test]
fn additive_v4_migration_preserves_epoch_and_does_not_infer_nodes() {
    let fixture = Fixture::new();
    let worker = fixture.worker(true);
    drop(worker);
    let database = fixture.database();
    database
        .execute("DROP TABLE agent_node_effects", [])
        .unwrap();
    database.pragma_update(None, "user_version", 4).unwrap();
    let epoch: Vec<u8> = database
        .query_row("SELECT request FROM epoch_installs", [], |row| row.get(0))
        .unwrap();
    let wrong = WorkerAuthorityConfig {
        fleet_id: "wrong-fleet".into(),
        ..fixture.config.clone()
    };
    assert!(crate::Worker::open_with_authority(wrong, fixture.root.path()).is_err());
    assert_eq!(
        database
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        4
    );
    let _worker = fixture.worker(false);
    assert_eq!(
        database
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        6
    );
    assert_eq!(
        database
            .query_row("SELECT request FROM epoch_installs", [], |row| row
                .get::<_, Vec<u8>>(0))
            .unwrap(),
        epoch
    );
    assert_eq!(
        database
            .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn clock_regression_never_changes_a_node_or_its_known_output() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let store = worker.authority_store.as_mut().unwrap();
    let before = "2026-09-04T00:00:59Z";
    assert!(store.confirm_agent_node(&permit, b"{}", before).is_err());
    assert!(store.mark_agent_node_unknown(&permit, before).is_err());
    store
        .mark_agent_node_unknown(&permit, "2026-09-04T00:01:02Z")
        .unwrap();
    assert!(store.confirm_agent_node(&permit, b"{}", NOW).is_err());
    let (state, output, updated): (String, Option<Vec<u8>>, String) = fixture
        .database()
        .query_row(
            "SELECT state,output,updated_at FROM agent_node_effects",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(state, "EFFECT_UNKNOWN");
    assert!(output.is_none());
    assert_eq!(updated, "2026-09-04T00:01:02Z");
    let known = store
        .confirm_agent_node(&permit, b"{}", "2026-09-04T00:01:03Z")
        .unwrap();
    assert_eq!(known.output, b"{}");
    assert!(
        fixture
            .database()
            .execute("UPDATE agent_node_effects SET updated_at=?1", [NOW])
            .is_err()
    );
}

#[test]
fn backwards_restart_clock_rolls_back_writer_takeover_and_node_recovery() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let _permit = started(fixture.begin(&mut worker, true).unwrap());
    drop(worker);
    let successor = WorkerAuthorityConfig {
        fencing_token: 5,
        now: Some("2026-09-04T00:00:59Z".into()),
        ..fixture.config.clone()
    };
    assert!(crate::Worker::open_with_authority(successor, fixture.root.path()).is_err());
    let database = fixture.database();
    let token: i64 = database
        .query_row(
            "SELECT fencing_token FROM worker_authority WHERE id=1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(token, 4);
    let (state, updated): (String, String) = database
        .query_row(
            "SELECT state,updated_at FROM agent_node_effects",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(state, "DISPATCHING");
    assert_eq!(updated, NOW);
    drop(database);
    let recovered = fixture.worker(false);
    let query = crate::QueryAgentNodeEffect {
        operation_id: fixture
            .response
            .intent
            .as_ref()
            .unwrap()
            .operation_id
            .clone(),
        fence: fixture
            .response
            .claim
            .as_ref()
            .unwrap()
            .fence
            .clone()
            .unwrap(),
        run_id: "run_rpc".into(),
        node_id: "coder".into(),
    };
    assert_eq!(
        recovered
            .authority_store
            .as_ref()
            .unwrap()
            .agent_node_snapshot(&query)
            .unwrap()
            .unwrap()
            .state,
        "EFFECT_UNKNOWN"
    );
}

#[test]
fn coherent_disk_tampering_is_rejected_before_writer_takeover_or_recovery() {
    for corruption in ["claim", "intent", "grant", "outcome"] {
        let fixture = Fixture::new();
        let mut worker = fixture.worker(true);
        let permit = started(fixture.begin(&mut worker, true).unwrap());
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .confirm_agent_node(&permit, b"{}", NOW)
            .unwrap();
        drop(worker);
        let database = fixture.database();
        // Simulate disk corruption outside the runtime. Restore the exact schema
        // so the test reaches cryptographic/canonical validation rather than only
        // rejecting a missing trigger. This is never an application write path.
        database
            .execute("DROP TRIGGER agent_node_identity_immutable", [])
            .unwrap();
        database
            .execute("DROP TRIGGER agent_node_terminal_immutable", [])
            .unwrap();
        match corruption {
            "claim" => {
                let mut claim = fixture.response.claim.clone().unwrap();
                claim.claim_token = "different-claim".into();
                database
                    .execute(
                        "UPDATE agent_node_effects SET claim=?1,claim_scope_digest=?2",
                        params![claim.encode_to_vec(), claim_scope_digest(&claim).unwrap()],
                    )
                    .unwrap();
            }
            "intent" => {
                let mut intent = fixture.response.intent.clone().unwrap();
                intent.task.push_str(" changed");
                let bytes = intent.encode_to_vec();
                database
                    .execute(
                        "UPDATE agent_node_effects SET intent=?1,intent_digest=?2",
                        params![bytes, agent_node_effect::digest(&bytes)],
                    )
                    .unwrap();
            }
            "grant" => {
                let mut document: Value = serde_json::from_slice(&fixture.grant).unwrap();
                document["signature"] = "A".repeat(86).into();
                let bytes = authority_request::canonical_json_bytes(&document).unwrap();
                database
                    .execute(
                        "UPDATE agent_node_effects SET grant=?1,grant_digest=?2",
                        params![bytes, agent_node_effect::digest(&bytes)],
                    )
                    .unwrap();
            }
            "outcome" => {
                let bytes = b"[ ]";
                database
                    .execute(
                        "UPDATE agent_node_effects SET output=?1,output_sha256=?2",
                        params![bytes.as_slice(), agent_node_effect::digest(bytes)],
                    )
                    .unwrap();
            }
            _ => unreachable!(),
        }
        for trigger in SCHEMA_V5.iter().filter(|sql| {
            sql.starts_with("CREATE TRIGGER agent_node_identity_immutable ")
                || sql.starts_with("CREATE TRIGGER agent_node_terminal_immutable ")
        }) {
            database.execute(trigger, []).unwrap();
        }
        let prior: (Vec<u8>, Vec<u8>, Vec<u8>, Vec<u8>) = database
            .query_row(
                "SELECT claim,intent,grant,output FROM agent_node_effects",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        let successor = WorkerAuthorityConfig {
            fencing_token: 5,
            ..fixture.config.clone()
        };
        assert!(
            crate::Worker::open_with_authority(successor, fixture.root.path()).is_err(),
            "accepted {corruption} corruption"
        );
        let token: i64 = database
            .query_row(
                "SELECT fencing_token FROM worker_authority WHERE id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(token, 4);
        let retained: (Vec<u8>, Vec<u8>, Vec<u8>, Vec<u8>) = database
            .query_row(
                "SELECT claim,intent,grant,output FROM agent_node_effects",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(
            retained, prior,
            "rewrote {corruption} evidence during refused startup"
        );
    }
}

fn retained_input(fixture: &Fixture) -> crate::BeginAgentNodeEffect {
    let intent = fixture.response.intent.as_ref().unwrap();
    crate::BeginAgentNodeEffect {
        claim: fixture.response.claim.as_ref().unwrap().clone(),
        signed_phase_grant: fixture.grant.clone(),
        operation_id: "process-dispatch-command".into(),
        actor: "rust-agent-worker".into(),
        node_id: intent.node_id.clone(),
    }
}

#[test]
fn retained_node_replay_never_returns_a_second_dispatch_permit_and_survives_restart() {
    let fixture = Fixture::new();
    let input = retained_input(&fixture);
    assert_ne!(
        input.operation_id,
        fixture.response.intent.as_ref().unwrap().operation_id
    );
    let mut worker = fixture.worker(true);
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .retained_agent_node_start(&input, &fixture.config)
            .unwrap()
            .is_none()
    );
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    assert!(matches!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .retained_agent_node_start(&input, &fixture.config)
            .unwrap(),
        Some(AgentNodeEffectStart::EffectUnknown)
    ));
    let body = authority_request::canonical_json_bytes(
        &json!({"publicEvent":{"type":"content","text":"retained"}}),
    )
    .unwrap();
    let original = worker
        .authority_store
        .as_mut()
        .unwrap()
        .confirm_agent_node(&permit, &body, NOW)
        .unwrap();
    drop(worker);
    let restarted = fixture.worker(false);
    let before: (i64, String) = restarted
        .authority_store
        .as_ref()
        .unwrap()
        .connection
        .query_row(
            "SELECT COUNT(*),MAX(updated_at) FROM agent_node_effects",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    let replay = restarted
        .authority_store
        .as_ref()
        .unwrap()
        .retained_agent_node_start(&input, &fixture.config)
        .unwrap();
    match replay {
        Some(AgentNodeEffectStart::Confirmed(replayed)) => {
            assert_eq!(replayed.output, original.output);
            assert_eq!(replayed.output_sha256, original.output_sha256);
            assert_eq!(replayed.operation_id, original.operation_id);
            assert_eq!(replayed.fence, original.fence);
        }
        other => panic!("retained confirmed result did not replay: {other:?}"),
    }
    let after: (i64, String) = restarted
        .authority_store
        .as_ref()
        .unwrap()
        .connection
        .query_row(
            "SELECT COUNT(*),MAX(updated_at) FROM agent_node_effects",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(before, after);
}

#[test]
fn retained_node_replay_refuses_rebound_scope_or_signed_grant() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let _permit = started(fixture.begin(&mut worker, true).unwrap());
    type Change = fn(&mut crate::BeginAgentNodeEffect);
    let changes: [(&str, Change); 10] = [
        ("node", |i| i.node_id = "critic".into()),
        ("run", |i| i.claim.run_id.push_str("-other")),
        ("action", |i| {
            i.claim.fence.as_mut().unwrap().action_id.push_str("-other")
        }),
        ("epoch", |i| {
            i.claim.fence.as_mut().unwrap().execution_epoch += 1
        }),
        ("plan", |i| i.claim.plan_digest = "0".repeat(64)),
        ("request", |i| i.claim.request.as_mut().unwrap().length += 1),
        ("owner", |i| i.claim.owner.push_str("-other")),
        ("token", |i| i.claim.claim_token.push_str("-other")),
        ("index", |i| i.claim.metadata_index += 1),
        ("grant", |i| i.signed_phase_grant.push(b' ')),
    ];
    for (name, change) in changes {
        let mut input = retained_input(&fixture);
        change(&mut input);
        assert!(
            worker
                .authority_store
                .as_ref()
                .unwrap()
                .retained_agent_node_start(&input, &fixture.config)
                .map_or(true, |retained| retained.is_none()),
            "rebound {name} reused node custody"
        );
    }
}

#[test]
fn retained_node_replay_rechecks_pinned_writer_and_signer_realm() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let _permit = started(fixture.begin(&mut worker, true).unwrap());
    type Change = fn(&mut WorkerAuthorityConfig);
    let changes: [Change; 4] = [
        |c| c.fencing_token += 1,
        |c| c.signer_public_key = "invalid".into(),
        |c| c.fleet_id.push_str("-other"),
        |c| c.environment.push_str("-other"),
    ];
    for change in changes {
        let mut config = fixture.config.clone();
        change(&mut config);
        assert!(
            worker
                .authority_store
                .as_ref()
                .unwrap()
                .retained_agent_node_start(&retained_input(&fixture), &config)
                .is_err()
        );
    }
}

#[test]
fn retained_node_new_command_id_cannot_restart_an_uncertain_effect() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let _permit = started(fixture.begin(&mut worker, true).unwrap());
    let mut input = retained_input(&fixture);
    input.operation_id = "different-process-dispatch-command".into();
    assert!(matches!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .retained_agent_node_start(&input, &fixture.config)
            .unwrap(),
        Some(AgentNodeEffectStart::EffectUnknown)
    ));
    for empty_actor in [false, true] {
        let mut malformed = retained_input(&fixture);
        if empty_actor {
            malformed.actor.clear();
        } else {
            malformed.operation_id.clear();
        }
        assert!(
            worker
                .authority_store
                .as_ref()
                .unwrap()
                .retained_agent_node_start(&malformed, &fixture.config)
                .is_err()
        );
    }
    let count: i64 = worker
        .authority_store
        .as_ref()
        .unwrap()
        .connection
        .query_row("SELECT COUNT(*) FROM agent_node_effects", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 1);
}
