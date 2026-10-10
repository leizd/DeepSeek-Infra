use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::model::{
    BACKUP_FENCE_TTL_MS, BackupCapabilities, BackupFence, CreateTaskInput, CreateTaskResult,
    RESTORE_FENCE_TTL_MS, RestoreJournal, RestorePhase, StoreError, TaskOutcome, TaskRecord,
    TaskStatus, canonical_request_hash, capabilities, make_task, portable_task, task_digest,
    task_json,
};
use crate::restore::{self, DecisionAction, StagedRestoreDecision};

struct RestoreFence {
    restore_id: String,
    expires_at: i64,
}

#[derive(Clone)]
struct InsertedKeys {
    tasks: BTreeSet<String>,
    indexes: BTreeSet<String>,
}

struct Inner {
    tasks: BTreeMap<String, TaskRecord>,
    idempotency: HashMap<String, String>,
    journals: HashMap<String, RestoreJournal>,
    generation: i64,
    restore_epoch: String,
    backup_fence: Option<BackupFence>,
    restore_fence: Option<RestoreFence>,
    staged: HashMap<String, Vec<StagedRestoreDecision>>,
    inserted: HashMap<String, InsertedKeys>,
}

pub struct MemoryTaskStore {
    inner: Mutex<Inner>,
}

impl MemoryTaskStore {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                tasks: BTreeMap::new(),
                idempotency: HashMap::new(),
                journals: HashMap::new(),
                generation: 0,
                restore_epoch: "initial".to_string(),
                backup_fence: None,
                restore_fence: None,
                staged: HashMap::new(),
                inserted: HashMap::new(),
            }),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn create_or_get(&self, input: CreateTaskInput) -> Result<CreateTaskResult, StoreError> {
        let candidate = make_task(&input, new_task_id());
        let mut inner = self.lock();
        if let Some(existing_id) = inner
            .idempotency
            .get(&candidate.idempotency_key_hash)
            .cloned()
        {
            let existing = inner.tasks.get(&existing_id).cloned().ok_or_else(|| {
                StoreError::Message("idempotency index points to a missing task".to_string())
            })?;
            if existing.request_hash != canonical_request_hash(&input.arguments) {
                return Err(StoreError::IdempotencyConflict);
            }
            return Ok(CreateTaskResult {
                task: existing,
                deduplicated: true,
            });
        }
        expire_backup(&mut inner, input.now);
        expire_restore(&mut inner, input.now);
        if inner.backup_fence.is_some() {
            return Err(StoreError::BackupFenced);
        }
        if inner.restore_fence.is_some() {
            return Err(StoreError::RestoreFenced);
        }
        inner
            .idempotency
            .insert(candidate.idempotency_key_hash.clone(), candidate.id.clone());
        inner.tasks.insert(candidate.id.clone(), candidate.clone());
        inner.generation += 1;
        Ok(CreateTaskResult {
            task: candidate,
            deduplicated: false,
        })
    }

    pub fn get(&self, task_id: &str) -> Result<Option<TaskRecord>, StoreError> {
        let inner = self.lock();
        Ok(match inner.tasks.get(task_id) {
            Some(task) if task.restore_pending.is_none() => Some(task.clone()),
            _ => None,
        })
    }

    pub fn claim(
        &self,
        instance_id: &str,
        now: i64,
        lease_ms: i64,
    ) -> Result<Option<TaskRecord>, StoreError> {
        let mut inner = self.lock();
        expire_backup(&mut inner, now);
        expire_restore(&mut inner, now);
        if inner.backup_fence.is_some() || inner.restore_fence.is_some() {
            return Ok(None);
        }
        let mut eligible: Vec<TaskRecord> = inner
            .tasks
            .values()
            .filter(|task| {
                task.status == TaskStatus::Queued
                    || (task.status == TaskStatus::Running
                        && task.lease_until.is_some_and(|until| until <= now))
            })
            .cloned()
            .collect();
        eligible.sort_by(|left, right| {
            left.created_at
                .cmp(&right.created_at)
                .then(left.id.cmp(&right.id))
        });
        let Some(mut chosen) = eligible.into_iter().next() else {
            return Ok(None);
        };
        chosen.status = TaskStatus::Running;
        chosen.owner_instance = Some(instance_id.to_string());
        chosen.lease_until = Some(now + lease_ms);
        chosen.attempts += 1;
        chosen.updated_at = now;
        chosen.error = None;
        inner.tasks.insert(chosen.id.clone(), chosen.clone());
        inner.generation += 1;
        Ok(Some(chosen))
    }

    pub fn heartbeat(
        &self,
        task_id: &str,
        instance_id: &str,
        now: i64,
        lease_ms: i64,
    ) -> Result<bool, StoreError> {
        let mut inner = self.lock();
        let Some(task) = inner.tasks.get_mut(task_id) else {
            return Ok(false);
        };
        if task.status != TaskStatus::Running || task.owner_instance.as_deref() != Some(instance_id)
        {
            return Ok(false);
        }
        task.lease_until = Some(now + lease_ms);
        task.updated_at = now;
        Ok(true)
    }

    pub fn complete(
        &self,
        task_id: &str,
        instance_id: &str,
        outcome: TaskOutcome,
        now: i64,
    ) -> Result<Option<TaskRecord>, StoreError> {
        let mut inner = self.lock();
        let Some(task) = inner.tasks.get(task_id).cloned() else {
            return Ok(None);
        };
        if task.status != TaskStatus::Running || task.owner_instance.as_deref() != Some(instance_id)
        {
            return Ok(None);
        }
        expire_restore(&mut inner, now);
        if inner.restore_fence.is_some() {
            return Err(StoreError::RestoreFenced);
        }
        let mut task = task;
        task.status = if outcome.error.is_none() && outcome.exit_code == Some(0) {
            TaskStatus::Succeeded
        } else {
            TaskStatus::Failed
        };
        task.stdout = outcome.stdout;
        task.stderr = outcome.stderr;
        task.exit_code = outcome.exit_code;
        task.error = outcome.error;
        task.lease_until = None;
        task.updated_at = now;
        inner.tasks.insert(task.id.clone(), task.clone());
        inner.generation += 1;
        Ok(Some(task))
    }

    pub fn backup_capabilities(&self) -> Result<BackupCapabilities, StoreError> {
        Ok(capabilities())
    }

    pub fn prepare_backup(&self, backup_id: &str, now: i64) -> Result<BackupFence, StoreError> {
        let mut inner = self.lock();
        expire_backup(&mut inner, now);
        expire_restore(&mut inner, now);
        if inner.restore_fence.is_some() {
            return Err(StoreError::RestoreFenced);
        }
        if let Some(existing) = &inner.backup_fence {
            if existing.backup_id != backup_id {
                return Err(StoreError::BackupFenced);
            }
            return Ok(existing.clone());
        }
        let fence = BackupFence {
            backup_id: backup_id.to_string(),
            generation: inner.generation,
            created_at: now,
            expires_at: now + BACKUP_FENCE_TTL_MS,
        };
        inner.backup_fence = Some(fence.clone());
        Ok(fence)
    }

    pub fn export_backup(&self, backup_id: &str) -> Result<String, StoreError> {
        let inner = self.lock();
        if inner
            .backup_fence
            .as_ref()
            .is_none_or(|fence| fence.backup_id != backup_id)
        {
            return Err(StoreError::Message(
                "backup fence is not owned by this request".to_string(),
            ));
        }
        let generation = inner.generation;
        let mut lines = vec![
            serde_json::json!({
                "type": "metadata",
                "schemaVersion": 1,
                "stateGeneration": generation,
                "restoreEpoch": inner.restore_epoch,
            })
            .to_string(),
        ];
        let mut tasks: Vec<&TaskRecord> = inner.tasks.values().collect();
        tasks.sort_by(|left, right| left.id.cmp(&right.id));
        for task in tasks {
            let mut portable = portable_task(task);
            portable.stdout.clear();
            portable.stderr.clear();
            lines.push(serde_json::json!({"type": "task", "schemaVersion": 1, "task": serde_json::from_str::<Value>(&task_json(&portable)).unwrap()}).to_string());
            lines.push(serde_json::json!({
                "type": "idempotency",
                "schemaVersion": 1,
                "record": {"hash": task.idempotency_key_hash, "taskId": task.id, "requestHash": task.request_hash}
            }).to_string());
            lines.push(
                serde_json::json!({
                    "type": "log",
                    "schemaVersion": 1,
                    "record": {"taskId": task.id, "stdout": task.stdout, "stderr": task.stderr}
                })
                .to_string(),
            );
        }
        if generation != inner.generation {
            return Err(StoreError::Message(
                "state generation changed during backup".to_string(),
            ));
        }
        lines.push(serde_json::json!({"type": "complete", "schemaVersion": 1, "stateGeneration": inner.generation}).to_string());
        let mut snapshot = lines.join("\n");
        snapshot.push('\n');
        Ok(snapshot)
    }

    pub fn release_backup(&self, backup_id: &str) -> Result<(), StoreError> {
        let mut inner = self.lock();
        if inner
            .backup_fence
            .as_ref()
            .is_some_and(|fence| fence.backup_id == backup_id)
        {
            inner.backup_fence = None;
        }
        Ok(())
    }

    pub fn restore_status(&self, restore_id: &str) -> Result<Option<RestoreJournal>, StoreError> {
        Ok(self.lock().journals.get(restore_id).cloned())
    }

    pub fn prepare_restore(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        source: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut inner = self.lock();
        if let Some(existing) = inner.journals.get(restore_id).cloned() {
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
            if existing.phase != RestorePhase::Preparing && existing.phase != RestorePhase::Aborting
            {
                return Ok(existing);
            }
            inner.staged.remove(restore_id);
        }
        let existing = inner.journals.get(restore_id).cloned();
        let plan = restore::plan_restore_decisions(restore_id, source, |task_ids, index_hashes| {
            restore::Lookup {
                tasks: task_ids
                    .iter()
                    .map(|id| inner.tasks.get(id).cloned())
                    .collect(),
                indexed: index_hashes
                    .iter()
                    .map(|hash| inner.idempotency.get(hash).cloned())
                    .collect(),
            }
        })
        .map_err(StoreError::Message)?;
        inner
            .staged
            .insert(restore_id.to_string(), plan.decisions.clone());
        let staged = inner.staged.get(restore_id).cloned().unwrap_or_default();
        if staged.len() != plan.decisions.len()
            || restore::prepared_digest_of(&staged) != plan.prepared_digest
        {
            inner.staged.remove(restore_id);
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
            previous_epoch: inner.restore_epoch.clone(),
            restore_epoch: crate::model::deterministic_task_id(restore_id, "restore-epoch", "v1"),
            created_at: existing.as_ref().map(|item| item.created_at).unwrap_or(now),
            updated_at: now,
        };
        inner
            .journals
            .insert(restore_id.to_string(), journal.clone());
        Ok(journal)
    }

    pub fn commit_restore_intent(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut inner = self.lock();
        let journal = expect_phase(
            &mut inner,
            restore_id,
            &[RestorePhase::Prepared, RestorePhase::CommitIntent],
            transaction_digest,
        )?;
        journal.phase = RestorePhase::CommitIntent;
        journal.updated_at = now;
        Ok(journal.clone())
    }

    pub fn commit_restore(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut inner = self.lock();
        expire_restore(&mut inner, now);
        let Some(current) = inner.journals.get(restore_id).cloned() else {
            return Err(StoreError::RestoreState(format!(
                "restore {restore_id} is unknown"
            )));
        };
        if current.phase == RestorePhase::CommittedPendingComplete
            && current.transaction_digest == transaction_digest
        {
            return Ok(current);
        }
        if current.phase != RestorePhase::Committing {
            expect_phase(
                &mut inner,
                restore_id,
                &[RestorePhase::CommitIntent],
                transaction_digest,
            )?;
            let journal = inner.journals.get_mut(restore_id).unwrap();
            journal.phase = RestorePhase::Committing;
            journal.updated_at = now;
        } else if current.transaction_digest != transaction_digest {
            return Err(StoreError::RestoreState(
                "restore transaction digest mismatch".to_string(),
            ));
        }
        if inner.backup_fence.is_some() {
            return Err(StoreError::RestoreFenced);
        }
        if inner
            .restore_fence
            .as_ref()
            .is_some_and(|fence| fence.restore_id != restore_id)
        {
            return Err(StoreError::RestoreState(
                "another restore holds the restore fence".to_string(),
            ));
        }
        inner.restore_fence = Some(RestoreFence {
            restore_id: restore_id.to_string(),
            expires_at: now + RESTORE_FENCE_TTL_MS,
        });
        let decisions = inner.staged.get(restore_id).cloned().unwrap_or_default();
        let prepared = inner
            .journals
            .get(restore_id)
            .map(|journal| journal.prepared_digest.clone())
            .unwrap_or_default();
        if restore::prepared_digest_of(&decisions) != prepared {
            return Err(StoreError::RestoreState(
                "staged restore namespace does not match the restore journal".to_string(),
            ));
        }
        let mut inserted = inner.inserted.remove(restore_id).unwrap_or(InsertedKeys {
            tasks: BTreeSet::new(),
            indexes: BTreeSet::new(),
        });
        for decision in &decisions {
            if decision.action == DecisionAction::Skip {
                let live = inner.tasks.get(&decision.skip_target_id);
                if live.is_none_or(|task| task_digest(task) != decision.skip_digest) {
                    return Err(StoreError::RestoreConflict);
                }
                continue;
            }
            if let Some(existing) = inner.tasks.get(&decision.final_task_id) {
                if task_json(existing) != decision.task_json {
                    return Err(StoreError::RestoreConflict);
                }
            }
            if let Some(indexed) = inner.idempotency.get(&decision.idempotency_hash) {
                if indexed != &decision.final_task_id {
                    return Err(StoreError::RestoreConflict);
                }
            }
            let parsed: TaskRecord = serde_json::from_str(&decision.task_json)
                .map_err(|error| StoreError::Message(error.to_string()))?;
            inner.tasks.insert(decision.final_task_id.clone(), parsed);
            inner.idempotency.insert(
                decision.idempotency_hash.clone(),
                decision.final_task_id.clone(),
            );
            inserted.tasks.insert(decision.final_task_id.clone());
            inserted.indexes.insert(decision.idempotency_hash.clone());
        }
        let epoch = inner
            .journals
            .get(restore_id)
            .map(|journal| journal.restore_epoch.clone())
            .unwrap_or_default();
        let imported = inner
            .journals
            .get(restore_id)
            .map(|journal| journal.imported)
            .unwrap_or(0);
        inner.restore_epoch = epoch;
        if imported > 0 {
            inner.generation += 1;
        }
        inner.inserted.insert(restore_id.to_string(), inserted);
        let journal = inner.journals.get_mut(restore_id).unwrap();
        journal.phase = RestorePhase::CommittedPendingComplete;
        journal.updated_at = now;
        Ok(journal.clone())
    }

    pub fn complete_restore(
        &self,
        restore_id: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        let mut inner = self.lock();
        let phase = inner.journals.get(restore_id).map(|journal| journal.phase);
        let journal = expect_phase(
            &mut inner,
            restore_id,
            &[
                RestorePhase::CommittedPendingComplete,
                RestorePhase::Complete,
            ],
            "",
        )?;
        if phase == Some(RestorePhase::Complete) {
            return Ok(journal.clone());
        }
        if let Some(inserted) = inner.inserted.get(restore_id).cloned() {
            for task_id in inserted.tasks {
                if let Some(task) = inner.tasks.get_mut(&task_id) {
                    if task.restore_pending.as_deref() == Some(restore_id) {
                        task.restore_pending = None;
                    }
                }
            }
        }
        inner.staged.remove(restore_id);
        inner.inserted.remove(restore_id);
        if inner
            .restore_fence
            .as_ref()
            .is_some_and(|fence| fence.restore_id == restore_id)
        {
            inner.restore_fence = None;
        }
        let journal = inner.journals.get_mut(restore_id).unwrap();
        journal.phase = RestorePhase::Complete;
        journal.updated_at = now;
        Ok(journal.clone())
    }

    pub fn abort_restore(&self, restore_id: &str, now: i64) -> Result<RestoreJournal, StoreError> {
        let mut inner = self.lock();
        let Some(mut journal) = inner.journals.get(restore_id).cloned() else {
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
        journal.phase = RestorePhase::Aborting;
        journal.updated_at = now;
        inner
            .journals
            .insert(restore_id.to_string(), journal.clone());
        let decisions = inner.staged.get(restore_id).cloned().unwrap_or_default();
        for decision in decisions {
            if decision.action != DecisionAction::Insert {
                continue;
            }
            if inner
                .tasks
                .get(&decision.final_task_id)
                .is_some_and(|existing| task_json(existing) == decision.task_json)
            {
                inner.tasks.remove(&decision.final_task_id);
            }
            if inner
                .idempotency
                .get(&decision.idempotency_hash)
                .is_some_and(|indexed| indexed == &decision.final_task_id)
            {
                inner.idempotency.remove(&decision.idempotency_hash);
            }
        }
        if inner.restore_epoch == journal.restore_epoch {
            inner.restore_epoch = journal.previous_epoch.clone();
        }
        inner.staged.remove(restore_id);
        inner.inserted.remove(restore_id);
        if inner
            .restore_fence
            .as_ref()
            .is_some_and(|fence| fence.restore_id == restore_id)
        {
            inner.restore_fence = None;
        }
        journal.phase = RestorePhase::RolledBack;
        journal.updated_at = now;
        inner
            .journals
            .insert(restore_id.to_string(), journal.clone());
        Ok(journal)
    }

    pub fn task_count(&self) -> usize {
        self.lock().tasks.len()
    }

    pub fn testing_replace_task(&self, task: TaskRecord) {
        let mut inner = self.lock();
        inner
            .idempotency
            .insert(task.idempotency_key_hash.clone(), task.id.clone());
        inner.tasks.insert(task.id.clone(), task);
    }
}

impl Default for MemoryTaskStore {
    fn default() -> Self {
        Self::new()
    }
}

fn expire_backup(inner: &mut Inner, now: i64) {
    if inner
        .backup_fence
        .as_ref()
        .is_some_and(|fence| fence.expires_at <= now)
    {
        inner.backup_fence = None;
    }
}

fn expire_restore(inner: &mut Inner, now: i64) {
    if inner
        .restore_fence
        .as_ref()
        .is_some_and(|fence| fence.expires_at <= now)
    {
        inner.restore_fence = None;
    }
}

fn expect_phase<'a>(
    inner: &'a mut Inner,
    restore_id: &str,
    expected: &[RestorePhase],
    transaction_digest: &str,
) -> Result<&'a mut RestoreJournal, StoreError> {
    let journal = inner
        .journals
        .get_mut(restore_id)
        .ok_or_else(|| StoreError::RestoreState(format!("restore {restore_id} is unknown")))?;
    if !expected.contains(&journal.phase) {
        return Err(StoreError::RestoreState(format!(
            "restore {restore_id} is in phase {}",
            phase_name(journal.phase)
        )));
    }
    if !transaction_digest.is_empty() && journal.transaction_digest != transaction_digest {
        return Err(StoreError::RestoreState(
            "restore transaction digest mismatch".to_string(),
        ));
    }
    Ok(journal)
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
        remapped: BTreeMap::new(),
        previous_epoch: "initial".to_string(),
        restore_epoch: String::new(),
        created_at: now,
        updated_at: now,
    }
}

pub(crate) fn new_task_id() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(1);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos() as u64)
        .unwrap_or(0);
    let mixed = nanos ^ n.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    format!(
        "{:08x}-{:04x}-4{:03x}-a{:03x}-{:012x}",
        (mixed >> 32) as u32,
        (mixed >> 16) as u16,
        (mixed & 0x0fff) as u16,
        (n as u16) & 0x0fff,
        n
    )
}

pub fn apply_snapshot(
    store: &MemoryTaskStore,
    restore_id: &str,
    snapshot: &str,
    now: i64,
) -> Result<RestoreJournal, StoreError> {
    let transaction_digest = crate::model::digest(&format!("transaction:{restore_id}"));
    if let Some(existing) = store.restore_status(restore_id)? {
        if existing.phase == RestorePhase::Complete {
            return Ok(existing);
        }
    }
    store.prepare_restore(restore_id, &transaction_digest, snapshot, now)?;
    store.commit_restore_intent(restore_id, &transaction_digest, now + 1)?;
    store.commit_restore(restore_id, &transaction_digest, now + 2)?;
    store.complete_restore(restore_id, now + 3)?;
    store
        .restore_status(restore_id)?
        .ok_or_else(|| StoreError::Message("restore journal is missing after complete".to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::TaskArguments;

    fn arguments() -> TaskArguments {
        TaskArguments {
            target: "tests/test_mcp.py".to_string(),
            keyword: None,
            markers: None,
            timeout_seconds: 30,
        }
    }

    #[test]
    fn idempotency_key_deduplicates_an_identical_test_task() {
        let store = MemoryTaskStore::new();
        let first = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        let retried = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 20,
            })
            .unwrap();
        assert!(!first.deduplicated);
        assert!(retried.deduplicated);
        assert_eq!(retried.task.id, first.task.id);
    }

    #[test]
    fn idempotency_key_rejects_different_arguments() {
        let store = MemoryTaskStore::new();
        store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        let mut changed = arguments();
        changed.target = "tests/test_web.py".to_string();
        let error = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-123".to_string(),
                arguments: changed,
                now: 20,
            })
            .unwrap_err();
        assert_eq!(error, StoreError::IdempotencyConflict);
    }

    #[test]
    fn expired_worker_lease_is_recovered_and_stale_completion_is_fenced() {
        let store = MemoryTaskStore::new();
        let created = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "request-lease".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        let first = store.claim("instance-1", 10, 100).unwrap().unwrap();
        assert_eq!(first.id, created.task.id);
        assert_eq!(first.attempts, 1);
        assert!(store.claim("instance-2", 109, 100).unwrap().is_none());
        let recovered = store.claim("instance-2", 110, 100).unwrap().unwrap();
        assert_eq!(recovered.attempts, 2);
        assert_eq!(recovered.owner_instance.as_deref(), Some("instance-2"));
        let stale = store
            .complete(
                &created.task.id,
                "instance-1",
                TaskOutcome {
                    stdout: "stale".to_string(),
                    stderr: String::new(),
                    exit_code: Some(0),
                    error: None,
                },
                111,
            )
            .unwrap();
        assert!(stale.is_none());
        let completed = store
            .complete(
                &created.task.id,
                "instance-2",
                TaskOutcome {
                    stdout: "recovered".to_string(),
                    stderr: String::new(),
                    exit_code: Some(0),
                    error: None,
                },
                112,
            )
            .unwrap()
            .unwrap();
        assert_eq!(completed.status, TaskStatus::Succeeded);
        assert_eq!(completed.stdout, "recovered");
    }

    #[test]
    fn backup_fence_blocks_new_work_and_exports_running_tasks_as_interrupted() {
        let store = MemoryTaskStore::new();
        let created = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "backup-task-123".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        assert_eq!(
            store.claim("instance-1", 10, 100).unwrap().unwrap().status,
            TaskStatus::Running
        );
        store.prepare_backup("backup-contract-123", 20).unwrap();
        let blocked = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "blocked-task-123".to_string(),
                arguments: arguments(),
                now: 21,
            })
            .unwrap_err();
        assert_eq!(blocked, StoreError::BackupFenced);
        assert!(store.claim("instance-2", 21, 100).unwrap().is_none());
        let snapshot = store.export_backup("backup-contract-123").unwrap();
        assert!(snapshot.contains("\"status\":\"interrupted\""));
        assert!(!snapshot.contains("ownerInstance\":\"instance-1\""));
        assert!(!snapshot.contains("leaseUntil\":110"));
        store.release_backup("backup-contract-123").unwrap();

        let restored = MemoryTaskStore::new();
        let result = apply_snapshot(&restored, "restore-contract-123", &snapshot, 30).unwrap();
        assert_eq!(result.imported, 1);
        assert_eq!(result.interrupted, 1);
        let task = restored.get(&created.task.id).unwrap().unwrap();
        assert_eq!(task.status, TaskStatus::Interrupted);
        assert!(task.owner_instance.is_none());
        assert!(restored.claim("instance-3", 40, 100).unwrap().is_none());
        let retried = apply_snapshot(&restored, "restore-contract-123", &snapshot, 99).unwrap();
        assert_eq!(retried.imported, 1);
        assert_eq!(retried.restore_epoch, result.restore_epoch);
        assert_eq!(restored.task_count(), 1);
    }

    #[test]
    fn backup_fence_permits_idempotent_retries_and_expires() {
        let store = MemoryTaskStore::new();
        let first = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "before-fence".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        store.prepare_backup("backup-expiry", 20).unwrap();
        let retried = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "before-fence".to_string(),
                arguments: arguments(),
                now: 21,
            })
            .unwrap();
        assert!(retried.deduplicated);
        assert_eq!(retried.task.id, first.task.id);
        let blocked = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "during-fence".to_string(),
                arguments: arguments(),
                now: 21,
            })
            .unwrap_err();
        assert_eq!(blocked, StoreError::BackupFenced);
        let after = store
            .create_or_get(CreateTaskInput {
                idempotency_key: "after-fence".to_string(),
                arguments: arguments(),
                now: 20 + BACKUP_FENCE_TTL_MS,
            })
            .unwrap();
        assert!(!after.deduplicated);
    }

    #[test]
    fn restore_deterministically_remaps_task_and_idempotency_collisions() {
        let source = MemoryTaskStore::new();
        let created = source
            .create_or_get(CreateTaskInput {
                idempotency_key: "collision-task-123".to_string(),
                arguments: arguments(),
                now: 10,
            })
            .unwrap();
        source.prepare_backup("backup-collision-123", 20).unwrap();
        let snapshot = source.export_backup("backup-collision-123").unwrap();
        let target = MemoryTaskStore::new();
        let mut collision = created.task.clone();
        collision.request_hash = "different".to_string();
        collision.arguments.target = "tests/other.py".to_string();
        target.testing_replace_task(collision);
        let result = apply_snapshot(&target, "restore-collision-123", &snapshot, 30).unwrap();
        assert_eq!(result.imported, 1);
        let remapped = result.remapped.get(&created.task.id).cloned().unwrap();
        assert_ne!(remapped, created.task.id);
        assert_eq!(target.task_count(), 2);
        let retried = apply_snapshot(&target, "restore-collision-123", &snapshot, 99).unwrap();
        assert_eq!(retried.imported, 1);
        assert_eq!(retried.remapped.get(&created.task.id), Some(&remapped));
        assert_eq!(retried.restore_epoch, result.restore_epoch);
        assert_eq!(target.task_count(), 2);
    }
}
