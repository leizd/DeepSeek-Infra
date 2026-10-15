package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"sort"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

var ErrAgentRunRecoveryUnsettled = errors.New("AGENT_RUN_RECOVERY_UNSETTLED")

type agentRecoveryHistory struct {
	effects   []*agentv1.AgentRunRecoveryEffectSource
	outputs   map[string]*agentv1.AgentRunRecoveryEffectSource
	outputIDs []string
	synthesis []Record
}

// This observes existing Go history only. It neither cancels an action nor
// releases a reservation. afterIndex must come from the preceding verified
// recovery scope, never from an operator's untrusted request.
func (store *Control) agentRecoveryHistoryTx(tx *sql.Tx, run agent.RunMetadata, afterIndex int64) (agentRecoveryHistory, error) {
	result := agentRecoveryHistory{effects: []*agentv1.AgentRunRecoveryEffectSource{},
		outputs: map[string]*agentv1.AgentRunRecoveryEffectSource{}, outputIDs: []string{}, synthesis: []Record{}}
	if afterIndex < 0 || afterIndex > run.NextIndex {
		return result, ErrAgentRunMetadataInvalid
	}
	var reserved bool
	if err := tx.QueryRow(`SELECT EXISTS(SELECT 1 FROM action_resource_leases WHERE resource_key=?)`,
		"agent-run:"+run.RunID).Scan(&reserved); err != nil {
		return result, err
	}
	// Expiry is not evidence that a remote effect did not occur.
	if reserved {
		return result, ErrAgentRunRecoveryUnsettled
	}
	rows, err := tx.Query(`SELECT id FROM action_journal
        WHERE json_extract(payload_json,'$.nativeAgentExecution.runId')=?
        AND json_extract(payload_json,'$.nativeAgentExecution.metadataIndex')>=? ORDER BY id`, run.RunID, afterIndex)
	if err != nil {
		return result, err
	}
	ids := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return result, err
		}
		ids = append(ids, id)
	}
	readErr, closeErr := rows.Err(), rows.Close()
	if readErr != nil {
		return result, readErr
	}
	if closeErr != nil {
		return result, closeErr
	}
	for _, id := range ids {
		record, found, err := readAgentExecutionActionTx(tx, id)
		if err != nil {
			return result, err
		}
		if !found {
			return result, ErrCorruptRecord
		}
		binding, err := decodeAgentExecution(record)
		if err != nil || binding.RunID != run.RunID || binding.MetadataIndex < afterIndex || binding.MetadataIndex >= run.NextIndex {
			return result, ErrCorruptRecord
		}
		if err := store.validateAgentRecoveryTerminalTx(tx, record, binding.Phase); err != nil {
			return result, err
		}
		nodes, err := readAgentNodeDispatches(record)
		if err != nil {
			return result, err
		}
		outcomes, err := store.agentNodeOutcomes(record, nodes)
		if err != nil {
			return result, err
		}
		if len(outcomes) != len(nodes) {
			return result, ErrAgentRunRecoveryUnsettled
		}
		markers := map[string]AgentTaskSettlement{}
		if binding.Phase == "tasks" {
			markers, err = store.validatedAgentTaskSettlementsTx(tx, record)
			if err != nil {
				return result, err
			}
		}
		roles := make([]string, 0, len(nodes))
		for role := range nodes {
			roles = append(roles, role)
		}
		sort.Strings(roles)
		for _, role := range roles {
			var receipt agentv1.AgentNodeOutcomeReceipt
			if proto.Unmarshal(outcomes[role].Receipt, &receipt) != nil {
				return result, ErrCorruptRecord
			}
			source := &agentv1.AgentRunRecoveryEffectSource{Dispatch: agentOutcomeIntent(nodes[role]), Outcome: &receipt}
			if marker, present := markers[role]; present {
				if marker.MetadataIndex >= run.NextIndex {
					return result, ErrCorruptRecord
				}
				source.SettlementMetadataIndex = marker.MetadataIndex
				if previous := result.outputs[role]; previous == nil || previous.SettlementMetadataIndex < marker.MetadataIndex {
					result.outputs[role] = source
				}
			}
			result.effects = append(result.effects, source)
		}
		if binding.Phase == "synthesis" && record.State == "SUCCEEDED" {
			result.synthesis = append(result.synthesis, record)
		}
	}
	for role := range result.outputs {
		result.outputIDs = append(result.outputIDs, role)
	}
	sort.Slice(result.outputIDs, func(first, second int) bool {
		left, right := result.outputs[result.outputIDs[first]], result.outputs[result.outputIDs[second]]
		if left.SettlementMetadataIndex == right.SettlementMetadataIndex {
			return result.outputIDs[first] < result.outputIDs[second]
		}
		return left.SettlementMetadataIndex < right.SettlementMetadataIndex
	})
	return result, nil
}

func (store *Control) validateAgentRecoveryTerminalTx(tx *sql.Tx, record Record, phase string) error {
	var fields map[string]json.RawMessage
	if json.Unmarshal(record.Payload, &fields) != nil {
		return ErrCorruptRecord
	}
	if record.State == "CANCELLED_CONFIRMED" {
		if _, present := fields[agentCancellationSettlementField]; !present {
			return ErrAgentRunRecoveryUnsettled
		}
		return store.validateAgentCancellationSettlementTx(tx, record)
	}
	if record.State != "SUCCEEDED" {
		// A FAILED label alone, a lost lease or EFFECT_UNKNOWN cannot authorize
		// another provider call. The original typed settlement must exist.
		return ErrAgentRunRecoveryUnsettled
	}
	var field string
	var validate func(*sql.Tx, Record) error
	switch phase {
	case "plan":
		field, validate = agentPlanSettlementField, store.validateAgentPlanSettlementTx
	case "tasks":
		field, validate = agentTasksCompletionField, store.validateAgentTasksCompletionTx
	case "synthesis":
		field, validate = agentSynthesisSettlementField, store.validateAgentSynthesisSettlementTx
	default:
		return ErrCorruptRecord
	}
	if _, present := fields[field]; !present {
		return ErrAgentRunRecoveryUnsettled
	}
	return validate(tx, record)
}
