package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

var ErrAgentNodeDispatchRequired = errors.New("AGENT_NODE_DISPATCH_REQUIRED")
var ErrAgentNodeDependencyUnverified = errors.New("AGENT_NODE_DEPENDENCY_UNVERIFIED")

const agentNodeDispatchField = "nativeAgentNodeDispatches"

func nativeAgentNodeDispatches(payload json.RawMessage) bool {
	var fields map[string]json.RawMessage
	if json.Unmarshal(payload, &fields) != nil {
		return false
	}
	_, found := fields[agentNodeDispatchField]
	return found
}

// An intent is a control-plane dispatch reservation, never a provider receipt.
// Retrying it returns Fresh=false, even if no remote outcome has been observed.
type AgentNodeDispatch struct {
	SchemaVersion        uint32            `json:"schemaVersion"`
	OperationID          string            `json:"operationId"`
	ActionID             string            `json:"actionId"`
	ExecutionEpoch       uint64            `json:"executionEpoch"`
	RunID                string            `json:"runId"`
	NodeID               string            `json:"nodeId"`
	Phase                string            `json:"phase"`
	Task                 string            `json:"task"`
	DependsOn            []string          `json:"dependsOn"`
	Request              agent.ArtifactRef `json:"requestRef"`
	PlanDigest           string            `json:"planDigest"`
	MetadataIndex        int64             `json:"metadataIndex"`
	MetadataEpoch        uint64            `json:"metadataEpoch"`
	State                string            `json:"state"`
	RecordedAt           int64             `json:"recordedAt"`
	SuccessorOperationID string            `json:"successorOperationId,omitempty"`
	ResetIndex           int64             `json:"resetIndex,omitempty"`
}

type AgentNodeDispatchRequest struct {
	OperationID string `json:"operationId"`
	Actor       string `json:"actor"`
	ActionID    string `json:"actionId"`
	Epoch       uint64 `json:"executionEpoch"`
	RunID       string `json:"runId"`
	NodeID      string `json:"nodeId"`
	Owner       string `json:"owner"`
	ClaimToken  string `json:"claimToken"`
}

type AgentNodeDispatchResult struct {
	Fresh            bool                   `json:"fresh"`
	Intent           AgentNodeDispatch      `json:"intent"`
	Mutation         OperatorMutationResult `json:"mutation"`
	Admission        AdmissionResult        `json:"admission"`
	WriterLeaseUntil int64                  `json:"writerLeaseUntil"`
}

func sameAgentNodeDispatches(before, after Record) bool {
	first, err := readAgentNodeDispatches(before)
	second, otherErr := readAgentNodeDispatches(after)
	if err != nil || otherErr != nil {
		return false
	}
	firstBytes, err := json.Marshal(first)
	secondBytes, otherErr := json.Marshal(second)
	return err == nil && otherErr == nil && bytes.Equal(firstBytes, secondBytes)
}

func readAgentNodeDispatches(record Record) (map[string]AgentNodeDispatch, error) {
	var payload map[string]json.RawMessage
	if json.Unmarshal(record.Payload, &payload) != nil {
		return nil, ErrCorruptRecord
	}
	nodes := map[string]AgentNodeDispatch{}
	if raw, present := payload[agentNodeDispatchField]; present {
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&nodes) != nil || nodes == nil || len(nodes) > 5 {
			return nil, ErrCorruptRecord
		}
	}
	binding, err := decodeAgentExecution(record)
	if err != nil {
		return nil, err
	}
	for id, node := range nodes {
		if node.SchemaVersion != 1 || id != node.NodeID || id == "" ||
			node.ActionID != record.ID || node.ExecutionEpoch == 0 || node.ExecutionEpoch > record.ExecutionEpoch ||
			node.RunID != binding.RunID || node.Phase != binding.Phase || node.Request != binding.Request ||
			node.PlanDigest != binding.PlanDigest || node.MetadataIndex != binding.MetadataIndex ||
			node.MetadataEpoch != binding.MetadataEpoch || node.State != "INTENT_RECORDED" || node.RecordedAt < 0 ||
			!validAgentNodeSuccessor(node) || !ValidRecordID(node.OperationID) || node.OperationID != agentNodeOperationID(node) {
			return nil, ErrCorruptRecord
		}
	}
	return nodes, nil
}

func agentNodeOperationID(node AgentNodeDispatch) string {
	node.OperationID, node.State, node.RecordedAt = "", "", 0
	node.SuccessorOperationID, node.ResetIndex = "", 0
	encoded, _ := json.Marshal(node)
	digest := sha256.Sum256(append([]byte("agent-node-dispatch-v1\n"), encoded...))
	return "agent-node-" + hex.EncodeToString(digest[:])
}

// An empty successor is the original record. A stored successor must be the
// shared digest and must not replace the original operation id.
func validAgentNodeSuccessor(node AgentNodeDispatch) bool {
	if node.SuccessorOperationID == "" {
		return node.ResetIndex == 0
	}
	if node.ResetIndex < 0 || node.SuccessorOperationID == node.OperationID {
		return false
	}
	expected := agent.SuccessorOperationID(node.OperationID, node.ActionID, node.ExecutionEpoch, node.NodeID, node.ResetIndex)
	return node.SuccessorOperationID == expected
}

func agentDispatchPlanNode(run agent.RunMetadata, binding AgentRunExecutionBinding, nodeID string, completed ...map[string]AgentTaskSettlement) (agent.RunPlanNode, error) {
	if binding.Phase == "plan" && nodeID == "leader" {
		return agent.RunPlanNode{ID: "leader", Task: "plan agent run", DependsOn: []string{}}, nil
	}
	if binding.Phase == "synthesis" && nodeID == "synthesizer" && completedAgentTasks(run) {
		dependencies := make([]string, 0, len(run.Plan))
		for _, node := range run.Plan {
			dependencies = append(dependencies, node.ID)
		}
		return agent.RunPlanNode{ID: "synthesizer", Task: "synthesize verified agent outputs", DependsOn: dependencies}, nil
	}
	if binding.Phase == "tasks" {
		for _, node := range run.Plan {
			if node.ID == nodeID {
				// The imported event projection is not effect evidence. Dependencies
				// need verified Rust outcomes before this path may admit them.
				for _, dependency := range node.DependsOn {
					if len(completed) != 1 {
						return agent.RunPlanNode{}, ErrAgentNodeDependencyUnverified
					}
					if _, verified := completed[0][dependency]; !verified || run.Nodes()[dependency].State != "succeeded" {
						return agent.RunPlanNode{}, ErrAgentNodeDependencyUnverified
					}
				}
				return node, nil
			}
		}
	}
	return agent.RunPlanNode{}, ErrAgentNodeDispatchRequired
}

func (store *Control) agentNodeLeaseTx(tx *sql.Tx, request AgentNodeDispatchRequest, now int64) (ActionLease, error) {
	lease, err := readActiveActionLeaseTx(tx, request.ActionID)
	if err != nil {
		return lease, err
	}
	if lease.Epoch != request.Epoch || lease.Owner != request.Owner || lease.WriterFencingToken != store.token {
		return lease, ErrActionLeaseStale
	}
	if lease.ClaimToken != request.ClaimToken {
		return lease, ErrInvalidClaimToken
	}
	if lease.LeaseUntil <= now || now < lease.UpdatedAt {
		return lease, ErrActionLeaseExpired
	}
	if _, _, err := validateActionLeaseResourcesTx(tx, lease); err != nil {
		return lease, err
	}
	return lease, nil
}

// BeginAgentNodeDispatch makes the single fresh-dispatch decision durable under
// both live leases and the unchanged Go run/plan scope. A lost response, another
// operation ID, a takeover or imported node progress cannot grant a second call.
// Rust still needs independent grant/fence validation and its own effect journal.
func (store *Control) BeginAgentNodeDispatch(input AgentNodeDispatchRequest) (AgentNodeDispatchResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.NodeID == "" || len(input.NodeID) > 64 || strings.TrimSpace(input.Owner) == "" || input.ClaimToken == "" {
		return AgentNodeDispatchResult{}, ErrAgentNodeDispatchRequired
	}
	encoded, _ := json.Marshal(input) // Fixed strings and integers have no encoding failure.
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	// The generic operator action channel stays fenced. As with phase admission,
	// enter through Agent authority and require the action authority/lease here.
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	defer tx.Rollback()
	actionAuthority, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	if !IsDomainGoAuthoritative(actionAuthority.State) {
		return AgentNodeDispatchResult{}, ErrCutoverNotAuthorized
	}
	// The receipt describes the action row, using that domain's actual cutover.
	context.cutover = actionAuthority
	lease, err := store.agentNodeLeaseTx(tx, input, context.now)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	record, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	if !found || record.ExecutionEpoch != input.Epoch || record.State != "CLAIMED" && record.State != "EXECUTING" {
		return AgentNodeDispatchResult{}, ErrActionNotClaimable
	}
	binding, err := decodeAgentExecution(record)
	if err != nil || binding.RunID != input.RunID {
		return AgentNodeDispatchResult{}, ErrAgentRunExecutionRequired
	}
	run, err := store.agentExecutionRunTx(tx, binding)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	if !executionPhaseReady(run, binding.Phase) || executionPlanDigest(run) != binding.PlanDigest {
		return AgentNodeDispatchResult{}, ErrActionNotClaimable
	}
	completed, err := store.validatedAgentTaskSettlementsTx(tx, record)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	if binding.Phase == "synthesis" {
		if _, err := store.verifiedAgentSynthesisSourceTx(tx, binding); err != nil {
			return AgentNodeDispatchResult{}, err
		}
	}
	planNode, err := agentDispatchPlanNode(run, binding, input.NodeID, completed)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentNodeDispatchResult{}, err
	}
	intent, exists := nodes[input.NodeID]
	if applied && (!exists || replay.Domain != "action" || replay.RecordID != input.ActionID) {
		return AgentNodeDispatchResult{}, ErrCorruptRecord
	}
	// A newer authority epoch may name the persisted successor. The stored
	// operation id and the original fence stay on the action record.
	successorFresh := exists && intent.SuccessorOperationID != "" && input.OperationID == intent.SuccessorOperationID &&
		intent.ExecutionEpoch > 0 && input.Epoch > intent.ExecutionEpoch
	if !exists {
		if record.Revision >= math.MaxInt64-1 {
			return AgentNodeDispatchResult{}, ErrRevisionConflict
		}
		intent = AgentNodeDispatch{SchemaVersion: 1, ActionID: record.ID, ExecutionEpoch: input.Epoch,
			RunID: binding.RunID, NodeID: planNode.ID, Phase: binding.Phase, Task: planNode.Task,
			DependsOn: append([]string{}, planNode.DependsOn...), Request: binding.Request, PlanDigest: binding.PlanDigest,
			MetadataIndex: binding.MetadataIndex, MetadataEpoch: binding.MetadataEpoch,
			State: "INTENT_RECORDED", RecordedAt: context.now}
		intent.OperationID = agentNodeOperationID(intent)
		nodes[input.NodeID] = intent
		var payload map[string]json.RawMessage
		if json.Unmarshal(record.Payload, &payload) != nil {
			return AgentNodeDispatchResult{}, ErrCorruptRecord
		}
		payload[agentNodeDispatchField], _ = json.Marshal(nodes)
		// Existing RawMessages were decoded above; the new map is fully typed.
		record.Payload, _ = json.Marshal(payload)
		record.Revision++
		record.State = "EXECUTING"
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			return AgentNodeDispatchResult{}, err
		}
		write.allowBoundLease, write.agentNodeDispatch = true, true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentNodeDispatchResult{}, err
		}
		record = write.record
	}
	if successorFresh && record.State == "CLAIMED" {
		if record.Revision >= math.MaxInt64-1 {
			return AgentNodeDispatchResult{}, ErrRevisionConflict
		}
		record.Revision++
		record.State = "EXECUTING"
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			return AgentNodeDispatchResult{}, err
		}
		write.allowBoundLease = true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentNodeDispatchResult{}, err
		}
		record = write.record
	}
	if !applied {
		payloadDigest, err := operatorPayloadDigest(record.Payload)
		if err != nil {
			return AgentNodeDispatchResult{}, err
		}
		replay = OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID,
			Domain: "action", RecordID: record.ID, Revision: record.Revision, State: record.State,
			ActionID: record.ID, ExecutionEpoch: record.ExecutionEpoch, CutoverRevision: context.cutover.Revision,
			FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest,
			RequestDigest: requestDigest, Actor: input.Actor}
		if err := insertOperatorMutationTx(tx, replay, store.token, context.now); err != nil {
			return AgentNodeDispatchResult{}, err
		}
	}
	commitNow := store.now()
	if commitNow < context.now || commitNow >= lease.LeaseUntil {
		return AgentNodeDispatchResult{}, ErrActionLeaseExpired
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentNodeDispatchResult{}, err
	}
	response := intent
	if successorFresh {
		response.OperationID = intent.SuccessorOperationID
		response.ExecutionEpoch = input.Epoch
	}
	return AgentNodeDispatchResult{Fresh: !exists || successorFresh, Intent: response, Mutation: replay,
		Admission: AdmissionResult{Record: record, Lease: lease}, WriterLeaseUntil: store.leaseUntil}, nil
}

// AgentSuccessorError is a refused generation. Its code is the admission code.
type AgentSuccessorError struct{ Code string }

func (e *AgentSuccessorError) Error() string {
	if e == nil || e.Code == "" {
		return "AGENT_RUNTIME_RESET_REQUIRES_GENERATION"
	}
	return e.Code
}

type AgentNodeSuccessorBindRequest struct {
	OperationID       string `json:"operationId"`
	Actor             string `json:"actor"`
	ActionID          string `json:"actionId"`
	Epoch             uint64 `json:"executionEpoch"`
	RunID             string `json:"runId"`
	NodeID            string `json:"nodeId"`
	Owner             string `json:"owner"`
	ClaimToken        string `json:"claimToken"`
	ResetIndex        int64  `json:"resetIndex"`
	Effect            string `json:"effect"`
	ResetAdmitted     bool   `json:"resetAdmitted"`
	ResetFenceMatches bool   `json:"resetFenceMatches"`
}

type AgentNodeSuccessorBindResult struct {
	OperationID          string
	SuccessorOperationID string
	ResetIndex           int64
	Replayed             bool
	Replaced             bool
}

// BindAgentNodeSuccessor stores the minted successor beside the original
// operation id. It does not execute and does not advance the epoch.
func (store *Control) BindAgentNodeSuccessor(input AgentNodeSuccessorBindRequest) (AgentNodeSuccessorBindResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.NodeID == "" || len(input.NodeID) > 64 || strings.TrimSpace(input.Owner) == "" || input.ClaimToken == "" || input.ResetIndex < 0 {
		return AgentNodeSuccessorBindResult{}, ErrAgentNodeDispatchRequired
	}
	encoded, _ := json.Marshal(input)
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	defer tx.Rollback()
	actionAuthority, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	if !IsDomainGoAuthoritative(actionAuthority.State) {
		return AgentNodeSuccessorBindResult{}, ErrCutoverNotAuthorized
	}
	context.cutover = actionAuthority
	if _, err := store.agentNodeLeaseTx(tx, AgentNodeDispatchRequest{ActionID: input.ActionID, Epoch: input.Epoch, Owner: input.Owner, ClaimToken: input.ClaimToken}, context.now); err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	record, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	if !found || record.ExecutionEpoch != input.Epoch || record.State != "EXECUTING" {
		return AgentNodeSuccessorBindResult{}, ErrActionNotClaimable
	}
	binding, err := decodeAgentExecution(record)
	if err != nil || binding.RunID != input.RunID {
		return AgentNodeSuccessorBindResult{}, ErrAgentRunExecutionRequired
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	intent, exists := nodes[input.NodeID]
	if !exists || intent.ExecutionEpoch != input.Epoch {
		return AgentNodeSuccessorBindResult{}, ErrAgentNodeDispatchRequired
	}
	decision := agent.SuccessorBinding(agent.SuccessorInput{
		IdentityValid: true, Lease: "live", Epoch: "match", Effect: input.Effect,
		ResetAdmitted: input.ResetAdmitted, ResetFenceMatches: input.ResetFenceMatches,
		OperationID: intent.OperationID, ActionID: intent.ActionID, ExecutionEpoch: intent.ExecutionEpoch,
		NodeID: intent.NodeID, ResetIndex: input.ResetIndex, ExistingSuccessor: intent.SuccessorOperationID,
	})
	if !decision.Persisted || decision.ReplacedOperationID || decision.OperationID != intent.OperationID {
		code := decision.Code
		if code == "" {
			code = "AGENT_RUNTIME_GENERATION_CONFLICT"
		}
		return AgentNodeSuccessorBindResult{}, &AgentSuccessorError{Code: code}
	}
	replay, applied, err := replayOperatorMutationTx(tx, input.OperationID, requestDigest)
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	if intent.SuccessorOperationID == "" {
		if record.Revision >= math.MaxInt64-1 {
			return AgentNodeSuccessorBindResult{}, ErrRevisionConflict
		}
		intent.SuccessorOperationID = decision.SuccessorOperationID
		intent.ResetIndex = decision.ResetIndex
		nodes[input.NodeID] = intent
		var payload map[string]json.RawMessage
		if json.Unmarshal(record.Payload, &payload) != nil {
			return AgentNodeSuccessorBindResult{}, ErrCorruptRecord
		}
		payload[agentNodeDispatchField], _ = json.Marshal(nodes)
		record.Payload, _ = json.Marshal(payload)
		record.Revision++
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			return AgentNodeSuccessorBindResult{}, err
		}
		write.allowBoundLease, write.agentNodeDispatch, write.agentSuccessorBind = true, true, true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentNodeSuccessorBindResult{}, err
		}
		record = write.record
	}
	if !applied {
		payloadDigest, err := operatorPayloadDigest(record.Payload)
		if err != nil {
			return AgentNodeSuccessorBindResult{}, err
		}
		replay = OperatorMutationResult{Status: OperatorMutationApplied, OperationID: input.OperationID,
			Domain: "action", RecordID: record.ID, Revision: record.Revision, State: record.State,
			ActionID: record.ID, ExecutionEpoch: record.ExecutionEpoch, CutoverRevision: context.cutover.Revision,
			FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest,
			RequestDigest: requestDigest, Actor: input.Actor}
		if err := insertOperatorMutationTx(tx, replay, store.token, context.now); err != nil {
			return AgentNodeSuccessorBindResult{}, err
		}
	}
	_ = replay
	lease, err := readActiveActionLeaseTx(tx, input.ActionID)
	if err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentNodeSuccessorBindResult{}, err
	}
	return AgentNodeSuccessorBindResult{OperationID: intent.OperationID, SuccessorOperationID: intent.SuccessorOperationID,
		ResetIndex: intent.ResetIndex, Replayed: decision.Replayed}, nil
}

type AgentSuccessorEpochRequest struct {
	OperationID       string
	Actor             string
	ActionID          string
	Epoch             uint64
	RunID             string
	NodeID            string
	Owner             string
	ClaimToken        string
	Effect            string
	ResetAdmitted     bool
	ResetFenceMatches bool
	ResetIndex        int64
}

// AdmitSuccessorDispatch opens dispatch on the epoch takeover already wrote.
// It does not choose a larger epoch and does not replace the original operation.
func (store *Control) AdmitSuccessorDispatch(input AgentSuccessorEpochRequest) (AdmissionResult, error) {
	if !ValidRecordID(input.ActionID) || !ValidRecordID(input.RunID) || input.Epoch == 0 || input.Epoch > math.MaxInt64 ||
		input.NodeID == "" || len(input.NodeID) > 64 || strings.TrimSpace(input.Owner) == "" || input.ClaimToken == "" {
		return AdmissionResult{}, ErrAgentNodeDispatchRequired
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(input.OperationID, input.RunID, input.Actor, "agent_run")
	if err != nil {
		return AdmissionResult{}, err
	}
	defer tx.Rollback()
	actionAuthority, err := readCutoverTx(tx, "action")
	if err != nil {
		return AdmissionResult{}, err
	}
	if !IsDomainGoAuthoritative(actionAuthority.State) {
		return AdmissionResult{}, ErrCutoverNotAuthorized
	}
	context.cutover = actionAuthority
	lease, err := store.agentNodeLeaseTx(tx, AgentNodeDispatchRequest{ActionID: input.ActionID, Epoch: input.Epoch, Owner: input.Owner, ClaimToken: input.ClaimToken}, context.now)
	if err != nil {
		return AdmissionResult{}, err
	}
	record, found, err := readAgentExecutionActionTx(tx, input.ActionID)
	if err != nil {
		return AdmissionResult{}, err
	}
	if !found || record.ExecutionEpoch != input.Epoch {
		return AdmissionResult{}, ErrActionNotClaimable
	}
	binding, err := decodeAgentExecution(record)
	if err != nil || binding.RunID != input.RunID {
		return AdmissionResult{}, ErrAgentRunExecutionRequired
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		return AdmissionResult{}, err
	}
	intent, exists := nodes[input.NodeID]
	if !exists {
		return AdmissionResult{}, ErrAgentNodeDispatchRequired
	}
	decision := agent.SuccessorExecution(agent.SuccessorExecutionInput{
		IdentityValid: true, Lease: "live", Epoch: "match", Effect: input.Effect,
		ResetAdmitted: input.ResetAdmitted, ResetFenceMatches: input.ResetFenceMatches,
		OperationID: intent.OperationID, ActionID: intent.ActionID, ExecutionEpoch: intent.ExecutionEpoch,
		NodeID: intent.NodeID, ResetIndex: input.ResetIndex, StoredSuccessor: intent.SuccessorOperationID,
		StoredOperationID: intent.OperationID, AuthorityIssued: record.ExecutionEpoch > intent.ExecutionEpoch,
		WorkerPromoted: false, AuthorityEpoch: record.ExecutionEpoch, PresentedEpoch: input.Epoch,
	})
	if !decision.Execute || decision.Promoted || decision.ExecutionEpoch != record.ExecutionEpoch || decision.RetainedOperationID != intent.OperationID {
		code := decision.Code
		if code == "" {
			code = "AGENT_RUNTIME_SUCCESSOR_HELD"
		}
		return AdmissionResult{}, &AgentSuccessorError{Code: code}
	}
	if record.State == "CLAIMED" || record.State == "EXECUTING" {
		if err := store.finishOperatorMutationTx(tx, operatorMutationContext{cutover: context.cutover, leaseUntil: context.leaseUntil, now: context.now, actionLeaseUntil: lease.LeaseUntil}); err != nil {
			return AdmissionResult{}, err
		}
		return AdmissionResult{Record: record, Lease: lease}, nil
	}
	if record.State != "RECONCILING" || record.Revision >= math.MaxInt64-1 {
		return AdmissionResult{}, ErrActionNotClaimable
	}
	record.Revision++
	record.State = "CLAIMED"
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		return AdmissionResult{}, err
	}
	write.allowBoundLease, write.agentSuccessorEpoch = true, true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AdmissionResult{}, err
	}
	context.actionLeaseUntil = lease.LeaseUntil
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AdmissionResult{}, err
	}
	return AdmissionResult{Record: write.record, Lease: lease}, nil
}

func nativeAgentSuccessorBindEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != before.Domain || before.ID != after.ID ||
		before.State != "EXECUTING" || after.State != "EXECUTING" ||
		after.Revision != before.Revision+1 || after.ExecutionEpoch != before.ExecutionEpoch {
		return false
	}
	oldNodes, err := readAgentNodeDispatches(before)
	newNodes, otherErr := readAgentNodeDispatches(after)
	if err != nil || otherErr != nil || len(oldNodes) == 0 || len(oldNodes) != len(newNodes) {
		return false
	}
	changed := 0
	for id, previous := range oldNodes {
		current, ok := newNodes[id]
		if !ok || current.OperationID != previous.OperationID {
			return false
		}
		strippedPrevious, strippedCurrent := previous, current
		strippedPrevious.SuccessorOperationID, strippedPrevious.ResetIndex = "", 0
		strippedCurrent.SuccessorOperationID, strippedCurrent.ResetIndex = "", 0
		left, leftErr := json.Marshal(strippedPrevious)
		right, rightErr := json.Marshal(strippedCurrent)
		if leftErr != nil || rightErr != nil || !bytes.Equal(left, right) {
			return false
		}
		if current.SuccessorOperationID == previous.SuccessorOperationID && current.ResetIndex == previous.ResetIndex {
			continue
		}
		if previous.SuccessorOperationID != "" || current.SuccessorOperationID == "" || current.SuccessorOperationID == current.OperationID {
			return false
		}
		changed++
	}
	return changed == 1
}

func nativeAgentSuccessorEpochEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != before.Domain || before.ID != after.ID ||
		before.State != "RECONCILING" || after.State != "CLAIMED" ||
		after.Revision != before.Revision+1 || after.ExecutionEpoch != before.ExecutionEpoch ||
		after.ExecutionEpoch == 0 || !bytes.Equal(before.Payload, after.Payload) {
		return false
	}
	nodes, err := readAgentNodeDispatches(after)
	if err != nil {
		return false
	}
	for _, node := range nodes {
		if node.SuccessorOperationID != "" && node.ExecutionEpoch < after.ExecutionEpoch && node.OperationID != node.SuccessorOperationID {
			return true
		}
	}
	return false
}

// An absent control intent is not a NOT_APPLIED remote effect proof.
func (store *Control) AgentNodeDispatch(actionID, nodeID string) (AgentNodeDispatch, bool, error) {
	_, intent, found, err := store.AgentNodeDispatchSnapshot(actionID, nodeID)
	return intent, found, err
}

// The record and intent come from one authenticated history snapshot. A query
// must not check an old action epoch and then read nodes from a newer record.
func (store *Control) AgentNodeDispatchSnapshot(actionID, nodeID string) (Record, AgentNodeDispatch, bool, error) {
	record, found, err := store.Get("action", actionID)
	if err != nil || !found {
		return record, AgentNodeDispatch{}, false, err
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		return record, AgentNodeDispatch{}, false, err
	}
	intent, found := nodes[nodeID]
	return record, intent, found, nil
}
