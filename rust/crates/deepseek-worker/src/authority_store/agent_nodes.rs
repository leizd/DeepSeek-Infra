use super::{AuthorityStore, WorkerAuthority, error};
use crate::agent_node_effect::{
    self, AgentNodeEffectOutcome, AgentNodeEffectPermit, AgentNodeEffectStart,
};
use crate::{WorkerAuthorityConfig, authority_request};
use authority_request::AuthorityRequestError;
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentNodeDispatchIntent, AgentRunExecutionClaim, BeginAgentNodeDispatchResponse,
};
use prost::Message;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use tonic::Status;

mod recovery;
mod task_outputs;
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

pub(super) fn schema_v6() -> Vec<String> {
    let mut schema: Vec<String> = SCHEMA_V5.iter().map(|sql| sql.to_string()).collect();
    // SQLite quotes the final name after ALTER TABLE RENAME. The validator still
    // compares every table/trigger definition exactly, including this new CHECK.
    schema[0] = schema[0]
        .replace(
            "CREATE TABLE agent_node_effects ",
            "CREATE TABLE \"agent_node_effects\" ",
        )
        .replace("phase IN (1,2)", "phase IN (1,2,3)");
    schema
}

pub(super) fn migrate_v6(connection: &Connection) -> Result<(), AuthorityRequestError> {
    // Called only inside AuthorityStore::open's IMMEDIATE transaction, after
    // exact v5 validation. Use SQLite's create/copy/drop/rename procedure; never
    // alter sqlite_schema or overwrite historical outcomes/claim encodings.
    // https://www.sqlite.org/lang_altertable.html#making_other_kinds_of_table_schema_changes
    let sql = SCHEMA_V5[0]
        .replace(
            "CREATE TABLE agent_node_effects ",
            "CREATE TABLE agent_node_effects_v6 ",
        )
        .replace("phase IN (1,2)", "phase IN (1,2,3)");
    connection.execute_batch(&sql).map_err(|_| error())?;
    connection.execute_batch("INSERT INTO agent_node_effects_v6(rowid,operation_id,action_id,epoch,fencing_token,run_id,phase,metadata_index,node_id,claim,intent,grant,claim_scope_digest,intent_digest,grant_digest,state,output,output_sha256,created_at,updated_at) SELECT rowid,operation_id,action_id,epoch,fencing_token,run_id,phase,metadata_index,node_id,claim,intent,grant,claim_scope_digest,intent_digest,grant_digest,state,output,output_sha256,created_at,updated_at FROM agent_node_effects; DROP TABLE agent_node_effects; ALTER TABLE agent_node_effects_v6 RENAME TO agent_node_effects;").map_err(|_| error())?;
    // Install insert fencing only after historical rows have been preserved:
    // their old writer token/epoch must not be mistaken for fresh admission.
    for trigger in &SCHEMA_V5[1..] {
        connection.execute_batch(trigger).map_err(|_| error())?;
    }
    Ok(())
}

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
    pub(crate) fn agent_node_outcome_evidence(
        &self,
        query: &crate::QueryAgentNodeEffect,
        public_event: deepseek_protocol::generated::deepseek::agent::v1::AgentArtifactReference,
        config: &WorkerAuthorityConfig,
    ) -> Result<deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeEvidence, Status>
    {
        use deepseek_protocol::generated::deepseek::agent::v1::{
            AgentNodeOutcomeEvidence, AgentRunPlanNode, AgentTaskOutputMetadata,
        };
        let snapshot = self.agent_node_snapshot(query)?.ok_or_else(denied)?;
        if snapshot.state != "CONFIRMED" {
            return Err(denied());
        }
        let row = stored(&self.connection, &query.operation_id)?.ok_or_else(denied)?;
        let output: serde_json::Value =
            serde_json::from_slice(row.output.as_ref().ok_or_else(denied)?)
                .map_err(|_| denied())?;
        let planner = row.phase == 1
            && row.node_id == "leader"
            && output["publicEvent"]["type"] == "agent_plan";
        let task = row.phase == 2
            && deepseek_policy::multi_agent::profile(&row.node_id).is_some()
            && output["publicEvent"]["type"] == "agent_output"
            && output["publicEvent"]["phase"] == row.node_id
            && output["publicEvent"]["output"]["id"] == row.node_id
            && output["publicEvent"]["output"]["task"] == row.intent.task;
        let synthesis = row.phase == 3
            && row.node_id == "synthesizer"
            && output["publicEvent"]["type"] == "content"
            && output["publicEvent"]["text"]
                .as_str()
                .is_some_and(|text| !text.trim().is_empty());
        if output["runId"] != query.run_id || !(planner || task || synthesis) {
            return Err(denied());
        }
        let event = deepseek_policy::agent_run_data::sanitize(&output["publicEvent"]);
        let bytes = serde_json::to_vec(&event).map_err(|_| denied())?;
        use sha2::{Digest as _, Sha256};
        if public_event.sha256 != format!("{:x}", Sha256::digest(&bytes))
            || public_event.length != bytes.len() as u64
        {
            return Err(denied());
        }
        // These are provider scheduling candidates; Go owns their normalization,
        // admission and the resulting plan/DAG. Bodies remain in the Rust store.
        let provider_plan = event["plan"]
            .get("tasks")
            .or_else(|| event["plan"].get("agents"))
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|task| {
                let id = task.get("role").or_else(|| task.get("id"))?.as_str()?;
                let text = task
                    .get("task")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("");
                let depends_on = task
                    .get("depends_on")
                    .or_else(|| task.get("dependsOn"))
                    .and_then(serde_json::Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(serde_json::Value::as_str)
                    .map(str::to_owned)
                    .collect();
                Some(AgentRunPlanNode {
                    id: id.into(),
                    task: text.into(),
                    depends_on,
                })
            })
            .collect();
        let task_output = if task || synthesis {
            let result = if synthesis {
                &output["synthesisOutput"]
            } else {
                &event["output"]
            };
            let counter = |key: &str| -> Result<u64, Status> {
                match result["usage"].get(key) {
                    None => Ok(0),
                    Some(value) => value
                        .as_u64()
                        .filter(|count| *count <= i64::MAX as u64)
                        .ok_or_else(denied),
                }
            };
            let failed = match result.get("failed") {
                None => false,
                Some(value) => value.as_bool().ok_or_else(denied)?,
            };
            Some(AgentTaskOutputMetadata {
                schema_version: 1,
                prompt_tokens: counter("prompt_tokens")?,
                completion_tokens: counter("completion_tokens")?,
                latency_ms: result["latencyMs"]
                    .as_i64()
                    .filter(|ms| *ms >= 0)
                    .ok_or_else(denied)?,
                failed,
            })
        } else {
            None
        };
        Ok(AgentNodeOutcomeEvidence {
            schema_version: 1,
            fence: row.intent.fence,
            operation_id: row.operation_id,
            run_id: row.run_id,
            node_id: row.node_id,
            phase: row.phase,
            request: row.intent.request,
            plan_digest: row.intent.plan_digest,
            metadata_index: row.metadata_index,
            metadata_epoch: row.intent.metadata_epoch,
            writer_fencing_token: row.token,
            created_at: row.created_at,
            confirmed_at: row.updated_at,
            outcome_sha256: snapshot.output_sha256.ok_or_else(denied)?,
            outcome_length: snapshot.output_length.ok_or_else(denied)?,
            public_event: Some(public_event),
            provider_plan,
            task_output,
            fleet_id: config.fleet_id.clone(),
            environment: config.environment.clone(),
        })
    }

    /// Historical reads do not spend a dispatch reservation or require a live
    /// lease. The complete outcome stays in Rust and is projected by its executor.
    pub(crate) fn agent_node_outcome(
        &self,
        query: &crate::QueryAgentNodeEffect,
    ) -> Result<Option<AgentNodeEffectOutcome>, Status> {
        let Some(snapshot) = self.agent_node_snapshot(query)? else {
            return Ok(None);
        };
        if snapshot.state != "CONFIRMED" {
            return Ok(None);
        }
        stored(&self.connection, &query.operation_id)?
            .map(outcome)
            .transpose()
    }

    /// Recheck the stored signed grant, writer and installed epoch immediately
    /// before starting provider I/O. A permit alone cannot outlive its lease.
    pub(crate) fn agent_node_send_deadline(
        &self,
        permit: &AgentNodeEffectPermit,
        config: &WorkerAuthorityConfig,
        now: &str,
    ) -> Result<i64, Status> {
        if self.fencing_token != permit.fencing_token
            || config.fencing_token != permit.fencing_token
        {
            return Err(denied());
        }
        live(&self.connection, &permit.fence, permit.fencing_token)?;
        let row = stored(&self.connection, &permit.operation_id)?.ok_or_else(denied)?;
        let at = authority_request::parse_utc_z_str(now).map_err(|_| denied())?;
        if row.state != "DISPATCHING"
            || row.intent_digest != permit.intent_digest
            || agent_node_effect::digest(&permit.intent.encode_to_vec()) != permit.intent_digest
            || row.grant_digest != permit.grant_digest
            || at < authority_request::parse_utc_z_str(&row.updated_at).map_err(|_| denied())?
        {
            return Err(denied());
        }
        let response = BeginAgentNodeDispatchResponse {
            fresh: false,
            intent: Some(row.intent),
            claim: Some(row.claim),
        };
        agent_node_effect::validate_scope(&response, &row.grant, config, now)?;
        let grant: serde_json::Value = serde_json::from_slice(&row.grant).map_err(|_| denied())?;
        let expires =
            authority_request::parse_utc_z_str(grant["expiresAt"].as_str().ok_or_else(denied)?)
                .map_err(|_| denied())?;
        let claim = response.claim.as_ref().ok_or_else(denied)?;
        Ok(expires.min(claim.lease_until).min(claim.writer_lease_until))
    }

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

    /// Reuse only the original immutable node custody under the exact admitted
    /// scope and signed grant. This read never renews, starts or repeats an effect.
    pub(crate) fn retained_agent_node_start(
        &self,
        input: &crate::BeginAgentNodeEffect,
        config: &WorkerAuthorityConfig,
    ) -> Result<Option<AgentNodeEffectStart>, Status> {
        if input.operation_id.is_empty() || input.actor.is_empty() || input.node_id.is_empty() {
            return Err(denied());
        }
        let fence = input.claim.fence.as_ref().ok_or_else(denied)?;
        let epoch = i64::try_from(fence.execution_epoch).map_err(|_| denied())?;
        // The caller's command ID indexes Go's operator receipt. Rust custody
        // uses Go's independently derived agent-node ID, unique per fence/node.
        let operation: Option<String> = self.connection.query_row(
            "SELECT operation_id FROM agent_node_effects WHERE action_id=?1 AND epoch=?2 AND node_id=?3",
            params![fence.action_id, epoch, input.node_id], |row| row.get(0),
        ).optional().map_err(|_| denied())?;
        let Some(operation) = operation else {
            return Ok(None);
        };
        let row = stored(&self.connection, &operation)?.ok_or_else(denied)?;
        if row.operation_id != operation
            || row.action_id != fence.action_id
            || row.epoch != fence.execution_epoch
            || row.node_id != input.node_id
            || row.run_id != input.claim.run_id
            || row.intent.fence.as_ref() != input.claim.fence.as_ref()
            || row.scope_digest != claim_scope_digest(&input.claim)?
            || row.grant_digest != agent_node_effect::digest(&input.signed_phase_grant)
            || row.token != config.fencing_token
            || row.intent_digest != agent_node_effect::digest(&row.intent.encode_to_vec())
            || row.grant_digest != agent_node_effect::digest(&row.grant)
        {
            return Err(denied());
        }
        let response = BeginAgentNodeDispatchResponse {
            fresh: false,
            intent: Some(row.intent.clone()),
            claim: Some(row.claim.clone()),
        };
        agent_node_effect::validate_scope(&response, &row.grant, config, &row.created_at)?;
        match row.state.as_str() {
            "CONFIRMED" => Ok(Some(AgentNodeEffectStart::Confirmed(outcome(row)?))),
            "DISPATCHING" | "EFFECT_UNKNOWN"
                if row.output.is_none() && row.output_digest.is_none() =>
            {
                Ok(Some(AgentNodeEffectStart::EffectUnknown))
            }
            _ => Err(denied()),
        }
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
                intent: Box::new(intent.clone()),
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
        if row.intent_digest != permit.intent_digest
            || row.grant_digest != permit.grant_digest
            || agent_node_effect::digest(&permit.intent.encode_to_vec()) != permit.intent_digest
        {
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
