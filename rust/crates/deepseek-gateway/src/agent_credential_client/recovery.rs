//! Typed recovery custody stays on the authenticated Rust-to-Rust TLS path.
use super::*;
use deepseek_policy::agent_run_data::MAX_ARTIFACT_BYTES;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRunRecoveryContext, PrepareAgentCredentialsResponse,
};

pub(crate) struct RecoveryPreparation<'a> {
    pub(crate) input: Preparation<'a>,
    pub(crate) context: AgentRunRecoveryContext,
    pub(crate) context_sha256: &'a str,
    pub(crate) expect_credential_object: bool,
}

fn hex64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn valid_ref(value: Option<&AgentArtifactReference>) -> bool {
    value.is_some_and(|r| hex64(&r.sha256) && r.length > 0 && r.length <= MAX_ARTIFACT_BYTES)
}

fn validate_response(
    input: &RecoveryPreparation<'_>,
    response: &PrepareAgentCredentialsResponse,
) -> Result<(), tonic::Status> {
    let invalid = || tonic::Status::data_loss("Invalid recovery credential response");
    let scope = response.scope.as_ref().ok_or_else(invalid)?;
    if scope.run_id != input.input.run_id
        || scope.metadata_epoch != input.input.epoch
        || scope.runtime_revision != input.input.runtime_revision
        || scope.fleet_id.is_empty()
        || scope.environment.is_empty()
        || !hex64(&scope.request_sha256)
        || scope.request_length == 0
        || scope.request_length > MAX_ARTIFACT_BYTES
        || !valid_ref(response.prepared_request.as_ref())
        || !valid_ref(response.recovery_context.as_ref())
        || response.recovery_context_sha256 != input.context_sha256
        || !hex64(&response.recovery_context_sha256)
        || response.recovery_event_bodies.is_empty()
        || response.recovery_event_bodies.len() > 6
        || response
            .recovery_event_bodies
            .iter()
            .any(|r| !valid_ref(Some(r)))
        || input.expect_credential_object && response.object.is_none()
    {
        return Err(invalid());
    }
    if response
        .object
        .as_ref()
        .is_some_and(|o| !hex64(&o.id) || !hex64(&o.sha256) || !(1..=65536).contains(&o.length))
    {
        return Err(invalid());
    }
    // Recovery inserts its private context before computing the credential
    // scope. That scope binds a new Rust base, not the incoming draft. The
    // public route independently opens both bases and verifies their bodies.
    Ok(())
}

pub(crate) async fn prepare_recovery(
    config: &WorkerConfig,
    input: RecoveryPreparation<'_>,
) -> Result<PrepareAgentCredentialsResponse, tonic::Status> {
    let invalid = || tonic::Status::invalid_argument("Invalid recovery credential scope");
    let update = input.input.runtime_update.as_ref().ok_or_else(invalid)?;
    if input.context.schema_version != 1
        || input.context.run_id != input.input.run_id
        || input.context.metadata_epoch != input.input.epoch
        || input.context.expected_runtime_revision.checked_add(1)
            != Some(input.input.runtime_revision)
        || input.context.expected_request != update.previous_request
        || input.context.expected_runtime_revision != update.previous_runtime_revision
        || !hex64(input.context_sha256)
    {
        return Err(invalid());
    }
    let mut client = connect(config)
        .await
        .map_err(|_| tonic::Status::unavailable("Recovery custody is unavailable"))?;
    let mut request = Request::new(PrepareAgentCredentialsRequest {
        schema_version: 1,
        run_id: input.input.run_id.into(),
        fence: Some(ActionFence {
            action_id: input.input.run_id.into(),
            execution_epoch: input.input.epoch,
        }),
        request: Some(AgentArtifactReference {
            sha256: input.input.reference.sha256.clone(),
            length: input.input.reference.length,
        }),
        runtime_revision: input.input.runtime_revision,
        api_key: input.input.api_key.unwrap_or_default().into(),
        tavily_api_key: input.input.tavily_api_key.unwrap_or_default().into(),
        runtime_update: input.input.runtime_update.clone(),
        recovery_context: Some(input.context.clone()),
    });
    let mut authorization: MetadataValue<_> = format!("Bearer {}", config.bearer.as_str())
        .parse()
        .map_err(|_| tonic::Status::unavailable("Recovery custody is unavailable"))?;
    authorization.set_sensitive(true);
    request
        .metadata_mut()
        .insert("authorization", authorization);
    let response = client.prepare_credentials(request).await?.into_inner();
    validate_response(&input, &response)?;
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_protocol::generated::deepseek::agent::v1::AgentCredentialScope;

    #[test]
    fn recovery_response_binds_the_new_context_base_and_rejects_foreign_or_incomplete_refs() {
        let previous = AgentArtifactReference {
            sha256: "a".repeat(64),
            length: 50,
        };
        let reference = ArtifactReference {
            sha256: previous.sha256.clone(),
            length: previous.length,
        };
        let input = RecoveryPreparation {
            input: Preparation {
                run_id: "run",
                epoch: 2,
                reference: &reference,
                runtime_revision: 3,
                api_key: None,
                tavily_api_key: None,
                runtime_update: None,
            },
            context: AgentRunRecoveryContext::default(),
            context_sha256: &"d".repeat(64),
            expect_credential_object: false,
        };
        let response = PrepareAgentCredentialsResponse {
            scope: Some(AgentCredentialScope {
                run_id: "run".into(),
                metadata_epoch: 2,
                runtime_revision: 3,
                fleet_id: "fleet".into(),
                environment: "test".into(),
                request_sha256: "b".repeat(64),
                request_length: 70,
            }),
            prepared_request: Some(AgentArtifactReference {
                sha256: "c".repeat(64),
                length: 80,
            }),
            recovery_context: Some(previous.clone()),
            recovery_context_sha256: "d".repeat(64),
            recovery_event_bodies: vec![previous],
            ..Default::default()
        };
        validate_response(&input, &response).unwrap();
        for changed in [
            "run",
            "epoch",
            "runtime",
            "fleet",
            "scope-hash",
            "request",
            "context",
            "digest",
            "events",
        ] {
            let mut bad = response.clone();
            match changed {
                "run" => bad.scope.as_mut().unwrap().run_id = "foreign".into(),
                "epoch" => bad.scope.as_mut().unwrap().metadata_epoch += 1,
                "runtime" => bad.scope.as_mut().unwrap().runtime_revision += 1,
                "fleet" => bad.scope.as_mut().unwrap().fleet_id.clear(),
                "scope-hash" => bad.scope.as_mut().unwrap().request_sha256 = "bad".into(),
                "request" => bad.prepared_request = None,
                "context" => bad.recovery_context = None,
                "digest" => bad.recovery_context_sha256 = "e".repeat(64),
                _ => bad.recovery_event_bodies.clear(),
            }
            assert!(validate_response(&input, &bad).is_err(), "{changed}");
        }
        let mut requires_object = input;
        requires_object.expect_credential_object = true;
        assert!(validate_response(&requires_object, &response).is_err());
    }

    #[tokio::test]
    async fn invalid_recovery_scope_and_missing_transport_keep_distinct_typed_errors() {
        let previous = AgentArtifactReference {
            sha256: "a".repeat(64),
            length: 50,
        };
        let reference = ArtifactReference {
            sha256: "b".repeat(64),
            length: 60,
        };
        let config = WorkerConfig {
            origin: "invalid-origin".into(),
            ca_file: std::env::temp_dir().join("unused-recovery-transport-ca"),
            server_name: "worker".into(),
            bearer: zeroize::Zeroizing::new("isolated-recovery-transport-fixture".into()),
        };
        for (bad_context, expected) in [
            (true, tonic::Code::InvalidArgument),
            (false, tonic::Code::Unavailable),
        ] {
            let error = prepare_recovery(
                &config,
                RecoveryPreparation {
                    input: Preparation {
                        run_id: "run",
                        epoch: 2,
                        reference: &reference,
                        runtime_revision: 3,
                        api_key: None,
                        tavily_api_key: None,
                        runtime_update: Some(AgentCredentialRuntimeUpdate {
                            previous_request: Some(previous.clone()),
                            previous_runtime_revision: 2,
                            ..Default::default()
                        }),
                    },
                    context: AgentRunRecoveryContext {
                        schema_version: 1,
                        run_id: "run".into(),
                        metadata_epoch: if bad_context { 3 } else { 2 },
                        expected_runtime_revision: 2,
                        expected_request: Some(previous.clone()),
                        ..Default::default()
                    },
                    context_sha256: &"d".repeat(64),
                    expect_credential_object: false,
                },
            )
            .await
            .unwrap_err();
            assert_eq!(error.code(), expected);
            assert!(
                !error
                    .message()
                    .contains("isolated-recovery-transport-fixture")
            );
        }
    }
}
