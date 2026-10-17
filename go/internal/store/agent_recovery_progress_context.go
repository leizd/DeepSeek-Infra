package store

import (
	"database/sql"
	"math"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

type AgentRecoveryProgressExpected struct {
	RunID           string                        `json:"runId"`
	Fence           *internalprotocol.ActionFence `json:"fence"`
	ExpectedIndex   int64                         `json:"expectedIndex"`
	RuntimeRevision uint64                        `json:"runtimeRevision"`
	Request         agent.ArtifactRef             `json:"request"`
}

func validRecoveryProgressExpected(input AgentRecoveryProgressExpected) bool {
	return ValidRecordID(input.RunID) && input.ExpectedIndex > 0 && input.ExpectedIndex < math.MaxInt64-2 &&
		input.RuntimeRevision >= 2 && input.RuntimeRevision <= math.MaxInt64 && validAgentArtifact(input.Request)
}

// Only a current progress admission needs a free run reservation. Historical
// receipt verification must still work while a later synthesis holds that run.
func agentRecoveryProgressUnreservedTx(tx *sql.Tx, runID string) error {
	var reserved bool
	if err := tx.QueryRow(`SELECT EXISTS(SELECT 1 FROM action_resource_leases WHERE resource_key=?)`, "agent-run:"+runID).Scan(&reserved); err != nil {
		return err
	}
	if reserved {
		return ErrAgentRunRecoveryUnsettled
	}
	return nil
}

func recoveryTaskProgressRequired(run agent.RunMetadata) bool {
	if run.Recovery == nil || run.Recovery.Metadata == nil {
		return false
	}
	context := run.Recovery.Metadata
	return context.Selection.GetKind() == agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN &&
		len(context.Execute) == 1 && context.Execute[0] != nil && context.Execute[0].Id == context.Selection.AgentId && !context.CompleteImmediately
}

func (store *Control) agentRecoveryProgressContextTx(tx *sql.Tx, run agent.RunMetadata, epoch uint64) (*agentv1.AgentRecoveryProgressContext, error) {
	if run.SchemaVersion != 3 || run.Status != "running" || run.CancellationActive() || !validAgentRecoveryScope(run) || run.RecoveryProgress != nil ||
		!recoveryTaskProgressRequired(run) || !completedAgentTasks(run) || epoch == 0 || epoch > math.MaxInt64 {
		return nil, ErrAgentRunRecoveryRequired
	}
	if _, err := store.verifiedAgentRecoveryOutputsTx(tx, run); err != nil {
		return nil, err
	}
	intent := AgentRunExecutionBinding{SchemaVersion: 1, RunID: run.RunID, Phase: "synthesis", MetadataIndex: run.NextIndex,
		MetadataEpoch: epoch, Request: run.Request, PlanDigest: executionPlanDigest(run)}
	// This validates the selected task's own typed termination. It does not
	// bypass the separate synthesis gate, which still requires this progress.
	source, err := store.verifiedSameRequestSynthesisSourceTx(tx, intent, true)
	if err != nil {
		return nil, err
	}
	nodes, err := readAgentNodeDispatches(source)
	if err != nil {
		return nil, err
	}
	outcomes, err := store.agentNodeOutcomes(source, nodes)
	if err != nil {
		return nil, err
	}
	markers, err := store.validatedAgentTaskSettlementsTx(tx, source)
	if err != nil {
		return nil, err
	}
	role := run.Recovery.Metadata.Selection.AgentId
	node, nodePresent := nodes[role]
	outcome, outcomePresent := outcomes[role]
	marker, markerPresent := markers[role]
	if !nodePresent || !outcomePresent || !markerPresent || len(nodes) != 1 || len(markers) != 1 {
		return nil, ErrAgentNodeDependencyUnverified
	}
	var receipt agentv1.AgentNodeOutcomeReceipt
	if proto.Unmarshal(outcome.Receipt, &receipt) != nil {
		return nil, ErrCorruptRecord
	}
	return &agentv1.AgentRecoveryProgressContext{SchemaVersion: 1, RunId: run.RunID, Generation: run.Recovery.Generation,
		MetadataEpoch: epoch, ExpectedIndex: run.NextIndex, RuntimeRevision: run.RuntimeVersion(), Request: recoveryArtifact(run.Request),
		RecoveryContextSha256: run.Recovery.ContextSHA256, TaskActionId: source.ID, TaskActionRevision: source.Revision,
		TaskMetadataIndex: run.NextIndex, Role: role, Synthesize: run.Recovery.Metadata.Synthesize,
		Outputs: []*agentv1.AgentRunRecoveryEffectSource{{Dispatch: agentOutcomeIntent(node), Outcome: &receipt, SettlementMetadataIndex: marker.MetadataIndex}}}, nil
}

func (store *Control) PreviewAgentRecoveryProgress(input AgentRecoveryProgressExpected) (*agentv1.AgentRecoveryProgressContext, error) {
	if !validRecoveryProgressExpected(input) {
		return nil, ErrAgentRunMetadataInvalid
	}
	if input.Fence != nil {
		input.Fence = &internalprotocol.ActionFence{ActionId: input.Fence.ActionId, ExecutionEpoch: input.Fence.ExecutionEpoch}
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, authority, err := store.beginOperatorMutationTx("agent-recovery-progress-preview", input.RunID, "operator:agent-recovery-progress-preview", "agent_run")
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if input.Fence == nil || input.Fence.ActionId != input.RunID {
		return nil, internalprotocol.ErrFenceMismatch
	}
	if err := internalprotocol.AdmitCommand(input.Fence, uint64(authority.cutover.Epoch)); err != nil {
		return nil, err
	}
	record, found, err := readAgentRecordTx(tx, input.RunID)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, ErrEmptyRecordID
	}
	run, err := decodeAgentMetadata(record)
	if err != nil {
		return nil, err
	}
	if run.NextIndex != input.ExpectedIndex || run.RuntimeVersion() != input.RuntimeRevision || run.Request != input.Request {
		return nil, ErrRevisionConflict
	}
	if err := agentRecoveryProgressUnreservedTx(tx, run.RunID); err != nil {
		return nil, err
	}
	return store.agentRecoveryProgressContextTx(tx, run, uint64(authority.cutover.Epoch))
}

func recoveryProgressHeaders(context *agentv1.AgentRecoveryProgressContext) []agent.RunEvent {
	events := []agent.RunEvent{{Type: "agent_note", Phase: context.Role}}
	if context.Synthesize {
		return append(events, agent.RunEvent{Type: "final_reset", Scope: "final_answer"})
	}
	return append(events, agent.RunEvent{Type: "run_status", Status: "done"})
}

func (store *Control) verifyRecoveryProgressReceipt(receipt *agentv1.AgentRecoveryProgressReceipt, context *agentv1.AgentRecoveryProgressContext) error {
	return agentproof.VerifyAgentRecoveryProgress(receipt, context, agentproof.Binding{PublicKey: store.agentOutcomeSignerKey, FleetID: store.fleetID, Environment: store.environment})
}
