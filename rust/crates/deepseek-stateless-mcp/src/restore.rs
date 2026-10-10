use std::collections::BTreeMap;

use sha2::{Digest, Sha256};

use crate::model::{
    TaskRecord, deterministic_task_id, digest, hex_encode, portable_task, task_digest, task_json,
};
use crate::snapshot::{self, SnapshotEntry};

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StagedRestoreDecision {
    pub original_id: String,
    pub final_task_id: String,
    pub action: DecisionAction,
    pub idempotency_hash: String,
    pub task_json: String,
    pub skip_target_id: String,
    pub skip_digest: String,
    pub interrupted: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DecisionAction {
    Insert,
    Skip,
}

#[derive(Debug, Clone)]
pub struct RestorePlan {
    pub decisions: Vec<StagedRestoreDecision>,
    pub source_digest: String,
    pub prepared_digest: String,
    pub records: u64,
    pub imported: u64,
    pub skipped: u64,
    pub interrupted: u64,
    pub remapped: BTreeMap<String, String>,
}

pub struct Lookup {
    pub tasks: Vec<Option<TaskRecord>>,
    pub indexed: Vec<Option<String>>,
}

const DECISION_BATCH: usize = 500;

fn decision_digest(decision: &StagedRestoreDecision) -> String {
    let action = match decision.action {
        DecisionAction::Insert => "insert",
        DecisionAction::Skip => "skip",
    };
    digest(&format!(
        "{}{}{}{}{}{}",
        decision.final_task_id,
        decision.task_json,
        decision.idempotency_hash,
        action,
        decision.skip_target_id,
        decision.skip_digest
    ))
}

pub fn prepared_digest_of(decisions: &[StagedRestoreDecision]) -> String {
    let mut values: Vec<String> = decisions.iter().map(decision_digest).collect();
    values.sort();
    let mut hasher = Sha256::new();
    for value in values {
        hasher.update(value.as_bytes());
        hasher.update(b"\n");
    }
    hex_encode(&hasher.finalize())
}

pub fn plan_restore_decisions(
    restore_id: &str,
    source: &str,
    mut lookup: impl FnMut(&[String], &[String]) -> Lookup,
) -> Result<RestorePlan, String> {
    let parsed = snapshot::parse_snapshot(source)?;
    let mut decisions = Vec::new();
    let mut remapped = BTreeMap::new();
    let mut imported = 0u64;
    let mut skipped = 0u64;
    let mut current_task: Option<TaskRecord> = None;
    let mut current_log = (String::new(), String::new());
    let mut pending: Vec<TaskRecord> = Vec::new();

    let mut flush = |pending: &mut Vec<TaskRecord>,
                     decisions: &mut Vec<StagedRestoreDecision>,
                     remapped: &mut BTreeMap<String, String>,
                     imported: &mut u64,
                     skipped: &mut u64|
     -> Result<(), String> {
        if pending.is_empty() {
            return Ok(());
        }
        let batch = std::mem::take(pending);
        let first = lookup(
            &batch.iter().map(|task| task.id.clone()).collect::<Vec<_>>(),
            &batch
                .iter()
                .map(|task| task.idempotency_key_hash.clone())
                .collect::<Vec<_>>(),
        );
        let mut remapped_ids = Vec::new();
        let mut remapped_index = Vec::new();
        for (index, portable) in batch.iter().enumerate() {
            let existing = first.tasks.get(index).and_then(|item| item.as_ref());
            if existing.is_some_and(|task| task_digest(task) != task_digest(portable)) {
                remapped_index.push(index);
                remapped_ids.push(deterministic_task_id(
                    restore_id,
                    &portable.id,
                    &task_digest(portable),
                ));
            }
        }
        let second = if remapped_ids.is_empty() {
            Lookup {
                tasks: Vec::new(),
                indexed: Vec::new(),
            }
        } else {
            lookup(&remapped_ids, &[])
        };
        for (index, portable) in batch.iter().enumerate() {
            let existing = first.tasks.get(index).and_then(|item| item.clone());
            if existing
                .as_ref()
                .is_some_and(|task| task_digest(task) == task_digest(portable))
            {
                *skipped += 1;
                decisions.push(StagedRestoreDecision {
                    original_id: portable.id.clone(),
                    final_task_id: portable.id.clone(),
                    action: DecisionAction::Skip,
                    idempotency_hash: portable.idempotency_key_hash.clone(),
                    task_json: String::new(),
                    skip_target_id: portable.id.clone(),
                    skip_digest: task_digest(existing.as_ref().unwrap()),
                    interrupted: portable.status == crate::model::TaskStatus::Interrupted,
                });
                continue;
            }
            let mut final_task_id = portable.id.clone();
            let mut remapped_existing: Option<TaskRecord> = None;
            if existing.is_some() {
                final_task_id =
                    deterministic_task_id(restore_id, &portable.id, &task_digest(portable));
                remapped.insert(portable.id.clone(), final_task_id.clone());
                if let Some(second_index) = remapped_index.iter().position(|value| *value == index)
                {
                    remapped_existing =
                        second.tasks.get(second_index).and_then(|item| item.clone());
                }
            }
            if let Some(existing_remap) = remapped_existing {
                let mut normalized = existing_remap.clone();
                normalized.id = portable.id.clone();
                normalized.idempotency_key_hash = portable.idempotency_key_hash.clone();
                if task_digest(&normalized) == task_digest(portable) {
                    *skipped += 1;
                    decisions.push(StagedRestoreDecision {
                        original_id: portable.id.clone(),
                        final_task_id: final_task_id.clone(),
                        action: DecisionAction::Skip,
                        idempotency_hash: portable.idempotency_key_hash.clone(),
                        task_json: String::new(),
                        skip_target_id: final_task_id,
                        skip_digest: task_digest(&existing_remap),
                        interrupted: portable.status == crate::model::TaskStatus::Interrupted,
                    });
                    continue;
                }
                return Err("deterministic restore task collision".to_string());
            }
            let mut index_hash = portable.idempotency_key_hash.clone();
            if let Some(indexed) = first.indexed.get(index).and_then(|item| item.clone()) {
                if indexed != final_task_id {
                    index_hash = digest(&format!("{index_hash}{final_task_id}"));
                }
            }
            let mut restored = portable.clone();
            restored.id = final_task_id.clone();
            restored.idempotency_key_hash = index_hash.clone();
            restored.restore_pending = Some(restore_id.to_string());
            *imported += 1;
            decisions.push(StagedRestoreDecision {
                original_id: portable.id.clone(),
                final_task_id,
                action: DecisionAction::Insert,
                idempotency_hash: index_hash,
                task_json: task_json(&restored),
                skip_target_id: String::new(),
                skip_digest: String::new(),
                interrupted: portable.status == crate::model::TaskStatus::Interrupted,
            });
        }
        Ok(())
    };

    for entry in parsed.entries {
        match entry {
            SnapshotEntry::Task(task) => {
                if let Some(current) = current_task.take() {
                    let mut with_log = current;
                    with_log.stdout = current_log.0.clone();
                    with_log.stderr = current_log.1.clone();
                    pending.push(portable_task(&with_log));
                }
                current_task = Some(*task);
                current_log = (String::new(), String::new());
                if pending.len() >= DECISION_BATCH {
                    flush(
                        &mut pending,
                        &mut decisions,
                        &mut remapped,
                        &mut imported,
                        &mut skipped,
                    )?;
                }
            }
            SnapshotEntry::Log {
                task_id,
                stdout,
                stderr,
            } => {
                if current_task.as_ref().is_some_and(|task| task.id == task_id) {
                    current_log = (stdout, stderr);
                }
            }
            _ => {}
        }
    }
    if let Some(current) = current_task {
        let mut with_log = current;
        with_log.stdout = current_log.0;
        with_log.stderr = current_log.1;
        pending.push(portable_task(&with_log));
    }
    flush(
        &mut pending,
        &mut decisions,
        &mut remapped,
        &mut imported,
        &mut skipped,
    )?;
    let interrupted = decisions
        .iter()
        .filter(|decision| decision.interrupted)
        .count() as u64;
    let records = decisions
        .iter()
        .filter(|decision| decision.action == DecisionAction::Insert)
        .count() as u64;
    Ok(RestorePlan {
        prepared_digest: prepared_digest_of(&decisions),
        decisions,
        source_digest: parsed.digest_hex,
        records,
        imported,
        skipped,
        interrupted,
        remapped,
    })
}
