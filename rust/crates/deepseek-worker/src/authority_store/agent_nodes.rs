use super::{AuthorityStore, WorkerAuthority, error};
use crate::agent_node_effect::{
    self, AgentNodeEffectOutcome, AgentNodeEffectPermit, AgentNodeEffectStart,
};
use crate::{WorkerAuthorityConfig, authority_request};
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentNodeDispatchIntent, AgentRunExecutionClaim, BeginAgentNodeDispatchResponse,
};
use prost::Message;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use tonic::Status;

#[cfg(test)]
mod tests;

pub(super) const SCHEMA_V5: &[&str] = &[
    "CREATE TABLE agent_node_effects (operation_id TEXT PRIMARY KEY NOT NULL, action_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), fencing_token INTEGER NOT NULL CHECK(fencing_token>0), run_id TEXT NOT NULL, phase INTEGER NOT NULL CHECK(phase IN (1,2)), metadata_index INTEGER NOT NULL CHECK(metadata_index>=0), node_id TEXT NOT NULL, claim BLOB NOT NULL CHECK(length(claim) BETWEEN 1 AND 65536), intent BLOB NOT NULL CHECK(length(intent) BETWEEN 1 AND 65536), grant BLOB NOT NULL CHECK(length(grant) BETWEEN 1 AND 16384), claim_scope_digest TEXT NOT NULL CHECK(length(claim_scope_digest)=64), intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64), grant_digest TEXT NOT NULL CHECK(length(grant_digest)=64), state TEXT NOT NULL CHECK(state IN ('DISPATCHING','EFFECT_UNKNOWN','CONFIRMED')), output BLOB, output_sha256 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(action_id,epoch,node_id), UNIQUE(run_id,phase,metadata_index,node_id), CHECK((state='CONFIRMED' AND output IS NOT NULL AND length(output) BETWEEN 1 AND 1048576 AND length(output_sha256)=64) OR (state!='CONFIRMED' AND output IS NULL AND output_sha256 IS NULL))) STRICT",
    "CREATE TRIGGER agent_node_fence BEFORE INSERT ON agent_node_effects WHEN NEW.fencing_token!=(SELECT fencing_token FROM worker_authority WHERE id=1) OR NOT EXISTS (SELECT 1 FROM epoch_installs e WHERE e.action_id=NEW.action_id AND e.epoch=NEW.epoch AND e.fencing_token=NEW.fencing_token AND e.epoch=(SELECT MAX(epoch) FROM epoch_installs WHERE action_id=NEW.action_id)) BEGIN SELECT RAISE(ABORT,'stale Agent node effect fence'); END",
    "CREATE TRIGGER agent_node_no_delete BEFORE DELETE ON agent_node_effects BEGIN SELECT RAISE(ABORT,'immutable Agent node journal'); END",
    "CREATE TRIGGER agent_node_no_replace BEFORE INSERT ON agent_node_effects WHEN EXISTS (SELECT 1 FROM agent_node_effects e WHERE e.rowid=NEW.rowid OR e.operation_id=NEW.operation_id OR (e.action_id=NEW.action_id AND e.epoch=NEW.epoch AND e.node_id=NEW.node_id) OR (e.run_id=NEW.run_id AND e.phase=NEW.phase AND e.metadata_index=NEW.metadata_index AND e.node_id=NEW.node_id)) BEGIN SELECT RAISE(ABORT,'immutable Agent node identity'); END",
    "CREATE TRIGGER agent_node_identity_immutable BEFORE UPDATE ON agent_node_effects WHEN NEW.rowid IS NOT OLD.rowid OR NEW.operation_id IS NOT OLD.operation_id OR NEW.action_id IS NOT OLD.action_id OR NEW.epoch IS NOT OLD.epoch OR NEW.fencing_token IS NOT OLD.fencing_token OR NEW.run_id IS NOT OLD.run_id OR NEW.phase IS NOT OLD.phase OR NEW.metadata_index IS NOT OLD.metadata_index OR NEW.node_id IS NOT OLD.node_id OR NEW.claim IS NOT OLD.claim OR NEW.intent IS NOT OLD.intent OR NEW.grant IS NOT OLD.grant OR NEW.claim_scope_digest IS NOT OLD.claim_scope_digest OR NEW.intent_digest IS NOT OLD.intent_digest OR NEW.grant_digest IS NOT OLD.grant_digest OR NEW.created_at IS NOT OLD.created_at BEGIN SELECT RAISE(ABORT,'immutable Agent node scope'); END",
    "CREATE TRIGGER agent_node_terminal_immutable BEFORE UPDATE ON agent_node_effects WHEN OLD.state='CONFIRMED' BEGIN SELECT RAISE(ABORT,'immutable Agent node outcome'); END",
    "CREATE TRIGGER agent_node_transition BEFORE UPDATE ON agent_node_effects WHEN NOT (OLD.state=NEW.state OR (OLD.state='DISPATCHING' AND NEW.state IN ('EFFECT_UNKNOWN','CONFIRMED')) OR (OLD.state='EFFECT_UNKNOWN' AND NEW.state='CONFIRMED')) BEGIN SELECT RAISE(ABORT,'illegal Agent node effect transition'); END",
    "CREATE TRIGGER agent_node_time_monotonic BEFORE UPDATE ON agent_node_effects WHEN NEW.updated_at<OLD.updated_at OR NEW.updated_at<NEW.created_at BEGIN SELECT RAISE(ABORT,'Agent node journal clock moved backwards'); END",
];

fn denied() -> Status {
    agent_node_effect::denied()
}

fn claim_scope_digest(claim: &AgentRunExecutionClaim) -> Result<String, Status> {
    let mut value = crate::agent_execution_grant::claim_payload(claim)?;
    let map = value.as_object_mut().ok_or_else(denied)?;
    map.remove("leaseUntil");
    map.remove("writerLeaseUntil");
    Ok(agent_node_effect::digest(
        &authority_request::canonical_json_bytes(&value).map_err(|_| denied())?,
    ))
}

fn live(connection: &Connection, fence: &ActionFence, token: i64) -> Result<(), Status> {
    let epoch = i64::try_from(fence.execution_epoch).map_err(|_| denied())?;
    let valid: bool = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM worker_authority a JOIN epoch_installs e ON e.fencing_token=a.fencing_token WHERE a.id=1 AND a.fencing_token=?1 AND e.action_id=?2 AND e.epoch=?3 AND e.epoch=(SELECT MAX(epoch) FROM epoch_installs WHERE action_id=?2))",
        params![token, fence.action_id, epoch], |row| row.get(0),
    ).map_err(|_| denied())?;
    if valid { Ok(()) } else { Err(denied()) }
}

struct Stored {
    intent: AgentNodeDispatchIntent,
    claim: AgentRunExecutionClaim,
    grant: Vec<u8>,
    scope_digest: String,
    intent_digest: String,
    grant_digest: String,
    state: String,
    output: Option<Vec<u8>>,
    output_digest: Option<String>,
    created_at: String,
    updated_at: String,
    token: i64,
    action_id: String,
    epoch: u64,
    run_id: String,
    phase: i32,
    metadata_index: i64,
    node_id: String,
    operation_id: String,
}

fn stored(connection: &Connection, operation: &str) -> Result<Option<Stored>, Status> {
    connection.query_row(
        "SELECT intent,claim,grant,claim_scope_digest,intent_digest,grant_digest,state,output,output_sha256,created_at,updated_at,fencing_token,action_id,epoch,run_id,phase,metadata_index,node_id,operation_id FROM agent_node_effects WHERE operation_id=?1",
        [operation], |row| {
            let intent: Vec<u8> = row.get(0)?;
            let claim: Vec<u8> = row.get(1)?;
            let convert = |column, bytes: Vec<u8>, kind| rusqlite::Error::FromSqlConversionFailure(column, rusqlite::types::Type::Blob, Box::new(std::io::Error::new(std::io::ErrorKind::InvalidData, format!("invalid Agent {kind}: {} bytes", bytes.len()))));
            let decoded_intent = AgentNodeDispatchIntent::decode(intent.as_slice()).map_err(|_| convert(0, intent.clone(), "intent"))?;
            let decoded_claim = AgentRunExecutionClaim::decode(claim.as_slice()).map_err(|_| convert(1, claim.clone(), "claim"))?;
            if decoded_intent.encode_to_vec()!=intent || decoded_claim.encode_to_vec()!=claim {
                return Err(convert(0, intent, "encoding"));
            }
            let epoch: i64 = row.get(13)?;
            Ok(Stored {intent: decoded_intent, claim: decoded_claim, grant: row.get(2)?, scope_digest: row.get(3)?,
                intent_digest: row.get(4)?, grant_digest: row.get(5)?, state: row.get(6)?, output: row.get(7)?,
                output_digest: row.get(8)?, created_at: row.get(9)?, updated_at: row.get(10)?, token: row.get(11)?,
                action_id: row.get(12)?, epoch: epoch as u64, run_id: row.get(14)?, phase: row.get(15)?,
                metadata_index: row.get(16)?, node_id: row.get(17)?, operation_id: row.get(18)?})
        },
    ).optional().map_err(|_| denied())
}

fn outcome(row: Stored) -> Result<AgentNodeEffectOutcome, Status> {
    let output = row.output.ok_or_else(denied)?;
    let output_sha256 = row.output_digest.ok_or_else(denied)?;
    if row.state != "CONFIRMED"
        || output.is_empty()
        || output.len() > crate::MAX_AGENT_NODE_OUTCOME_BYTES
        || agent_node_effect::digest(&output) != output_sha256
    {
        return Err(denied());
    }
    let value: serde_json::Value = serde_json::from_slice(&output).map_err(|_| denied())?;
    if authority_request::canonical_json_bytes(&value).map_err(|_| denied())? != output {
        return Err(denied());
    }
    Ok(AgentNodeEffectOutcome {
        operation_id: row.operation_id,
        fence: row.intent.fence.ok_or_else(denied)?,
        node_id: row.node_id,
        output_sha256,
        output,
    })
}

impl AuthorityStore {
    pub(crate) fn agent_node_snapshot(
        &self,
        query: &crate::QueryAgentNodeEffect,
    ) -> Result<Option<crate::AgentNodeEffectSnapshot>, Status> {
        let Some(row) = stored(&self.connection, &query.operation_id)? else {
            return Ok(None);
        };
        let fence = row.intent.fence.as_ref().ok_or_else(denied)?;
        if fence != &query.fence || row.run_id != query.run_id || row.node_id != query.node_id {
            return Err(denied());
        }
        let mut snapshot = crate::AgentNodeEffectSnapshot {
            operation_id: row.operation_id.clone(),
            fence: fence.clone(),
            run_id: row.run_id.clone(),
            node_id: row.node_id.clone(),
            state: row.state.clone(),
            output_sha256: None,
            output_length: None,
        };
        if row.state == "CONFIRMED" {
            let known = outcome(row)?;
            snapshot.output_length = Some(known.output.len() as u64);
            snapshot.output_sha256 = Some(known.output_sha256);
        } else if row.output.is_some() || row.output_digest.is_some() {
            return Err(denied());
        }
        Ok(Some(snapshot))
    }

    pub(crate) fn begin_agent_node(
        &mut self,
        response: &BeginAgentNodeDispatchResponse,
        grant: &[u8],
        config: &WorkerAuthorityConfig,
        now: &str,
    ) -> Result<AgentNodeEffectStart, Status> {
        agent_node_effect::validate_scope(response, grant, config, now)?;
        let claim = response.claim.as_ref().ok_or_else(denied)?;
        let intent = response.intent.as_ref().ok_or_else(denied)?;
        let fence = intent.fence.as_ref().ok_or_else(denied)?;
        let scope = claim_scope_digest(claim)?;
        let intent_bytes = intent.encode_to_vec();
        let intent_digest = agent_node_effect::digest(&intent_bytes);
        let grant_digest = agent_node_effect::digest(grant);
        let token = self.fencing_token;
        if token != config.fencing_token {
            return Err(denied());
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| denied())?;
        live(&transaction, fence, token)?;
        if let Some(row) = stored(&transaction, &intent.operation_id)? {
            if row.intent_digest != intent_digest || row.scope_digest != scope || row.token != token
            {
                return Err(denied());
            }
            return if row.state == "CONFIRMED" {
                Ok(AgentNodeEffectStart::Confirmed(outcome(row)?))
            } else {
                Ok(AgentNodeEffectStart::EffectUnknown)
            };
        }
        let state = if response.fresh {
            "DISPATCHING"
        } else {
            "EFFECT_UNKNOWN"
        };
        transaction.execute("INSERT INTO agent_node_effects(operation_id,action_id,epoch,fencing_token,run_id,phase,metadata_index,node_id,claim,intent,grant,claim_scope_digest,intent_digest,grant_digest,state,created_at,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?16)",
            params![intent.operation_id, fence.action_id, fence.execution_epoch as i64, token, intent.run_id, intent.phase,
                intent.metadata_index, intent.node_id, claim.encode_to_vec(), intent_bytes, grant, scope, intent_digest, grant_digest, state, now]).map_err(|_| denied())?;
        transaction.commit().map_err(|_| denied())?;
        if response.fresh {
            Ok(AgentNodeEffectStart::Started(AgentNodeEffectPermit {
                operation_id: intent.operation_id.clone(),
                fence: fence.clone(),
                fencing_token: token,
                grant_digest,
                intent_digest,
            }))
        } else {
            Ok(AgentNodeEffectStart::EffectUnknown)
        }
    }

    pub(crate) fn confirm_agent_node(
        &mut self,
        permit: &AgentNodeEffectPermit,
        output: &[u8],
        now: &str,
    ) -> Result<AgentNodeEffectOutcome, Status> {
        if output.is_empty()
            || output.len() > crate::MAX_AGENT_NODE_OUTCOME_BYTES
            || self.fencing_token != permit.fencing_token
        {
            return Err(denied());
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| denied())?;
        live(&transaction, &permit.fence, permit.fencing_token)?;
        let row = stored(&transaction, &permit.operation_id)?.ok_or_else(denied)?;
        let at = authority_request::parse_utc_z_str(now).map_err(|_| denied())?;
        if at < authority_request::parse_utc_z_str(&row.updated_at).map_err(|_| denied())? {
            return Err(denied());
        }
        if row.intent_digest != permit.intent_digest || row.grant_digest != permit.grant_digest {
            return Err(denied());
        }
        if row.state == "CONFIRMED" {
            let retained = outcome(row)?;
            return if retained.output == output {
                Ok(retained)
            } else {
                Err(denied())
            };
        }
        let digest = agent_node_effect::digest(output);
        let changed = transaction.execute("UPDATE agent_node_effects SET state='CONFIRMED',output=?1,output_sha256=?2,updated_at=?3 WHERE operation_id=?4 AND state IN ('DISPATCHING','EFFECT_UNKNOWN')",
            params![output, digest, now, permit.operation_id]).map_err(|_| denied())?;
        if changed != 1 {
            return Err(denied());
        }
        let result = outcome(stored(&transaction, &permit.operation_id)?.ok_or_else(denied)?)?;
        transaction.commit().map_err(|_| denied())?;
        Ok(result)
    }

    pub(crate) fn mark_agent_node_unknown(
        &mut self,
        permit: &AgentNodeEffectPermit,
        now: &str,
    ) -> Result<(), Status> {
        if self.fencing_token != permit.fencing_token {
            return Err(denied());
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| denied())?;
        live(&transaction, &permit.fence, permit.fencing_token)?;
        let row = stored(&transaction, &permit.operation_id)?.ok_or_else(denied)?;
        let at = authority_request::parse_utc_z_str(now).map_err(|_| denied())?;
        if at < authority_request::parse_utc_z_str(&row.updated_at).map_err(|_| denied())? {
            return Err(denied());
        }
        if row.intent_digest != permit.intent_digest
            || row.grant_digest != permit.grant_digest
            || row.state == "CONFIRMED"
        {
            return Err(denied());
        }
        transaction.execute("UPDATE agent_node_effects SET state='EFFECT_UNKNOWN',updated_at=?1 WHERE operation_id=?2", params![now, permit.operation_id]).map_err(|_| denied())?;
        transaction.commit().map_err(|_| denied())?;
        Ok(())
    }
}

pub(super) fn validate_journal(
    connection: &Connection,
    authority: &WorkerAuthority,
    head_token: i64,
) -> Result<(), crate::AuthorityRequestError> {
    let mut statement = connection
        .prepare("SELECT operation_id FROM agent_node_effects")
        .map_err(|_| error())?;
    let operations = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| error())?;
    for operation in operations {
        let row = stored(connection, &operation)
            .map_err(|_| error())?
            .ok_or_else(error)?;
        if row.token > head_token
            || row.epoch == 0
            || row.epoch > i64::MAX as u64
            || row.intent.operation_id != row.operation_id
            || row.intent.fence.as_ref()
                != Some(&ActionFence {
                    action_id: row.action_id.clone(),
                    execution_epoch: row.epoch,
                })
            || row.intent.run_id != row.run_id
            || row.intent.phase != row.phase
            || row.intent.metadata_index != row.metadata_index
            || row.intent.node_id != row.node_id
            || agent_node_effect::digest(&row.intent.encode_to_vec()) != row.intent_digest
            || agent_node_effect::digest(&row.grant) != row.grant_digest
            || claim_scope_digest(&row.claim).map_err(|_| error())? != row.scope_digest
            || authority_request::parse_utc_z_str(&row.updated_at)?
                < authority_request::parse_utc_z_str(&row.created_at)?
        {
            return Err(error());
        }
        let config = WorkerAuthorityConfig {
            signer_public_key: authority.signer_public_key.clone(),
            fleet_id: authority.fleet_id.clone(),
            environment: authority.environment.clone(),
            fencing_token: row.token,
            now: None,
        };
        let response = BeginAgentNodeDispatchResponse {
            fresh: false,
            intent: Some(row.intent.clone()),
            claim: Some(row.claim.clone()),
        };
        agent_node_effect::validate_scope(&response, &row.grant, &config, &row.created_at)
            .map_err(|_| error())?;
        let installed: bool = connection.query_row("SELECT EXISTS(SELECT 1 FROM epoch_installs WHERE action_id=?1 AND epoch=?2 AND fencing_token=?3)",
            params![row.action_id, row.epoch as i64, row.token], |value| value.get(0)).map_err(|_| error())?;
        if !installed {
            return Err(error());
        }
        if row.state == "CONFIRMED" {
            outcome(row).map_err(|_| error())?;
        } else if row.output.is_some() || row.output_digest.is_some() {
            return Err(error());
        }
    }
    Ok(())
}
