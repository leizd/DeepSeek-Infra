use std::sync::{Mutex, OnceLock};

use serde_json::Value;

use crate::memory::new_task_id;
use crate::model::{
    BACKUP_FENCE_TTL_MS, BackupCapabilities, BackupFence, CreateTaskInput, CreateTaskResult,
    RESTORE_FENCE_TTL_MS, RESTORE_JOURNAL_TTL_MS, RestoreJournal, RestorePhase, StoreError,
    TaskOutcome, TaskRecord, canonical_request_hash, capabilities, deterministic_task_id,
    make_task, outcome_json, portable_task, task_digest, task_json,
};
use crate::redis_client::{BlockingRedisClient, RedisValue};
use crate::restore::{self, DecisionAction, Lookup, StagedRestoreDecision};

struct LuaScripts {
    create: String,
    claim: String,
    heartbeat: String,
    complete: String,
    journal_transition: String,
    fence_acquire: String,
    fence_release: String,
    install: String,
    uninstall: String,
    clear_pending: String,
    finalize: String,
    reset_epoch: String,
}

pub struct RedisTaskStore {
    client: Mutex<BlockingRedisClient>,
    prefix: String,
}

impl RedisTaskStore {
    pub fn connect(url: &str, prefix: &str) -> Result<Self, String> {
        Ok(Self {
            client: Mutex::new(BlockingRedisClient::connect(url)?),
            prefix: prefix.to_string(),
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, BlockingRedisClient> {
        self.client
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn create_or_get(&self, input: CreateTaskInput) -> Result<CreateTaskResult, StoreError> {
        let mut client = self.lock();
        create_or_get(&mut client, &self.prefix, input)
    }

    pub fn get(&self, task_id: &str) -> Result<Option<TaskRecord>, StoreError> {
        let mut client = self.lock();
        let raw = get_text(&mut client, &task_key(&self.prefix, task_id))?;
        let Some(raw) = raw else { return Ok(None) };
        let task = parse_task(&raw)?;
        Ok(if task.restore_pending.is_none() {
            Some(task)
        } else {
            None
        })
    }

    pub fn claim(
        &self,
        instance_id: &str,
        now: i64,
        lease_ms: i64,
    ) -> Result<Option<TaskRecord>, StoreError> {
        let mut client = self.lock();
        let raw = as_bulk(eval(
            &mut client,
            &scripts().claim,
            &[
                queue_key(&self.prefix),
                backup_fence_key(&self.prefix),
                generation_key(&self.prefix),
                restore_fence_key(&self.prefix),
            ],
            &[
                now.to_string(),
                instance_id.to_string(),
                lease_ms.to_string(),
                task_prefix(&self.prefix),
            ],
        )?)?;
        if raw.is_empty() {
            Ok(None)
        } else {
            Ok(Some(parse_task(&raw)?))
        }
    }

    pub fn heartbeat(
        &self,
        task_id: &str,
        instance_id: &str,
        now: i64,
        lease_ms: i64,
    ) -> Result<bool, StoreError> {
        let mut client = self.lock();
        let updated = as_int(eval(
            &mut client,
            &scripts().heartbeat,
            &[task_key(&self.prefix, task_id), queue_key(&self.prefix)],
            &[
                instance_id.to_string(),
                now.to_string(),
                lease_ms.to_string(),
            ],
        )?)?;
        Ok(updated == 1)
    }

    pub fn complete(
        &self,
        task_id: &str,
        instance_id: &str,
        outcome: TaskOutcome,
        now: i64,
    ) -> Result<Option<TaskRecord>, StoreError> {
        let mut client = self.lock();
        let raw = as_bulk(eval(
            &mut client,
            &scripts().complete,
            &[
                task_key(&self.prefix, task_id),
                queue_key(&self.prefix),
                generation_key(&self.prefix),
                restore_fence_key(&self.prefix),
            ],
            &[
                instance_id.to_string(),
                outcome_json(&outcome),
                now.to_string(),
            ],
        )?)?;
        if raw == "FENCED" {
            return Err(StoreError::RestoreFenced);
        }
        if raw.is_empty() {
            Ok(None)
        } else {
            Ok(Some(parse_task(&raw)?))
        }
    }

    pub fn backup_capabilities(&self) -> Result<BackupCapabilities, StoreError> {
        let mut client = self.lock();
        match client.call(&[b"PING"]).map_err(StoreError::Message)? {
            RedisValue::Status(text) | RedisValue::Bulk(text) if text == "PONG" => {
                Ok(capabilities())
            }
            other => Err(StoreError::Message(format!(
                "unexpected ping reply {other:?}"
            ))),
        }
    }

    pub fn prepare_backup(&self, backup_id: &str, now: i64) -> Result<BackupFence, StoreError> {
        let mut client = self.lock();
        prepare_backup(&mut client, &self.prefix, backup_id, now)
    }

    pub fn export_backup(&self, backup_id: &str) -> Result<String, StoreError> {
        let mut client = self.lock();
        export_backup(&mut client, &self.prefix, backup_id)
    }

    pub fn release_backup(&self, backup_id: &str) -> Result<(), StoreError> {
        let mut client = self.lock();
        let raw = get_text(&mut client, &backup_fence_key(&self.prefix))?;
        if let Some(raw) = raw {
            let fence: BackupFence = serde_json::from_str(&raw)
                .map_err(|error| StoreError::Message(error.to_string()))?;
            if fence.backup_id == backup_id {
                del(&mut client, &[backup_fence_key(&self.prefix)])?;
            }
        }
        Ok(())
    }

    pub fn restore_status(&self, restore_id: &str) -> Result<Option<RestoreJournal>, StoreError> {
        let mut client = self.lock();
        read_journal(&mut client, &self.prefix, restore_id)
    }

    pub fn prepare_restore(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        source: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut client = self.lock();
        prepare_restore(
            &mut client,
            &self.prefix,
            restore_id,
            transaction_digest,
            source,
            now,
        )
    }

    pub fn commit_restore_intent(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut client = self.lock();
        if let Some(journal) = read_journal(&mut client, &self.prefix, restore_id)? {
            if journal.phase == RestorePhase::CommitIntent
                && journal.transaction_digest == transaction_digest
            {
                return Ok(journal);
            }
        }
        let mut journal = transition(
            &mut client,
            &self.prefix,
            restore_id,
            &["prepared"],
            "commit-intent",
            transaction_digest,
            now,
        )?;
        journal.updated_at = now;
        Ok(journal)
    }

    pub fn commit_restore(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut client = self.lock();
        commit_restore(
            &mut client,
            &self.prefix,
            restore_id,
            transaction_digest,
            now,
        )
    }

    pub fn complete_restore(
        &self,
        restore_id: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut client = self.lock();
        complete_restore(&mut client, &self.prefix, restore_id, now)
    }

    pub fn abort_restore(&self, restore_id: &str, now: i64) -> Result<RestoreJournal, StoreError> {
        let mut client = self.lock();
        abort_restore(&mut client, &self.prefix, restore_id, now)
    }
}

fn scripts() -> &'static LuaScripts {
    static SCRIPTS: OnceLock<LuaScripts> = OnceLock::new();
    SCRIPTS.get_or_init(|| LuaScripts {
        create: include_str!("lua/v1/create.lua").to_owned(),
        claim: include_str!("lua/v1/claim.lua").to_owned(),
        heartbeat: include_str!("lua/v1/heartbeat.lua").to_owned(),
        complete: include_str!("lua/v1/complete.lua").to_owned(),
        journal_transition: include_str!("lua/v1/journal_transition.lua").to_owned(),
        fence_acquire: include_str!("lua/v1/fence_acquire.lua").to_owned(),
        fence_release: include_str!("lua/v1/fence_release.lua").to_owned(),
        install: include_str!("lua/v1/install.lua").to_owned(),
        uninstall: include_str!("lua/v1/uninstall.lua").to_owned(),
        clear_pending: include_str!("lua/v1/clear_pending.lua").to_owned(),
        finalize: include_str!("lua/v1/finalize.lua").to_owned(),
        reset_epoch: include_str!("lua/v1/reset_epoch.lua").to_owned(),
    })
}

#[cfg(test)]
fn extract_script(source: &str, name: &str) -> String {
    let marker = format!("const {name} = `");
    let start = source
        .find(&marker)
        .unwrap_or_else(|| panic!("missing {name}"))
        + marker.len();
    let rest = &source[start..];
    let end = rest
        .find("`;")
        .unwrap_or_else(|| panic!("unterminated {name}"));
    rest[..end].to_string()
}

fn create_or_get(
    client: &mut BlockingRedisClient,
    prefix: &str,
    input: CreateTaskInput,
) -> Result<CreateTaskResult, StoreError> {
    let candidate = make_task(&input, new_task_id());
    let encoded = task_json(&candidate);
    let reply = eval(
        client,
        &scripts().create,
        &[
            idempotency_key(prefix, &candidate.idempotency_key_hash),
            task_key(prefix, &candidate.id),
            queue_key(prefix),
            backup_fence_key(prefix),
            generation_key(prefix),
            restore_fence_key(prefix),
        ],
        &[
            candidate.id.clone(),
            encoded,
            task_prefix(prefix),
            input.now.to_string(),
        ],
    )?;
    let (marker, body) = two_strings(reply)?;
    if body.is_empty() {
        return Err(StoreError::Message(
            "idempotency index points to a missing task".to_string(),
        ));
    }
    let task = parse_task(&body)?;
    if task.request_hash != canonical_request_hash(&input.arguments) {
        return Err(StoreError::IdempotencyConflict);
    }
    Ok(CreateTaskResult {
        task,
        deduplicated: marker == "existing",
    })
}

fn prepare_backup(
    client: &mut BlockingRedisClient,
    prefix: &str,
    backup_id: &str,
    now: i64,
) -> Result<BackupFence, StoreError> {
    if exists(client, &restore_fence_key(prefix))? {
        return Err(StoreError::RestoreFenced);
    }
    let generation = generation_of(client, prefix)?;
    let fence = BackupFence {
        backup_id: backup_id.to_string(),
        generation,
        created_at: now,
        expires_at: now + BACKUP_FENCE_TTL_MS,
    };
    let body =
        serde_json::to_string(&fence).map_err(|error| StoreError::Message(error.to_string()))?;
    if set_nx_px(
        client,
        &backup_fence_key(prefix),
        &body,
        BACKUP_FENCE_TTL_MS,
    )? {
        return Ok(fence);
    }
    let existing = get_text(client, &backup_fence_key(prefix))?.unwrap_or_else(|| "{}".to_string());
    let existing: BackupFence =
        serde_json::from_str(&existing).map_err(|error| StoreError::Message(error.to_string()))?;
    if existing.backup_id != backup_id {
        return Err(StoreError::BackupFenced);
    }
    Ok(existing)
}

fn export_backup(
    client: &mut BlockingRedisClient,
    prefix: &str,
    backup_id: &str,
) -> Result<String, StoreError> {
    let raw = get_text(client, &backup_fence_key(prefix))?.unwrap_or_else(|| "{}".to_string());
    let fence: BackupFence = serde_json::from_str(&raw).unwrap_or(BackupFence {
        backup_id: String::new(),
        generation: 0,
        created_at: 0,
        expires_at: 0,
    });
    if fence.backup_id != backup_id {
        return Err(StoreError::Message(
            "backup fence is not owned by this request".to_string(),
        ));
    }
    let generation = generation_of(client, prefix)?;
    let restore_epoch =
        get_text(client, &restore_epoch_key(prefix))?.unwrap_or_else(|| "initial".to_string());
    let mut lines = vec![serde_json::json!({"type": "metadata", "schemaVersion": 1, "stateGeneration": generation, "restoreEpoch": restore_epoch}).to_string()];
    let mut cursor = "0".to_string();
    let pattern = format!("{}:task:*", prefix);
    loop {
        let (next, keys) = scan(client, &cursor, &pattern, 200)?;
        cursor = next;
        if !keys.is_empty() {
            let values = mget(client, &keys)?;
            let mut tasks = Vec::new();
            for raw in values.into_iter().flatten() {
                tasks.push(parse_task(&raw)?);
            }
            tasks.sort_by(|left, right| left.id.cmp(&right.id));
            for task in tasks {
                let mut portable = portable_task(&task);
                portable.stdout.clear();
                portable.stderr.clear();
                let task_value: Value = serde_json::from_str(&task_json(&portable))
                    .map_err(|error| StoreError::Message(error.to_string()))?;
                lines.push(
                    serde_json::json!({"type": "task", "schemaVersion": 1, "task": task_value})
                        .to_string(),
                );
                lines.push(serde_json::json!({"type": "idempotency", "schemaVersion": 1, "record": {"hash": task.idempotency_key_hash, "taskId": task.id, "requestHash": task.request_hash}}).to_string());
                lines.push(serde_json::json!({"type": "log", "schemaVersion": 1, "record": {"taskId": task.id, "stdout": task.stdout, "stderr": task.stderr}}).to_string());
            }
        }
        if cursor == "0" {
            break;
        }
    }
    let end_generation = generation_of(client, prefix)?;
    if generation != end_generation {
        return Err(StoreError::Message(
            "state generation changed during backup".to_string(),
        ));
    }
    lines.push(serde_json::json!({"type": "complete", "schemaVersion": 1, "stateGeneration": end_generation}).to_string());
    let mut snapshot = lines.join("\n");
    snapshot.push('\n');
    Ok(snapshot)
}

fn prepare_restore(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
    transaction_digest: &str,
    source: &str,
    now: i64,
) -> Result<RestoreJournal, StoreError> {
    let existing = read_journal(client, prefix, restore_id)?;
    if let Some(existing) = &existing {
        if existing.transaction_digest != transaction_digest {
            return Err(StoreError::RestoreState(
                "restore transaction digest mismatch".to_string(),
            ));
        }
        if existing.phase == RestorePhase::RolledBack {
            return Err(StoreError::RestoreState(format!(
                "restore {restore_id} was rolled back"
            )));
        }
        if existing.phase != RestorePhase::Preparing && existing.phase != RestorePhase::Aborting {
            return Ok(existing.clone());
        }
        delete_staged(client, prefix, restore_id)?;
    }
    let mut lookup_error = None;
    let plan = restore::plan_restore_decisions(restore_id, source, |task_ids, index_hashes| {
        match lookup_ids(client, prefix, task_ids, index_hashes) {
            Ok(found) => found,
            Err(error) => {
                lookup_error = Some(error);
                Lookup {
                    tasks: Vec::new(),
                    indexed: Vec::new(),
                }
            }
        }
    });
    if let Some(error) = lookup_error {
        return Err(error);
    }
    let plan = plan.map_err(StoreError::Message)?;
    if let Err(error) = stage_decisions(client, prefix, restore_id, &plan.decisions) {
        let _ = delete_staged(client, prefix, restore_id);
        return Err(error);
    }
    let staged = staged_decisions(client, prefix, restore_id)?;
    if staged.len() != plan.decisions.len()
        || restore::prepared_digest_of(&staged) != plan.prepared_digest
    {
        let _ = delete_staged(client, prefix, restore_id);
        return Err(StoreError::RestoreState(
            "staged restore namespace failed read-back verification".to_string(),
        ));
    }
    let journal = RestoreJournal {
        contributor_id: "stateless-mcp".to_string(),
        schema_version: 1,
        restore_id: restore_id.to_string(),
        transaction_digest: transaction_digest.to_string(),
        source_digest: plan.source_digest,
        prepared_digest: plan.prepared_digest,
        phase: RestorePhase::Prepared,
        records: plan.records,
        imported: plan.imported,
        skipped: plan.skipped,
        interrupted: plan.interrupted,
        remapped: plan.remapped,
        previous_epoch: get_text(client, &restore_epoch_key(prefix))?
            .unwrap_or_else(|| "initial".to_string()),
        restore_epoch: deterministic_task_id(restore_id, "restore-epoch", "v1"),
        created_at: existing.as_ref().map(|item| item.created_at).unwrap_or(now),
        updated_at: now,
    };
    save_journal(client, prefix, &journal)?;
    Ok(journal)
}

fn commit_restore(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
    transaction_digest: &str,
    now: i64,
) -> Result<RestoreJournal, StoreError> {
    let current = read_journal(client, prefix, restore_id)?
        .ok_or_else(|| StoreError::RestoreState(format!("restore {restore_id} is unknown")))?;
    if current.phase == RestorePhase::CommittedPendingComplete
        && current.transaction_digest == transaction_digest
    {
        return Ok(current);
    }
    if current.phase != RestorePhase::Committing {
        transition(
            client,
            prefix,
            restore_id,
            &["commit-intent"],
            "committing",
            transaction_digest,
            now,
        )?;
    } else if current.transaction_digest != transaction_digest {
        return Err(StoreError::RestoreState(
            "restore transaction digest mismatch".to_string(),
        ));
    }
    let fence = as_bulk(eval(
        client,
        &scripts().fence_acquire,
        &[restore_fence_key(prefix), backup_fence_key(prefix)],
        &[
            restore_id.to_string(),
            now.to_string(),
            RESTORE_FENCE_TTL_MS.to_string(),
        ],
    )?)?;
    if fence == "backup-fenced" {
        return Err(StoreError::RestoreFenced);
    }
    if fence != "ok" {
        return Err(StoreError::RestoreState(
            "another restore holds the restore fence".to_string(),
        ));
    }
    let decisions = staged_decisions(client, prefix, restore_id)?;
    let journal = read_journal(client, prefix, restore_id)?.ok_or_else(|| {
        StoreError::RestoreState(
            "staged restore namespace does not match the restore journal".to_string(),
        )
    })?;
    if restore::prepared_digest_of(&decisions) != journal.prepared_digest {
        return Err(StoreError::RestoreState(
            "staged restore namespace does not match the restore journal".to_string(),
        ));
    }
    let mut installed = 0u64;
    for decision in &decisions {
        if decision.action == DecisionAction::Skip {
            let raw = get_text(client, &task_key(prefix, &decision.skip_target_id))?;
            let Some(raw) = raw else {
                return Err(StoreError::RestoreConflict);
            };
            if task_digest(&parse_task(&raw)?) != decision.skip_digest {
                return Err(StoreError::RestoreConflict);
            }
            continue;
        }
        let result = as_bulk(eval(
            client,
            &scripts().install,
            &[
                task_key(prefix, &decision.final_task_id),
                idempotency_key(prefix, &decision.idempotency_hash),
                inserted_tasks_key(prefix, restore_id),
                inserted_indexes_key(prefix, restore_id),
            ],
            &[decision.task_json.clone(), decision.final_task_id.clone()],
        )?)?;
        if result != "ok" {
            return Err(StoreError::RestoreConflict);
        }
        installed += 1;
        if installed % 500 == 0 {
            exec(
                client,
                &[
                    "PEXPIRE".to_string(),
                    restore_fence_key(prefix),
                    RESTORE_FENCE_TTL_MS.to_string(),
                ],
            )?;
        }
    }
    let finalized = as_bulk(eval(
        client,
        &scripts().finalize,
        &[
            journal_key(prefix, restore_id),
            restore_epoch_key(prefix),
            generation_key(prefix),
        ],
        &[
            transaction_digest.to_string(),
            now.to_string(),
            RESTORE_JOURNAL_TTL_MS.to_string(),
        ],
    )?)?;
    interpret_journal(restore_id, &finalized)
}

fn complete_restore(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
    now: i64,
) -> Result<RestoreJournal, StoreError> {
    let mut journal = read_journal(client, prefix, restore_id)?
        .ok_or_else(|| StoreError::RestoreState(format!("restore {restore_id} is unknown")))?;
    if journal.phase == RestorePhase::Complete {
        return Ok(journal);
    }
    if journal.phase != RestorePhase::CommittedPendingComplete {
        return Err(StoreError::RestoreState(format!(
            "restore {restore_id} is in phase {}",
            phase_name(journal.phase)
        )));
    }
    let mut cursor = "0".to_string();
    loop {
        let (next, members) = sscan(
            client,
            &inserted_tasks_key(prefix, restore_id),
            &cursor,
            500,
        )?;
        cursor = next;
        for key in members {
            eval(
                client,
                &scripts().clear_pending,
                &[key],
                &[restore_id.to_string()],
            )?;
        }
        if cursor == "0" {
            break;
        }
    }
    delete_staged(client, prefix, restore_id)?;
    eval(
        client,
        &scripts().fence_release,
        &[restore_fence_key(prefix)],
        &[restore_id.to_string()],
    )?;
    journal.phase = RestorePhase::Complete;
    journal.updated_at = now;
    save_journal(client, prefix, &journal)?;
    Ok(journal)
}

fn abort_restore(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
    now: i64,
) -> Result<RestoreJournal, StoreError> {
    let Some(mut journal) = read_journal(client, prefix, restore_id)? else {
        return Ok(empty_rollback(restore_id, now));
    };
    if journal.phase == RestorePhase::RolledBack {
        return Ok(journal);
    }
    if journal.phase == RestorePhase::Complete {
        return Err(StoreError::RestoreState(format!(
            "restore {restore_id} is already complete"
        )));
    }
    if journal.phase != RestorePhase::Aborting {
        transition(
            client,
            prefix,
            restore_id,
            &[
                "preparing",
                "prepared",
                "commit-intent",
                "committing",
                "committed-pending-complete",
            ],
            "aborting",
            "",
            now,
        )?;
    }
    for decision in staged_decisions(client, prefix, restore_id)? {
        if decision.action != DecisionAction::Insert {
            continue;
        }
        eval(
            client,
            &scripts().uninstall,
            &[
                task_key(prefix, &decision.final_task_id),
                idempotency_key(prefix, &decision.idempotency_hash),
            ],
            &[decision.task_json, decision.final_task_id],
        )?;
    }
    eval(
        client,
        &scripts().reset_epoch,
        &[restore_epoch_key(prefix)],
        &[
            journal.restore_epoch.clone(),
            journal.previous_epoch.clone(),
        ],
    )?;
    delete_staged(client, prefix, restore_id)?;
    eval(
        client,
        &scripts().fence_release,
        &[restore_fence_key(prefix)],
        &[restore_id.to_string()],
    )?;
    let mut aborted = read_journal(client, prefix, restore_id)?
        .ok_or_else(|| StoreError::RestoreState(format!("restore {restore_id} is unknown")))?;
    aborted.phase = RestorePhase::RolledBack;
    aborted.updated_at = now;
    save_journal(client, prefix, &aborted)?;
    journal = aborted;
    Ok(journal)
}

fn lookup_ids(
    client: &mut BlockingRedisClient,
    prefix: &str,
    task_ids: &[String],
    index_hashes: &[String],
) -> Result<Lookup, StoreError> {
    let tasks = if task_ids.is_empty() {
        Vec::new()
    } else {
        let keys: Vec<String> = task_ids.iter().map(|id| task_key(prefix, id)).collect();
        mget(client, &keys)?
            .into_iter()
            .map(|raw| raw.map(|text| parse_task(&text)).transpose())
            .collect::<Result<Vec<_>, _>>()?
    };
    let indexed = if index_hashes.is_empty() {
        Vec::new()
    } else {
        let keys: Vec<String> = index_hashes
            .iter()
            .map(|hash| idempotency_key(prefix, hash))
            .collect();
        mget(client, &keys)?
    };
    Ok(Lookup { tasks, indexed })
}

fn stage_decisions(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
    decisions: &[StagedRestoreDecision],
) -> Result<(), StoreError> {
    if decisions.is_empty() {
        return Ok(());
    }
    if exec(client, &["MULTI".to_string()]).is_err() {
        return Err(StoreError::Message(
            "redis transaction could not start".to_string(),
        ));
    }
    for decision in decisions {
        let body = match serde_json::to_string(decision) {
            Ok(body) => body,
            Err(error) => {
                let _ = exec(client, &["DISCARD".to_string()]);
                return Err(StoreError::Message(error.to_string()));
            }
        };
        let ttl = RESTORE_JOURNAL_TTL_MS.to_string();
        if let Err(error) = exec(
            client,
            &[
                "SET".to_string(),
                staged_task_key(prefix, restore_id, &decision.final_task_id),
                body,
                "PX".to_string(),
                ttl.clone(),
            ],
        ) {
            let _ = exec(client, &["DISCARD".to_string()]);
            return Err(error);
        }
        if decision.action == DecisionAction::Insert {
            if let Err(error) = exec(
                client,
                &[
                    "SET".to_string(),
                    staged_idempotency_key(prefix, restore_id, &decision.idempotency_hash),
                    decision.final_task_id.clone(),
                    "PX".to_string(),
                    ttl,
                ],
            ) {
                let _ = exec(client, &["DISCARD".to_string()]);
                return Err(error);
            }
        }
    }
    exec(client, &["EXEC".to_string()])?;
    Ok(())
}

fn staged_decisions(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
) -> Result<Vec<StagedRestoreDecision>, StoreError> {
    let mut decisions = Vec::new();
    let mut cursor = "0".to_string();
    let pattern = format!("{prefix}:restore:{restore_id}:staged-task:*");
    loop {
        let (next, keys) = scan(client, &cursor, &pattern, 500)?;
        cursor = next;
        if !keys.is_empty() {
            for raw in mget(client, &keys)?.into_iter().flatten() {
                decisions.push(
                    serde_json::from_str(&raw)
                        .map_err(|error| StoreError::Message(error.to_string()))?,
                );
            }
        }
        if cursor == "0" {
            break;
        }
    }
    Ok(decisions)
}

fn delete_staged(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
) -> Result<(), StoreError> {
    for pattern in [
        format!("{prefix}:restore:{restore_id}:staged-task:*"),
        format!("{prefix}:restore:{restore_id}:staged-idempotency:*"),
    ] {
        let mut cursor = "0".to_string();
        loop {
            let (next, keys) = scan(client, &cursor, &pattern, 500)?;
            cursor = next;
            del(client, &keys)?;
            if cursor == "0" {
                break;
            }
        }
    }
    del(
        client,
        &[
            inserted_tasks_key(prefix, restore_id),
            inserted_indexes_key(prefix, restore_id),
        ],
    )
}

fn transition(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
    expected: &[&str],
    next: &str,
    transaction_digest: &str,
    now: i64,
) -> Result<RestoreJournal, StoreError> {
    let expected_json =
        serde_json::to_string(expected).map_err(|error| StoreError::Message(error.to_string()))?;
    let raw = as_bulk(eval(
        client,
        &scripts().journal_transition,
        &[journal_key(prefix, restore_id)],
        &[
            expected_json,
            next.to_string(),
            transaction_digest.to_string(),
            now.to_string(),
            RESTORE_JOURNAL_TTL_MS.to_string(),
        ],
    )?)?;
    interpret_journal(restore_id, &raw)
}

fn interpret_journal(restore_id: &str, raw: &str) -> Result<RestoreJournal, StoreError> {
    if raw == "missing" {
        return Err(StoreError::RestoreState(format!(
            "restore {restore_id} is unknown"
        )));
    }
    if raw == "digest" {
        return Err(StoreError::RestoreState(
            "restore transaction digest mismatch".to_string(),
        ));
    }
    if let Some(phase) = raw.strip_prefix("phase:") {
        return Err(StoreError::RestoreState(format!(
            "restore {restore_id} is in phase {phase}"
        )));
    }
    serde_json::from_str(raw).map_err(|error| StoreError::Message(error.to_string()))
}

fn read_journal(
    client: &mut BlockingRedisClient,
    prefix: &str,
    restore_id: &str,
) -> Result<Option<RestoreJournal>, StoreError> {
    let Some(raw) = get_text(client, &journal_key(prefix, restore_id))? else {
        return Ok(None);
    };
    Ok(Some(
        serde_json::from_str(&raw).map_err(|error| StoreError::Message(error.to_string()))?,
    ))
}

fn save_journal(
    client: &mut BlockingRedisClient,
    prefix: &str,
    journal: &RestoreJournal,
) -> Result<(), StoreError> {
    let body =
        serde_json::to_string(journal).map_err(|error| StoreError::Message(error.to_string()))?;
    set_px(
        client,
        &journal_key(prefix, &journal.restore_id),
        &body,
        RESTORE_JOURNAL_TTL_MS,
    )
}

fn eval(
    client: &mut BlockingRedisClient,
    script: &str,
    keys: &[String],
    args: &[String],
) -> Result<RedisValue, StoreError> {
    let mut parts = Vec::with_capacity(3 + keys.len() + args.len());
    parts.push("EVAL".to_string());
    parts.push(script.to_string());
    parts.push(keys.len().to_string());
    parts.extend(keys.iter().cloned());
    parts.extend(args.iter().cloned());
    exec(client, &parts)
}

fn exec(client: &mut BlockingRedisClient, args: &[String]) -> Result<RedisValue, StoreError> {
    let owned: Vec<Vec<u8>> = args.iter().map(|arg| arg.as_bytes().to_vec()).collect();
    let refs: Vec<&[u8]> = owned.iter().map(|item| item.as_slice()).collect();
    match client.call(&refs) {
        Ok(value) => Ok(value),
        Err(error) if error.contains("BACKUP_FENCED") => Err(StoreError::BackupFenced),
        Err(error) if error.contains("RESTORE_FENCED") => Err(StoreError::RestoreFenced),
        Err(error) => Err(StoreError::Message(error)),
    }
}

fn get_text(client: &mut BlockingRedisClient, key: &str) -> Result<Option<String>, StoreError> {
    match exec(client, &["GET".to_string(), key.to_string()])? {
        RedisValue::Nil => Ok(None),
        RedisValue::Bulk(text) => Ok(Some(text)),
        other => Err(StoreError::Message(format!(
            "expected redis string, got {other:?}"
        ))),
    }
}

fn generation_of(client: &mut BlockingRedisClient, prefix: &str) -> Result<i64, StoreError> {
    match get_text(client, &generation_key(prefix))? {
        Some(text) => text
            .parse::<i64>()
            .map_err(|error| StoreError::Message(error.to_string())),
        None => Ok(0),
    }
}

fn exists(client: &mut BlockingRedisClient, key: &str) -> Result<bool, StoreError> {
    Ok(as_int(exec(client, &["EXISTS".to_string(), key.to_string()])?)? == 1)
}

fn set_px(
    client: &mut BlockingRedisClient,
    key: &str,
    value: &str,
    ttl_ms: i64,
) -> Result<(), StoreError> {
    exec(
        client,
        &[
            "SET".to_string(),
            key.to_string(),
            value.to_string(),
            "PX".to_string(),
            ttl_ms.to_string(),
        ],
    )?;
    Ok(())
}

fn set_nx_px(
    client: &mut BlockingRedisClient,
    key: &str,
    value: &str,
    ttl_ms: i64,
) -> Result<bool, StoreError> {
    match exec(
        client,
        &[
            "SET".to_string(),
            key.to_string(),
            value.to_string(),
            "NX".to_string(),
            "PX".to_string(),
            ttl_ms.to_string(),
        ],
    )? {
        RedisValue::Nil => Ok(false),
        RedisValue::Status(text) if text == "OK" => Ok(true),
        RedisValue::Bulk(text) if text == "OK" => Ok(true),
        other => Err(StoreError::Message(format!(
            "unexpected set reply {other:?}"
        ))),
    }
}

fn del(client: &mut BlockingRedisClient, keys: &[String]) -> Result<(), StoreError> {
    if keys.is_empty() {
        return Ok(());
    }
    let mut args = Vec::with_capacity(keys.len() + 1);
    args.push("DEL".to_string());
    args.extend(keys.iter().cloned());
    exec(client, &args)?;
    Ok(())
}

fn mget(
    client: &mut BlockingRedisClient,
    keys: &[String],
) -> Result<Vec<Option<String>>, StoreError> {
    if keys.is_empty() {
        return Ok(Vec::new());
    }
    let mut args = Vec::with_capacity(keys.len() + 1);
    args.push("MGET".to_string());
    args.extend(keys.iter().cloned());
    match exec(client, &args)? {
        RedisValue::Array(items) => items
            .into_iter()
            .map(|item| match item {
                RedisValue::Nil => Ok(None),
                RedisValue::Bulk(text) => Ok(Some(text)),
                other => Err(StoreError::Message(format!(
                    "expected redis string, got {other:?}"
                ))),
            })
            .collect(),
        other => Err(StoreError::Message(format!(
            "expected redis array, got {other:?}"
        ))),
    }
}

fn scan(
    client: &mut BlockingRedisClient,
    cursor: &str,
    pattern: &str,
    count: i64,
) -> Result<(String, Vec<String>), StoreError> {
    page(exec(
        client,
        &[
            "SCAN".to_string(),
            cursor.to_string(),
            "MATCH".to_string(),
            pattern.to_string(),
            "COUNT".to_string(),
            count.to_string(),
        ],
    )?)
}

fn sscan(
    client: &mut BlockingRedisClient,
    key: &str,
    cursor: &str,
    count: i64,
) -> Result<(String, Vec<String>), StoreError> {
    page(exec(
        client,
        &[
            "SSCAN".to_string(),
            key.to_string(),
            cursor.to_string(),
            "COUNT".to_string(),
            count.to_string(),
        ],
    )?)
}

fn page(value: RedisValue) -> Result<(String, Vec<String>), StoreError> {
    let RedisValue::Array(mut items) = value else {
        return Err(StoreError::Message(
            "redis scan reply is malformed".to_string(),
        ));
    };
    if items.len() != 2 {
        return Err(StoreError::Message(
            "redis scan reply is malformed".to_string(),
        ));
    }
    let keys = match items.pop().unwrap() {
        RedisValue::Array(keys) => keys
            .into_iter()
            .map(as_bulk)
            .collect::<Result<Vec<_>, _>>()?,
        other => {
            return Err(StoreError::Message(format!(
                "expected redis array, got {other:?}"
            )));
        }
    };
    Ok((as_bulk(items.pop().unwrap())?, keys))
}

fn as_bulk(value: RedisValue) -> Result<String, StoreError> {
    match value {
        RedisValue::Bulk(text) | RedisValue::Status(text) => Ok(text),
        other => Err(StoreError::Message(format!(
            "expected redis string, got {other:?}"
        ))),
    }
}

fn as_int(value: RedisValue) -> Result<i64, StoreError> {
    match value {
        RedisValue::Int(value) => Ok(value),
        RedisValue::Bulk(text) => text
            .parse()
            .map_err(|error: std::num::ParseIntError| StoreError::Message(error.to_string())),
        other => Err(StoreError::Message(format!(
            "expected redis integer, got {other:?}"
        ))),
    }
}

fn two_strings(value: RedisValue) -> Result<(String, String), StoreError> {
    let RedisValue::Array(items) = value else {
        return Err(StoreError::Message("expected redis array".to_string()));
    };
    if items.len() != 2 {
        return Err(StoreError::Message("expected redis pair".to_string()));
    }
    let mut items = items;
    let second = as_bulk(items.pop().unwrap())?;
    let first = as_bulk(items.pop().unwrap())?;
    Ok((first, second))
}

fn parse_task(raw: &str) -> Result<TaskRecord, StoreError> {
    serde_json::from_str(raw).map_err(|error| StoreError::Message(error.to_string()))
}

fn phase_name(phase: RestorePhase) -> &'static str {
    match phase {
        RestorePhase::Preparing => "preparing",
        RestorePhase::Prepared => "prepared",
        RestorePhase::CommitIntent => "commit-intent",
        RestorePhase::Committing => "committing",
        RestorePhase::CommittedPendingComplete => "committed-pending-complete",
        RestorePhase::Complete => "complete",
        RestorePhase::Aborting => "aborting",
        RestorePhase::RolledBack => "rolled-back",
        RestorePhase::RecoveryRequired => "recovery-required",
    }
}

fn empty_rollback(restore_id: &str, now: i64) -> RestoreJournal {
    RestoreJournal {
        contributor_id: "stateless-mcp".to_string(),
        schema_version: 1,
        restore_id: restore_id.to_string(),
        transaction_digest: String::new(),
        source_digest: String::new(),
        prepared_digest: String::new(),
        phase: RestorePhase::RolledBack,
        records: 0,
        imported: 0,
        skipped: 0,
        interrupted: 0,
        remapped: std::collections::BTreeMap::new(),
        previous_epoch: "initial".to_string(),
        restore_epoch: String::new(),
        created_at: now,
        updated_at: now,
    }
}

fn task_key(prefix: &str, task_id: &str) -> String {
    format!("{prefix}:task:{task_id}")
}
fn idempotency_key(prefix: &str, hash: &str) -> String {
    format!("{prefix}:idempotency:{hash}")
}
fn queue_key(prefix: &str) -> String {
    format!("{prefix}:queue")
}
fn generation_key(prefix: &str) -> String {
    format!("{prefix}:state-generation")
}
fn backup_fence_key(prefix: &str) -> String {
    format!("{prefix}:backup-fence")
}
fn restore_epoch_key(prefix: &str) -> String {
    format!("{prefix}:restore-epoch")
}
fn restore_fence_key(prefix: &str) -> String {
    format!("{prefix}:restore-fence")
}
fn journal_key(prefix: &str, restore_id: &str) -> String {
    format!("{prefix}:restore:{restore_id}:journal")
}
fn staged_task_key(prefix: &str, restore_id: &str, task_id: &str) -> String {
    format!("{prefix}:restore:{restore_id}:staged-task:{task_id}")
}
fn staged_idempotency_key(prefix: &str, restore_id: &str, hash: &str) -> String {
    format!("{prefix}:restore:{restore_id}:staged-idempotency:{hash}")
}
fn inserted_tasks_key(prefix: &str, restore_id: &str) -> String {
    format!("{prefix}:restore:{restore_id}:inserted-tasks")
}
fn inserted_indexes_key(prefix: &str, restore_id: &str) -> String {
    format!("{prefix}:restore:{restore_id}:inserted-indexes")
}
fn task_prefix(prefix: &str) -> String {
    format!("{prefix}:task:")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::TaskArguments;
    use crate::redis_client::decode;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn arguments() -> TaskArguments {
        TaskArguments {
            target: "tests/test_mcp.py".to_string(),
            keyword: None,
            markers: None,
            timeout_seconds: 30,
        }
    }

    #[test]
    fn native_lua_scripts_match_the_frozen_typescript_oracle() {
        let loaded = scripts();
        let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let oracle =
            std::fs::read_to_string(repo.join("stateless-mcp/src/redis-task-store.ts")).unwrap();
        let bodies = [
            loaded.create.as_str(),
            loaded.claim.as_str(),
            loaded.heartbeat.as_str(),
            loaded.complete.as_str(),
            loaded.journal_transition.as_str(),
            loaded.fence_acquire.as_str(),
            loaded.install.as_str(),
            loaded.uninstall.as_str(),
            loaded.clear_pending.as_str(),
            loaded.finalize.as_str(),
            loaded.reset_epoch.as_str(),
            loaded.fence_release.as_str(),
        ];
        for body in bodies {
            assert!(body.contains("redis.call"), "{body}");
            assert!(!body.contains("const "), "{body}");
            assert!(!body.contains("`;"));
        }
        assert!(
            loaded.create.contains("BACKUP_FENCED") && loaded.create.contains("RESTORE_FENCED")
        );
        assert!(loaded.claim.contains("ZRANGEBYSCORE"));
        assert!(loaded.complete.contains("\"FENCED\""));
        assert!(loaded.finalize.contains("committed-pending-complete"));
        for (body, name) in [
            (&loaded.create, "CREATE_SCRIPT"),
            (&loaded.claim, "CLAIM_SCRIPT"),
            (&loaded.heartbeat, "HEARTBEAT_SCRIPT"),
            (&loaded.complete, "COMPLETE_SCRIPT"),
            (
                &loaded.journal_transition,
                "RESTORE_JOURNAL_TRANSITION_SCRIPT",
            ),
            (&loaded.fence_acquire, "RESTORE_FENCE_ACQUIRE_SCRIPT"),
            (&loaded.fence_release, "RESTORE_FENCE_RELEASE_SCRIPT"),
            (&loaded.install, "RESTORE_INSTALL_SCRIPT"),
            (&loaded.uninstall, "RESTORE_UNINSTALL_SCRIPT"),
            (&loaded.clear_pending, "RESTORE_CLEAR_PENDING_SCRIPT"),
            (&loaded.finalize, "RESTORE_FINALIZE_COMMIT_SCRIPT"),
            (&loaded.reset_epoch, "RESTORE_RESET_EPOCH_SCRIPT"),
        ] {
            assert_eq!(*body, extract_script(&oracle, name), "{name}");
        }
    }

    #[test]
    fn production_compose_keeps_the_redis_failover_topology() {
        let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let compose =
            std::fs::read_to_string(repo.join("docker-compose.stateless-mcp.yml")).unwrap();
        let dockerfile = std::fs::read_to_string(repo.join("stateless-mcp/Dockerfile")).unwrap();
        for marker in [
            "mcp-instance-1",
            "mcp-instance-2",
            "mcp-lb",
            "redis:7.4-alpine",
            "deepseek-stateless-mcp",
            "appendonly",
        ] {
            assert!(compose.contains(marker), "{marker}");
        }
        let lowered = compose.to_ascii_lowercase();
        assert!(!lowered.contains("python"));
        assert!(!compose.contains("node"));
        assert!(dockerfile.contains("deepseek-stateless-mcp"));
        assert!(!dockerfile.to_ascii_lowercase().contains("python"));
        assert!(!dockerfile.contains("node:"));
    }

    fn serve_one(reply: impl FnOnce(&[u8]) -> Vec<u8> + Send + 'static) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut buf = Vec::new();
            let mut chunk = [0u8; 8192];
            loop {
                if decode(&buf).ok().flatten().is_some() {
                    break;
                }
                let read = socket.read(&mut chunk).unwrap();
                if read == 0 {
                    return;
                }
                buf.extend_from_slice(&chunk[..read]);
            }
            socket.write_all(&reply(&buf)).unwrap();
        });
        format!("redis://{address}")
    }

    #[test]
    fn create_or_get_sends_the_typescript_script_and_task_json() {
        let url = serve_one(|request| {
            let (value, _) = decode(request).unwrap().unwrap();
            let RedisValue::Array(items) = value else {
                panic!("command")
            };
            let RedisValue::Bulk(script) = &items[1] else {
                panic!("script")
            };
            assert!(script.contains("BACKUP_FENCED"));
            let RedisValue::Bulk(idempotency) = &items[3] else {
                panic!("key")
            };
            assert!(idempotency.contains(":idempotency:"));
            let RedisValue::Bulk(json) = &items[10] else {
                panic!("task json")
            };
            let header = format!("*2\r\n$7\r\ncreated\r\n${}\r\n", json.len());
            let mut out = header.into_bytes();
            out.extend_from_slice(json.as_bytes());
            out.extend_from_slice(b"\r\n");
            out
        });
        let store = RedisTaskStore::connect(&url, "deepseek-infra:mcp:v1").unwrap();
        let created = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        assert!(!created.deduplicated);
        assert_eq!(
            created.task.request_hash,
            canonical_request_hash(&arguments())
        );
        assert_eq!(created.task.status, crate::model::TaskStatus::Queued);
    }

    #[test]
    fn redis_backup_fence_error_is_not_reported_as_success() {
        let url = serve_one(|_| b"-BACKUP_FENCED\r\n".to_vec());
        let store = RedisTaskStore::connect(&url, "deepseek-infra:mcp:v1").unwrap();
        let error = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap_err();
        assert_eq!(error, StoreError::BackupFenced);
    }

    #[test]
    #[ignore = "requires REDIS_URL pointing at a disposable Redis 7 server"]
    fn redis_lua_deduplicates_claims_and_backup_fence() {
        let url = std::env::var("REDIS_URL").expect("REDIS_URL");
        let prefix = format!("deepseek-infra:mcp:probe:{}", std::process::id());
        let store = RedisTaskStore::connect(&url, &prefix).unwrap();
        let created = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 1_000,
            })
            .unwrap();
        assert!(!created.deduplicated);
        let again = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 1_100,
            })
            .unwrap();
        assert!(again.deduplicated);
        assert_eq!(again.task.id, created.task.id);
        let mut changed = arguments();
        changed.timeout_seconds = 31;
        assert_eq!(
            store
                .create_or_get(CreateTaskInput {
                    idempotency_key: "request-123".to_string(),
                    arguments: changed,
                    now: 1_200
                })
                .unwrap_err(),
            StoreError::IdempotencyConflict
        );
        let claimed = store
            .claim("mcp-instance-1", 1_300, 1_000)
            .unwrap()
            .expect("queued task");
        assert_eq!(claimed.owner_instance.as_deref(), Some("mcp-instance-1"));
        assert_eq!(claimed.attempts, 1);
        assert!(
            store
                .claim("mcp-instance-2", 1_500, 1_000)
                .unwrap()
                .is_none()
        );
        assert!(
            store
                .heartbeat(&claimed.id, "mcp-instance-1", 1_600, 1_000)
                .unwrap()
        );
        let taken = store
            .claim("mcp-instance-2", 2_700, 1_000)
            .unwrap()
            .expect("expired lease");
        assert_eq!(taken.owner_instance.as_deref(), Some("mcp-instance-2"));
        assert_eq!(taken.attempts, 2);
        assert!(
            store
                .complete(
                    &taken.id,
                    "mcp-instance-1",
                    TaskOutcome {
                        stdout: String::new(),
                        stderr: String::new(),
                        exit_code: Some(0),
                        error: None
                    },
                    2_800
                )
                .unwrap()
                .is_none()
        );
        let finished = store
            .complete(
                &taken.id,
                "mcp-instance-2",
                TaskOutcome {
                    stdout: "ok".to_string(),
                    stderr: String::new(),
                    exit_code: Some(0),
                    error: None,
                },
                2_900,
            )
            .unwrap()
            .expect("owner completes");
        assert_eq!(finished.status, crate::model::TaskStatus::Succeeded);
        assert_eq!(finished.stdout, "ok");
        let fence = store.prepare_backup("backup-1234", 3_000).unwrap();
        assert_eq!(fence.backup_id, "backup-1234");
        assert_eq!(
            store.prepare_backup("backup-9999", 3_100).unwrap_err(),
            StoreError::BackupFenced
        );
        assert_eq!(
            store
                .create_or_get(CreateTaskInput {
                    idempotency_key: "request-456".to_string(),
                    arguments: arguments(),
                    now: 3_200
                })
                .unwrap_err(),
            StoreError::BackupFenced
        );
        store.release_backup("backup-1234").unwrap();
        let after = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-456".to_string(),
                arguments: arguments(),
                now: 3_300,
            })
            .unwrap();
        assert!(!after.deduplicated);
    }
}
