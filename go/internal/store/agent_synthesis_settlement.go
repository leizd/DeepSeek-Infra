package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"math"
	"reflect"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

const agentSynthesisSettlementField = "nativeAgentSynthesisSettlement"

type AgentSynthesisSettlementRequest struct {
	OperationID, Actor, ActionID, RunID, Owner, ClaimToken string
	Epoch                                                  uint64
	Projection                                             *agentv1.AgentSynthesisProjectionReceipt
}
type AgentSynthesisSettlementResult struct {
	AlreadyApplied bool
	Run            agent.RunMetadata
	Action         Record
}

// Re-check retained projection signatures against immutable source metadata on
// reads and replay, including after the original writer has been replaced.
func (store *Control) validateAgentSynthesisSettlementTx(tx *sql.Tx, action Record) error {
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return ErrCorruptRecord
	}
	raw, present := fields[agentSynthesisSettlementField]
	if !present {
		return nil
	}
	if action.State != "SUCCEEDED" {
		return ErrCorruptRecord
	}
	var encoded []byte
	var receipt agentv1.AgentSynthesisProjectionReceipt
	if json.Unmarshal(raw, &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil || receipt.Evidence == nil {
		return ErrCorruptRecord
	}
	canonical, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
	if err != nil || !bytes.Equal(canonical, encoded) {
		return ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.Phase != "synthesis" {
		return ErrCorruptRecord
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return err
	}
	source, present := outcomes["synthesizer"]
	if !present {
		return ErrCorruptRecord
	}
	var sourceReceipt agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(source.Receipt, &sourceReceipt) != nil || sourceReceipt.Evidence == nil {
		return ErrCorruptRecord
	}
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, binding.MetadataIndex), "agent_run")
	if err != nil || !found {
		return ErrCorruptRecord
	}
	original, err := decodeAgentMetadata(record)
	if err != nil || original.Request != binding.Request || executionPlanDigest(original) != binding.PlanDigest || !executionPhaseReady(original, "synthesis") {
		return ErrCorruptRecord
	}
	var writer, settlementAt int64
	if tx.QueryRow(`SELECT writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, action.Revision).Scan(&writer, &settlementAt) != nil {
		return ErrCorruptRecord
	}
	if sourceReceipt.Evidence.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS ||
		sourceReceipt.Evidence.TaskOutput == nil || sourceReceipt.Evidence.TaskOutput.Failed {
		return ErrCorruptRecord
	}
	if _, err := store.verifiedAgentSynthesisSourceTx(tx, binding); err != nil {
		return err
	}
	sourceHash := sha256.Sum256(source.Receipt)
	expected := &agentv1.AgentSynthesisProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(sourceHash[:]),
		SettlementFence: &commonv1.ActionFence{ActionId: action.ID, ExecutionEpoch: action.ExecutionEpoch}, SettlementWriterToken: writer,
		RunId: binding.RunID, MetadataEpoch: record.ExecutionEpoch, ExpectedIndex: binding.MetadataIndex, ContentBody: sourceReceipt.Evidence.PublicEvent,
		FleetId: store.fleetID, Environment: store.environment}
	if agentproof.VerifyAgentSynthesisProjection(&receipt, expected, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment}) != nil {
		return ErrCorruptRecord
	}
	expectedRun := original
	for offset, kind := range []string{"content", "done"} {
		checkpoint, meta, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, binding.RunID, binding.MetadataIndex+int64(offset)+1), "agent_run")
		if err != nil || !found || meta.writerToken != writer || meta.timestamp != settlementAt {
			return ErrCorruptRecord
		}
		run, err := decodeAgentMetadata(checkpoint)
		if err != nil || checkpoint.ExecutionEpoch != record.ExecutionEpoch || run.Request != binding.Request ||
			executionPlanDigest(run) != binding.PlanDigest || run.LastEvent.Type != kind || run.LastEvent.Index != binding.MetadataIndex+int64(offset) {
			return ErrCorruptRecord
		}
		body := receipt.Evidence.ContentBody
		if offset == 1 {
			body = receipt.Evidence.DoneBody
		}
		event := agent.RunEvent{ActionID: binding.RunID, ExecutionEpoch: record.ExecutionEpoch, RunID: binding.RunID,
			Index: binding.MetadataIndex + int64(offset), CreatedAt: time.Unix(settlementAt, 0).UTC().Format(time.RFC3339), Type: kind,
			Body: agent.ArtifactRef{SHA256: body.Sha256, Length: body.Length}}
		if offset == 0 {
			event.Phase = "synthesizer"
		}
		expectedRun.ApplyRunEvent(event)
		if !reflect.DeepEqual(run, expectedRun) {
			return ErrCorruptRecord
		}

		if run.LastEvent.Body.SHA256 != body.Sha256 || run.LastEvent.Body.Length != body.Length || (offset == 0 && run.Status != "running") ||
			(offset == 1 && run.Status != "done") {
			return ErrCorruptRecord
		}
	}
	retained, meta, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, action.Revision), "action")
	if err != nil || !found || !bytes.Equal(retained.Payload, action.Payload) || retained.State != "SUCCEEDED" || meta.writerToken != writer || meta.timestamp != settlementAt {
		return ErrCorruptRecord
	}
	manifest, _ := json.Marshal([]string{"agent-run:" + binding.RunID})
	var count int
	if tx.QueryRow(`SELECT count(*) FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED' AND epoch=? AND claim_revision=? AND writer_fencing_token=? AND recorded_at=? AND resource_keys_json=?`, action.ID, int64(action.ExecutionEpoch), action.Revision, writer, meta.timestamp, string(manifest)).Scan(&count) != nil || count != 1 {
		return ErrCorruptRecord
	}
	if tx.QueryRow(`SELECT count(*) FROM action_leases WHERE action_id=? AND epoch=? AND terminal_state='SUCCEEDED' AND writer_fencing_token=? AND updated_at=?`, action.ID, int64(action.ExecutionEpoch), writer, meta.timestamp).Scan(&count) != nil || count != 1 {
		return ErrCorruptRecord
	}
	if tx.QueryRow(`SELECT count(*) FROM action_resource_leases WHERE action_id=?`, action.ID).Scan(&count) != nil || count != 0 {
		return ErrCorruptRecord
	}
	return nil
}

// The only extra terminal edge is an immutable synthesis settlement. Live writes
// additionally require the private settlement path and independent verification.
func nativeAgentSynthesisSettlementEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != "action" || before.ID != after.ID ||
		before.ExecutionEpoch != after.ExecutionEpoch || after.Revision != before.Revision+1 ||
		after.State != "SUCCEEDED" || !agentOutcomeRecordState(before.State) {
		return false
	}
	var previous, current map[string]json.RawMessage
	if json.Unmarshal(before.Payload, &previous) != nil || json.Unmarshal(after.Payload, &current) != nil {
		return false
	}
	if _, exists := previous[agentSynthesisSettlementField]; exists {
		return false
	}
	raw, ok := current[agentSynthesisSettlementField]
	if !ok {
		return false
	}
	var encoded []byte
	var receipt agentv1.AgentSynthesisProjectionReceipt
	if json.Unmarshal(raw, &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		receipt.Evidence.SettlementFence == nil || receipt.Evidence.SettlementFence.ActionId != after.ID ||
		receipt.Evidence.SettlementFence.ExecutionEpoch != after.ExecutionEpoch || len(receipt.Signature) != 64 {
		return false
	}
	delete(current, agentSynthesisSettlementField)
	a, _ := json.Marshal(previous)
	b, _ := json.Marshal(current)
	return bytes.Equal(a, b)
}

// Final content/done cursors, action completion, lease termination and resource
// release commit together. No RPC or provider request runs inside this transaction.
func (store *Control) SettleAgentSynthesis(input AgentSynthesisSettlementRequest) (AgentSynthesisSettlementResult, error) {
	invalid := agentproof.ErrAgentSynthesisProjectionInvalid
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 || input.Projection == nil {
		return AgentSynthesisSettlementResult{}, invalid
	}
	projection := proto.Clone(input.Projection).(*agentv1.AgentSynthesisProjectionReceipt)
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(projection)
	if err != nil || len(encoded) == 0 || len(encoded) > maximumPayloadBytes {
		return AgentSynthesisSettlementResult{}, invalid
	}
	hash := sha256.Sum256(encoded)
	requestBytes, _ := json.Marshal(struct {
		OperationID, Actor, ActionID, RunID, Owner, ClaimToken, ProjectionDigest string
		Epoch                                                                    uint64
	}{
		input.OperationID, input.Actor, input.ActionID, input.RunID, input.Owner, input.ClaimToken, hex.EncodeToString(hash[:]), input.Epoch})
	requestDigest, err := operatorPayloadDigest(requestBytes)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	defer tx.Rollback()
	actionCutover, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if !IsDomainGoAuthoritative(actionCutover.State) {
		return AgentSynthesisSettlementResult{}, ErrCutoverNotAuthorized
	}
	action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil || !found {
		return AgentSynthesisSettlementResult{}, ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.RunID != input.RunID || binding.Phase != "synthesis" || action.ExecutionEpoch != input.Epoch {
		return AgentSynthesisSettlementResult{}, ErrAgentRunExecutionRequired
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if applied {
		if err := store.validateAgentSynthesisSettlementTx(tx, action); err != nil {
			return AgentSynthesisSettlementResult{}, err
		}
		var fields map[string]json.RawMessage
		var prior []byte
		if replay.Domain != "action" || replay.RecordID != action.ID || action.State != "SUCCEEDED" || json.Unmarshal(action.Payload, &fields) != nil ||
			json.Unmarshal(fields[agentSynthesisSettlementField], &prior) != nil || !bytes.Equal(prior, encoded) {
			return AgentSynthesisSettlementResult{}, ErrCorruptRecord
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return AgentSynthesisSettlementResult{}, err
		}
		return AgentSynthesisSettlementResult{AlreadyApplied: true, Run: run, Action: action}, nil
	}
	if !agentOutcomeRecordState(action.State) || run.NextIndex != binding.MetadataIndex || executionPlanDigest(run) != binding.PlanDigest || !executionPhaseReady(run, "synthesis") {
		return AgentSynthesisSettlementResult{}, ErrRevisionConflict
	}
	lease, err := readActiveActionLeaseTx(tx, action.ID)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if lease.Epoch != input.Epoch || lease.WriterFencingToken != store.token || lease.Owner != input.Owner || lease.ClaimToken != input.ClaimToken {
		return AgentSynthesisSettlementResult{}, ErrActionLeaseStale
	}
	if context.now < lease.UpdatedAt || context.now >= lease.LeaseUntil {
		return AgentSynthesisSettlementResult{}, ErrActionLeaseExpired
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	stored, exists := outcomes["synthesizer"]
	if !exists {
		return AgentSynthesisSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	original, err := readAgentOutcomeAdmissionTx(tx, nodes["synthesizer"], manifest)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if original.WriterFencingToken != stored.WriterFencingToken {
		return AgentSynthesisSettlementResult{}, ErrCorruptRecord
	}
	var source agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(stored.Receipt, &source) != nil || source.Evidence == nil {
		return AgentSynthesisSettlementResult{}, ErrCorruptRecord
	}
	if source.Evidence.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS || source.Evidence.TaskOutput == nil || source.Evidence.TaskOutput.Failed {
		return AgentSynthesisSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	if _, err := store.verifiedAgentSynthesisSourceTx(tx, binding); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	sourceHash := sha256.Sum256(stored.Receipt)
	expected := &agentv1.AgentSynthesisProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(sourceHash[:]),
		SettlementFence: &commonv1.ActionFence{ActionId: action.ID, ExecutionEpoch: input.Epoch}, SettlementWriterToken: store.token,
		RunId: run.RunID, MetadataEpoch: uint64(context.cutover.Epoch), ExpectedIndex: run.NextIndex, ContentBody: source.Evidence.PublicEvent,
		FleetId: store.fleetID, Environment: store.environment}
	if err := agentproof.VerifyAgentSynthesisProjection(projection, expected, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment}); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	evidence := projection.Evidence
	events := []agent.RunEvent{{Type: "content", Phase: "synthesizer", Body: agent.ArtifactRef{SHA256: evidence.ContentBody.Sha256, Length: evidence.ContentBody.Length}},
		{Type: "done", Body: agent.ArtifactRef{SHA256: evidence.DoneBody.Sha256, Length: evidence.DoneBody.Length}}}
	for _, event := range events {
		if run.NextIndex >= math.MaxInt64 {
			return AgentSynthesisSettlementResult{}, ErrRevisionConflict
		}
		event.ActionID, event.ExecutionEpoch = run.RunID, uint64(context.cutover.Epoch)
		event.RunID, event.Index, event.CreatedAt = run.RunID, run.NextIndex, time.Unix(context.now, 0).UTC().Format(time.RFC3339)
		run.ApplyRunEvent(event)
		payload, err := json.Marshal(run)
		if err != nil {
			return AgentSynthesisSettlementResult{}, err
		}
		write, err := prepareControlRecordWrite(Record{Domain: "agent_run", ID: run.RunID, Revision: run.NextIndex, ExecutionEpoch: event.ExecutionEpoch, State: run.Status, Payload: payload}, nil)
		if err != nil {
			return AgentSynthesisSettlementResult{}, err
		}
		write.agentEvent = true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentSynthesisSettlementResult{}, err
		}
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return AgentSynthesisSettlementResult{}, ErrCorruptRecord
	}
	fields[agentSynthesisSettlementField], err = json.Marshal(encoded)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	action.Payload, err = json.Marshal(fields)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	action.Revision++
	action.State = "SUCCEEDED"
	write, err := prepareControlRecordWrite(action, nil)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	write.allowBoundLease, write.agentNodeDispatch, write.agentSynthesisSettlement = true, true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	action = write.record
	result, err := tx.Exec(`UPDATE action_leases SET terminal_state='SUCCEEDED',updated_at=?,writer_fencing_token=? WHERE action_id=? AND epoch=? AND claim_token=? AND terminal_state IS NULL`, context.now, store.token, action.ID, int64(input.Epoch), input.ClaimToken)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		return AgentSynthesisSettlementResult{}, ErrActionLeaseStale
	}
	if _, err := tx.Exec(`DELETE FROM action_resource_leases WHERE action_id=?`, action.ID); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO action_lease_events(action_id,event_type,owner,epoch,claim_token,lease_until,claim_revision,writer_fencing_token,recorded_at,resource_keys_json) VALUES(?,'TERMINATED',?,?,?,?,?,?,?,?)`,
		action.ID, lease.Owner, int64(lease.Epoch), lease.ClaimToken, lease.LeaseUntil, action.Revision, store.token, context.now, manifest); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	if err := store.validateAgentSynthesisSettlementTx(tx, action); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(action.Payload)
	if err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	receipt := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "action", RecordID: action.ID, Revision: action.Revision, State: action.State,
		ActionID: action.ID, ExecutionEpoch: input.Epoch, CutoverRevision: context.cutover.Revision, FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, receipt, store.token, context.now); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return AgentSynthesisSettlementResult{}, ErrWriterFenceHeld
	}
	if commitNow >= lease.LeaseUntil {
		return AgentSynthesisSettlementResult{}, ErrActionLeaseExpired
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentSynthesisSettlementResult{}, err
	}
	return AgentSynthesisSettlementResult{Run: run, Action: action}, nil
}
