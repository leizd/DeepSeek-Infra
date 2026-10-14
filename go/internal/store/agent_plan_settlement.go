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
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

const agentPlanSettlementField = "nativeAgentPlanSettlement"

type AgentPlanSettlementRequest struct {
	OperationID, Actor, ActionID, RunID, Owner, ClaimToken string
	Epoch                                                  uint64
	Projection                                             *agentv1.AgentPlanProjectionReceipt
}
type AgentPlanSettlementResult struct {
	AlreadyApplied bool
	Run            agent.RunMetadata
	Action         Record
}

// Re-check retained projection signatures against immutable source metadata on
// reads and replay, including after the original writer has been replaced.
func (store *Control) validateAgentPlanSettlementTx(tx *sql.Tx, action Record) error {
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return ErrCorruptRecord
	}
	raw, present := fields[agentPlanSettlementField]
	if !present {
		return nil
	}
	if action.State != "SUCCEEDED" {
		return ErrCorruptRecord
	}
	var encoded []byte
	var receipt agentv1.AgentPlanProjectionReceipt
	if json.Unmarshal(raw, &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil || receipt.Evidence == nil {
		return ErrCorruptRecord
	}
	canonical, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
	if err != nil || !bytes.Equal(canonical, encoded) {
		return ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.Phase != "plan" {
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
	source, present := outcomes["leader"]
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
	if err != nil || original.Request != binding.Request || executionPlanDigest(original) != binding.PlanDigest || !executionPhaseReady(original, "plan") {
		return ErrCorruptRecord
	}
	var writer int64
	if tx.QueryRow(`SELECT writer_fencing_token FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, action.ID, action.Revision).Scan(&writer) != nil {
		return ErrCorruptRecord
	}
	candidates := []agent.RunPlanNode{}
	for _, node := range sourceReceipt.Evidence.ProviderPlan {
		candidates = append(candidates, agent.RunPlanNode{ID: node.Id, Task: node.Task, DependsOn: node.DependsOn})
	}
	normalized := []*agentv1.AgentRunPlanNode{}
	for _, node := range agent.NormalizeRunPlan(candidates) {
		normalized = append(normalized, &agentv1.AgentRunPlanNode{Id: node.ID, Task: node.Task, DependsOn: node.DependsOn})
	}
	next := "running"
	if original.ConfirmPlan {
		next = "awaiting_plan"
	}
	sourceHash := sha256.Sum256(source.Receipt)
	expected := &agentv1.AgentPlanProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(sourceHash[:]),
		SettlementFence: &commonv1.ActionFence{ActionId: action.ID, ExecutionEpoch: action.ExecutionEpoch}, SettlementWriterToken: writer,
		RunId: binding.RunID, MetadataEpoch: record.ExecutionEpoch, ExpectedIndex: binding.MetadataIndex, NormalizedPlan: normalized, NextStatus: next, PlanningRequired: original.Status == "created",
		FleetId: store.fleetID, Environment: store.environment}
	if agentproof.VerifyAgentPlanProjection(&receipt, expected, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment}) != nil {
		return ErrCorruptRecord
	}
	return nil
}

// The only extra terminal edge is an immutable planner settlement. Live writes
// additionally require the private settlement path and independent verification.
func nativeAgentPlanSettlementEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != "action" || before.ID != after.ID ||
		before.ExecutionEpoch != after.ExecutionEpoch || after.Revision != before.Revision+1 ||
		after.State != "SUCCEEDED" || !agentOutcomeRecordState(before.State) {
		return false
	}
	var previous, current map[string]json.RawMessage
	if json.Unmarshal(before.Payload, &previous) != nil || json.Unmarshal(after.Payload, &current) != nil {
		return false
	}
	if _, exists := previous[agentPlanSettlementField]; exists {
		return false
	}
	raw, ok := current[agentPlanSettlementField]
	if !ok {
		return false
	}
	var encoded []byte
	var receipt agentv1.AgentPlanProjectionReceipt
	if json.Unmarshal(raw, &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		receipt.Evidence.SettlementFence == nil || receipt.Evidence.SettlementFence.ActionId != after.ID ||
		receipt.Evidence.SettlementFence.ExecutionEpoch != after.ExecutionEpoch || len(receipt.Signature) != 64 {
		return false
	}
	delete(current, agentPlanSettlementField)
	a, _ := json.Marshal(previous)
	b, _ := json.Marshal(current)
	return bytes.Equal(a, b)
}

// Both public event cursors, action completion, lease termination and resource
// release commit together. No RPC or provider request runs inside this transaction.
func (store *Control) SettleAgentPlan(input AgentPlanSettlementRequest) (AgentPlanSettlementResult, error) {
	invalid := agentproof.ErrAgentPlanProjectionInvalid
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 || input.Projection == nil {
		return AgentPlanSettlementResult{}, invalid
	}
	projection := proto.Clone(input.Projection).(*agentv1.AgentPlanProjectionReceipt)
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(projection)
	if err != nil || len(encoded) == 0 || len(encoded) > maximumPayloadBytes {
		return AgentPlanSettlementResult{}, invalid
	}
	hash := sha256.Sum256(encoded)
	requestBytes, _ := json.Marshal(struct {
		OperationID, Actor, ActionID, RunID, Owner, ClaimToken, ProjectionDigest string
		Epoch                                                                    uint64
	}{
		input.OperationID, input.Actor, input.ActionID, input.RunID, input.Owner, input.ClaimToken, hex.EncodeToString(hash[:]), input.Epoch})
	requestDigest, err := operatorPayloadDigest(requestBytes)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	defer tx.Rollback()
	actionCutover, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	if !IsDomainGoAuthoritative(actionCutover.State) {
		return AgentPlanSettlementResult{}, ErrCutoverNotAuthorized
	}
	action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil || !found {
		return AgentPlanSettlementResult{}, ErrCorruptRecord
	}
	binding, err := decodeAgentExecution(action)
	if err != nil || binding.RunID != input.RunID || binding.Phase != "plan" || action.ExecutionEpoch != input.Epoch {
		return AgentPlanSettlementResult{}, ErrAgentRunExecutionRequired
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	if applied {
		if err := store.validateAgentPlanSettlementTx(tx, action); err != nil {
			return AgentPlanSettlementResult{}, err
		}
		var fields map[string]json.RawMessage
		var prior []byte
		if replay.Domain != "action" || replay.RecordID != action.ID || action.State != "SUCCEEDED" || json.Unmarshal(action.Payload, &fields) != nil ||
			json.Unmarshal(fields[agentPlanSettlementField], &prior) != nil || !bytes.Equal(prior, encoded) {
			return AgentPlanSettlementResult{}, ErrCorruptRecord
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return AgentPlanSettlementResult{}, err
		}
		return AgentPlanSettlementResult{AlreadyApplied: true, Run: run, Action: action}, nil
	}
	if !agentOutcomeRecordState(action.State) || run.NextIndex != binding.MetadataIndex || executionPlanDigest(run) != binding.PlanDigest || !executionPhaseReady(run, "plan") {
		return AgentPlanSettlementResult{}, ErrRevisionConflict
	}
	lease, err := readActiveActionLeaseTx(tx, action.ID)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	if lease.Epoch != input.Epoch || lease.WriterFencingToken != store.token || lease.Owner != input.Owner || lease.ClaimToken != input.ClaimToken {
		return AgentPlanSettlementResult{}, ErrActionLeaseStale
	}
	if context.now < lease.UpdatedAt || context.now >= lease.LeaseUntil {
		return AgentPlanSettlementResult{}, ErrActionLeaseExpired
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	nodes, err := readAgentNodeDispatches(action)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	outcomes, err := store.agentNodeOutcomes(action, nodes)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	stored, exists := outcomes["leader"]
	if !exists {
		return AgentPlanSettlementResult{}, ErrAgentNodeOutcomeRequired
	}
	original, err := readAgentOutcomeAdmissionTx(tx, nodes["leader"], manifest)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	if original.WriterFencingToken != stored.WriterFencingToken {
		return AgentPlanSettlementResult{}, ErrCorruptRecord
	}
	var source agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(stored.Receipt, &source) != nil || source.Evidence == nil {
		return AgentPlanSettlementResult{}, ErrCorruptRecord
	}
	candidates := []agent.RunPlanNode{}
	for _, node := range source.Evidence.ProviderPlan {
		candidates = append(candidates, agent.RunPlanNode{ID: node.Id, Task: node.Task, DependsOn: append([]string(nil), node.DependsOn...)})
	}
	plan := agent.NormalizeRunPlan(candidates)
	normalized := []*agentv1.AgentRunPlanNode{}
	for _, node := range plan {
		normalized = append(normalized, &agentv1.AgentRunPlanNode{Id: node.ID, Task: node.Task, DependsOn: node.DependsOn})
	}
	nextStatus := "running"
	if run.ConfirmPlan {
		nextStatus = "awaiting_plan"
	}
	sourceHash := sha256.Sum256(stored.Receipt)
	expected := &agentv1.AgentPlanProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(sourceHash[:]),
		SettlementFence: &commonv1.ActionFence{ActionId: action.ID, ExecutionEpoch: input.Epoch}, SettlementWriterToken: store.token,
		RunId: run.RunID, MetadataEpoch: uint64(context.cutover.Epoch), ExpectedIndex: run.NextIndex, NormalizedPlan: normalized, NextStatus: nextStatus, PlanningRequired: run.Status == "created",
		FleetId: store.fleetID, Environment: store.environment}
	if err := agentproof.VerifyAgentPlanProjection(projection, expected, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment}); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	evidence := projection.Evidence
	events := []agent.RunEvent{{Type: "agent_plan", Plan: plan, Body: agent.ArtifactRef{SHA256: evidence.PlanBody.Sha256, Length: evidence.PlanBody.Length}},
		{Type: "run_status", Status: nextStatus, Body: agent.ArtifactRef{SHA256: evidence.StatusBody.Sha256, Length: evidence.StatusBody.Length}}}
	if run.Status == "created" {
		events = append([]agent.RunEvent{{Type: "run_status", Status: "planning", Body: agent.ArtifactRef{SHA256: evidence.PlanningBody.Sha256, Length: evidence.PlanningBody.Length}}}, events...)
	}
	for _, event := range events {
		if run.NextIndex >= math.MaxInt64 {
			return AgentPlanSettlementResult{}, ErrRevisionConflict
		}
		event.ActionID, event.ExecutionEpoch = run.RunID, uint64(context.cutover.Epoch)
		event.RunID, event.Index, event.CreatedAt = run.RunID, run.NextIndex, time.Unix(context.now, 0).UTC().Format(time.RFC3339)
		run.ApplyRunEvent(event)
		payload, err := json.Marshal(run)
		if err != nil {
			return AgentPlanSettlementResult{}, err
		}
		write, err := prepareControlRecordWrite(Record{Domain: "agent_run", ID: run.RunID, Revision: run.NextIndex, ExecutionEpoch: event.ExecutionEpoch, State: run.Status, Payload: payload}, nil)
		if err != nil {
			return AgentPlanSettlementResult{}, err
		}
		write.agentEvent = true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentPlanSettlementResult{}, err
		}
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(action.Payload, &fields) != nil {
		return AgentPlanSettlementResult{}, ErrCorruptRecord
	}
	fields[agentPlanSettlementField], err = json.Marshal(encoded)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	action.Payload, err = json.Marshal(fields)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	action.Revision++
	action.State = "SUCCEEDED"
	write, err := prepareControlRecordWrite(action, nil)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	write.allowBoundLease, write.agentNodeDispatch, write.agentPlanSettlement = true, true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	action = write.record
	result, err := tx.Exec(`UPDATE action_leases SET terminal_state='SUCCEEDED',updated_at=?,writer_fencing_token=? WHERE action_id=? AND epoch=? AND claim_token=? AND terminal_state IS NULL`, context.now, store.token, action.ID, int64(input.Epoch), input.ClaimToken)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	if n, err := result.RowsAffected(); err != nil || n != 1 {
		return AgentPlanSettlementResult{}, ErrActionLeaseStale
	}
	if _, err := tx.Exec(`DELETE FROM action_resource_leases WHERE action_id=?`, action.ID); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO action_lease_events(action_id,event_type,owner,epoch,claim_token,lease_until,claim_revision,writer_fencing_token,recorded_at,resource_keys_json) VALUES(?,'TERMINATED',?,?,?,?,?,?,?,?)`,
		action.ID, lease.Owner, int64(lease.Epoch), lease.ClaimToken, lease.LeaseUntil, action.Revision, store.token, context.now, manifest); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(action.Payload)
	if err != nil {
		return AgentPlanSettlementResult{}, err
	}
	receipt := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID, Domain: "action", RecordID: action.ID, Revision: action.Revision, State: action.State,
		ActionID: action.ID, ExecutionEpoch: input.Epoch, CutoverRevision: context.cutover.Revision, FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: input.Actor}
	if err := insertOperatorMutationTx(tx, receipt, store.token, context.now); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return AgentPlanSettlementResult{}, ErrWriterFenceHeld
	}
	if commitNow >= lease.LeaseUntil {
		return AgentPlanSettlementResult{}, ErrActionLeaseExpired
	}
	if _, err := store.assertWriterTx(tx, commitNow); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentPlanSettlementResult{}, err
	}
	return AgentPlanSettlementResult{Run: run, Action: action}, nil
}
