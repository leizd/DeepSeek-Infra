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
	SchemaVersion  uint32            `json:"schemaVersion"`
	OperationID    string            `json:"operationId"`
	ActionID       string            `json:"actionId"`
	ExecutionEpoch uint64            `json:"executionEpoch"`
	RunID          string            `json:"runId"`
	NodeID         string            `json:"nodeId"`
	Phase          string            `json:"phase"`
	Task           string            `json:"task"`
	DependsOn      []string          `json:"dependsOn"`
	Request        agent.ArtifactRef `json:"requestRef"`
	PlanDigest     string            `json:"planDigest"`
	MetadataIndex  int64             `json:"metadataIndex"`
	MetadataEpoch  uint64            `json:"metadataEpoch"`
	State          string            `json:"state"`
	RecordedAt     int64             `json:"recordedAt"`
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
			!ValidRecordID(node.OperationID) || node.OperationID != agentNodeOperationID(node) {
			return nil, ErrCorruptRecord
		}
	}
	return nodes, nil
}

func agentNodeOperationID(node AgentNodeDispatch) string {
	node.OperationID, node.State, node.RecordedAt = "", "", 0
	encoded, _ := json.Marshal(node)
	digest := sha256.Sum256(append([]byte("agent-node-dispatch-v1\n"), encoded...))
	return "agent-node-" + hex.EncodeToString(digest[:])
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
	return AgentNodeDispatchResult{Fresh: !exists, Intent: intent, Mutation: replay,
		Admission: AdmissionResult{Record: record, Lease: lease}, WriterLeaseUntil: store.leaseUntil}, nil
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
