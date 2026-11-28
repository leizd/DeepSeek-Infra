package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
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

func equalStringSlice(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func sameSuccessorDispatchIntent(left, right AgentNodeDispatch) bool {
	return left.SchemaVersion == right.SchemaVersion &&
		left.OperationID == right.OperationID &&
		left.ActionID == right.ActionID &&
		left.ExecutionEpoch == right.ExecutionEpoch &&
		left.RunID == right.RunID &&
		left.NodeID == right.NodeID &&
		left.Phase == right.Phase &&
		left.Task == right.Task &&
		equalStringSlice(left.DependsOn, right.DependsOn) &&
		left.Request == right.Request &&
		left.PlanDigest == right.PlanDigest &&
		left.MetadataIndex == right.MetadataIndex &&
		left.MetadataEpoch == right.MetadataEpoch &&
		left.RecordedAt == right.RecordedAt &&
		left.SuccessorOperationID == right.SuccessorOperationID &&
		left.ResetIndex == right.ResetIndex
}

func (store *Control) resolveAgentOutcomeTargetTx(
	tx *sql.Tx,
	record Record,
	intent AgentNodeDispatch,
	receipt *agentv1.AgentNodeOutcomeReceipt,
) (AgentNodeDispatch, int64, error) {
	if receipt == nil || receipt.Evidence == nil || receipt.Evidence.Fence == nil {
		return AgentNodeDispatch{}, 0, agentproof.ErrAgentNodeOutcomeInvalid
	}
	evidence := receipt.Evidence
	if evidence.OperationId == intent.OperationID {
		return intent, intent.RecordedAt, nil
	}
	if intent.SuccessorOperationID == "" || evidence.OperationId != intent.SuccessorOperationID {
		return AgentNodeDispatch{}, 0, agentproof.ErrAgentNodeOutcomeInvalid
	}
	if evidence.Fence.ActionId != intent.ActionID {
		return AgentNodeDispatch{}, 0, agentproof.ErrAgentNodeOutcomeInvalid
	}
	succEpoch := evidence.Fence.ExecutionEpoch
	if succEpoch <= intent.ExecutionEpoch || succEpoch > math.MaxInt64 || succEpoch > record.ExecutionEpoch {
		return AgentNodeDispatch{}, 0, agentproof.ErrAgentNodeOutcomeInvalid
	}
	if tx == nil {
		return AgentNodeDispatch{}, 0, ErrWriterFenceHeld
	}
	var (
		mutDomain        string
		mutRecordID      string
		mutRevision      int64
		mutState         string
		mutPayloadDigest string
		mutRequestDigest string
		mutActionID      string
		mutEpoch         uint64
		mutWriterToken   int64
		mutActor         string
		mutRecordedAt    int64
	)
	err := tx.QueryRow(
		`SELECT domain, record_id, record_revision, record_state, payload_digest,
			request_digest, action_id, execution_epoch, writer_fencing_token, actor, recorded_at
		 FROM `+operatorMutationTable+` WHERE operation_id = ?`,
		intent.SuccessorOperationID,
	).Scan(
		&mutDomain, &mutRecordID, &mutRevision, &mutState, &mutPayloadDigest,
		&mutRequestDigest, &mutActionID, &mutEpoch, &mutWriterToken, &mutActor, &mutRecordedAt,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return AgentNodeDispatch{}, 0, ErrAgentNodeDispatchRequired
	}
	if err != nil {
		return AgentNodeDispatch{}, 0, err
	}
	if mutDomain != "action" || mutRecordID != intent.ActionID || mutActionID != intent.ActionID ||
		mutState != "EXECUTING" || mutRevision < 1 || mutEpoch != succEpoch ||
		mutWriterToken <= 0 || mutRecordedAt < 0 {
		return AgentNodeDispatch{}, 0, ErrAgentNodeDispatchRequired
	}
	dispatchRec, dispatchMeta, found, err := scanControlRecord(tx.QueryRow(
		`SELECT record_id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, recorded_at
		 FROM control_events WHERE domain='action' AND record_id=? AND revision=?`,
		intent.ActionID, mutRevision,
	), "action")
	if err != nil {
		return AgentNodeDispatch{}, 0, err
	}
	if !found || dispatchRec.ExecutionEpoch != succEpoch || dispatchRec.State != "EXECUTING" ||
		dispatchRec.Digest != mutPayloadDigest || dispatchMeta.writerToken <= 0 ||
		dispatchMeta.writerToken > mutWriterToken || dispatchMeta.timestamp < 0 ||
		dispatchMeta.timestamp > mutRecordedAt || !sameAgentExecutionIntent(dispatchRec, record) {
		return AgentNodeDispatch{}, 0, ErrCorruptRecord
	}
	dispatchNodes, err := readAgentNodeDispatches(dispatchRec)
	if err != nil {
		return AgentNodeDispatch{}, 0, ErrCorruptRecord
	}
	dispatched, present := dispatchNodes[intent.NodeID]
	if !present || !sameSuccessorDispatchIntent(dispatched, intent) {
		return AgentNodeDispatch{}, 0, ErrCorruptRecord
	}
	var count int
	if err := tx.QueryRow(
		`SELECT COUNT(*) FROM action_lease_events
		 WHERE action_id=? AND epoch=? AND event_type IN ('ADMITTED','TAKEOVER')`,
		intent.ActionID, int64(succEpoch),
	).Scan(&count); err != nil {
		return AgentNodeDispatch{}, 0, err
	}
	if count != 1 {
		return AgentNodeDispatch{}, 0, ErrActionLeaseStale
	}
	var owner, claimToken string
	err = tx.QueryRow(
		`SELECT owner, claim_token FROM action_lease_events
		 WHERE action_id=? AND epoch=? AND event_type IN ('ADMITTED','TAKEOVER')`,
		intent.ActionID, int64(succEpoch),
	).Scan(&owner, &claimToken)
	if errors.Is(err, sql.ErrNoRows) {
		return AgentNodeDispatch{}, 0, ErrActionLeaseStale
	}
	if err != nil {
		return AgentNodeDispatch{}, 0, err
	}
	expectedReq, _ := json.Marshal(AgentNodeDispatchRequest{
		OperationID: intent.SuccessorOperationID,
		Actor:       mutActor,
		ActionID:    intent.ActionID,
		Epoch:       succEpoch,
		RunID:       intent.RunID,
		NodeID:      intent.NodeID,
		Owner:       owner,
		ClaimToken:  claimToken,
	})
	expectedDigest, err := operatorPayloadDigest(expectedReq)
	if err != nil || mutRequestDigest != expectedDigest {
		return AgentNodeDispatch{}, 0, ErrAgentNodeDispatchRequired
	}
	effective := intent
	effective.OperationID = intent.SuccessorOperationID
	effective.ExecutionEpoch = succEpoch
	return effective, mutRecordedAt, nil
}

func (store *Control) readAgentOutcomeAdmissionForReceiptTx(
	tx *sql.Tx,
	record Record,
	intent AgentNodeDispatch,
	currentManifest string,
	receipt *agentv1.AgentNodeOutcomeReceipt,
) (AgentNodeDispatch, ActionLease, error) {
	effective, admissionAt, err := store.resolveAgentOutcomeTargetTx(tx, record, intent, receipt)
	if err != nil {
		return AgentNodeDispatch{}, ActionLease{}, err
	}
	lease, err := readAgentOutcomeAdmissionAtTx(tx, effective, currentManifest, admissionAt)
	if err != nil {
		return AgentNodeDispatch{}, ActionLease{}, err
	}
	return effective, lease, nil
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

func (store *Control) verifyAgentNodeOutcomesTx(
	tx *sql.Tx,
	record Record,
	nodes map[string]AgentNodeDispatch,
	values map[string]storedAgentNodeOutcome,
) (map[string]storedAgentNodeOutcome, error) {
	for id, stored := range values {
		encoded := stored.Receipt
		intent, present := nodes[id]
		if !present || len(encoded) == 0 || len(encoded) > maximumPayloadBytes || stored.WriterFencingToken <= 0 {
			return nil, ErrCorruptRecord
		}
		var receipt agentv1.AgentNodeOutcomeReceipt
		if proto.Unmarshal(encoded, &receipt) != nil {
			return nil, ErrCorruptRecord
		}
		effective, _, err := store.resolveAgentOutcomeTargetTx(tx, record, intent, &receipt)
		if err != nil || store.verifyAgentOutcome(&receipt, effective, ActionLease{
			ActionID:           effective.ActionID,
			Epoch:              effective.ExecutionEpoch,
			WriterFencingToken: stored.WriterFencingToken,
		}) != nil {
			return nil, ErrCorruptRecord
		}
		canonical, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
		if err != nil || !bytes.Equal(canonical, encoded) {
			return nil, ErrCorruptRecord
		}
	}
	return values, nil
}

func (store *Control) agentNodeOutcomesTx(tx *sql.Tx, record Record, nodes map[string]AgentNodeDispatch) (map[string]storedAgentNodeOutcome, error) {
	values, err := agentNodeOutcomeBytes(record)
	if err != nil || len(values) == 0 {
		return values, err
	}
	return store.verifyAgentNodeOutcomesTx(tx, record, nodes, values)
}

func (store *Control) agentNodeOutcomes(record Record, nodes map[string]AgentNodeDispatch) (map[string]storedAgentNodeOutcome, error) {
	values, err := agentNodeOutcomeBytes(record)
	if err != nil || len(values) == 0 {
		return values, err
	}
	var tx *sql.Tx
	for id, stored := range values {
		intent, present := nodes[id]
		var receipt agentv1.AgentNodeOutcomeReceipt
		if present && len(stored.Receipt) > 0 && len(stored.Receipt) <= maximumPayloadBytes &&
			proto.Unmarshal(stored.Receipt, &receipt) == nil && receipt.Evidence != nil &&
			receipt.Evidence.OperationId != intent.OperationID && store.db != nil && !store.closed {
			tx, err = store.db.Begin()
			if err != nil {
				return nil, err
			}
			defer tx.Rollback()
			break
		}
	}
	return store.verifyAgentNodeOutcomesTx(tx, record, nodes, values)
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
	effective, original, err := store.readAgentOutcomeAdmissionForReceiptTx(tx, record, intent, manifest, receipt)
	if err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	if err := store.verifyAgentOutcome(receipt, effective, original); err != nil {
		return AgentNodeOutcomeResult{}, err
	}
	values, err := store.agentNodeOutcomesTx(tx, record, nodes)
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
	values, err := store.agentNodeOutcomesTx(tx, record, nodes)
	if err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	if err := store.validateAgentFailureSettlementTx(tx, record); err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	if err := store.validateAgentSynthesisSettlementTx(tx, record); err != nil {
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
	if err := store.validateAgentCancellationSettlementTx(tx, record); err != nil {
		return record, AgentNodeDispatch{}, nil, false, err
	}
	intent, found := nodes[nodeID]
	var receipt *agentv1.AgentNodeOutcomeReceipt
	if stored, present := values[nodeID]; present {
		receipt = new(agentv1.AgentNodeOutcomeReceipt)
		if proto.Unmarshal(stored.Receipt, receipt) != nil {
			return record, AgentNodeDispatch{}, nil, false, ErrCorruptRecord
		}
		effective, _, err := store.resolveAgentOutcomeTargetTx(tx, record, intent, receipt)
		if err != nil {
			return record, AgentNodeDispatch{}, nil, false, err
		}
		intent = effective
	}
	return record, intent, receipt, found, nil
}
