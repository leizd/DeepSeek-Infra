package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

var ErrAgentNodeOutcomeRequired = errors.New("AGENT_NODE_OUTCOME_REQUIRED")
var ErrAgentNodeOutcomeConflict = errors.New("AGENT_NODE_OUTCOME_CONFLICT")

const agentNodeOutcomeField = "nativeAgentNodeOutcomes"

// Original writer is captured only after verification against the admitted lease.
// This immutable control-history witness survives later lease settlement.
type storedAgentNodeOutcome struct {
	WriterFencingToken int64  `json:"fencingToken"`
	Receipt            []byte `json:"receipt"`
}

type AgentNodeOutcomeRequest struct {
	OperationID string
	Actor       string
	ActionID    string
	Epoch       uint64
	RunID       string
	NodeID      string
	Receipt     *agentv1.AgentNodeOutcomeReceipt
}

type AgentNodeOutcomeResult struct {
	AlreadyRecorded bool
	ReceiptSHA256   string
	Mutation        OperatorMutationResult
}

func nativeAgentNodeOutcomes(payload json.RawMessage) bool {
	var fields map[string]json.RawMessage
	if json.Unmarshal(payload, &fields) != nil {
		return false
	}
	_, found := fields[agentNodeOutcomeField]
	return found
}

func agentNodeOutcomeBytes(record Record) (map[string]storedAgentNodeOutcome, error) {
	var fields map[string]json.RawMessage
	if json.Unmarshal(record.Payload, &fields) != nil {
		return nil, ErrCorruptRecord
	}
	values := map[string]storedAgentNodeOutcome{}
	if raw, present := fields[agentNodeOutcomeField]; present {
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&values) != nil || values == nil || len(values) > 5 {
			return nil, ErrCorruptRecord
		}
	}
	return values, nil
}

func sameAgentNodeOutcomes(before, after Record) bool {
	if nativeAgentNodeOutcomes(before.Payload) != nativeAgentNodeOutcomes(after.Payload) {
		return false
	}
	first, err := agentNodeOutcomeBytes(before)
	second, otherErr := agentNodeOutcomeBytes(after)
	if err != nil || otherErr != nil {
		return false
	}
	firstBytes, err := json.Marshal(first)
	secondBytes, otherErr := json.Marshal(second)
	return err == nil && otherErr == nil && bytes.Equal(firstBytes, secondBytes)
}

func agentOutcomeIntent(intent AgentNodeDispatch) *agentv1.AgentNodeDispatchIntent {
	phase := agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
	if intent.Phase == "plan" {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
	} else if intent.Phase == "synthesis" {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
	}
	return &agentv1.AgentNodeDispatchIntent{SchemaVersion: intent.SchemaVersion, OperationId: intent.OperationID,
		Fence: &commonv1.ActionFence{ActionId: intent.ActionID, ExecutionEpoch: intent.ExecutionEpoch},
		RunId: intent.RunID, NodeId: intent.NodeID, Phase: phase, Task: intent.Task,
		DependsOn:  append([]string(nil), intent.DependsOn...),
		Request:    &agentv1.AgentArtifactReference{Sha256: intent.Request.SHA256, Length: intent.Request.Length},
		PlanDigest: intent.PlanDigest, MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch,
		State: intent.State, RecordedAt: intent.RecordedAt}
}

func (store *Control) verifyAgentOutcome(receipt *agentv1.AgentNodeOutcomeReceipt, intent AgentNodeDispatch, lease ActionLease) error {
	if lease.Epoch != intent.ExecutionEpoch || lease.ActionID != intent.ActionID {
		return ErrActionLeaseStale
	}
	verified, err := agentproof.VerifyAgentNodeOutcome(receipt, agentOutcomeIntent(intent), lease.WriterFencingToken,
		agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment})
	if err != nil {
		return err
	}
	// The durable protobuf contains only typed, verified public evidence. Check
	// scheduling text before encoding it so private material cannot hide inside
	// an otherwise valid receipt's base64 representation.
	for _, candidate := range verified.Evidence().ProviderPlan {
		values := append([]string{candidate.Id, candidate.Task}, candidate.DependsOn...)
		for _, value := range values {
			if err := rejectControlSecretMaterial(value, 0); err != nil {
				return err
			}
		}
	}
	return nil
}

func (store *Control) agentNodeOutcomes(record Record, nodes map[string]AgentNodeDispatch) (map[string]storedAgentNodeOutcome, error) {
	values, err := agentNodeOutcomeBytes(record)
	if err != nil || len(values) == 0 {
		return values, err
	}
	for id, stored := range values {
		encoded := stored.Receipt
		intent, present := nodes[id]
		if !present || len(encoded) == 0 || len(encoded) > maximumPayloadBytes || stored.WriterFencingToken <= 0 {
			return nil, ErrCorruptRecord
		}
		var receipt agentv1.AgentNodeOutcomeReceipt
		if proto.Unmarshal(encoded, &receipt) != nil || store.verifyAgentOutcome(&receipt, intent, ActionLease{ActionID: intent.ActionID, Epoch: intent.ExecutionEpoch, WriterFencingToken: stored.WriterFencingToken}) != nil {
			return nil, ErrCorruptRecord
		}
		canonical, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
		if err != nil || !bytes.Equal(canonical, encoded) {
			return nil, ErrCorruptRecord
		}
	}
	return values, nil
}

// RecordAgentNodeOutcome consumes independent Rust evidence under Go's current
// authority. An expired dispatch lease can retain a historical result, but this
// operation neither renews that lease nor settles the phase or releases locks.
func (store *Control) RecordAgentNodeOutcome(input AgentNodeOutcomeRequest) (AgentNodeOutcomeResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.NodeID == "" || len(input.NodeID) > 64 || input.Receipt == nil {
		return AgentNodeOutcomeResult{}, ErrAgentNodeOutcomeRequired
	}
	receipt := proto.Clone(input.Receipt).(*agentv1.AgentNodeOutcomeReceipt)
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt)
	if err != nil || len(encoded) == 0 || len(encoded) > maximumPayloadBytes {
		return AgentNodeOutcomeResult{}, ErrAgentNodeOutcomeRequired
	}
	digest := sha256.Sum256(encoded)
	receiptDigest := hex.EncodeToString(digest[:])
	requestBytes, _ := json.Marshal(struct {
		OperationID, Actor, ActionID, RunID, NodeID, ReceiptSHA256 string
		Epoch                                                      uint64
	}{input.OperationID, input.Actor, input.ActionID, input.RunID, input.NodeID, receiptDigest, input.Epoch})
	requestDigest, err := operatorPayloadDigest(requestBytes)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	defer tx.Rollback()
	cutover, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if !IsDomainGoAuthoritative(cutover.State) {
		return AgentNodeOutcomeResult{}, ErrCutoverNotAuthorized
	}
	context.cutover = cutover
	record, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if !found || record.ExecutionEpoch != input.Epoch || !agentOutcomeRecordState(record.State) {
		return AgentNodeOutcomeResult{}, ErrActionNotClaimable
	}
	binding, err := decodeAgentExecution(record)
	if err != nil || binding.RunID != input.RunID {
		return AgentNodeOutcomeResult{}, ErrAgentRunExecutionRequired
	}
	if _, err := store.agentExecutionRunTx(tx, binding); err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	intent, exists := nodes[input.NodeID]
	if !exists || intent.ExecutionEpoch == 0 || intent.ExecutionEpoch > input.Epoch {
		return AgentNodeOutcomeResult{}, ErrAgentNodeDispatchRequired
	}
	lease, err := readActiveActionLeaseTx(tx, record.ID)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if lease.Epoch != input.Epoch {
		return AgentNodeOutcomeResult{}, ErrActionLeaseStale
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	original, err := readAgentOutcomeAdmissionTx(tx, intent, manifest)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if err := store.verifyAgentOutcome(receipt, intent, original); err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	values, err := store.agentNodeOutcomes(record, nodes)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	previous, recorded := values[input.NodeID]
	if recorded && !bytes.Equal(previous.Receipt, encoded) {
		return AgentNodeOutcomeResult{}, ErrAgentNodeOutcomeConflict
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if applied && (!recorded || replay.Domain != "action" || replay.RecordID != record.ID) {
		return AgentNodeOutcomeResult{}, ErrCorruptRecord
	}
	if !recorded {
		if record.Revision >= math.MaxInt64-1 {
			return AgentNodeOutcomeResult{}, ErrRevisionConflict
		}
		values[input.NodeID] = storedAgentNodeOutcome{WriterFencingToken: original.WriterFencingToken, Receipt: encoded}
		var fields map[string]json.RawMessage
		if json.Unmarshal(record.Payload, &fields) != nil {
			return AgentNodeOutcomeResult{}, ErrCorruptRecord
		}
		fields[agentNodeOutcomeField], _ = json.Marshal(values)
		record.Payload, _ = json.Marshal(fields)
		record.Revision++
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			return AgentNodeOutcomeResult{}, err
		}
		write.allowBoundLease, write.agentNodeOutcome = true, true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentNodeOutcomeResult{}, err
		}
		record = write.record
	}
	if !applied {
		payloadDigest, err := operatorPayloadDigest(record.Payload)
		if err != nil {
			return AgentNodeOutcomeResult{}, err
		}
		replay = OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID,
			Domain: "action", RecordID: record.ID, Revision: record.Revision, State: record.State,
			ActionID: record.ID, ExecutionEpoch: record.ExecutionEpoch, CutoverRevision: context.cutover.Revision,
			FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
		if err := insertOperatorMutationTx(tx, replay, store.token, context.now); err != nil {
			return AgentNodeOutcomeResult{}, err
		}
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return AgentNodeOutcomeResult{}, ErrWriterFenceHeld
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	return AgentNodeOutcomeResult{AlreadyRecorded: recorded, ReceiptSHA256: receiptDigest, Mutation: replay}, nil
}

// The intent and proof are read from one authenticated control-history snapshot.
func (store *Control) AgentNodeOutcomeSnapshot(actionID, nodeID string) (Record, AgentNodeDispatch, *agentv1.AgentNodeOutcomeReceipt, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return Record{}, AgentNodeDispatch{}, nil, false, ErrWriterFenceHeld
	}
	tx, err := store.db.Begin()
	if err != nil {
		return Record{}, AgentNodeDispatch{}, nil, false, err
	}
	defer tx.Rollback()
	record, found, err := readAgentExecutionActionTx(tx, actionID)
	if err != nil || !found {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	values, err := store.agentNodeOutcomes(record, nodes)
	if err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	if err := store.validateAgentPlanSettlementTx(tx, record); err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	if _, err := store.validatedAgentTaskSettlementsTx(tx, record); err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	if err := store.validateAgentTasksCompletionTx(tx, record); err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	intent, found := nodes[nodeID]
	var receipt *agentv1.AgentNodeOutcomeReceipt
	if stored, present := values[nodeID]; present {
		receipt = new(agentv1.AgentNodeOutcomeReceipt)
		if proto.Unmarshal(stored.Receipt, receipt) != nil {
			return record, AgentNodeDispatch{}, nil, false, ErrCorruptRecord
		}
	}
	return record, intent, receipt, found, nil
}
