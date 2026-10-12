package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"math"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

const agentTaskSettlementField = "nativeAgentTaskSettlements"

type AgentTaskSettlement struct {
	SchemaVersion       uint32 `json:"schemaVersion"`
	SourceReceiptSHA256 string `json:"sourceReceiptSha256"`
	MetadataIndex       int64  `json:"metadataIndex"`
	MetadataEpoch       uint64 `json:"metadataEpoch"`
	ActionRevision      int64  `json:"actionRevision"`
	ExecutionEpoch      uint64 `json:"executionEpoch"`
	WriterFencingToken  int64  `json:"writerFencingToken"`
	RecordedAt          int64  `json:"recordedAt"`
}

type AgentTaskSettlementRequest struct {
	OperationID, Actor, ActionID, RunID, NodeID, Owner, ClaimToken, SourceReceiptSHA256 string
	Epoch                                                                               uint64
	ExpectedIndex                                                                       int64
}

type AgentTaskSettlementResult struct {
	AlreadyApplied bool
	Run            agent.RunMetadata
	Action         Record
}

func readAgentTaskSettlements(record Record) (map[string]AgentTaskSettlement, error) {
	var fields map[string]json.RawMessage
	if json.Unmarshal(record.Payload, &fields) != nil {
		return nil, ErrCorruptRecord
	}
	values := map[string]AgentTaskSettlement{}
	if raw, present := fields[agentTaskSettlementField]; present {
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&values) != nil || values == nil || len(values) == 0 || len(values) > 4 {
			return nil, ErrCorruptRecord
		}
	}
	for node, marker := range values {
		if node == "" || node == "leader" || node == "synthesizer" || len(node) > 64 || marker.SchemaVersion != 1 ||
			!agentproof.ValidNodeOutcomeDigest(marker.SourceReceiptSHA256) || marker.MetadataIndex < 1 || marker.MetadataIndex >= math.MaxInt64 ||
			marker.MetadataEpoch == 0 || marker.MetadataEpoch > math.MaxInt64 || marker.ActionRevision < 1 || marker.ActionRevision > record.Revision ||
			marker.ExecutionEpoch == 0 || marker.ExecutionEpoch > record.ExecutionEpoch || marker.WriterFencingToken <= 0 || marker.RecordedAt < 0 {
			return nil, ErrCorruptRecord
		}
	}
	return values, nil
}

// A same-state edge adds exactly one immutable task marker. The transaction
// and every dependency admission additionally verify its original source proof.
func nativeAgentTaskSettlementEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != "action" || before.ID != after.ID || !agentOutcomeRecordState(before.State) ||
		after.State != before.State || after.Revision != before.Revision+1 || after.ExecutionEpoch != before.ExecutionEpoch ||
		!nativeAgentExecution(before.Payload) || !sameAgentExecutionIntent(before, after) {
		return false
	}
	var previous, current map[string]json.RawMessage
	if json.Unmarshal(before.Payload, &previous) != nil || json.Unmarshal(after.Payload, &current) != nil {
		return false
	}
	delete(previous, agentTaskSettlementField)
	delete(current, agentTaskSettlementField)
	a, _ := json.Marshal(previous)
	b, _ := json.Marshal(current)
	old, err := readAgentTaskSettlements(before)
	next, other := readAgentTaskSettlements(after)
	if err != nil || other != nil || !bytes.Equal(a, b) || !nativeMetadataMapAppend(old, next) {
		return false
	}
	for id, marker := range next {
		if _, exists := old[id]; !exists && (marker.ActionRevision != after.Revision || marker.ExecutionEpoch != after.ExecutionEpoch) {
			return false
		}
	}
	return true
}

func taskSettlementEvent(runID, nodeID string, marker AgentTaskSettlement, evidence *agentv1.AgentNodeOutcomeEvidence) agent.RunEvent {
	duration := evidence.TaskOutput.LatencyMs
	return agent.RunEvent{ActionID: runID, ExecutionEpoch: marker.MetadataEpoch, RunID: runID, Index: marker.MetadataIndex,
		CreatedAt: time.Unix(marker.RecordedAt, 0).UTC().Format(time.RFC3339), Type: "agent_output", Phase: nodeID,
		Failed: evidence.TaskOutput.Failed, DurationMS: &duration, PromptTokens: evidence.TaskOutput.PromptTokens,
		CompletionTokens: evidence.TaskOutput.CompletionTokens, Body: agent.ArtifactRef{SHA256: evidence.PublicEvent.Sha256, Length: evidence.PublicEvent.Length}}
}

// Reopen and dependency admission verify the immutable Rust proof, original
// admission, Go event and the actual writer of that settlement, independently
// of mutable node-success flags or a successor's current fencing token.
func (store *Control) validatedAgentTaskSettlementsTx(tx *sql.Tx, action Record) (map[string]AgentTaskSettlement, error) {
	markers, err := readAgentTaskSettlements(action)
	if err != nil || len(markers) == 0 {
		return markers, err
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.Phase != "tasks" {
		return nil, ErrCorruptRecord
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return nil, err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return nil, err
	}
	originalRecord, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, binding.MetadataIndex), "agent_run")
	if err != nil || !found {
		return nil, ErrCorruptRecord
	}
	original, err := decodeAgentMetadata(originalRecord)
	if err != nil || originalRecord.ExecutionEpoch != binding.MetadataEpoch || original.NextIndex != binding.MetadataIndex || original.Request != binding.Request || executionPlanDigest(original) != binding.PlanDigest || !executionPhaseReady(original, "tasks") {
		return nil, ErrCorruptRecord
	}
	manifest, _ := json.Marshal([]string{"agent-run:" + binding.RunID})
	indices := map[int64]bool{}
	for node, marker := range markers {
		source, exists := outcomes[node]
		intent, dispatched := nodes[node]
		if !exists || !dispatched || intent.Phase != "tasks" || marker.MetadataIndex < binding.MetadataIndex || marker.MetadataEpoch != binding.MetadataEpoch ||
			intent.ExecutionEpoch > marker.ExecutionEpoch || indices[marker.MetadataIndex] {
			return nil, ErrCorruptRecord
		}
		indices[marker.MetadataIndex] = true
		digest := sha256.Sum256(source.Receipt)
		if marker.SourceReceiptSHA256 != hex.EncodeToString(digest[:]) {
			return nil, ErrCorruptRecord
		}
		admission, err := readAgentOutcomeAdmissionTx(tx, intent, string(manifest))
		if err != nil || admission.WriterFencingToken != source.WriterFencingToken {
			return nil, ErrCorruptRecord
		}
		var receipt agentv1.AgentNodeOutcomeReceipt
		if proto.Unmarshal(source.Receipt, &receipt) != nil || receipt.Evidence == nil || receipt.Evidence.TaskOutput == nil {
			return nil, ErrCorruptRecord
		}
		metadata, eventMeta, present, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, marker.MetadataIndex+1), "agent_run")
		if err != nil || !present || metadata.ExecutionEpoch != marker.MetadataEpoch || eventMeta.writerToken != marker.WriterFencingToken || eventMeta.timestamp != marker.RecordedAt {
			return nil, ErrCorruptRecord
		}
		run, err := decodeAgentMetadata(metadata)
		if err != nil || run.Request != binding.Request || executionPlanDigest(run) != binding.PlanDigest {
			return nil, ErrCorruptRecord
		}
		expected := taskSettlementEvent(binding.RunID, node, marker, receipt.Evidence)
		actualBytes, _ := json.Marshal(run.LastEvent)
		expectedBytes, _ := json.Marshal(expected)
		if !bytes.Equal(actualBytes, expectedBytes) {
			return nil, ErrCorruptRecord
		}
		settled, settledMeta, present, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, marker.ActionRevision), "action")
		if err != nil || !present || settled.ExecutionEpoch != marker.ExecutionEpoch || settledMeta.writerToken != marker.WriterFencingToken || settledMeta.timestamp != marker.RecordedAt || !sameAgentExecutionIntent(settled, action) {
			return nil, ErrCorruptRecord
		}
		retained, err := readAgentTaskSettlements(settled)
		if err != nil || retained[node] != marker {
			return nil, ErrCorruptRecord
		}
	}
	return markers, nil
}

// Commit the public output and dependency proof together. Completing one task
// does not terminate its phase lease, release resources or fabricate synthesis.
func (store *Control) SettleAgentTask(input AgentTaskSettlementRequest) (AgentTaskSettlementResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.NodeID == "" || len(input.NodeID) > 64 || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.ExpectedIndex < 1 || input.ExpectedIndex >= math.MaxInt64 || !agentproof.ValidNodeOutcomeDigest(input.SourceReceiptSHA256) {
		return AgentTaskSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	encoded, _ := json.Marshal(input)
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	defer tx.Rollback()
	actionAuthority, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	if !IsDomainGoAuthoritative(actionAuthority.State) {
		return AgentTaskSettlementResult{}, ErrCutoverNotAuthorized
	}
	action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil || !found {
		return AgentTaskSettlementResult{}, ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.Phase != "tasks" || binding.RunID != input.RunID || action.ExecutionEpoch != input.Epoch {
		return AgentTaskSettlementResult{}, ErrAgentRunExecutionRequired
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	markers, err := store.validatedAgentTaskSettlementsTx(tx, action)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	if applied {
		marker, present := markers[input.NodeID]
		if !present || replay.Domain != "action" || replay.RecordID != action.ID || marker.SourceReceiptSHA256 != input.SourceReceiptSHA256 || marker.MetadataIndex != input.ExpectedIndex {
			return AgentTaskSettlementResult{}, ErrCorruptRecord
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return AgentTaskSettlementResult{}, err
		}
		return AgentTaskSettlementResult{AlreadyApplied: true, Run: run, Action: action}, nil
	}
	if _, exists := markers[input.NodeID]; exists || !agentOutcomeRecordState(action.State) || run.NextIndex != input.ExpectedIndex || executionPlanDigest(run) != binding.PlanDigest || !executionPhaseReady(run, "tasks") {
		return AgentTaskSettlementResult{}, ErrRevisionConflict
	}
	lease, err := store.agentNodeLeaseTx(tx, AgentNodeDispatchRequest{ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, Owner: input.Owner, ClaimToken: input.ClaimToken}, context.now)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	source, exists := outcomes[input.NodeID]
	intent, dispatched := nodes[input.NodeID]
	if !exists || !dispatched || intent.Phase != "tasks" {
		return AgentTaskSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	sourceHash := sha256.Sum256(source.Receipt)
	if hex.EncodeToString(sourceHash[:]) != input.SourceReceiptSHA256 {
		return AgentTaskSettlementResult{}, ErrAgentNodeOutcomeConflict
	}
	admission, err := readAgentOutcomeAdmissionTx(tx, intent, manifest)
	if err != nil || admission.WriterFencingToken != source.WriterFencingToken {
		return AgentTaskSettlementResult{}, ErrCorruptRecord
	}
	var receipt agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(source.Receipt, &receipt) != nil || receipt.Evidence == nil || receipt.Evidence.TaskOutput == nil {
		return AgentTaskSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	if action.Revision >= math.MaxInt64-1 {
		return AgentTaskSettlementResult{}, ErrRevisionConflict
	}
	marker := AgentTaskSettlement{SchemaVersion: 1, SourceReceiptSHA256: input.SourceReceiptSHA256, MetadataIndex: run.NextIndex, MetadataEpoch: uint64(context.cutover.Epoch), ActionRevision: action.Revision + 1, ExecutionEpoch: input.Epoch, WriterFencingToken: store.token, RecordedAt: context.now}
	event := taskSettlementEvent(run.RunID, input.NodeID, marker, receipt.Evidence)
	run.ApplyRunEvent(event)
	payload, err := json.Marshal(run)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	write, err := prepareControlRecordWrite(Record{Domain: "agent_run", ID: run.RunID, Revision: run.NextIndex, ExecutionEpoch: event.ExecutionEpoch, State: run.Status, Payload: payload}, nil)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	write.agentEvent = true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AgentTaskSettlementResult{}, err
	}
	markers[input.NodeID] = marker
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return AgentTaskSettlementResult{}, ErrCorruptRecord
	}
	fields[agentTaskSettlementField], err = json.Marshal(markers)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	action.Payload, err = json.Marshal(fields)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	action.Revision++
	write, err = prepareControlRecordWrite(action, nil)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	write.allowBoundLease, write.agentTaskSettlement = true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AgentTaskSettlementResult{}, err
	}
	action = write.record
	retained, found, err := readAgentExecutionActionTx(tx, action.ID)
	if err != nil || !found || retained.Revision != action.Revision || !bytes.Equal(retained.Payload, action.Payload) {
		return AgentTaskSettlementResult{}, ErrCorruptRecord
	}
	retainedRun, err := store.agentExecutionRunTx(tx, binding)
	if err != nil || retainedRun.NextIndex != run.NextIndex {
		return AgentTaskSettlementResult{}, ErrCorruptRecord
	}
	if _, err := store.validatedAgentTaskSettlementsTx(tx, action); err != nil {
		return AgentTaskSettlementResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(action.Payload)
	if err != nil {
		return AgentTaskSettlementResult{}, err
	}
	operation := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "action", RecordID: action.ID, Revision: action.Revision, State: action.State,
		ActionID: action.ID, ExecutionEpoch: input.Epoch, CutoverRevision: actionAuthority.Revision, FencingToken: actionAuthority.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, operation, store.token, context.now); err != nil {
		return AgentTaskSettlementResult{}, err
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return AgentTaskSettlementResult{}, ErrWriterFenceHeld
	}
	if commitNow >= lease.LeaseUntil {
		return AgentTaskSettlementResult{}, ErrActionLeaseExpired
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return AgentTaskSettlementResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentTaskSettlementResult{}, err
	}
	return AgentTaskSettlementResult{Run: run, Action: action}, nil
}
