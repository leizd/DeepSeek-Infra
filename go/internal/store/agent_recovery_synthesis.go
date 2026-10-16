package store

import (
	"database/sql"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func recoverySynthesisOnlyReady(run agent.RunMetadata) bool {
	if run.Recovery == nil || run.Recovery.Metadata == nil {
		return false
	}
	context := run.Recovery.Metadata
	return run.Status == "running" && !run.CancellationActive() && context.Synthesize &&
		!context.CompleteImmediately && len(context.Execute) == 0 && len(context.SynthesisOutputIds) > 0
}

// History validation uses the admitted immutable checkpoint. A later request
// swap must neither invalidate original proofs nor rebind them to that request.
func (store *Control) verifiedAgentSynthesisSourceTx(tx *sql.Tx, intent AgentRunExecutionBinding) (Record, error) {
	if intent.Phase != "synthesis" {
		return Record{}, ErrAgentRunExecutionRequired
	}
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,
        record_digest,writer_fencing_token,recorded_at FROM control_events
        WHERE domain='agent_run' AND record_id=? AND revision=?`, intent.RunID, intent.MetadataIndex), "agent_run")
	if err != nil {
		return Record{}, err
	}
	if !found || record.ExecutionEpoch == 0 || record.ExecutionEpoch > intent.MetadataEpoch {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	run, err := decodeAgentMetadata(record)
	if err != nil {
		return Record{}, err
	}
	if run.Request != intent.Request || executionPlanDigest(run) != intent.PlanDigest || run.NextIndex != intent.MetadataIndex {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	if run.Recovery == nil {
		return store.verifiedSameRequestSynthesisSourceTx(tx, intent, false)
	}
	if !executionPhaseReady(run, "synthesis") {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	context := run.Recovery.Metadata
	if context.Selection.GetKind() == agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN &&
		len(context.Execute) > 0 && context.ResetFinal && run.FinalAfter < run.Recovery.StartIndex {
		// A task rerun keeps the prior final answer until its separately typed
		// progress transition. A lease alone cannot skip that public boundary.
		return Record{}, ErrAgentRunRecoveryRequired
	}
	retained, err := store.verifiedAgentRecoveryOutputsTx(tx, run)
	if err != nil {
		return Record{}, err
	}
	current := map[string]AgentTaskSettlement{}
	var source Record
	if len(run.ExecutionTasks()) != 0 {
		source, err = store.verifiedSameRequestSynthesisSourceTx(tx, intent, true)
		if err != nil {
			return Record{}, err
		}
		current, err = store.validatedAgentTaskSettlementsTx(tx, source)
		if err != nil {
			return Record{}, err
		}
	}
	ids := run.Recovery.Metadata.SynthesisOutputIds
	if len(ids) == 0 || len(ids) > 4 {
		return Record{}, ErrAgentNodeDependencyUnverified
	}
	seen := map[string]bool{}
	for _, id := range ids {
		if seen[id] {
			return Record{}, ErrAgentNodeDependencyUnverified
		}
		seen[id] = true
		_, fresh := current[id]
		if !fresh && retained[id] == nil {
			return Record{}, ErrAgentNodeDependencyUnverified
		}
	}
	// A direct synthesis has multiple original sources and no invented task
	// action. All callers use this result only for its independently checked error.
	return source, nil
}
