//! Compare Go's recovery selection with immutable, original Rust effect custody.
use super::{AuthorityStore, stored};
use crate::{QueryAgentNodeEffect, WorkerAuthorityConfig};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentRunRecoveryContext, AgentRunRecoveryEffectSource,
};
use std::collections::HashSet;
use tonic::Status;

fn denied() -> Status {
    Status::failed_precondition("AGENT_RECOVERY_CUSTODY_DENIED")
}

impl AuthorityStore {
    /// Read only. This neither settles Go actions nor signs, rebinds or starts
    /// an effect. The caller also checks every receipt in Rust's signer journal.
    pub(crate) fn verify_agent_recovery_context(
        &self,
        context: &AgentRunRecoveryContext,
        config: &WorkerAuthorityConfig,
    ) -> Result<(), Status> {
        if context.schema_version != 1
            || context.run_id.is_empty()
            || context.expected_index < 1
            || context.metadata_epoch == 0
            || context.generation == 0
            || context.generation > i64::MAX as u64
            || context.prior_effects.len() > 2048
            || context.retained_outputs.len() > 4
        {
            return Err(denied());
        }
        let start = match &context.previous_scope {
            None if context.generation == 1 => 0,
            Some(scope)
                if scope.generation.checked_add(1) == Some(context.generation)
                    && scope.start_index > 0
                    && scope.start_index < context.expected_index =>
            {
                scope.start_index
            }
            _ => return Err(denied()),
        };
        // An older unresolved effect is still unsafe. A generation checkpoint
        // limits new proof enumeration, never this all-history safety check.
        let unresolved: bool = self.connection.query_row(
            "SELECT EXISTS(SELECT 1 FROM agent_node_effects WHERE run_id=?1 AND state!='CONFIRMED')",
            [&context.run_id], |row| row.get(0),
        ).map_err(|_| denied())?;
        if unresolved {
            return Err(denied());
        }

        let mut supplied = HashSet::new();
        for source in &context.prior_effects {
            self.verify_recovery_source(context, source, config, false)?;
            let dispatch = source.dispatch.as_ref().ok_or_else(denied)?;
            if dispatch.metadata_index < start || !supplied.insert(dispatch.operation_id.clone()) {
                return Err(denied());
            }
        }
        let mut query=self.connection.prepare(
            "SELECT operation_id FROM agent_node_effects WHERE run_id=?1 AND metadata_index>=?2 LIMIT 2049",
        ).map_err(|_| denied())?;
        let known = query
            .query_map(rusqlite::params![context.run_id, start], |row| {
                row.get::<_, String>(0)
            })
            .map_err(|_| denied())?
            .collect::<Result<HashSet<_>, _>>()
            .map_err(|_| denied())?;
        if known.len() > 2048 || supplied != known {
            return Err(denied());
        }

        if context.retained_ids.len() != context.retained_outputs.len() {
            return Err(denied());
        }
        let mut retained = HashSet::new();
        for (id, source) in context.retained_ids.iter().zip(&context.retained_outputs) {
            self.verify_recovery_source(context, source, config, true)?;
            let dispatch = source.dispatch.as_ref().ok_or_else(denied)?;
            if id != &dispatch.node_id || !retained.insert(id) {
                return Err(denied());
            }
        }
        Ok(())
    }

    fn verify_recovery_source(
        &self,
        context: &AgentRunRecoveryContext,
        source: &AgentRunRecoveryEffectSource,
        config: &WorkerAuthorityConfig,
        retained: bool,
    ) -> Result<(), Status> {
        let dispatch = source.dispatch.as_ref().ok_or_else(denied)?;
        let receipt = source.outcome.as_ref().ok_or_else(denied)?;
        let evidence = receipt.evidence.as_ref().ok_or_else(denied)?;
        if dispatch.run_id != context.run_id
            || dispatch.metadata_index < 0
            || dispatch.metadata_index >= context.expected_index
            || dispatch.metadata_epoch == 0
            || dispatch.metadata_epoch > context.metadata_epoch
            || receipt.schema_version != 1
            || receipt.signature.len() != 64
            || receipt.signer_public_key != config.signer_public_key
            || source.settlement_metadata_index < 0
            || source.settlement_metadata_index >= context.expected_index
            || source.settlement_metadata_index > 0
                && source.settlement_metadata_index < dispatch.metadata_index
            || retained && (dispatch.phase != 2 || source.settlement_metadata_index == 0)
        {
            return Err(denied());
        }
        let original = stored(&self.connection, &dispatch.operation_id)?.ok_or_else(denied)?;
        if original.state != "CONFIRMED" || original.intent != *dispatch {
            return Err(denied());
        }
        let expected = self.agent_node_outcome_evidence(
            &QueryAgentNodeEffect {
                operation_id: dispatch.operation_id.clone(),
                fence: dispatch.fence.clone().ok_or_else(denied)?,
                run_id: dispatch.run_id.clone(),
                node_id: dispatch.node_id.clone(),
            },
            evidence.public_event.clone().ok_or_else(denied)?,
            config,
        )?;
        if evidence != &expected {
            return Err(denied());
        }
        Ok(())
    }
}
