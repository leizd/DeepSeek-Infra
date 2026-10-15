package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

var ErrAgentRunRecoveryRequired = errors.New("AGENT_RUN_RECOVERY_REQUIRED")

type CommitAgentRunRecoveryRequest struct {
	AgentRunRecoveryExpected
	OperationID   string              `json:"operationId"`
	Actor         string              `json:"actor"`
	ContextSHA256 string              `json:"contextSha256"`
	Context       agent.ArtifactRef   `json:"contextRef"`
	Request       agent.ArtifactRef   `json:"requestRef"`
	EventBodies   []agent.ArtifactRef `json:"eventBodies"`
}

type CommitAgentRunRecoveryResult struct {
	Mutation OperatorMutationResult
	Run      agent.RunMetadata
	Events   []agent.RunEvent
}

func agentRecoveryEvents(preview AgentRunRecoveryPreview) []agent.RunEvent {
	context := preview.Context
	events := []agent.RunEvent{{Type: "run_status", Status: "running"}}
	for _, id := range context.ResetIds {
		events = append(events, agent.RunEvent{Type: "agent_reset", Phase: id})
	}
	// A one-Agent rerun keeps the old final answer while its task executes.
	// Its driver resets that answer only after task settlement and the rerun
	// note, immediately before the separately admitted synthesis.
	if context.ResetFinal && (context.Selection.GetKind() == agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME || len(context.Execute) == 0) {
		events = append(events, agent.RunEvent{Type: "final_reset", Scope: "final_answer"})
	}
	if context.CompleteImmediately {
		events = append(events, agent.RunEvent{Type: "run_status", Status: "done"})
	}
	return events
}

func changedAgentRecoveryScope(before, after Record) bool {
	var first, second agent.RunMetadata
	if len(before.Payload) != 0 && json.Unmarshal(before.Payload, &first) != nil || json.Unmarshal(after.Payload, &second) != nil {
		return true
	}
	left, err := json.Marshal(first.Recovery)
	right, otherErr := json.Marshal(second.Recovery)
	return err != nil || otherErr != nil || !bytes.Equal(left, right)
}

// Every accepted and historical scope transition must have the same immutable
// predecessor, cursor and request revision. Ordinary event appends cannot clear
// cancellation, rewrite an existing scope or downgrade its metadata schema.
func validateAgentRecoveryMetadataEdge(before, after Record) error {
	next, err := decodeAgentMetadata(after)
	if err != nil {
		return err
	}
	if before.ID == "" {
		if next.Recovery != nil {
			return ErrAgentRunRecoveryRequired
		}
		return nil
	}
	previous, err := decodeAgentMetadata(before)
	if err != nil {
		return err
	}
	expectedCancelled, expectedGeneration := previous.Cancelled, previous.CancellationGeneration
	if next.LastEvent.Type == "run_status" && next.LastEvent.Status == "cancelled" {
		expectedCancelled = true
		if next.Recovery != nil {
			expectedGeneration = next.Recovery.Generation
		}
	}
	if next.Cancelled != expectedCancelled || next.CancellationGeneration != expectedGeneration {
		return ErrAgentRunRecoveryRequired
	}
	if !changedAgentRecoveryScope(before, after) {
		return nil
	}
	if next.Recovery == nil || next.LastEvent.Type != "run_status" || next.LastEvent.Status != "running" ||
		next.LastEvent.Index != before.Revision || next.NextIndex != before.Revision+1 {
		return ErrAgentRunRecoveryRequired
	}
	context := next.Recovery.Metadata
	if context.MetadataEpoch != next.LastEvent.ExecutionEpoch || context.ExpectedIndex != previous.NextIndex || context.ExpectedRuntimeRevision != previous.RuntimeVersion() ||
		!proto.Equal(context.ExpectedRequest, recoveryArtifact(previous.Request)) ||
		!proto.Equal(context.PreviousScope, recoveryScopeProtocol(previous.Recovery)) {
		return ErrAgentRunRecoveryRequired
	}
	expected := previous
	expected.SchemaVersion, expected.RuntimeRevision, expected.Request, expected.Recovery = 3, previous.RuntimeVersion()+1, next.Request, next.Recovery
	expected.ApplyRunEvent(next.LastEvent)
	first, err := json.Marshal(expected)
	second, otherErr := json.Marshal(next)
	if err != nil || otherErr != nil || !bytes.Equal(first, second) {
		return ErrAgentRunRecoveryRequired
	}
	return nil
}

func recoveryReplayEventsTx(tx *sql.Tx, runID string, first, last int64) ([]agent.RunEvent, error) {
	if first < 1 || last <= first || last-first > 64 {
		return nil, ErrCorruptRecord
	}
	rows, err := tx.Query(`SELECT payload_json FROM control_events WHERE domain='agent_run'
        AND record_id=? AND revision>? AND revision<=? ORDER BY revision`, runID, first, last)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	events := make([]agent.RunEvent, 0, int(last-first))
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		var snapshot agent.RunMetadata
		if json.Unmarshal([]byte(payload), &snapshot) != nil || snapshot.LastEvent.Index != first+int64(len(events)) {
			return nil, ErrCorruptRecord
		}
		events = append(events, snapshot.LastEvent)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if int64(len(events)) != last-first {
		return nil, ErrCorruptRecord
	}
	return events, nil
}

// The complete old-effect audit, context CAS, immutable request swap, new
// generation, initial public events and operation receipt commit atomically.
// This never claims a worker phase, releases a reservation or grants an effect.
func (store *Control) CommitAgentRunRecovery(input CommitAgentRunRecoveryRequest) (CommitAgentRunRecoveryResult, error) {
	if !validAgentRecoveryExpected(input.AgentRunRecoveryExpected) || !validAgentArtifact(input.Context) ||
		!validAgentArtifact(input.Request) || input.Request == input.ExpectedRequest ||
		!agentproof.ValidNodeOutcomeDigest(input.ContextSHA256) || len(input.EventBodies) == 0 || len(input.EventBodies) > 64 {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	for _, body := range input.EventBodies {
		if !validAgentArtifact(body) {
			return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
		}
	}
	selection, _, err := normalizeAgentRecoverySelection(input.Selection)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	input.Selection = selection
	encoded, err := json.Marshal(input)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	var mutation CommitAgentRunRecoveryRequest
	if json.Unmarshal(encoded, &mutation) != nil {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, authority, err := store.beginOperatorMutationTx(mutation.OperationID, mutation.RunID, mutation.Actor, "agent_run")
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	defer tx.Rollback()
	if mutation.Fence == nil || mutation.Fence.ActionId != mutation.RunID {
		return CommitAgentRunRecoveryResult{}, internalprotocol.ErrFenceMismatch
	}
	if err := internalprotocol.AdmitCommand(mutation.Fence, uint64(authority.cutover.Epoch)); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, mutation.OperationID, requestDigest)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	record, found, err := readAgentRecordTx(tx, mutation.RunID)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	if !found {
		return CommitAgentRunRecoveryResult{}, ErrEmptyRecordID
	}
	run, err := decodeAgentMetadata(record)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	if applied {
		if replay.Domain != "agent_run" || replay.RecordID != mutation.RunID {
			return CommitAgentRunRecoveryResult{}, ErrCorruptRecord
		}
		events, err := recoveryReplayEventsTx(tx, mutation.RunID, mutation.ExpectedIndex, replay.Revision)
		if err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
		if err := store.finishOperatorMutationTx(tx, authority); err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
		return CommitAgentRunRecoveryResult{Mutation: replay, Run: run, Events: events}, nil
	}
	preview, err := store.agentRecoveryContextTx(tx, record, mutation.AgentRunRecoveryExpected)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	if preview.ContextSHA256 != mutation.ContextSHA256 {
		return CommitAgentRunRecoveryResult{}, ErrRevisionConflict
	}
	if run.Recovery != nil && mutation.Context == run.Recovery.Context {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	events := agentRecoveryEvents(preview)
	if len(events) != len(mutation.EventBodies) {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	run.SchemaVersion, run.RuntimeRevision, run.Request = 3, run.RuntimeVersion()+1, mutation.Request
	run.Recovery = &agent.RunRecoveryScope{Generation: preview.Context.Generation, Context: mutation.Context,
		ContextSHA256: preview.ContextSHA256, StartIndex: mutation.ExpectedIndex, Metadata: preview.Context}
	for index := range events {
		event := &events[index]
		event.Body, event.RunID, event.ActionID = mutation.EventBodies[index], run.RunID, run.RunID
		event.Index, event.ExecutionEpoch, event.CreatedAt = run.NextIndex, uint64(authority.cutover.Epoch), time.Unix(authority.now, 0).UTC().Format(time.RFC3339)
		run.ApplyRunEvent(*event)
		payload, err := json.Marshal(run)
		if err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
		record = Record{Domain: "agent_run", ID: run.RunID, State: run.Status, Revision: run.NextIndex, ExecutionEpoch: event.ExecutionEpoch, Payload: payload}
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
		write.agentEvent, write.agentRecovery = true, true
		if err := store.putControlRecordTx(tx, write, authority.now); err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
	}
	payloadDigest, err := operatorPayloadDigest(record.Payload)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	result := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: mutation.OperationID, Domain: "agent_run", RecordID: run.RunID,
		Revision: run.NextIndex, State: run.Status, ActionID: run.RunID, ExecutionEpoch: uint64(authority.cutover.Epoch),
		CutoverRevision: authority.cutover.Revision, FencingToken: authority.cutover.FencingToken,
		PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: mutation.Actor}
	if err := insertOperatorMutationTx(tx, result, store.token, authority.now); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, authority); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	return CommitAgentRunRecoveryResult{Mutation: result, Run: run, Events: events}, nil
}
