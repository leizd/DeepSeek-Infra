package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"math"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

const agentTasksCompletionField = "nativeAgentTasksCompletion"

type AgentTasksCompletion struct {
	SchemaVersion      uint32 `json:"schemaVersion"`
	SettlementsSHA256  string `json:"settlementsSha256"`
	MetadataIndex      int64  `json:"metadataIndex"`
	MetadataEpoch      uint64 `json:"metadataEpoch"`
	ActionRevision     int64  `json:"actionRevision"`
	ExecutionEpoch     uint64 `json:"executionEpoch"`
	WriterFencingToken int64  `json:"fencingToken"`
	RecordedAt         int64  `json:"recordedAt"`
}

type CompleteAgentTasksRequest struct {
	OperationID, Actor, ActionID, RunID, Owner, ClaimToken string
	Epoch                                                  uint64
	ExpectedIndex                                          int64
}

type CompleteAgentTasksResult struct {
	AlreadyApplied bool
	Run            agent.RunMetadata
	Action         Record
}

func completedAgentTasks(run agent.RunMetadata) bool {
	if run.Status != "running" || run.Cancelled || len(run.Plan) == 0 || len(run.Plan) > 4 {
		return false
	}
	nodes := run.Nodes()
	for _, entry := range run.Plan {
		if nodes[entry.ID].State != "succeeded" || nodes[entry.ID].Failed {
			return false
		}
	}
	return true
}

func taskSettlementsDigest(markers map[string]AgentTaskSettlement) string {
	encoded, _ := json.Marshal(markers)
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:])
}

func readAgentTasksCompletion(action Record) (AgentTasksCompletion, bool, error) {
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return AgentTasksCompletion{}, false, ErrCorruptRecord
	}
	raw, present := fields[agentTasksCompletionField]
	if !present {
		return AgentTasksCompletion{}, false, nil
	}
	var value AgentTasksCompletion
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&value) != nil || value.SchemaVersion != 1 || action.State != "SUCCEEDED" ||
		value.ActionRevision != action.Revision || value.ExecutionEpoch != action.ExecutionEpoch ||
		value.MetadataIndex < 1 || value.MetadataIndex >= math.MaxInt64 || value.MetadataEpoch == 0 || value.MetadataEpoch > math.MaxInt64 ||
		value.WriterFencingToken <= 0 || value.RecordedAt < 0 || len(value.SettlementsSHA256) != 64 {
		return AgentTasksCompletion{}, true, ErrCorruptRecord
	}
	if digest, err := hex.DecodeString(value.SettlementsSHA256); err != nil || len(digest) != sha256.Size || strings.ToLower(value.SettlementsSHA256) != value.SettlementsSHA256 {
		return AgentTasksCompletion{}, true, ErrCorruptRecord
	}
	return value, true, nil
}

func nativeAgentTasksCompletionEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != "action" || before.ID != after.ID || !agentOutcomeRecordState(before.State) ||
		after.State != "SUCCEEDED" || after.Revision != before.Revision+1 || after.ExecutionEpoch != before.ExecutionEpoch || !sameAgentExecutionIntent(before, after) {
		return false
	}
	var previous, current map[string]json.RawMessage
	if json.Unmarshal(before.Payload, &previous) != nil || json.Unmarshal(after.Payload, &current) != nil {
		return false
	}
	if _, present := previous[agentTasksCompletionField]; present {
		return false
	}
	if _, present, err := readAgentTasksCompletion(after); err != nil || !present {
		return false
	}
	delete(current, agentTasksCompletionField)
	a, _ := json.Marshal(previous)
	b, _ := json.Marshal(current)
	return bytes.Equal(a, b)
}

// The terminal marker is independently bound to all original signed task
// settlements, the immutable metadata checkpoint and the termination journal.
func (store *Control) validateAgentTasksCompletionTx(tx *sql.Tx, action Record) error {
	completion, present, err := readAgentTasksCompletion(action)
	if err != nil || !present {
		return err
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.Phase != "tasks" || completion.MetadataEpoch != binding.MetadataEpoch {
		return ErrCorruptRecord
	}
	markers, err := store.validatedAgentTaskSettlementsTx(tx, action)
	if err != nil || taskSettlementsDigest(markers) != completion.SettlementsSHA256 {
		return ErrCorruptRecord
	}
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, completion.MetadataIndex), "agent_run")
	if err != nil || !found || record.ExecutionEpoch != completion.MetadataEpoch {
		return ErrCorruptRecord
	}
	run, err := decodeAgentMetadata(record)
	if err != nil || run.Request != binding.Request || executionPlanDigest(run) != binding.PlanDigest || !completedAgentTasks(run) || len(markers) != len(run.Plan) {
		return ErrCorruptRecord
	}
	for _, node := range run.Plan {
		marker, present := markers[node.ID]
		if !present || marker.MetadataIndex >= completion.MetadataIndex {
			return ErrCorruptRecord
		}
	}
	retained, meta, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, completion.ActionRevision), "action")
	if err != nil || !found || !bytes.Equal(retained.Payload, action.Payload) || retained.State != "SUCCEEDED" ||
		meta.writerToken != completion.WriterFencingToken || meta.timestamp != completion.RecordedAt {
		return ErrCorruptRecord
	}
	manifest, _ := json.Marshal([]string{"agent-run:" + binding.RunID})
	var count int
	if tx.QueryRow(`SELECT count(*) FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED' AND epoch=? AND claim_revision=? AND writer_fencing_token=? AND recorded_at=? AND resource_keys_json=?`, action.ID, int64(completion.ExecutionEpoch), completion.ActionRevision, completion.WriterFencingToken, completion.RecordedAt, string(manifest)).Scan(&count) != nil || count != 1 {
		return ErrCorruptRecord
	}
	if tx.QueryRow(`SELECT count(*) FROM action_leases WHERE action_id=? AND epoch=? AND terminal_state='SUCCEEDED' AND writer_fencing_token=? AND updated_at=?`, action.ID, int64(completion.ExecutionEpoch), completion.WriterFencingToken, completion.RecordedAt).Scan(&count) != nil || count != 1 {
		return ErrCorruptRecord
	}
	if tx.QueryRow(`SELECT count(*) FROM action_resource_leases WHERE action_id=?`, action.ID).Scan(&count) != nil || count != 0 {
		return ErrCorruptRecord
	}
	return nil
}

// This completes the task phase only. The run remains running until a separately
// verified synthesis produces its final output; no public done event is invented.
func (store *Control) CompleteAgentTasks(input CompleteAgentTasksRequest) (CompleteAgentTasksResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.ExpectedIndex < 1 || input.ExpectedIndex >= math.MaxInt64 || strings.TrimSpace(input.Owner) == "" || input.ClaimToken == "" {
		return CompleteAgentTasksResult{}, ErrAgentNodeOutcomeRequired
	}
	encoded, _ := json.Marshal(input)
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	defer tx.Rollback()
	authority, err := readCutoverTx(tx, "action")
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if !IsDomainGoAuthoritative(authority.State) {
		return CompleteAgentTasksResult{}, ErrCutoverNotAuthorized
	}
	action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil || !found {
		return CompleteAgentTasksResult{}, ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.Phase != "tasks" || binding.RunID != input.RunID || action.ExecutionEpoch != input.Epoch {
		return CompleteAgentTasksResult{}, ErrAgentRunExecutionRequired
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if err := store.validateAgentTasksCompletionTx(tx, action); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if applied {
		completion, present, err := readAgentTasksCompletion(action)
		if err != nil || !present || completion.MetadataIndex != input.ExpectedIndex || replay.Domain != "action" || replay.RecordID != action.ID || replay.Revision != action.Revision {
			return CompleteAgentTasksResult{}, ErrCorruptRecord
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return CompleteAgentTasksResult{}, err
		}
		return CompleteAgentTasksResult{AlreadyApplied: true, Run: run, Action: action}, nil
	}
	if !agentOutcomeRecordState(action.State) || run.NextIndex != input.ExpectedIndex || executionPlanDigest(run) != binding.PlanDigest || !completedAgentTasks(run) || action.Revision >= math.MaxInt64-1 {
		return CompleteAgentTasksResult{}, ErrRevisionConflict
	}
	markers, err := store.validatedAgentTaskSettlementsTx(tx, action)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if len(markers) != len(run.Plan) {
		return CompleteAgentTasksResult{}, ErrAgentNodeOutcomeRequired
	}
	for _, node := range run.Plan {
		if _, present := markers[node.ID]; !present {
			return CompleteAgentTasksResult{}, ErrAgentNodeOutcomeRequired
		}
	}
	lease, err := store.agentNodeLeaseTx(tx, AgentNodeDispatchRequest{ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, Owner: input.Owner, ClaimToken: input.ClaimToken}, context.now)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	completion := AgentTasksCompletion{SchemaVersion: 1, SettlementsSHA256: taskSettlementsDigest(markers), MetadataIndex: run.NextIndex, MetadataEpoch: binding.MetadataEpoch,
		ActionRevision: action.Revision + 1, ExecutionEpoch: input.Epoch, WriterFencingToken: store.token, RecordedAt: context.now}
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return CompleteAgentTasksResult{}, ErrCorruptRecord
	}
	fields[agentTasksCompletionField], err = json.Marshal(completion)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	action.Payload, err = json.Marshal(fields)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	action.Revision++
	action.State = "SUCCEEDED"
	write, err := prepareControlRecordWrite(action, nil)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	write.allowBoundLease, write.agentNodeDispatch, write.agentTasksCompletion = true, true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	action = write.record
	retained, found, err := readAgentExecutionActionTx(tx, action.ID)
	if err != nil || !found || retained.Revision != action.Revision || !bytes.Equal(retained.Payload, action.Payload) {
		return CompleteAgentTasksResult{}, ErrCorruptRecord
	}
	result, err := tx.Exec(`UPDATE action_leases SET terminal_state='SUCCEEDED',updated_at=?,writer_fencing_token=? WHERE action_id=? AND epoch=? AND claim_token=? AND terminal_state IS NULL`, context.now, store.token, action.ID, int64(input.Epoch), input.ClaimToken)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		return CompleteAgentTasksResult{}, ErrActionLeaseStale
	}
	if _, err := tx.Exec(`DELETE FROM action_resource_leases WHERE action_id=?`, action.ID); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO action_lease_events(action_id,event_type,owner,epoch,claim_token,lease_until,claim_revision,writer_fencing_token,recorded_at,resource_keys_json) VALUES(?,'TERMINATED',?,?,?,?,?,?,?,?)`, action.ID, lease.Owner, int64(lease.Epoch), lease.ClaimToken, lease.LeaseUntil, action.Revision, store.token, context.now, manifest); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	if err := store.validateAgentTasksCompletionTx(tx, action); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(action.Payload)
	if err != nil {
		return CompleteAgentTasksResult{}, err
	}
	receipt := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "action", RecordID: action.ID, Revision: action.Revision, State: action.State,
		ActionID: action.ID, ExecutionEpoch: input.Epoch, CutoverRevision: authority.Revision, FencingToken: authority.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, receipt, store.token, context.now); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return CompleteAgentTasksResult{}, ErrWriterFenceHeld
	}
	if commitNow >= lease.LeaseUntil {
		return CompleteAgentTasksResult{}, ErrActionLeaseExpired
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return CompleteAgentTasksResult{}, err
	}
	return CompleteAgentTasksResult{Run: run, Action: action}, nil
}
