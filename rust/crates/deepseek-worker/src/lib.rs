use std::collections::HashMap;

use deepseek_protocol::{
    ActionFence, AdmitError, EffectState, admit_command, interpret_remote_outcome,
};

#[derive(Debug, Default)]
pub struct Worker {
    live_epochs: HashMap<String, u64>,
    effects: HashMap<(String, u64), EffectState>,
}

impl Worker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn admit(&mut self, fence: &ActionFence) -> Result<(), AdmitError> {
        let live = self.live_epochs.get(&fence.action_id).copied().unwrap_or(0);
        admit_command(fence, live)?;
        self.live_epochs
            .insert(fence.action_id.clone(), fence.execution_epoch);
        Ok(())
    }

    pub fn query_effect(&self, fence: &ActionFence) -> Result<EffectState, AdmitError> {
        match self
            .effects
            .get(&(fence.action_id.clone(), fence.execution_epoch))
            .copied()
        {
            Some(state) => interpret_remote_outcome(state),
            None => Err(AdmitError::UnknownEffect),
        }
    }

    pub fn record_effect(
        &mut self,
        fence: &ActionFence,
        state: EffectState,
    ) -> Result<(), AdmitError> {
        self.admit(fence)?;
        let interpreted = interpret_remote_outcome(state)?;
        self.effects.insert(
            (fence.action_id.clone(), fence.execution_epoch),
            interpreted,
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fence(epoch: u64) -> ActionFence {
        ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: epoch,
        }
    }

    #[test]
    fn missing_effect_is_unknown_not_not_applied() {
        let worker = Worker::new();
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn stale_epoch_cannot_commit() {
        let mut worker = Worker::new();
        worker.admit(&fence(4)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(3), EffectState::Applied),
            Err(AdmitError::StaleEpoch)
        );
    }

    #[test]
    fn applied_effect_round_trips() {
        let mut worker = Worker::new();
        worker
            .record_effect(&fence(1), EffectState::Applied)
            .unwrap();
        assert_eq!(worker.query_effect(&fence(1)), Ok(EffectState::Applied));
    }

    #[test]
    fn unknown_recorded_state_is_rejected() {
        let mut worker = Worker::new();
        assert_eq!(
            worker.record_effect(&fence(1), EffectState::Unknown),
            Err(AdmitError::UnknownEffect)
        );
    }
}
