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

const agentCancellationSettlementField = "nativeAgentCancellationSettlement"

type AgentCancellationSettlement struct {
	SchemaVersion      uint32 `json:"schemaVersion"`
	OutcomesSHA256     string `json:"outcomesSha256"`
	MetadataIndex      int64  `json:"metadataIndex"`
	MetadataEpoch      uint64 `json:"metadataEpoch"`
	ActionRevision     int64  `json:"actionRevision"`
	ExecutionEpoch     uint64 `json:"executionEpoch"`
	WriterFencingToken int64  `json:"fencingToken"`
	RecordedAt         int64  `json:"recordedAt"`
}

type SettleAgentCancellationRequest struct {
	OperationID, Actor, ActionID, RunID, Owner, ClaimToken string
	Epoch                                                  uint64
	ExpectedIndex                                          int64
}

type SettleAgentCancellationResult struct {
	AlreadyApplied bool
	Run            agent.RunMetadata
	Action         Record
}

// Cancellation settles the phase's retained effects, never a successful task or run.
func cancelledAgentRun(run agent.RunMetadata) bool {
	return run.Cancelled && run.Status == "cancelled"
}

func cancelledNodeOutcomesDigest(outcomes map[string]storedAgentNodeOutcome) string {
	encoded, _ := json.Marshal(outcomes)
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:])
}

func (store *Control) cancelledAgentNodeOutcomes(action Record) (map[string]storedAgentNodeOutcome, error) {
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return nil, err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return nil, err
	}
	// A missing custody result cannot prove that a committed dispatch had no effect.
	if len(outcomes) != len(nodes) {
		return nil, ErrAgentNodeOutcomeRequired
	}
	for id := range nodes {
		if _, present := outcomes[id]; !present {
			return nil, ErrAgentNodeOutcomeRequired
		}
	}
	return outcomes, nil
}

func readAgentCancellationSettlement(action Record) (AgentCancellationSettlement, bool, error) {
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return AgentCancellationSettlement{}, false, ErrCorruptRecord
	}
	raw, present := fields[agentCancellationSettlementField]
	if !present {
		return AgentCancellationSettlement{}, false, nil
	}
	var value AgentCancellationSettlement
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&value) != nil || value.SchemaVersion != 1 || action.State != "CANCELLED_CONFIRMED" ||
		value.ActionRevision != action.Revision || value.ExecutionEpoch != action.ExecutionEpoch ||
		value.MetadataIndex < 1 || value.MetadataIndex >= math.MaxInt64 || value.MetadataEpoch == 0 || value.MetadataEpoch > math.MaxInt64 ||
		value.WriterFencingToken <= 0 || value.RecordedAt < 0 || len(value.OutcomesSHA256) != 64 {
		return AgentCancellationSettlement{}, true, ErrCorruptRecord
	}
	if digest, err := hex.DecodeString(value.OutcomesSHA256); err != nil || len(digest) != sha256.Size || strings.ToLower(value.OutcomesSHA256) != value.OutcomesSHA256 {
		return AgentCancellationSettlement{}, true, ErrCorruptRecord
	}
	return value, true, nil
}

func nativeAgentCancellationSettlementEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != "action" || before.ID != after.ID || !agentOutcomeRecordState(before.State) ||
		after.State != "CANCELLED_CONFIRMED" || after.Revision != before.Revision+1 || after.ExecutionEpoch != before.ExecutionEpoch || !sameAgentExecutionIntent(before, after) {
		return false
	}
	var previous, current map[string]json.RawMessage
	if json.Unmarshal(before.Payload, &previous) != nil || json.Unmarshal(after.Payload, &current) != nil {
		return false
	}
	if _, present := previous[agentCancellationSettlementField]; present {
		return false
	}
	if _, present, err := readAgentCancellationSettlement(after); err != nil || !present {
		return false
	}
	delete(current, agentCancellationSettlementField)
	a, _ := json.Marshal(previous)
	b, _ := json.Marshal(current)
	return bytes.Equal(a, b)
}

// The terminal marker binds every admitted node outcome, the immutable cancelled
// checkpoint and the termination journal. It never projects successful work.
func (store *Control) validateAgentCancellationSettlementTx(tx *sql.Tx, action Record) error {
	completion, present, err := readAgentCancellationSettlement(action)
	if err != nil || !present {
		return err
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || completion.MetadataEpoch != binding.MetadataEpoch {
		return ErrCorruptRecord
	}
	outcomes, err := store.cancelledAgentNodeOutcomes(action)
	if err != nil || cancelledNodeOutcomesDigest(outcomes) != completion.OutcomesSHA256 {
		return ErrCorruptRecord
	}
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, completion.MetadataIndex), "agent_run")
	if err != nil || !found || record.ExecutionEpoch != completion.MetadataEpoch {
		return ErrCorruptRecord
	}
	run, err := decodeAgentMetadata(record)
	if err != nil || run.Request != binding.Request || executionPlanDigest(run) != binding.PlanDigest || !cancelledAgentRun(run) {
		return ErrCorruptRecord
	}
	retained, meta, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, completion.ActionRevision), "action")
	if err != nil || !found || !bytes.Equal(retained.Payload, action.Payload) || retained.State != "CANCELLED_CONFIRMED" ||
		meta.writerToken != completion.WriterFencingToken || meta.timestamp != completion.RecordedAt {
		return ErrCorruptRecord
	}
	manifest, _ := json.Marshal([]string{"agent-run:" + binding.RunID})
	var count int
	if tx.QueryRow(`SELECT count(*) FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED' AND epoch=? AND claim_revision=? AND writer_fencing_token=? AND recorded_at=? AND resource_keys_json=?`, action.ID, int64(completion.ExecutionEpoch), completion.ActionRevision, completion.WriterFencingToken, completion.RecordedAt, string(manifest)).Scan(&count) != nil || count != 1 {
		return ErrCorruptRecord
	}
	if tx.QueryRow(`SELECT count(*) FROM action_leases WHERE action_id=? AND epoch=? AND terminal_state='CANCELLED_CONFIRMED' AND writer_fencing_token=? AND updated_at=?`, action.ID, int64(completion.ExecutionEpoch), completion.WriterFencingToken, completion.RecordedAt).Scan(&count) != nil || count != 1 {
		return ErrCorruptRecord
	}
	if tx.QueryRow(`SELECT count(*) FROM action_resource_leases WHERE action_id=?`, action.ID).Scan(&count) != nil || count != 0 {
		return ErrCorruptRecord
	}
	return nil
}

// This terminates only a cancelled phase whose dispatched effects all have
// independently verified outcomes. The public cancelled cursor stays unchanged.
func (store *Control) SettleAgentCancellation(input SettleAgentCancellationRequest) (SettleAgentCancellationResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.ExpectedIndex < 1 || input.ExpectedIndex >= math.MaxInt64 || strings.TrimSpace(input.Owner) == "" || input.ClaimToken == "" {
		return SettleAgentCancellationResult{}, ErrAgentNodeOutcomeRequired
	}
	encoded, _ := json.Marshal(input)
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	defer tx.Rollback()
	authority, err := readCutoverTx(tx, "action")
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	if !IsDomainGoAuthoritative(authority.State) {
		return SettleAgentCancellationResult{}, ErrCutoverNotAuthorized
	}
	action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil || !found {
		return SettleAgentCancellationResult{}, ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.RunID != input.RunID || action.ExecutionEpoch != input.Epoch {
		return SettleAgentCancellationResult{}, ErrAgentRunExecutionRequired
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	if err := store.validateAgentCancellationSettlementTx(tx, action); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	if applied {
		completion, present, err := readAgentCancellationSettlement(action)
		if err != nil || !present || completion.MetadataIndex != input.ExpectedIndex || replay.Domain != "action" || replay.RecordID != action.ID || replay.Revision != action.Revision {
			return SettleAgentCancellationResult{}, ErrCorruptRecord
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return SettleAgentCancellationResult{}, err
		}
		return SettleAgentCancellationResult{AlreadyApplied: true, Run: run, Action: action}, nil
	}
	if !agentOutcomeRecordState(action.State) || run.NextIndex != input.ExpectedIndex || executionPlanDigest(run) != binding.PlanDigest || !cancelledAgentRun(run) || action.Revision >= math.MaxInt64-1 {
		return SettleAgentCancellationResult{}, ErrRevisionConflict
	}
	outcomes, err := store.cancelledAgentNodeOutcomes(action)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	lease, err := store.agentNodeLeaseTx(tx, AgentNodeDispatchRequest{ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, Owner: input.Owner, ClaimToken: input.ClaimToken}, context.now)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	completion := AgentCancellationSettlement{SchemaVersion: 1, OutcomesSHA256: cancelledNodeOutcomesDigest(outcomes), MetadataIndex: run.NextIndex, MetadataEpoch: binding.MetadataEpoch,
		ActionRevision: action.Revision + 1, ExecutionEpoch: input.Epoch, WriterFencingToken: store.token, RecordedAt: context.now}
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return SettleAgentCancellationResult{}, ErrCorruptRecord
	}
	fields[agentCancellationSettlementField], err = json.Marshal(completion)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	action.Payload, err = json.Marshal(fields)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	action.Revision++
	action.State = "CANCELLED_CONFIRMED"
	write, err := prepareControlRecordWrite(action, nil)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	write.allowBoundLease, write.agentNodeDispatch, write.agentCancellationSettlement = true, true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	action = write.record
	result, err := tx.Exec(`UPDATE action_leases SET terminal_state='CANCELLED_CONFIRMED',updated_at=?,writer_fencing_token=? WHERE action_id=? AND epoch=? AND claim_token=? AND terminal_state IS NULL`, context.now, store.token, action.ID, int64(input.Epoch), input.ClaimToken)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		return SettleAgentCancellationResult{}, ErrActionLeaseStale
	}
	if _, err := tx.Exec(`DELETE FROM action_resource_leases WHERE action_id=?`, action.ID); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO action_lease_events(action_id,event_type,owner,epoch,claim_token,lease_until,claim_revision,writer_fencing_token,recorded_at,resource_keys_json) VALUES(?,'TERMINATED',?,?,?,?,?,?,?,?)`, action.ID, lease.Owner, int64(lease.Epoch), lease.ClaimToken, lease.LeaseUntil, action.Revision, store.token, context.now, manifest); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	if err := store.validateAgentCancellationSettlementTx(tx, action); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(action.Payload)
	if err != nil {
		return SettleAgentCancellationResult{}, err
	}
	receipt := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "action", RecordID: action.ID, Revision: action.Revision, State: action.State,
		ActionID: action.ID, ExecutionEpoch: input.Epoch, CutoverRevision: authority.Revision, FencingToken: authority.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, receipt, store.token, context.now); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return SettleAgentCancellationResult{}, ErrWriterFenceHeld
	}
	if commitNow >= lease.LeaseUntil {
		return SettleAgentCancellationResult{}, ErrActionLeaseExpired
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return SettleAgentCancellationResult{}, err
	}
	return SettleAgentCancellationResult{Run: run, Action: action}, nil
}
