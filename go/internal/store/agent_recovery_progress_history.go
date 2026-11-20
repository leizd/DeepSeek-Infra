package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"math"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func validRecoveryProgressHeader(run agent.RunMetadata) bool {
	if run.RecoveryProgress == nil {
		return true
	}
	if run.Recovery == nil || !recoveryTaskProgressRequired(run) {
		return false
	}
	receipt := run.RecoveryProgress.Receipt
	if receipt == nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil || receipt.Evidence.SchemaVersion != 1 || len(receipt.Signature) != 64 {
		return false
	}
	evidence := receipt.Evidence
	context := evidence.Context
	return context != nil && context.SchemaVersion == 1 && context.RunId == run.RunID && context.Generation == run.Recovery.Generation &&
		context.MetadataEpoch > 0 && context.MetadataEpoch <= run.LastEvent.ExecutionEpoch && context.MetadataEpoch >= run.Recovery.Metadata.MetadataEpoch &&
		context.ExpectedIndex > run.Recovery.StartIndex && context.ExpectedIndex < math.MaxInt64-2 && context.ExpectedIndex < run.NextIndex &&
		context.RuntimeRevision == run.RuntimeVersion() && context.Request != nil && context.Request.Sha256 == run.Request.SHA256 && context.Request.Length == run.Request.Length &&
		context.RecoveryContextSha256 == run.Recovery.ContextSHA256 && context.TaskActionId != "" && context.TaskActionRevision > 0 && context.TaskMetadataIndex == context.ExpectedIndex &&
		context.Role == run.Recovery.Metadata.Selection.AgentId && context.Synthesize == run.Recovery.Metadata.Synthesize && len(context.Outputs) == 1 &&
		agentproof.ValidNodeOutcomeArtifact(evidence.NoteBody) && agentproof.ValidNodeOutcomeArtifact(evidence.NextBody)
}

func changedRecoveryProgress(before, after Record) bool {
	var left, right agent.RunMetadata
	if len(before.Payload) != 0 && json.Unmarshal(before.Payload, &left) != nil || json.Unmarshal(after.Payload, &right) != nil {
		return true
	}
	a, err := json.Marshal(left.RecoveryProgress)
	b, other := json.Marshal(right.RecoveryProgress)
	return err != nil || other != nil || !bytes.Equal(a, b)
}

func validateRecoveryProgressEdge(before, after Record, previous, next agent.RunMetadata) error {
	if !changedRecoveryProgress(before, after) {
		if previous.RecoveryProgress != nil {
			proof := previous.RecoveryProgress.Receipt.Evidence
			if previous.NextIndex == proof.Context.ExpectedIndex+1 && !matchesRecoveryProgressEvent(next.LastEvent, proof.Context, proof.NextBody, false) {
				return ErrAgentRunRecoveryRequired
			}
		}
		return nil
	}
	if changedAgentRecoveryScope(before, after) && next.RecoveryProgress == nil {
		return nil
	}
	if previous.RecoveryProgress != nil || next.RecoveryProgress == nil || !validRecoveryProgressHeader(next) || changedAgentRecoveryScope(before, after) {
		return ErrAgentRunRecoveryRequired
	}
	context := next.RecoveryProgress.Receipt.Evidence.Context
	if context.ExpectedIndex != previous.NextIndex || next.NextIndex != previous.NextIndex+1 ||
		!matchesRecoveryProgressEvent(next.LastEvent, context, next.RecoveryProgress.Receipt.Evidence.NoteBody, true) ||
		next.Request != previous.Request || next.RuntimeVersion() != previous.RuntimeVersion() || previous.Status != "running" || next.Status != "running" {
		return ErrAgentRunRecoveryRequired
	}
	return nil
}

func matchesRecoveryProgressEvent(event agent.RunEvent, context *agentv1.AgentRecoveryProgressContext, body *agentv1.AgentArtifactReference, note bool) bool {
	if context == nil || body == nil || event.RunID != context.RunId || event.ActionID != context.RunId || event.ExecutionEpoch != context.MetadataEpoch ||
		event.Body.SHA256 != body.Sha256 || event.Body.Length != body.Length {
		return false
	}
	if note {
		return event.Index == context.ExpectedIndex && event.Type == "agent_note" && event.Phase == context.Role && event.Status == "" && event.Scope == ""
	}
	if context.Synthesize {
		return event.Index == context.ExpectedIndex+1 && event.Type == "final_reset" && event.Phase == "" && event.Status == "" && event.Scope == "final_answer"
	}
	return event.Index == context.ExpectedIndex+1 && event.Type == "run_status" && event.Phase == "" && event.Status == "done" && event.Scope == ""
}

func (store *Control) validateRecoveryProgressTx(tx *sql.Tx, run agent.RunMetadata) error {
	if run.RecoveryProgress == nil {
		return nil
	}
	if !validRecoveryProgressHeader(run) {
		return ErrAgentRunRecoveryRequired
	}
	context := run.RecoveryProgress.Receipt.Evidence.Context
	if run.NextIndex < context.ExpectedIndex+2 {
		return ErrAgentRunRecoveryRequired
	}
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,
        record_digest,writer_fencing_token,recorded_at FROM control_events
        WHERE domain='agent_run' AND record_id=? AND revision=?`, run.RunID, context.ExpectedIndex), "agent_run")
	if err != nil {
		return err
	}
	if !found {
		return ErrCorruptRecord
	}
	previous, err := decodeAgentMetadata(record)
	if err != nil {
		return err
	}
	expected, err := store.agentRecoveryProgressContextTx(tx, previous, context.MetadataEpoch)
	if err != nil {
		return err
	}
	if err := store.verifyRecoveryProgressReceipt(run.RecoveryProgress.Receipt, expected); err != nil {
		return err
	}
	events, err := recoveryReplayEventsTx(tx, run.RunID, context.ExpectedIndex, context.ExpectedIndex+2)
	if err != nil {
		return err
	}
	proof := run.RecoveryProgress.Receipt.Evidence
	if !matchesRecoveryProgressEvent(events[0], context, proof.NoteBody, true) || !matchesRecoveryProgressEvent(events[1], context, proof.NextBody, false) {
		return ErrAgentRunRecoveryRequired
	}
	return nil
}
