package store

import (
	"encoding/json"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

type CommitAgentRecoveryProgressRequest struct {
	AgentRecoveryProgressExpected
	OperationID string
	Actor       string
	Receipt     *agentv1.AgentRecoveryProgressReceipt
}

func (store *Control) CommitAgentRecoveryProgress(input CommitAgentRecoveryProgressRequest) (CommitAgentRunRecoveryResult, error) {
	if !validRecoveryProgressExpected(input.AgentRecoveryProgressExpected) || input.Receipt == nil {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	proof, err := (proto.MarshalOptions{Deterministic: true}).Marshal(input.Receipt)
	if err != nil || len(proof) > 1<<20 {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	var receipt agentv1.AgentRecoveryProgressReceipt
	if proto.Unmarshal(proof, &receipt) != nil {
		return CommitAgentRunRecoveryResult{}, ErrAgentRunMetadataInvalid
	}
	expected := input.AgentRecoveryProgressExpected
	if input.Fence != nil {
		expected.Fence = &internalprotocol.ActionFence{ActionId: input.Fence.ActionId, ExecutionEpoch: input.Fence.ExecutionEpoch}
	}
	encoded, err := json.Marshal(struct {
		AgentRecoveryProgressExpected
		OperationID  string `json:"operationId"`
		Actor        string `json:"actor"`
		ReceiptProto []byte `json:"receiptProto"`
	}{expected, input.OperationID, input.Actor, proof})
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, authority, err := store.beginOperatorMutationTx(input.OperationID, expected.RunID, input.Actor, "agent_run")
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	defer tx.Rollback()
	if expected.Fence == nil || expected.Fence.ActionId != expected.RunID {
		return CommitAgentRunRecoveryResult{}, internalprotocol.ErrFenceMismatch
	}
	if err := internalprotocol.AdmitCommand(expected.Fence, uint64(authority.cutover.Epoch)); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	record, found, err := readAgentRecordTx(tx, expected.RunID)
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
		if replay.Domain != "agent_run" || replay.RecordID != expected.RunID {
			return CommitAgentRunRecoveryResult{}, ErrCorruptRecord
		}
		events, err := recoveryReplayEventsTx(tx, expected.RunID, expected.ExpectedIndex, replay.Revision)
		if err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
		if err := store.finishOperatorMutationTx(tx, authority); err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
		return CommitAgentRunRecoveryResult{Mutation: replay, Run: run, Events: events}, nil
	}
	if run.NextIndex != expected.ExpectedIndex || run.RuntimeVersion() != expected.RuntimeRevision || run.Request != expected.Request {
		return CommitAgentRunRecoveryResult{}, ErrRevisionConflict
	}
	if err := agentRecoveryProgressUnreservedTx(tx, run.RunID); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	context, err := store.agentRecoveryProgressContextTx(tx, run, uint64(authority.cutover.Epoch))
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	if err := store.verifyRecoveryProgressReceipt(&receipt, context); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	events := recoveryProgressHeaders(context)
	run.SchemaVersion = 4
	run.RecoveryProgress = &agent.RunRecoveryProgress{Receipt: &receipt}
	bodies := []*agentv1.AgentArtifactReference{receipt.Evidence.NoteBody, receipt.Evidence.NextBody}
	for index := range events {
		event := &events[index]
		event.Body = agent.ArtifactRef{SHA256: bodies[index].Sha256, Length: bodies[index].Length}
		event.RunID, event.ActionID, event.Index = run.RunID, run.RunID, run.NextIndex
		event.ExecutionEpoch, event.CreatedAt = uint64(authority.cutover.Epoch), time.Unix(authority.now, 0).UTC().Format(time.RFC3339)
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
		write.agentEvent, write.agentRecoveryProgress = true, true
		if err := store.putControlRecordTx(tx, write, authority.now); err != nil {
			return CommitAgentRunRecoveryResult{}, err
		}
	}
	payloadDigest, err := operatorPayloadDigest(record.Payload)
	if err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	result := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "agent_run", RecordID: run.RunID,
		Revision: run.NextIndex, State: run.Status, ActionID: run.RunID, ExecutionEpoch: uint64(authority.cutover.Epoch), CutoverRevision: authority.cutover.Revision,
		FencingToken: authority.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, result, store.token, authority.now); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, authority); err != nil {
		return CommitAgentRunRecoveryResult{}, err
	}
	return CommitAgentRunRecoveryResult{Mutation: result, Run: run, Events: events}, nil
}
