package store

import (
	"crypto/ed25519"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func recoveryExecutionFixture(t *testing.T, target string, resynthesize bool) (*Control, agent.RunMetadata, ed25519.PrivateKey) {
	t.Helper()
	control, completion, private := synthesisSettlementFixture(t)
	if _, err := control.SettleAgentSynthesis(completion); err != nil {
		t.Fatal(err)
	}
	expected := recoveryExpectedFromRun(t, control, completion.RunID, &agentv1.AgentRunRecoverySelection{
		Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN, AgentId: target, Resynthesize: proto.Bool(resynthesize)})
	input, _ := recoveryInputFromPreview(t, control, expected)
	committed, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	return control, committed.Run, private
}

func recoveryExecutionClaim(t *testing.T, control *Control, run agent.RunMetadata, phase string) AgentRunExecutionResult {
	t.Helper()
	expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{})
	result, err := control.ClaimAgentRunExecution(AgentRunExecutionRequest{OperationID: "recover-claim-" + phase, Actor: "operator:recovery-execution",
		RunID: run.RunID, Phase: phase, ExpectedIndex: run.NextIndex, Fence: expected.Fence, Owner: "recovery-execution", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func TestRecoveredOutputAdmissionUsesOriginalProofsAndReturnsDetachedSources(t *testing.T) {
	control, run, _ := recoveryExecutionFixture(t, "critic", true)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	sources, err := control.verifiedAgentRecoveryOutputsTx(tx, run)
	if err != nil || len(sources) != 1 || sources["coder"] == nil {
		t.Fatal("original dependency custody missing", err)
	}
	original := proto.Clone(run.Recovery.Metadata.RetainedOutputs[0]).(*agentv1.AgentRunRecoveryEffectSource)
	sources["coder"].Dispatch.MetadataEpoch++
	if !proto.Equal(original, run.Recovery.Metadata.RetainedOutputs[0]) {
		t.Fatal("reader rewrote an original receipt")
	}
	if empty, err := control.verifiedAgentRecoveryOutputsTx(tx, agent.RunMetadata{}); err != nil || len(empty) != 0 {
		t.Fatal(err)
	}
}

func TestRecoveredOutputAdmissionRejectsReboundOrUnpublishedSources(t *testing.T) {
	control, run, _ := recoveryExecutionFixture(t, "critic", true)
	changes := []func(*agentv1.AgentRunRecoveryContext){
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedIds = nil },
		func(c *agentv1.AgentRunRecoveryContext) {
			c.RetainedIds = []string{"coder", "coder"}
			c.RetainedOutputs = append(c.RetainedOutputs, c.RetainedOutputs[0])
		},
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedIds[0] = "" },
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0] = nil },
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0].Dispatch = nil },
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0].Dispatch.Fence = nil },
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0].Outcome = nil },
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0].SettlementMetadataIndex = 0 },
		func(c *agentv1.AgentRunRecoveryContext) {
			c.RetainedOutputs[0].SettlementMetadataIndex = c.ExpectedIndex
		},
		func(c *agentv1.AgentRunRecoveryContext) {
			c.RetainedOutputs[0].Dispatch.MetadataIndex = c.ExpectedIndex
		},
		func(c *agentv1.AgentRunRecoveryContext) {
			c.RetainedOutputs[0].Dispatch.Fence.ActionId = "absent-original-action"
		},
		func(c *agentv1.AgentRunRecoveryContext) {
			c.RetainedOutputs[0].Dispatch.Request.Sha256 = strings.Repeat("f", 64)
		},
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0].Outcome.Signature[0] ^= 1 },
		func(c *agentv1.AgentRunRecoveryContext) { c.RetainedOutputs[0].SettlementMetadataIndex++ },
	}
	for i, change := range changes {
		copy := run
		scope := *run.Recovery
		copy.Recovery = &scope
		copy.Recovery.Metadata = proto.Clone(run.Recovery.Metadata).(*agentv1.AgentRunRecoveryContext)
		change(copy.Recovery.Metadata)
		digest, err := agentRecoveryContextDigest(copy.Recovery.Metadata)
		if err != nil {
			t.Fatal(err)
		}
		copy.Recovery.ContextSHA256 = digest
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		_, err = control.verifiedAgentRecoveryOutputsTx(tx, copy)
		tx.Rollback()
		if err == nil {
			t.Fatal("source mutation was accepted", i)
		}
	}
}

func TestRecoveredTaskAdmissionUsesOldDependencyWithoutReexecutingIt(t *testing.T) {
	control, run, _ := recoveryExecutionFixture(t, "critic", true)
	before := append([]agent.RunPlanNode(nil), run.Plan...)
	claim := recoveryExecutionClaim(t, control, run, "tasks")
	lease := claim.Admission.Lease
	request := AgentNodeDispatchRequest{OperationID: "recover-selected-critic", Actor: "operator:recovery-execution", RunID: run.RunID,
		ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, NodeID: "critic"}
	selected, err := control.BeginAgentNodeDispatch(request)
	if err != nil || !selected.Fresh || selected.Intent.NodeID != "critic" || !reflect.DeepEqual(selected.Intent.DependsOn, []string{"coder"}) {
		t.Fatal("retained original dependency did not admit selected task", err)
	}
	request.OperationID, request.NodeID = "reject-old-coder", "coder"
	if _, err := control.BeginAgentNodeDispatch(request); err == nil {
		t.Fatal("public plan admitted an unselected old node")
	}
	after, _, err := control.AgentRunMetadata(run.RunID)
	if err != nil || !reflect.DeepEqual(before, after.Plan) {
		t.Fatal("recovery rewrote the retained public plan", err)
	}
}

func TestRecoveredSynthesisOnlyAdmitsOriginalOutputsWithoutANewTaskPhase(t *testing.T) {
	control, run, _ := recoveryExecutionFixture(t, "synth", false)
	if executionPhaseReady(run, "tasks") || !executionPhaseReady(run, "synthesis") {
		t.Fatal("synthesis-only recovery selected task work")
	}
	claim := recoveryExecutionClaim(t, control, run, "synthesis")
	lease := claim.Admission.Lease
	node, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "recover-synthesis-only", Actor: "operator:recovery-execution", RunID: run.RunID,
		ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, NodeID: "synthesizer"})
	if err != nil || !reflect.DeepEqual(node.Intent.DependsOn, []string{"coder", "critic"}) {
		t.Fatal("original sources did not admit direct synthesis", err)
	}
}

func TestRecoveredTaskCompletesOnlyItsOwnSelectedSettlements(t *testing.T) {
	control, run, private := recoveryExecutionFixture(t, "critic", true)
	claim := recoveryExecutionClaim(t, control, run, "tasks")
	lease := claim.Admission.Lease
	request := AgentNodeDispatchRequest{OperationID: "recover-complete-critic", Actor: "operator:recovery-execution", RunID: run.RunID,
		ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, NodeID: "critic"}
	node, err := control.BeginAgentNodeDispatch(request)
	if err != nil {
		t.Fatal(err)
	}
	intent := agentOutcomeIntent(node.Intent)
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId,
			RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase, Request: intent.Request, PlanDigest: intent.PlanDigest,
			MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: lease.WriterFencingToken,
			CreatedAt: time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z"), ConfirmedAt: time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z"),
			OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 30, PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 20},
			FleetId: control.fleetID, Environment: control.environment, TaskOutput: &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 1, CompletionTokens: 1, LatencyMs: 1}}}
	agentOutcomeSign(t, receipt, private)
	recorded, err := control.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{OperationID: "recover-record-critic", Actor: request.Actor,
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: run.RunID, NodeID: "critic", Receipt: receipt})
	if err != nil {
		t.Fatal(err)
	}
	settled, err := control.SettleAgentTask(AgentTaskSettlementRequest{OperationID: "recover-settle-critic", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: run.RunID, NodeID: "critic", Owner: lease.Owner, ClaimToken: lease.ClaimToken, ExpectedIndex: run.NextIndex, SourceReceiptSHA256: recorded.ReceiptSHA256})
	if err != nil {
		t.Fatal(err)
	}
	completed, err := control.CompleteAgentTasks(CompleteAgentTasksRequest{OperationID: "recover-complete-selected", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: run.RunID, Owner: lease.Owner, ClaimToken: lease.ClaimToken, ExpectedIndex: settled.Run.NextIndex})
	if err != nil || completed.Action.State != "SUCCEEDED" {
		t.Fatal("selected completion demanded old nodes be settled again", err)
	}
	markers, err := readAgentTaskSettlements(completed.Action)
	if err != nil || len(markers) != 1 {
		t.Fatal("old proof became a forged current settlement", err)
	}
	if _, found := markers["coder"]; found {
		t.Fatal("retained coder was rebound to current phase")
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.validateAgentTasksCompletionTx(tx, completed.Action); err != nil {
		t.Fatal("selected terminal marker did not independently validate", err)
	}
}
