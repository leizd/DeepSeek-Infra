package store

import (
	"database/sql"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

// Recovery references retain their original action, epoch, request and receipt.
// Recheck those original journals inside the caller's admission transaction;
// never manufacture current-generation task settlement markers for old output.
func (store *Control) verifiedAgentRecoveryOutputsTx(tx *sql.Tx, run agent.RunMetadata) (map[string]*agentv1.AgentRunRecoveryEffectSource, error) {
	result := map[string]*agentv1.AgentRunRecoveryEffectSource{}
	if run.Recovery == nil {
		return result, nil
	}
	if !validAgentRecoveryScope(run) {
		return nil, ErrAgentRunRecoveryRequired
	}
	context := run.Recovery.Metadata
	if len(context.RetainedIds) > 4 || len(context.RetainedIds) != len(context.RetainedOutputs) {
		return nil, ErrAgentNodeDependencyUnverified
	}
	expected := map[string]bool{}
	for _, id := range context.RetainedIds {
		if id == "" || expected[id] {
			return nil, ErrAgentNodeDependencyUnverified
		}
		expected[id] = true
	}
	for _, source := range context.RetainedOutputs {
		if source == nil || source.Dispatch == nil || source.Dispatch.Fence == nil || source.Outcome == nil ||
			!expected[source.Dispatch.NodeId] || result[source.Dispatch.NodeId] != nil ||
			source.SettlementMetadataIndex < 1 || source.SettlementMetadataIndex >= run.Recovery.StartIndex ||
			source.Dispatch.MetadataIndex >= run.Recovery.StartIndex {
			return nil, ErrAgentNodeDependencyUnverified
		}
		action, found, err := readAgentExecutionActionTx(tx, source.Dispatch.Fence.ActionId)
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, ErrAgentNodeDependencyUnverified
		}
		binding, err := decodeAgentExecution(action)
		if err != nil {
			return nil, err
		}
		if binding.RunID != run.RunID || binding.Phase != "tasks" {
			return nil, ErrAgentNodeDependencyUnverified
		}
		dispatches, err := readAgentNodeDispatches(action)
		if err != nil {
			return nil, err
		}
		dispatch, found := dispatches[source.Dispatch.NodeId]
		if !found || !proto.Equal(agentOutcomeIntent(dispatch), source.Dispatch) {
			return nil, ErrAgentNodeDependencyUnverified
		}
		outcomes, err := store.agentNodeOutcomes(action, dispatches)
		if err != nil {
			return nil, err
		}
		outcome, found := outcomes[source.Dispatch.NodeId]
		var retained agentv1.AgentNodeOutcomeReceipt
		if !found || proto.Unmarshal(outcome.Receipt, &retained) != nil || !proto.Equal(&retained, source.Outcome) {
			return nil, ErrAgentNodeDependencyUnverified
		}
		settlements, err := store.validatedAgentTaskSettlementsTx(tx, action)
		if err != nil {
			return nil, err
		}
		marker, found := settlements[source.Dispatch.NodeId]
		if !found || marker.MetadataIndex != source.SettlementMetadataIndex {
			return nil, ErrAgentNodeDependencyUnverified
		}
		if err := store.validateAgentRecoveryTerminalTx(tx, action, "tasks"); err != nil {
			return nil, err
		}
		var reset int64
		if err := tx.QueryRow(`SELECT COALESCE(MAX(json_extract(payload_json,'$.lastEvent.index')),-1)
            FROM control_events WHERE domain='agent_run' AND record_id=? AND revision<=?
            AND json_extract(payload_json,'$.lastEvent.type')='agent_reset'
            AND json_extract(payload_json,'$.lastEvent.phase')=?`, run.RunID, run.NextIndex, source.Dispatch.NodeId).Scan(&reset); err != nil {
			return nil, err
		}
		if reset >= source.SettlementMetadataIndex {
			return nil, ErrAgentNodeDependencyUnverified
		}
		result[source.Dispatch.NodeId] = proto.Clone(source).(*agentv1.AgentRunRecoveryEffectSource)
	}
	return result, nil
}
