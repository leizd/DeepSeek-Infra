use crate::memory::MemoryTaskStore;
use crate::model::{
    BackupCapabilities, BackupFence, CreateTaskInput, CreateTaskResult, RestoreJournal, StoreError,
    TaskOutcome, TaskRecord,
};
use crate::redis_store::RedisTaskStore;

pub enum TaskBackend {
    Memory(MemoryTaskStore),
    Redis(RedisTaskStore),
}

impl TaskBackend {
    pub fn memory() -> Self {
        Self::Memory(MemoryTaskStore::new())
    }

    pub fn connect_redis(url: &str, prefix: &str) -> Result<Self, String> {
        Ok(Self::Redis(RedisTaskStore::connect(url, prefix)?))
    }

    pub fn advertised_durable_task_state(redis_connected: bool) -> &'static str {
        if redis_connected { "redis" } else { "memory" }
    }

    pub fn create_or_get(&self, input: CreateTaskInput) -> Result<CreateTaskResult, StoreError> {
        match self {
            Self::Memory(store) => store.create_or_get(input),
            Self::Redis(store) => store.create_or_get(input),
        }
    }

    pub fn get(&self, task_id: &str) -> Result<Option<TaskRecord>, StoreError> {
        match self {
            Self::Memory(store) => store.get(task_id),
            Self::Redis(store) => store.get(task_id),
        }
    }

    pub fn claim(
        &self,
        instance_id: &str,
        now: i64,
        lease_ms: i64,
    ) -> Result<Option<TaskRecord>, StoreError> {
        match self {
            Self::Memory(store) => store.claim(instance_id, now, lease_ms),
            Self::Redis(store) => store.claim(instance_id, now, lease_ms),
        }
    }

    pub fn heartbeat(
        &self,
        task_id: &str,
        instance_id: &str,
        now: i64,
        lease_ms: i64,
    ) -> Result<bool, StoreError> {
        match self {
            Self::Memory(store) => store.heartbeat(task_id, instance_id, now, lease_ms),
            Self::Redis(store) => store.heartbeat(task_id, instance_id, now, lease_ms),
        }
    }

    pub fn complete(
        &self,
        task_id: &str,
        instance_id: &str,
        outcome: TaskOutcome,
        now: i64,
    ) -> Result<Option<TaskRecord>, StoreError> {
        match self {
            Self::Memory(store) => store.complete(task_id, instance_id, outcome, now),
            Self::Redis(store) => store.complete(task_id, instance_id, outcome, now),
        }
    }

    pub fn backup_capabilities(&self) -> Result<BackupCapabilities, StoreError> {
        match self {
            Self::Memory(store) => store.backup_capabilities(),
            Self::Redis(store) => store.backup_capabilities(),
        }
    }

    pub fn prepare_backup(&self, backup_id: &str, now: i64) -> Result<BackupFence, StoreError> {
        match self {
            Self::Memory(store) => store.prepare_backup(backup_id, now),
            Self::Redis(store) => store.prepare_backup(backup_id, now),
        }
    }

    pub fn export_backup(&self, backup_id: &str) -> Result<String, StoreError> {
        match self {
            Self::Memory(store) => store.export_backup(backup_id),
            Self::Redis(store) => store.export_backup(backup_id),
        }
    }

    pub fn release_backup(&self, backup_id: &str) -> Result<(), StoreError> {
        match self {
            Self::Memory(store) => store.release_backup(backup_id),
            Self::Redis(store) => store.release_backup(backup_id),
        }
    }

    pub fn restore_status(&self, restore_id: &str) -> Result<Option<RestoreJournal>, StoreError> {
        match self {
            Self::Memory(store) => store.restore_status(restore_id),
            Self::Redis(store) => store.restore_status(restore_id),
        }
    }

    pub fn prepare_restore(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        source: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        match self {
            Self::Memory(store) => {
                store.prepare_restore(restore_id, transaction_digest, source, now)
            }
            Self::Redis(store) => {
                store.prepare_restore(restore_id, transaction_digest, source, now)
            }
        }
    }

    pub fn commit_restore_intent(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        match self {
            Self::Memory(store) => store.commit_restore_intent(restore_id, transaction_digest, now),
            Self::Redis(store) => store.commit_restore_intent(restore_id, transaction_digest, now),
        }
    }

    pub fn commit_restore(
        &self,
        restore_id: &str,
        transaction_digest: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        match self {
            Self::Memory(store) => store.commit_restore(restore_id, transaction_digest, now),
            Self::Redis(store) => store.commit_restore(restore_id, transaction_digest, now),
        }
    }

    pub fn complete_restore(
        &self,
        restore_id: &str,
        now: i64,
    ) -> Result<RestoreJournal, StoreError> {
        match self {
            Self::Memory(store) => store.complete_restore(restore_id, now),
            Self::Redis(store) => store.complete_restore(restore_id, now),
        }
    }

    pub fn abort_restore(&self, restore_id: &str, now: i64) -> Result<RestoreJournal, StoreError> {
        match self {
            Self::Memory(store) => store.abort_restore(restore_id, now),
            Self::Redis(store) => store.abort_restore(restore_id, now),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durable_task_state_is_redis_only_after_redis_connects() {
        assert_eq!(TaskBackend::advertised_durable_task_state(false), "memory");
        assert_eq!(TaskBackend::advertised_durable_task_state(true), "redis");
    }
}
