package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"math"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

type AgentRunRecoveryExpected struct {
	RunID                   string                             `json:"runId"`
	Fence                   *internalprotocol.ActionFence      `json:"fence"`
	ExpectedIndex           int64                              `json:"expectedIndex"`
	ExpectedRuntimeRevision uint64                             `json:"expectedRuntimeRevision"`
	ExpectedRequest         agent.ArtifactRef                  `json:"expectedRequest"`
	Selection               *agentv1.AgentRunRecoverySelection `json:"selection"`
}

type AgentRunRecoveryPreview struct {
	Context       *agentv1.AgentRunRecoveryContext
	ContextSHA256 string
}

func agentRecoveryContextDigest(context *agentv1.AgentRunRecoveryContext) (string, error) {
	if context == nil || len(context.ProtoReflect().GetUnknown()) != 0 {
		return "", ErrAgentRunMetadataInvalid
	}
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(context)
	if err != nil || len(encoded) == 0 || len(encoded) > 1<<20 {
		return "", ErrAgentRunMetadataInvalid
	}
	digest := sha256.Sum256(encoded)
	return hex.EncodeToString(digest[:]), nil
}

func recoveryArtifact(ref agent.ArtifactRef) *agentv1.AgentArtifactReference {
	return &agentv1.AgentArtifactReference{Sha256: ref.SHA256, Length: ref.Length}
}

func recoveryScopeProtocol(scope *agent.RunRecoveryScope) *agentv1.AgentRunRecoveryScope {
	if scope == nil {
		return nil
	}
	return &agentv1.AgentRunRecoveryScope{Generation: scope.Generation, Context: recoveryArtifact(scope.Context),
		ContextSha256: scope.ContextSHA256, StartIndex: scope.StartIndex}
}

func validAgentRecoveryScope(run agent.RunMetadata) bool {
	scope := run.Recovery
	if scope == nil || scope.Generation == 0 || scope.Generation > math.MaxInt64 || scope.StartIndex < 1 ||
		scope.StartIndex >= run.NextIndex || !validAgentArtifact(scope.Context) || scope.Metadata == nil ||
		run.CancellationGeneration > scope.Generation || !run.Cancelled && run.CancellationGeneration != 0 {
		return false
	}
	context := scope.Metadata
	if context.SchemaVersion != 1 || context.RunId != run.RunID || context.Generation != scope.Generation ||
		context.ExpectedIndex != scope.StartIndex || context.ExpectedRuntimeRevision == 0 || context.ExpectedRuntimeRevision >= math.MaxInt64 ||
		run.RuntimeVersion() != context.ExpectedRuntimeRevision+1 || context.MetadataEpoch == 0 || context.MetadataEpoch > run.LastEvent.ExecutionEpoch ||
		context.ExpectedRequest == nil || !validAgentArtifact(agent.ArtifactRef{SHA256: context.ExpectedRequest.Sha256, Length: context.ExpectedRequest.Length}) ||
		proto.Equal(context.ExpectedRequest, recoveryArtifact(run.Request)) {
		return false
	}
	if scope.Generation == 1 {
		if context.PreviousScope != nil {
			return false
		}
	} else if context.PreviousScope == nil || context.PreviousScope.Generation != scope.Generation-1 ||
		context.PreviousScope.StartIndex >= scope.StartIndex || context.PreviousScope.StartIndex < 1 ||
		!agentproof.ValidNodeOutcomeDigest(context.PreviousScope.ContextSha256) ||
		context.PreviousScope.Context == nil || !validAgentArtifact(agent.ArtifactRef{
		SHA256: context.PreviousScope.Context.Sha256, Length: context.PreviousScope.Context.Length}) {
		return false
	}
	digest, err := agentRecoveryContextDigest(context)
	return err == nil && digest == scope.ContextSHA256
}

func normalizeAgentRecoverySelection(input *agentv1.AgentRunRecoverySelection) (*agentv1.AgentRunRecoverySelection, agent.RunRecoveryRequest, error) {
	if input == nil || len(input.ProtoReflect().GetUnknown()) != 0 {
		return nil, agent.RunRecoveryRequest{}, agent.ErrInvalidRunRecovery
	}
	selection := proto.Clone(input).(*agentv1.AgentRunRecoverySelection)
	selection.AgentId = strings.TrimSpace(selection.AgentId)
	request := agent.RunRecoveryRequest{AgentID: selection.AgentId, Resynthesize: selection.Resynthesize == nil || selection.GetResynthesize()}
	switch selection.Kind {
	case agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME:
		if selection.AgentId != "" {
			return nil, request, agent.ErrInvalidRunRecovery
		}
		request.Mode = "resume"
		selection.Resynthesize = nil
	case agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN:
		request.Mode = "rerun"
		selection.Resynthesize = proto.Bool(request.Resynthesize)
	default:
		return nil, request, agent.ErrInvalidRunRecovery
	}
	return selection, request, nil
}

func validAgentRecoveryExpected(input AgentRunRecoveryExpected) bool {
	return ValidRecordID(input.RunID) && input.ExpectedIndex >= 1 && input.ExpectedIndex < math.MaxInt64-64 &&
		input.ExpectedRuntimeRevision > 0 && input.ExpectedRuntimeRevision < math.MaxInt64 && validAgentArtifact(input.ExpectedRequest)
}

func recoveryPlanProtocol(plan []agent.RunPlanNode) []*agentv1.AgentRunPlanNode {
	result := make([]*agentv1.AgentRunPlanNode, 0, len(plan))
	for _, node := range plan {
		result = append(result, &agentv1.AgentRunPlanNode{Id: node.ID, Task: node.Task, DependsOn: append([]string{}, node.DependsOn...)})
	}
	return result
}

// Inspect the latest accepted public reset as well as original signed outputs.
// A reset removed that role's old output in the retained reducer; custody alone
// must not silently put the removed output back into a later recovery.
func recoveryOutputResetTx(tx *sql.Tx, runID, role string) (int64, error) {
	var reset int64
	err := tx.QueryRow(`SELECT COALESCE(MAX(json_extract(payload_json,'$.lastEvent.index')),-1)
        FROM control_events WHERE domain='agent_run' AND record_id=?
        AND json_extract(payload_json,'$.lastEvent.type')='agent_reset'
        AND json_extract(payload_json,'$.lastEvent.phase')=?`, runID, role).Scan(&reset)
	return reset, err
}

func recoveryFinalPresentTx(tx *sql.Tx, run agent.RunMetadata, history agentRecoveryHistory) (bool, error) {
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,
        payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events
        WHERE domain='agent_run' AND record_id=? AND json_extract(payload_json,'$.lastEvent.type')='content'
        AND json_extract(payload_json,'$.lastEvent.index')>? ORDER BY revision DESC LIMIT 1`, run.RunID, run.FinalAfter), "agent_run")
	if err != nil || !found {
		return false, err
	}
	latest, err := decodeAgentMetadata(record)
	if err != nil {
		return false, err
	}
	for _, action := range history.synthesis {
		binding, err := decodeAgentExecution(action)
		if err != nil {
			return false, err
		}
		if binding.MetadataIndex != latest.LastEvent.Index {
			continue
		}
		nodes, err := readAgentNodeDispatches(action)
		if err != nil {
			return false, err
		}
		outcomes, err := agentNodeOutcomeBytes(action)
		if err != nil || nodes["synthesizer"].NodeID != "synthesizer" {
			return false, ErrCorruptRecord
		}
		var receipt agentv1.AgentNodeOutcomeReceipt
		if proto.Unmarshal(outcomes["synthesizer"].Receipt, &receipt) != nil || receipt.Evidence == nil || receipt.Evidence.PublicEvent == nil {
			return false, ErrCorruptRecord
		}
		return latest.LastEvent.Body == (agent.ArtifactRef{SHA256: receipt.Evidence.PublicEvent.Sha256, Length: receipt.Evidence.PublicEvent.Length}), nil
	}
	return false, nil
}

func (store *Control) agentRecoveryContextTx(tx *sql.Tx, record Record, input AgentRunRecoveryExpected) (AgentRunRecoveryPreview, error) {
	run, err := decodeAgentMetadata(record)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	if run.NextIndex != input.ExpectedIndex || run.RuntimeVersion() != input.ExpectedRuntimeRevision || run.Request != input.ExpectedRequest {
		return AgentRunRecoveryPreview{}, ErrRevisionConflict
	}
	selection, request, err := normalizeAgentRecoverySelection(input.Selection)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	if run.Status == "created" || run.Status == "planning" || run.Status == "running" || request.Mode == "resume" && run.Status == "awaiting_plan" {
		return AgentRunRecoveryPreview{}, ErrIllegalTransition
	}
	// Recheck every original effect, including effects from earlier generations.
	// The previous scope only bounds the new custody context, not this safety audit.
	history, err := store.agentRecoveryHistoryTx(tx, run, 0)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	checkpoint := agent.RunRecoveryCheckpoint{Plan: run.Plan, NodeStates: map[string]string{}, Outputs: []agent.RunRecoveryOutput{}}
	for id, node := range run.Nodes() {
		checkpoint.NodeStates[id] = node.State
	}
	available := map[string]*agentv1.AgentRunRecoveryEffectSource{}
	for _, id := range history.outputIDs {
		source := history.outputs[id]
		reset, err := recoveryOutputResetTx(tx, run.RunID, id)
		if err != nil {
			return AgentRunRecoveryPreview{}, err
		}
		if source.SettlementMetadataIndex <= reset {
			continue
		}
		if source.Outcome.GetEvidence().GetTaskOutput() == nil {
			return AgentRunRecoveryPreview{}, ErrCorruptRecord
		}
		available[id] = source
		checkpoint.Outputs = append(checkpoint.Outputs, agent.RunRecoveryOutput{ID: id, Failed: source.Outcome.Evidence.TaskOutput.Failed})
	}
	checkpoint.FinalAnswerPresent, err = recoveryFinalPresentTx(tx, run, history)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	decision, err := agent.PlanRunRecovery(request, checkpoint)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	context := &agentv1.AgentRunRecoveryContext{SchemaVersion: 1, RunId: run.RunID, Generation: 1,
		MetadataEpoch: input.Fence.GetExecutionEpoch(), ExpectedIndex: run.NextIndex, ExpectedRuntimeRevision: run.RuntimeVersion(),
		ExpectedRequest: recoveryArtifact(run.Request), PreviousScope: recoveryScopeProtocol(run.Recovery), Selection: selection,
		Plan: recoveryPlanProtocol(decision.Plan), Execute: recoveryPlanProtocol(decision.Execute), CompletedIds: decision.CompletedIDs,
		RetainedIds: decision.RetainedIDs, PriorIds: decision.PriorIDs, ResetIds: decision.ResetIDs, SynthesisOutputIds: decision.SynthesisOutputIDs,
		Synthesize: decision.Synthesize, ResetFinal: decision.ResetFinal, CompleteImmediately: decision.CompleteImmediately,
		PriorEffects: []*agentv1.AgentRunRecoveryEffectSource{}, RetainedOutputs: []*agentv1.AgentRunRecoveryEffectSource{}}
	var previousStart int64
	if run.Recovery != nil {
		if run.Recovery.Generation >= math.MaxInt64 {
			return AgentRunRecoveryPreview{}, ErrRevisionConflict
		}
		context.Generation = run.Recovery.Generation + 1
		previousStart = run.Recovery.StartIndex
	}
	for _, source := range history.effects {
		if source.Dispatch.MetadataIndex >= previousStart {
			context.PriorEffects = append(context.PriorEffects, source)
		}
	}
	for _, id := range decision.RetainedIDs {
		if available[id] == nil {
			return AgentRunRecoveryPreview{}, ErrCorruptRecord
		}
		context.RetainedOutputs = append(context.RetainedOutputs, available[id])
	}
	digest, err := agentRecoveryContextDigest(context)
	return AgentRunRecoveryPreview{Context: context, ContextSHA256: digest}, err
}

// Preview seals nothing and neither releases old resources nor advances a cursor.
// Commit must repeat this complete audit inside its own live authority transaction.
func (store *Control) PreviewAgentRunRecovery(input AgentRunRecoveryExpected) (AgentRunRecoveryPreview, error) {
	if !validAgentRecoveryExpected(input) {
		return AgentRunRecoveryPreview{}, ErrAgentRunMetadataInvalid
	}
	selection, _, err := normalizeAgentRecoverySelection(input.Selection)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	input.Selection = selection
	encoded, err := json.Marshal(input)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	var expected AgentRunRecoveryExpected
	if json.Unmarshal(encoded, &expected) != nil {
		return AgentRunRecoveryPreview{}, ErrAgentRunMetadataInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	// Reuse the existing authority checks, but roll this read-only transaction
	// back: no operation receipt or writer-lease update is made for a preview.
	tx, authority, err := store.beginOperatorMutationTx("agent-recovery-preview", expected.RunID, "operator:agent-recovery-preview", "agent_run")
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	defer tx.Rollback()
	if expected.Fence == nil || expected.Fence.ActionId != expected.RunID {
		return AgentRunRecoveryPreview{}, internalprotocol.ErrFenceMismatch
	}
	if err := internalprotocol.AdmitCommand(expected.Fence, uint64(authority.cutover.Epoch)); err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	record, found, err := readAgentRecordTx(tx, expected.RunID)
	if err != nil {
		return AgentRunRecoveryPreview{}, err
	}
	if !found {
		return AgentRunRecoveryPreview{}, ErrEmptyRecordID
	}
	return store.agentRecoveryContextTx(tx, record, expected)
}
