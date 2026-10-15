package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func recoveryExpectedFromRun(t *testing.T, control *Control, runID string, selection *agentv1.AgentRunRecoverySelection) AgentRunRecoveryExpected {
	t.Helper()
	run, found, err := control.AgentRunMetadata(runID)
	if err != nil || !found {
		t.Fatal(err)
	}
	authority, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	return AgentRunRecoveryExpected{RunID: runID, Fence: &internalprotocol.ActionFence{ActionId: runID, ExecutionEpoch: uint64(authority.Epoch)},
		ExpectedIndex: run.NextIndex, ExpectedRuntimeRevision: run.RuntimeVersion(), ExpectedRequest: run.Request, Selection: selection}
}

func recoveryInputFromPreview(t *testing.T, control *Control, expected AgentRunRecoveryExpected) (CommitAgentRunRecoveryRequest, AgentRunRecoveryPreview) {
	t.Helper()
	preview, err := control.PreviewAgentRunRecovery(expected)
	if err != nil {
		t.Fatal(err)
	}
	input := CommitAgentRunRecoveryRequest{AgentRunRecoveryExpected: expected, OperationID: "recover-known-sources", Actor: "operator:recovery-test",
		ContextSHA256: preview.ContextSHA256, Context: agent.ArtifactRef{SHA256: strings.Repeat("e", 64), Length: 512},
		Request: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 130}}
	for index := range agentRecoveryEvents(preview) {
		input.EventBodies = append(input.EventBodies, agent.ArtifactRef{SHA256: strings.Repeat(fmt.Sprintf("%x", index+1), 64), Length: 40})
	}
	return input, preview
}

func recoveryCompletedRun(t *testing.T) (*Control, string) {
	t.Helper()
	control, input, _ := synthesisSettlementFixture(t)
	if _, err := control.SettleAgentSynthesis(input); err != nil {
		t.Fatal(err)
	}
	return control, input.RunID
}

func TestRecoveryRetainsOriginalSignedOutputsAfterConfirmedTaskCompletionAndCancellation(t *testing.T) {
	control, complete := tasksCompletionFixture(t, false)
	if _, err := control.CompleteAgentTasks(complete); err != nil {
		t.Fatal(err)
	}
	run, _, err := control.AgentRunMetadata(complete.RunID)
	if err != nil {
		t.Fatal(err)
	}
	cancel := eventMutation(t, control, run.NextIndex, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	cancel.Create = nil
	if _, err := control.AppendAgentRunEvent(cancel); err != nil {
		t.Fatal(err)
	}
	expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME})
	input, preview := recoveryInputFromPreview(t, control, expected)
	if len(preview.Context.Execute) != 0 || len(preview.Context.RetainedOutputs) != 2 || len(preview.Context.PriorEffects) != 2 ||
		!preview.Context.Synthesize || !preview.Context.ResetFinal || preview.Context.CompleteImmediately {
		t.Fatal("completed tasks were rerun or synthesis was fabricated", preview.Context)
	}
	for _, source := range preview.Context.RetainedOutputs {
		if !proto.Equal(source.Dispatch.Request, recoveryArtifact(run.Request)) ||
			!proto.Equal(source.Outcome.Evidence.Fence, source.Dispatch.Fence) ||
			source.SettlementMetadataIndex < 1 || source.SettlementMetadataIndex >= expected.ExpectedIndex ||
			source.Outcome.Evidence.TaskOutput.Failed {
			t.Fatal("retained output lost its original immutable provenance")
		}
	}
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil || result.Run.CancellationActive() || !result.Run.Cancelled || len(result.Events) != 2 ||
		result.Run.Nodes()["coder"].State != "succeeded" || result.Run.Nodes()["critic"].State != "succeeded" {
		t.Fatal("recovery changed verified completed nodes", result, err)
	}
}

func TestResumeWithVerifiedFinalAnswerCommitsNoNewExecutionOrFinalReset(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	expected := recoveryExpectedFromRun(t, control, runID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME})
	input, preview := recoveryInputFromPreview(t, control, expected)
	if !preview.Context.CompleteImmediately || preview.Context.Synthesize || preview.Context.ResetFinal ||
		len(preview.Context.Execute) != 0 || len(preview.Context.PriorEffects) != 3 || len(preview.Context.RetainedOutputs) != 2 {
		t.Fatal("verified completed run was scheduled again", preview.Context)
	}
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil || result.Run.Status != "done" || result.Run.FinalAfter != -1 || len(result.Events) != 2 || result.Events[1].Status != "done" {
		t.Fatal("completed resume lost its retained final answer", result, err)
	}
}

func TestSingleAgentRerunRetainsTheRealFinalAndUnselectedOriginalOutputs(t *testing.T) {
	for _, selected := range []string{"coder", "researcher"} {
		t.Run(selected, func(t *testing.T) {
			control, runID := recoveryCompletedRun(t)
			expected := recoveryExpectedFromRun(t, control, runID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN, AgentId: selected})
			input, preview := recoveryInputFromPreview(t, control, expected)
			if len(preview.Context.Execute) != 1 || preview.Context.Execute[0].Id != selected || !preview.Context.Synthesize ||
				len(preview.Context.PriorEffects) != 3 || len(preview.Context.Plan) != 2 || len(input.EventBodies) != 2 {
				t.Fatal("rerun changed its single selection or omitted old effects", preview.Context)
			}
			for _, source := range preview.Context.RetainedOutputs {
				if source.Dispatch.NodeId == selected {
					t.Fatal("selected old output was incorrectly reused")
				}
			}
			if selected == "researcher" && (preview.Context.Execute[0].Task != "重新运行该 Agent 并给出公开摘要" ||
				len(preview.Context.PriorIds) != 2 || len(preview.Context.SynthesisOutputIds) != 3) {
				t.Fatal("known role outside the public plan lost retained behavior")
			}
			result, err := control.CommitAgentRunRecovery(input)
			if err != nil || result.Run.FinalAfter != -1 || len(result.Run.Plan) != 2 || result.Events[1].Type != "agent_reset" {
				t.Fatal("rerun erased the real old final before its task completed", result, err)
			}
		})
	}
}

func TestSynthesisAliasesReuseVerifiedOutputsEvenWhenResynthesizeIsExplicitlyFalse(t *testing.T) {
	for _, id := range []string{"synth", "leader", "synthesizer"} {
		t.Run(id, func(t *testing.T) {
			control, runID := recoveryCompletedRun(t)
			expected := recoveryExpectedFromRun(t, control, runID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN,
				AgentId: id, Resynthesize: proto.Bool(false)})
			input, preview := recoveryInputFromPreview(t, control, expected)
			if !preview.Context.Synthesize || !preview.Context.ResetFinal || preview.Context.CompleteImmediately ||
				len(preview.Context.Execute) != 0 || len(preview.Context.RetainedOutputs) != 2 {
				t.Fatal("synthesis alias inherited the single-task resynthesize flag", preview.Context)
			}
			result, err := control.CommitAgentRunRecovery(input)
			if err != nil || len(result.Events) != 2 || result.Events[1].Type != "final_reset" || result.Run.Status != "running" {
				t.Fatal("direct synthesis did not reset immediately", result, err)
			}
		})
	}
}

func TestRecoveryCannotRestoreASignedOutputRemovedByALaterPublicReset(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	run, _, err := control.AgentRunMetadata(runID)
	if err != nil {
		t.Fatal(err)
	}
	for _, event := range []agent.RunEvent{{Type: "run_status", Status: "running"}, {Type: "agent_reset", Phase: "coder"}, {Type: "run_status", Status: "failed"}} {
		mutation := eventMutation(t, control, run.NextIndex, event)
		mutation.Create = nil
		result, err := control.AppendAgentRunEvent(mutation)
		if err != nil {
			t.Fatal(err)
		}
		run = result.Run
	}
	expected := recoveryExpectedFromRun(t, control, runID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME})
	preview, err := control.PreviewAgentRunRecovery(expected)
	if err != nil || len(preview.Context.Execute) != 1 || preview.Context.Execute[0].Id != "coder" ||
		len(preview.Context.RetainedOutputs) != 1 || preview.Context.RetainedOutputs[0].Dispatch.NodeId != "critic" {
		t.Fatal("custody resurrected an output removed from the public checkpoint", preview, err)
	}
}

func TestRecoveryGenerationAndCancellationChainSurviveReopeningTheControlDatabase(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	first, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	cancel := eventMutation(t, control, first.Run.NextIndex, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	cancel.Create = nil
	stopped, err := control.AppendAgentRunEvent(cancel)
	if err != nil || !stopped.Run.CancellationActive() || stopped.Run.CancellationGeneration != 1 {
		t.Fatal("current scope cancellation was lost", err)
	}
	expected := recoveryExpectedFromRun(t, control, input.RunID, input.Selection)
	secondInput, preview := recoveryInputFromPreview(t, control, expected)
	if preview.Context.Generation != 2 || !proto.Equal(preview.Context.PreviousScope, recoveryScopeProtocol(first.Run.Recovery)) {
		t.Fatal("second scope lost its predecessor binding")
	}
	secondInput.OperationID, secondInput.Request.SHA256, secondInput.Context.SHA256 = "recover-second-generation", strings.Repeat("f", 64), strings.Repeat("9", 64)
	second, err := control.CommitAgentRunRecovery(secondInput)
	if err != nil || second.Run.RuntimeVersion() != 4 || second.Run.CancellationActive() || !second.Run.Cancelled || second.Run.CancellationGeneration != 1 {
		t.Fatal("second recovery erased cancellation or reused a request revision", second, err)
	}
	options := OpenOptions{Path: control.path, Owner: "recovery-successor", Now: control.now, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: control.agentOutcomeSignerKey,
		FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	after, found, err := reopened.AgentRunMetadata(input.RunID)
	if err != nil || !found || after.Recovery.Generation != 2 || after.RuntimeVersion() != 4 || after.CancellationActive() || !after.Cancelled ||
		!proto.Equal(after.Recovery.Metadata, second.Run.Recovery.Metadata) {
		t.Fatal("reopened scope lost immutable lineage", err)
	}
	count := countControlEvents(t, reopened)
	replay, err := reopened.CommitAgentRunRecovery(secondInput)
	if err != nil || replay.Mutation.Status != OperatorMutationAlreadyApplied || countControlEvents(t, reopened) != count {
		t.Fatal("writer takeover repeated recovery admission", err)
	}
}

func TestRecoveryPreviewCannotTreatAnExpiredReservationAsNoEffect(t *testing.T) {
	control, confirmation := runtimeUpdateFixture(t, true)
	expected := recoveryExpectedFromRun(t, control, confirmation.RunID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN, AgentId: "coder"})
	now := control.now()
	control.now = func() int64 { return now + 2 }
	count := countControlEvents(t, control)
	if _, err := control.PreviewAgentRunRecovery(expected); !errors.Is(err, ErrAgentRunRecoveryUnsettled) || countControlEvents(t, control) != count {
		t.Fatal("expired reservation authorized a fresh provider request", err)
	}
}

func TestRecoveryScopeMetadataRejectsAReusedContextObject(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	first, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	stop := eventMutation(t, control, first.Run.NextIndex, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	stop.Create = nil
	if _, err := control.AppendAgentRunEvent(stop); err != nil {
		t.Fatal(err)
	}
	expected := recoveryExpectedFromRun(t, control, input.RunID, input.Selection)
	next, _ := recoveryInputFromPreview(t, control, expected)
	next.OperationID, next.Request.SHA256 = "reused-context", strings.Repeat("f", 64)
	count := countControlEvents(t, control)
	if _, err := control.CommitAgentRunRecovery(next); !errors.Is(err, ErrAgentRunMetadataInvalid) || countControlEvents(t, control) != count {
		t.Fatal("a new context reused old immutable custody", err)
	}
}

func TestRecoveryHistoryNeverPromotesATaskProjectionWithoutSignedNativeCustody(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	run, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	for _, event := range []agent.RunEvent{{Type: "run_status", Status: "running"}, {Type: "agent_output", Phase: "coder"}, {Type: "run_status", Status: "failed"}} {
		mutation := eventMutation(t, control, run.NextIndex, event)
		mutation.Create = nil
		result, err := control.AppendAgentRunEvent(mutation)
		if err != nil {
			t.Fatal(err)
		}
		run = result.Run
	}
	expected := recoveryExpectedFromRun(t, control, input.RunID, input.Selection)
	count := countControlEvents(t, control)
	if _, err := control.PreviewAgentRunRecovery(expected); !errors.Is(err, agent.ErrRunRecoveryCheckpoint) || countControlEvents(t, control) != count {
		t.Fatal("public succeeded projection substituted for verified custody", err)
	}
}

func TestRecoveryHistoryRejectsClearingCurrentCancellationWithoutANewScope(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	first, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	stop := eventMutation(t, control, first.Run.NextIndex, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	stop.Create = nil
	result, err := control.AppendAgentRunEvent(stop)
	if err != nil {
		t.Fatal(err)
	}
	beforePayload, _ := json.Marshal(result.Run)
	before := Record{Domain: "agent_run", ID: input.RunID, State: result.Run.Status, Revision: result.Run.NextIndex,
		ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch, Payload: beforePayload}
	afterRun := result.Run
	afterRun.ApplyRunEvent(agent.RunEvent{RunID: input.RunID, ActionID: input.RunID, ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch,
		Type: "run_status", Status: "running", Index: result.Run.NextIndex, Body: stop.Event.Body})
	afterRun.CancellationGeneration = 0
	afterPayload, _ := json.Marshal(afterRun)
	after := Record{Domain: "agent_run", ID: input.RunID, State: afterRun.Status, Revision: afterRun.NextIndex,
		ExecutionEpoch: afterRun.LastEvent.ExecutionEpoch, Payload: afterPayload}
	if err := validateAgentRecoveryMetadataEdge(before, after); !errors.Is(err, ErrAgentRunRecoveryRequired) {
		t.Fatal("ordinary status append cleared current cancellation", err)
	}
}
