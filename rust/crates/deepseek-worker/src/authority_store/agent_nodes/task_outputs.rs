//! Read original recovery outputs without changing their effect identities.
use super::{
    AgentNodeEffectPermit, AuthorityStore, agent_node_effect, denied, live, outcome, stored,
};
use crate::WorkerAuthorityConfig;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentRunRecoveryContext, AgentRunRecoveryKind,
};
use prost::Message;
use rusqlite::params;
use serde_json::Value;
use tonic::Status;

impl AuthorityStore {
    pub(crate) fn verify_agent_recovery_retained_outputs(
        &self,
        context: &AgentRunRecoveryContext,
        config: &WorkerAuthorityConfig,
    ) -> Result<(), Status> {
        if context.schema_version != 1
            || context.run_id.is_empty()
            || context.expected_index < 1
            || context.generation == 0
            || context.generation > i64::MAX as u64
            || context.metadata_epoch == 0
            || context.expected_runtime_revision == 0
            || context.retained_ids.len() > 4
            || context.retained_ids.len() != context.retained_outputs.len()
        {
            return Err(denied());
        }
        let mut seen = std::collections::HashSet::new();
        for (id, source) in context.retained_ids.iter().zip(&context.retained_outputs) {
            if !seen.insert(id)
                || source
                    .dispatch
                    .as_ref()
                    .is_none_or(|dispatch| &dispatch.node_id != id)
            {
                return Err(denied());
            }
            self.verify_recovery_source(context, source, config, true)?;
        }
        Ok(())
    }

    /// Ordinary execution admits bodies from its current request only. Recovery
    /// must enter the separate path with independently retained source proofs.
    pub(crate) fn agent_task_prior_outputs(
        &self,
        permit: &AgentNodeEffectPermit,
    ) -> Result<Vec<Value>, Status> {
        self.agent_task_outputs(permit, None)
    }

    pub(crate) fn agent_recovery_task_prior_outputs(
        &self,
        permit: &AgentNodeEffectPermit,
        context: &AgentRunRecoveryContext,
        config: &WorkerAuthorityConfig,
    ) -> Result<Vec<Value>, Status> {
        self.verify_agent_recovery_retained_outputs(context, config)?;
        if context.run_id != permit.intent.run_id
            || context.expected_index >= permit.intent.metadata_index
            || context.metadata_epoch > permit.intent.metadata_epoch
            || context.complete_immediately
            || context.expected_request == permit.intent.request
        {
            return Err(denied());
        }
        if permit.intent.phase == 2 {
            let selected = context
                .execute
                .iter()
                .find(|node| node.id == permit.intent.node_id)
                .ok_or_else(denied)?;
            if selected.task != permit.intent.task
                || selected.depends_on != permit.intent.depends_on
            {
                return Err(denied());
            }
        } else if permit.intent.phase != 3
            || !context.synthesize
            || context.synthesis_output_ids != permit.intent.depends_on
        {
            return Err(denied());
        }
        self.agent_task_outputs(permit, Some(context))
    }

    fn agent_task_outputs(
        &self,
        permit: &AgentNodeEffectPermit,
        recovery: Option<&AgentRunRecoveryContext>,
    ) -> Result<Vec<Value>, Status> {
        live(&self.connection, &permit.fence, permit.fencing_token)?;
        let current = stored(&self.connection, &permit.operation_id)?.ok_or_else(denied)?;
        if self.fencing_token != permit.fencing_token
            || current.state != "DISPATCHING"
            || !matches!(current.intent.phase, 2 | 3)
            || (current.intent.phase == 3
                && (current.intent.node_id != "synthesizer"
                    || current.intent.depends_on.is_empty()))
            || current.intent_digest != permit.intent_digest
            || current.grant_digest != permit.grant_digest
            || agent_node_effect::digest(&permit.intent.encode_to_vec()) != permit.intent_digest
            || permit.intent.depends_on.len() > 4
        {
            return Err(denied());
        }
        let mut dependencies = permit.intent.depends_on.clone();
        if let Some(context) = recovery {
            if permit.intent.phase == 2
                && context
                    .selection
                    .as_ref()
                    .is_some_and(|s| s.kind == AgentRunRecoveryKind::Rerun as i32)
            {
                for id in &context.prior_ids {
                    if !dependencies.contains(id) {
                        dependencies.push(id.clone());
                    }
                }
            }
        }
        if dependencies.len() > 4 {
            return Err(denied());
        }
        let mut outputs = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for dependency in &dependencies {
            if dependency == &permit.intent.node_id
                || !seen.insert(dependency)
                || deepseek_policy::multi_agent::profile(dependency).is_none()
            {
                return Err(denied());
            }
            let retained = recovery.and_then(|context| {
                context.retained_outputs.iter().find(|source| {
                    source
                        .dispatch
                        .as_ref()
                        .is_some_and(|dispatch| &dispatch.node_id == dependency)
                })
            });
            let source = if let Some(retained) = retained {
                stored(
                    &self.connection,
                    &retained.dispatch.as_ref().ok_or_else(denied)?.operation_id,
                )?
                .ok_or_else(denied)?
            } else {
                let operation:String=self.connection.query_row(
                    "SELECT operation_id FROM agent_node_effects WHERE run_id=?1 AND phase=2 AND node_id=?2 AND metadata_index<=?3 ORDER BY metadata_index DESC LIMIT 1",
                    params![permit.intent.run_id,dependency,permit.intent.metadata_index],|row|row.get(0),
                ).map_err(|_|denied())?;
                let source = stored(&self.connection, &operation)?.ok_or_else(denied)?;
                if source.intent.plan_digest != permit.intent.plan_digest
                    || source.intent.request != permit.intent.request
                    || source.intent.metadata_epoch > permit.intent.metadata_epoch
                    || (permit.intent.phase == 3
                        && recovery.is_none()
                        && source.intent.metadata_epoch != permit.intent.metadata_epoch)
                    || recovery.is_some_and(|context| {
                        source.intent.metadata_index <= context.expected_index
                            || source.intent.metadata_epoch < context.metadata_epoch
                            || !context.execute.iter().any(|node| &node.id == dependency)
                    })
                {
                    return Err(denied());
                }
                source
            };
            if source.token > permit.fencing_token || source.intent.task.is_empty() {
                return Err(denied());
            }
            let source_task = source.intent.task.clone();
            let known = outcome(source)?;
            let envelope: Value = serde_json::from_slice(&known.output).map_err(|_| denied())?;
            let event = &envelope["publicEvent"];
            let output = &event["output"];
            if envelope["runId"] != permit.intent.run_id
                || event["type"] != "agent_output"
                || event["phase"] != dependency.as_str()
                || output["id"] != dependency.as_str()
                || output["task"] != source_task
                || !output.is_object()
            {
                return Err(denied());
            }
            outputs.push(output.clone());
        }
        Ok(outputs)
    }
}
