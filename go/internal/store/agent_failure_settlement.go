package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

const agentFailureSettlementField = "nativeAgentFailureSettlement"

type AgentFailureSettlementRequest struct {
	OperationID, Actor, ActionID, RunID, Owner, ClaimToken string
	Epoch                                                  uint64
	Projection                                             *agentv1.AgentFailureProjectionReceipt
}
type AgentFailureSettlementResult struct {
	AlreadyApplied bool
	Run            agent.RunMetadata
	Action         Record
}

// Re-check retained projection signatures against immutable source metadata on
// reads and replay, including after the original writer has been replaced.
func (store *Control) validateAgentFailureSettlementTx(tx *sql.Tx, action Record) error {
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return fmt.Errorf("%w: agent failure history payload", ErrCorruptRecord)
	}
	raw, present := fields[agentFailureSettlementField]
	if !present {
		return nil
	}
	if action.State != "FAILED" {
		return fmt.Errorf("%w: agent failure history action-state", ErrCorruptRecord)
	}
	var encoded []byte
	var receipt agentv1.AgentFailureProjectionReceipt
	if json.Unmarshal(raw, &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil || receipt.Evidence == nil {
		return fmt.Errorf("%w: agent failure history receipt-decode", ErrCorruptRecord)
	}
	canonical, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
	if err != nil || !bytes.Equal(canonical, encoded) {
		return fmt.Errorf("%w: agent failure history receipt-canonical", ErrCorruptRecord)
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || (binding.Phase != "plan" && binding.Phase != "synthesis") {
		return fmt.Errorf("%w: agent failure history execution-binding", ErrCorruptRecord)
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return err
	}
	sourceNode := agentFailureNode(binding.Phase)
	source, present := outcomes[sourceNode]
	if !present {
		return fmt.Errorf("%w: agent failure history source-absent", ErrCorruptRecord)
	}
	var sourceReceipt agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(source.Receipt, &sourceReceipt) != nil || sourceReceipt.Evidence == nil {
		return fmt.Errorf("%w: agent failure history source-receipt", ErrCorruptRecord)
	}
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, binding.MetadataIndex), "agent_run")
	if err != nil || !found {
		return fmt.Errorf("%w: agent failure history original-checkpoint", ErrCorruptRecord)
	}
	original, err := decodeAgentMetadata(record)
	if err != nil || original.Request != binding.Request || executionPlanDigest(original) != binding.PlanDigest || !executionPhaseReady(original, binding.Phase) {
		return fmt.Errorf("%w: agent failure history original-run", ErrCorruptRecord)
	}
	var writer, settlementAt int64
	if tx.QueryRow(`SELECT writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, action.Revision).Scan(&writer, &settlementAt) != nil {
		return fmt.Errorf("%w: agent failure history settlement-journal", ErrCorruptRecord)
	}
	if sourceReceipt.Evidence.NodeId != sourceNode || !agentproof.ValidTerminalFailure(sourceReceipt.Evidence) {
		return fmt.Errorf("%w: agent failure history source-kind", ErrCorruptRecord)
	}
	sourceManifest, _ := json.Marshal([]string{"agent-run:" + binding.RunID})
	admission, err := readAgentOutcomeAdmissionTx(tx, nodes[sourceNode], string(sourceManifest))
	if err != nil || admission.WriterFencingToken != source.WriterFencingToken {
		return fmt.Errorf("%w: agent failure history source-admission", ErrCorruptRecord)
	}
	if err := store.validateAgentFailureSourceTx(tx, binding); err != nil {
		return err
	}
	sourceHash := sha256.Sum256(source.Receipt)
	expected := &agentv1.AgentFailureProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(sourceHash[:]),
		SettlementFence: &commonv1.ActionFence{ActionId: action.ID, ExecutionEpoch: action.ExecutionEpoch}, SettlementWriterToken: writer,
		RunId: binding.RunID, MetadataEpoch: record.ExecutionEpoch, ExpectedIndex: binding.MetadataIndex, Phase: sourceReceipt.Evidence.Phase, ErrorBody: sourceReceipt.Evidence.PublicEvent,
		FleetId: store.fleetID, Environment: store.environment}
	if agentproof.VerifyAgentFailureProjection(&receipt, expected, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment}) != nil {
		return fmt.Errorf("%w: agent failure history projection-signature", ErrCorruptRecord)
	}
	expectedRun := original
	checkpoint, checkpointMeta, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, binding.MetadataIndex+1), "agent_run")
	if err != nil || !found || checkpointMeta.writerToken != writer || checkpointMeta.timestamp != settlementAt {
		return fmt.Errorf("%w: agent failure history failure-checkpoint", ErrCorruptRecord)
	}
	failedRun, err := decodeAgentMetadata(checkpoint)
	if err != nil || checkpoint.ExecutionEpoch != record.ExecutionEpoch || failedRun.Request != binding.Request || executionPlanDigest(failedRun) != binding.PlanDigest {
		return fmt.Errorf("%w: agent failure history failure-run", ErrCorruptRecord)
	}
	body := receipt.Evidence.ErrorBody
	event := agent.RunEvent{ActionID: binding.RunID, ExecutionEpoch: record.ExecutionEpoch, RunID: binding.RunID,
		Index: binding.MetadataIndex, CreatedAt: time.Unix(settlementAt, 0).UTC().Format(time.RFC3339), Type: "error", Phase: sourceNode,
		Body: agent.ArtifactRef{SHA256: body.Sha256, Length: body.Length}}
	expectedRun.ApplyRunEvent(event)
	if !reflect.DeepEqual(failedRun, expectedRun) || failedRun.Status != "failed" {
		return fmt.Errorf("%w: agent failure history failure-reducer", ErrCorruptRecord)
	}
	retained, meta, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, action.Revision), "action")
	if err != nil || !found || !bytes.Equal(retained.Payload, action.Payload) || retained.State != "FAILED" || meta.writerToken != writer || meta.timestamp != settlementAt {
		return fmt.Errorf("%w: agent failure history action-checkpoint", ErrCorruptRecord)
	}
	manifest, _ := json.Marshal([]string{"agent-run:" + binding.RunID})
	var count int
	if tx.QueryRow(`SELECT count(*) FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED' AND epoch=? AND claim_revision=? AND writer_fencing_token=? AND recorded_at=? AND resource_keys_json=?`, action.ID, int64(action.ExecutionEpoch), action.Revision, writer, meta.timestamp, string(manifest)).Scan(&count) != nil || count != 1 {
		return fmt.Errorf("%w: agent failure history termination-event", ErrCorruptRecord)
	}
	if tx.QueryRow(`SELECT count(*) FROM action_leases WHERE action_id=? AND epoch=? AND terminal_state='FAILED_CONFIRMED' AND writer_fencing_token=? AND updated_at=?`, action.ID, int64(action.ExecutionEpoch), writer, meta.timestamp).Scan(&count) != nil || count != 1 {
		return fmt.Errorf("%w: agent failure history lease-terminal", ErrCorruptRecord)
	}
	if tx.QueryRow(`SELECT count(*) FROM action_resource_leases WHERE action_id=?`, action.ID).Scan(&count) != nil || count != 0 {
		return fmt.Errorf("%w: agent failure history released-resources", ErrCorruptRecord)
	}
	return nil
}

// The only extra terminal edge is an immutable synthesis settlement. Live writes
// additionally require the private settlement path and independent verification.
func nativeAgentFailureSettlementEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != "action" || before.ID != after.ID ||
		before.ExecutionEpoch != after.ExecutionEpoch || after.Revision != before.Revision+1 ||
		after.State != "FAILED" || !agentOutcomeRecordState(before.State) {
		return false
	}
	var previous, current map[string]json.RawMessage
	if json.Unmarshal(before.Payload, &previous) != nil || json.Unmarshal(after.Payload, &current) != nil {
		return false
	}
	if _, exists := previous[agentFailureSettlementField]; exists {
		return false
	}
	raw, ok := current[agentFailureSettlementField]
	if !ok {
		return false
	}
	var encoded []byte
	var receipt agentv1.AgentFailureProjectionReceipt
	if json.Unmarshal(raw, &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		receipt.Evidence.SettlementFence == nil || receipt.Evidence.SettlementFence.ActionId != after.ID ||
		receipt.Evidence.SettlementFence.ExecutionEpoch != after.ExecutionEpoch || len(receipt.Signature) != 64 {
		return false
	}
	delete(current, agentFailureSettlementField)
	a, _ := json.Marshal(previous)
	b, _ := json.Marshal(current)
	return bytes.Equal(a, b)
}

// One proved error cursor, FAILED action, lease termination and only its own
// resource release commit together. Provider effects never run in this transaction.
func (store *Control) SettleAgentFailure(input AgentFailureSettlementRequest) (AgentFailureSettlementResult, error) {
	invalid := agentproof.ErrAgentFailureProjectionInvalid
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 || input.Projection == nil {
		return AgentFailureSettlementResult{}, invalid
	}
	projection := proto.Clone(input.Projection).(*agentv1.AgentFailureProjectionReceipt)
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(projection)
	if err != nil || len(encoded) == 0 || len(encoded) > maximumPayloadBytes {
		return AgentFailureSettlementResult{}, invalid
	}
	hash := sha256.Sum256(encoded)
	requestBytes, _ := json.Marshal(struct {
		OperationID, Actor, ActionID, RunID, Owner, ClaimToken, ProjectionDigest string
		Epoch                                                                    uint64
	}{
		input.OperationID, input.Actor, input.ActionID, input.RunID, input.Owner, input.ClaimToken, hex.EncodeToString(hash[:]), input.Epoch})
	requestDigest, err := operatorPayloadDigest(requestBytes)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	defer tx.Rollback()
	actionCutover, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if !IsDomainGoAuthoritative(actionCutover.State) {
		return AgentFailureSettlementResult{}, ErrCutoverNotAuthorized
	}
	action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil || !found {
		return AgentFailureSettlementResult{}, ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.RunID != input.RunID || (binding.Phase != "plan" && binding.Phase != "synthesis") || action.ExecutionEpoch != input.Epoch {
		return AgentFailureSettlementResult{}, ErrAgentRunExecutionRequired
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if applied {
		if err := store.validateAgentFailureSettlementTx(tx, action); err != nil {
			return AgentFailureSettlementResult{}, err
		}
		var fields map[string]json.RawMessage
		var prior []byte
		if replay.Domain != "action" || replay.RecordID != action.ID || action.State != "FAILED" || json.Unmarshal(action.Payload, &fields) != nil ||
			json.Unmarshal(fields[agentFailureSettlementField], &prior) != nil || !bytes.Equal(prior, encoded) {
			return AgentFailureSettlementResult{}, ErrCorruptRecord
		}
		checkpoint, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, binding.MetadataIndex+1), "agent_run")
		if err != nil || !found {
			return AgentFailureSettlementResult{}, ErrCorruptRecord
		}
		run, err := decodeAgentMetadata(checkpoint)
		if err != nil {
			return AgentFailureSettlementResult{}, err
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return AgentFailureSettlementResult{}, err
		}
		return AgentFailureSettlementResult{AlreadyApplied: true, Run: run, Action: action}, nil
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if !agentOutcomeRecordState(action.State) || run.NextIndex != binding.MetadataIndex || executionPlanDigest(run) != binding.PlanDigest || !executionPhaseReady(run, binding.Phase) {
		return AgentFailureSettlementResult{}, ErrRevisionConflict
	}
	lease, err := readActiveActionLeaseTx(tx, action.ID)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if lease.Epoch != input.Epoch || lease.WriterFencingToken != store.token || lease.Owner != input.Owner || lease.ClaimToken != input.ClaimToken {
		return AgentFailureSettlementResult{}, ErrActionLeaseStale
	}
	if context.now < lease.UpdatedAt || context.now >= lease.LeaseUntil {
		return AgentFailureSettlementResult{}, ErrActionLeaseExpired
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	sourceNode := agentFailureNode(binding.Phase)
	stored, exists := outcomes[sourceNode]
	if !exists {
		return AgentFailureSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	original, err := readAgentOutcomeAdmissionTx(tx, nodes[sourceNode], manifest)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if original.WriterFencingToken != stored.WriterFencingToken {
		return AgentFailureSettlementResult{}, ErrCorruptRecord
	}
	var source agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(stored.Receipt, &source) != nil || source.Evidence == nil {
		return AgentFailureSettlementResult{}, ErrCorruptRecord
	}
	if source.Evidence.NodeId != sourceNode || !agentproof.ValidTerminalFailure(source.Evidence) {
		return AgentFailureSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	if err := store.validateAgentFailureSourceTx(tx, binding); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	sourceHash := sha256.Sum256(stored.Receipt)
	expected := &agentv1.AgentFailureProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(sourceHash[:]),
		SettlementFence: &commonv1.ActionFence{ActionId: action.ID, ExecutionEpoch: input.Epoch}, SettlementWriterToken: store.token,
		RunId: run.RunID, MetadataEpoch: uint64(context.cutover.Epoch), ExpectedIndex: run.NextIndex, Phase: source.Evidence.Phase, ErrorBody: source.Evidence.PublicEvent,
		FleetId: store.fleetID, Environment: store.environment}
	if err := agentproof.VerifyAgentFailureProjection(projection, expected, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment}); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	evidence := projection.Evidence
	if run.NextIndex >= math.MaxInt64 {
		return AgentFailureSettlementResult{}, ErrRevisionConflict
	}
	event := agent.RunEvent{Type: "error", Phase: sourceNode, Body: agent.ArtifactRef{SHA256: evidence.ErrorBody.Sha256, Length: evidence.ErrorBody.Length},
		ActionID: run.RunID, ExecutionEpoch: uint64(context.cutover.Epoch), RunID: run.RunID, Index: run.NextIndex,
		CreatedAt: time.Unix(context.now, 0).UTC().Format(time.RFC3339)}
	run.ApplyRunEvent(event)
	runPayload, err := json.Marshal(run)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	runWrite, err := prepareControlRecordWrite(Record{Domain: "agent_run", ID: run.RunID, Revision: run.NextIndex, ExecutionEpoch: event.ExecutionEpoch, State: run.Status, Payload: runPayload}, nil)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	runWrite.agentEvent = true
	if err := store.putControlRecordTx(tx, runWrite, context.now); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return AgentFailureSettlementResult{}, ErrCorruptRecord
	}
	fields[agentFailureSettlementField], err = json.Marshal(encoded)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	action.Payload, err = json.Marshal(fields)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	action.Revision++
	action.State = "FAILED"
	write, err := prepareControlRecordWrite(action, nil)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	write.allowBoundLease, write.agentNodeDispatch, write.agentFailureSettlement = true, true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	action = write.record
	result, err := tx.Exec(`UPDATE action_leases SET terminal_state='FAILED_CONFIRMED',updated_at=?,writer_fencing_token=? WHERE action_id=? AND epoch=? AND claim_token=? AND terminal_state IS NULL`, context.now, store.token, action.ID, int64(input.Epoch), input.ClaimToken)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		return AgentFailureSettlementResult{}, ErrActionLeaseStale
	}
	if _, err := tx.Exec(`DELETE FROM action_resource_leases WHERE action_id=?`, action.ID); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO action_lease_events(action_id,event_type,owner,epoch,claim_token,lease_until,claim_revision,writer_fencing_token,recorded_at,resource_keys_json) VALUES(?,'TERMINATED',?,?,?,?,?,?,?,?)`,
		action.ID, lease.Owner, int64(lease.Epoch), lease.ClaimToken, lease.LeaseUntil, action.Revision, store.token, context.now, manifest); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	if err := store.validateAgentFailureSettlementTx(tx, action); err != nil {
		return AgentFailureSettlementResult{}, fmt.Errorf("failure settlement verification: %w", err)
	}
	payloadDigest, err := operatorPayloadDigest(action.Payload)
	if err != nil {
		return AgentFailureSettlementResult{}, err
	}
	receipt := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "action", RecordID: action.ID, Revision: action.Revision, State: action.State,
		ActionID: action.ID, ExecutionEpoch: input.Epoch, CutoverRevision: context.cutover.Revision, FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, receipt, store.token, context.now); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return AgentFailureSettlementResult{}, ErrWriterFenceHeld
	}
	if commitNow >= lease.LeaseUntil {
		return AgentFailureSettlementResult{}, ErrActionLeaseExpired
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return AgentFailureSettlementResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentFailureSettlementResult{}, fmt.Errorf("failure settlement commit: %w", err)
	}
	return AgentFailureSettlementResult{Run: run, Action: action}, nil
}

func agentFailureNode(phase string) string {
	switch phase {
	case "plan":
		return "leader"
	case "synthesis":
		return "synthesizer"
	default:
		return ""
	}
}

func (store *Control) validateAgentFailureSourceTx(tx *sql.Tx, binding agentExecutionIntent) error {
	switch binding.Phase {
	case "plan":
		return nil
	case "synthesis":
		_, err := store.verifiedAgentSynthesisSourceTx(tx, binding)
		return err
	default:
		return ErrCorruptRecord
	}
}
