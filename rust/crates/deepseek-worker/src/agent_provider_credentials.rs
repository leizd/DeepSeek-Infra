//! Request credentials stay in Rust custody and are bound to the admitted body.
use std::sync::Arc;

use deepseek_federation::{AgentCredentialObject, AgentCredentialScope, AgentProviderCredentials};
use deepseek_policy::agent_run_data::{ArtifactReference, ArtifactStore};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentCredentialObjectReference, AgentCredentialRuntimeUpdate, AgentRunExecutionClaim,
    PrepareAgentCredentialsRequest, PrepareAgentCredentialsResponse,
};
use serde_json::Value;
use tonic::{Request, Response, Status};
use zeroize::Zeroizing;

use crate::{AgentPlanExecutor, WorkerRpcService};

const PRIVATE_FIELD: &str = "_nativeProviderCredentials";

fn denied() -> Status {
    Status::failed_precondition("AGENT_CREDENTIAL_CUSTODY_DENIED")
}

fn read_body(root: &std::path::Path, reference: &ArtifactReference) -> Result<Value, Status> {
    ArtifactStore::open_readonly(root)
        .and_then(|store| store.get(reference))
        .map_err(|_| denied())
}

fn merge_credentials(
    inherited: Option<&AgentProviderCredentials>,
    update: Option<&AgentCredentialRuntimeUpdate>,
    api_key: &str,
    tavily_api_key: &str,
) -> Result<Option<AgentProviderCredentials>, Status> {
    if update.is_some_and(|value| {
        !value.replace_api_key && !api_key.is_empty()
            || !value.replace_tavily_api_key && !tavily_api_key.is_empty()
    }) {
        return Err(denied());
    }
    let api = if update.is_some_and(|value| !value.replace_api_key) {
        inherited.and_then(AgentProviderCredentials::api_key)
    } else {
        (!api_key.is_empty()).then_some(api_key)
    };
    let search = if update.is_some_and(|value| !value.replace_tavily_api_key) {
        inherited.and_then(AgentProviderCredentials::tavily_api_key)
    } else {
        (!tavily_api_key.is_empty()).then_some(tavily_api_key)
    };
    if api.is_some() || search.is_some() {
        AgentProviderCredentials::from_keys(api, search)
            .map(Some)
            .map_err(|_| denied())
    } else if update.is_some() {
        Ok(None)
    } else {
        Err(denied())
    }
}

fn bound_reference(
    root: &std::path::Path,
    mut body: Value,
    run_id: &str,
    metadata_epoch: u64,
    expected_revision: Option<u64>,
    fleet_id: &str,
    environment: &str,
) -> Result<Option<(AgentCredentialScope, AgentCredentialObject)>, Status> {
    let Some(private) = body
        .as_object_mut()
        .ok_or_else(denied)?
        .remove(PRIVATE_FIELD)
    else {
        return Ok(None);
    };
    let fields = private.as_object().ok_or_else(denied)?;
    if fields.len() != 2 {
        return Err(denied());
    }
    let scope: AgentCredentialScope =
        serde_json::from_value(private["scope"].clone()).map_err(|_| denied())?;
    let object: AgentCredentialObject =
        serde_json::from_value(private["object"].clone()).map_err(|_| denied())?;
    if scope.fleet_id != fleet_id
        || scope.environment != environment
        || scope.run_id != run_id
        || scope.metadata_epoch != metadata_epoch
        || scope.runtime_revision == 0
        || expected_revision.is_some_and(|revision| scope.runtime_revision != revision)
    {
        return Err(denied());
    }
    let base = ArtifactReference {
        sha256: scope.request_sha256.clone(),
        length: scope.request_length,
    };
    if read_body(root, &base)? != body {
        return Err(denied());
    }
    Ok(Some((scope, object)))
}

impl WorkerRpcService {
    pub(crate) async fn prepare_agent_provider_credentials(
        &self,
        request: Request<PrepareAgentCredentialsRequest>,
    ) -> Result<Response<PrepareAgentCredentialsResponse>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("AGENT_NODE_CALLER_DENIED"));
        }
        let mut input = request.into_inner();
        let api_key = Zeroizing::new(std::mem::take(&mut input.api_key));
        let tavily_api_key = Zeroizing::new(std::mem::take(&mut input.tavily_api_key));
        let update = input.runtime_update.take();
        let recovery_context = input.recovery_context.take();
        let fence = input.fence.as_ref().ok_or_else(denied)?;
        let reference = input.request.as_ref().ok_or_else(denied)?;
        if input.schema_version != 1
            || input.runtime_revision == 0
            || recovery_context.is_some() && update.is_none()
            || fence.action_id != input.run_id
            || fence.execution_epoch == 0
        {
            return Err(denied());
        }
        if let Some(previous) = update.as_ref() {
            if previous.previous_runtime_revision == 0
                || previous.previous_runtime_revision.checked_add(1) != Some(input.runtime_revision)
                || previous.previous_request.is_none()
                || !previous.replace_api_key && !api_key.is_empty()
                || !previous.replace_tavily_api_key && !tavily_api_key.is_empty()
            {
                return Err(denied());
            }
        }
        let live_epoch = self
            .agent_execution_control
            .as_ref()
            .ok_or_else(denied)?
            .metadata_epoch()
            .await?;
        if fence.execution_epoch != live_epoch {
            return Err(Status::failed_precondition("STALE_EXECUTION_EPOCH"));
        }
        if let Some(previous) = update.as_ref() {
            let control = self.agent_execution_control.as_ref().ok_or_else(denied)?;
            let current = control.runtime_binding(&input.run_id, live_epoch).await?;
            if current.runtime_revision != previous.previous_runtime_revision
                || current.request.as_ref() != previous.previous_request.as_ref()
            {
                return Err(denied());
            }
            if let Some(context) = recovery_context.as_ref() {
                if context.run_id != input.run_id
                    || context.metadata_epoch != live_epoch
                    || context.expected_index != current.next_index
                    || context.expected_runtime_revision != current.runtime_revision
                    || context.expected_request.as_ref() != current.request.as_ref()
                    || context.previous_scope.as_ref() != current.recovery_scope.as_ref()
                {
                    return Err(crate::agent_recovery_context::denied());
                }
                control.validate_recovery_context(context).await?;
            }
        }
        let root = self
            .agent_plan_executor
            .as_ref()
            .ok_or_else(denied)?
            .artifact_root
            .clone();
        let reference = ArtifactReference {
            sha256: reference.sha256.clone(),
            length: reference.length,
        };
        let signer = Arc::clone(self.control_signer.as_ref().ok_or_else(denied)?);
        let response = tokio::task::spawn_blocking(move || {
            let body = read_body(&root, &reference)?;
            if body.get(PRIVATE_FIELD).is_some() {
                return Err(denied());
            }
            let signer = signer.blocking_lock();
            let (fleet_id, environment) = signer.agent_credential_binding()?;
            let sealed_max = deepseek_federation::AgentCredentialVault::open(&root)
                .and_then(|vault| {
                    vault.max_runtime_revision(&fleet_id, &environment, &input.run_id, live_epoch)
                })
                .map_err(|_| denied())?;
            if !deepseek_policy::agent_runtime_admission::vault_revision_allowed(
                sealed_max,
                input.runtime_revision,
            ) {
                return Err(denied());
            }
            let inherited = if let Some(previous) = update.as_ref() {
                let previous_reference = previous.previous_request.as_ref().ok_or_else(denied)?;
                let previous_body = read_body(
                    &root,
                    &ArtifactReference {
                        sha256: previous_reference.sha256.clone(),
                        length: previous_reference.length,
                    },
                )?;
                // A new metadata epoch does not rewrite an old sealed credential
                // object's original scope. Go's CAS pins this prior request hash.
                let previous_epoch =
                    if recovery_context.is_some() && previous_body.get(PRIVATE_FIELD).is_some() {
                        previous_body[PRIVATE_FIELD]["scope"]["metadataEpoch"]
                            .as_u64()
                            .filter(|epoch| *epoch > 0 && *epoch <= live_epoch)
                            .ok_or_else(denied)?
                    } else {
                        live_epoch
                    };
                bound_reference(
                    &root,
                    previous_body,
                    &input.run_id,
                    previous_epoch,
                    Some(previous.previous_runtime_revision),
                    &fleet_id,
                    &environment,
                )?
                .map(|(scope, object)| signer.open_agent_credentials(&root, &scope, &object))
                .transpose()?
            } else {
                None
            };
            let credentials = merge_credentials(
                inherited.as_ref(),
                update.as_ref(),
                api_key.as_str(),
                tavily_api_key.as_str(),
            )?;
            let scope = AgentCredentialScope {
                fleet_id,
                environment,
                run_id: input.run_id,
                metadata_epoch: live_epoch,
                runtime_revision: input.runtime_revision,
                request_sha256: reference.sha256,
                request_length: reference.length,
            };
            let object = credentials
                .as_ref()
                .map(|value| signer.prepare_agent_credentials(&root, &scope, value))
                .transpose()?;
            Ok::<_, Status>(PrepareAgentCredentialsResponse {
                scope: Some(
                    deepseek_protocol::generated::deepseek::agent::v1::AgentCredentialScope {
                        fleet_id: scope.fleet_id,
                        environment: scope.environment,
                        run_id: scope.run_id,
                        metadata_epoch: scope.metadata_epoch,
                        runtime_revision: scope.runtime_revision,
                        request_sha256: scope.request_sha256,
                        request_length: scope.request_length,
                    },
                ),
                object: object.map(|object| AgentCredentialObjectReference {
                    id: object.id,
                    sha256: object.sha256,
                    length: object.length,
                }),
                ..Default::default()
            })
        })
        .await
        .map_err(|_| denied())??;
        Ok(Response::new(response))
    }

    /// The claim's final request hash covers the opaque credential reference.
    /// The encrypted object's scope independently binds the original sanitized body.
    pub(crate) async fn agent_executor_for_claim(
        &self,
        claim: &AgentRunExecutionClaim,
    ) -> Result<Arc<AgentPlanExecutor>, Status> {
        let executor = Arc::clone(self.agent_plan_executor.as_ref().ok_or_else(denied)?);
        let root = executor.artifact_root.clone();
        let reference = claim.request.as_ref().ok_or_else(denied)?;
        let reference = ArtifactReference {
            sha256: reference.sha256.clone(),
            length: reference.length,
        };
        let run_id = claim.run_id.clone();
        let metadata_epoch = claim.metadata_epoch;
        let signer = Arc::clone(self.control_signer.as_ref().ok_or_else(denied)?);
        tokio::task::spawn_blocking(move || {
            let body = read_body(&root, &reference)?;
            let signer = signer.blocking_lock();
            let (fleet_id, environment) = signer.agent_credential_binding()?;
            let Some((scope, object)) = bound_reference(
                &root,
                body,
                &run_id,
                metadata_epoch,
                None,
                &fleet_id,
                &environment,
            )?
            else {
                return Ok(executor);
            };
            let credentials = signer.open_agent_credentials(&root, &scope, &object)?;
            let mut selected = (*executor).clone();
            if let Some(key) = credentials.api_key() {
                selected.upstream.api_key = key.into();
            }
            // Tavily is retained in the sealed object for the independently admitted
            // tool continuation. It is never inserted into a provider request body.
            Ok(Arc::new(selected))
        })
        .await
        .map_err(|_| denied())?
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn credential_preparation_requires_authenticated_controller_and_valid_scope() {
        use crate::{CallerIdentity, StaticTokenAuthenticator, Worker};
        for role in ["controller", "worker"] {
            let service = WorkerRpcService::new_with_authenticator(
                Worker::new(),
                Arc::new(StaticTokenAuthenticator::new(
                    "isolated-bearer",
                    CallerIdentity {
                        service_name: "go-control-plane".into(),
                        role: role.into(),
                    },
                )),
            );
            assert_eq!(
                service
                    .prepare_agent_provider_credentials(Request::new(
                        PrepareAgentCredentialsRequest::default()
                    ))
                    .await
                    .unwrap_err()
                    .code(),
                tonic::Code::Unauthenticated
            );
            let mut request = Request::new(PrepareAgentCredentialsRequest::default());
            request
                .metadata_mut()
                .insert("authorization", "Bearer isolated-bearer".parse().unwrap());
            assert_eq!(
                service
                    .prepare_agent_provider_credentials(request)
                    .await
                    .unwrap_err()
                    .code(),
                if role == "controller" {
                    tonic::Code::FailedPrecondition
                } else {
                    tonic::Code::PermissionDenied
                }
            );
        }
    }

    fn bound_body(root: &std::path::Path) -> Value {
        let mut body = json!({"payload":{"messages":[{"role":"user","content":"Retained query"}]}});
        let reference = ArtifactStore::open_native(root, "python_disabled")
            .unwrap()
            .put(&body)
            .unwrap();
        body[PRIVATE_FIELD] = json!({"scope":{"fleetId":"fleet-a","environment":"test","runId":"run-a",
            "metadataEpoch":2,"runtimeRevision":1,"requestSha256":reference.sha256,"requestLength":reference.length},
            "object":{"id":"a".repeat(64),"sha256":"b".repeat(64),"length":300}});
        body
    }

    #[test]
    fn immutable_request_binding_accepts_only_the_expected_run_and_base_body() {
        let root = tempfile::tempdir().unwrap();
        let body = bound_body(root.path());
        let bound = bound_reference(
            root.path(),
            body.clone(),
            "run-a",
            2,
            None,
            "fleet-a",
            "test",
        )
        .unwrap()
        .unwrap();
        assert_eq!(bound.0.run_id, "run-a");
        assert_eq!(bound.1.id, "a".repeat(64));
        for (field, value) in [
            ("runId", json!("other")),
            ("metadataEpoch", json!(3)),
            ("runtimeRevision", json!(0)),
            ("fleetId", json!("other")),
            ("environment", json!("prod")),
            ("requestSha256", json!("c".repeat(64))),
            ("requestLength", json!(1)),
        ] {
            let mut changed = body.clone();
            changed[PRIVATE_FIELD]["scope"][field] = value;
            assert!(
                bound_reference(root.path(), changed, "run-a", 2, None, "fleet-a", "test").is_err(),
                "{field}"
            );
        }
        let mut next_revision = body.clone();
        next_revision[PRIVATE_FIELD]["scope"]["runtimeRevision"] = json!(2);
        let admitted = bound_reference(
            root.path(),
            next_revision,
            "run-a",
            2,
            None,
            "fleet-a",
            "test",
        )
        .unwrap()
        .unwrap();
        assert_eq!(admitted.0.runtime_revision, 2);
        let mut changed = body;
        changed["payload"]["messages"][0]["content"] = json!("Different query");
        assert!(
            bound_reference(root.path(), changed, "run-a", 2, None, "fleet-a", "test").is_err()
        );
    }

    #[test]
    fn missing_reference_preserves_server_fallback_and_malformed_reference_fails_closed() {
        let root = tempfile::tempdir().unwrap();
        assert!(
            bound_reference(
                root.path(),
                json!({"payload":{}}),
                "run-a",
                2,
                None,
                "fleet-a",
                "test"
            )
            .unwrap()
            .is_none()
        );
        for value in [
            Value::Null,
            json!([]),
            json!({}),
            json!({"scope":{},"object":{},"extra":true}),
        ] {
            let body = json!({PRIVATE_FIELD:value});
            assert!(
                bound_reference(root.path(), body, "run-a", 2, None, "fleet-a", "test").is_err()
            );
        }
    }
}
