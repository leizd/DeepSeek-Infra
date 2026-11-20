package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// These signatures qualify offline Go orchestration only. The actual Rust
// custody, artifact bodies and provider call matrix remain separate process gates.
type driverProgressFixture struct {
	*driverFixtureWorker
	progressCalls int
	fault         string
}

func (fixture *driverProgressFixture) PrepareAgentRecoveryProgress(_ context.Context, input *agentv1.PrepareAgentRecoveryProgressRequest) (*agentv1.AgentRecoveryProgressReceipt, error) {
	fixture.progressCalls++
	if fixture.fault == "transport" {
		return nil, context.DeadlineExceeded
	}
	if fixture.fault == "nil" {
		return nil, nil
	}
	proof := &agentv1.AgentRecoveryProgressReceipt{SchemaVersion: 1, SignerPublicKey: fixture.binding.PublicKey,
		Evidence: &agentv1.AgentRecoveryProgressEvidence{SchemaVersion: 1, Context: proto.Clone(input.Context).(*agentv1.AgentRecoveryProgressContext),
			NoteBody: driverFixtureReference("rerun-note:" + input.Context.Role), NextBody: driverFixtureReference("rerun-next"),
			FleetId: fixture.binding.FleetID, Environment: fixture.binding.Environment}}
	if fixture.fault == "stale" {
		proof.Evidence.Context.ExpectedIndex--
	}
	if fixture.fault == "unknown" {
		proof.Evidence.Context.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
	}
	proof.Signature = fixture.signed(proof.Evidence, agentproof.RecoveryProgressDomain)
	if fixture.fault == "signature" {
		proof.Signature[0] ^= 1
	}
	return proof, nil
}

func driverProgressFixtureRun(t *testing.T, role string, synthesize bool) (*agentRunDriver, *driverProgressFixture, agent.RunMetadata) {
	t.Helper()
	driver, fixture, run := newDriverFixture(t, false)
	for step := 0; step < 3; step++ {
		if err := driver.drive(context.Background(), run); err != nil {
			t.Fatal(err)
		}
		run = driverFixtureRun(t, driver, run.RunID)
	}
	if run.Status != "done" || strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer" {
		t.Fatal("original workflow was not independently complete")
	}
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	expected := store.AgentRunRecoveryExpected{RunID: run.RunID, Fence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch},
		ExpectedIndex: run.NextIndex, ExpectedRuntimeRevision: run.RuntimeVersion(), ExpectedRequest: run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN, AgentId: role, Resynthesize: proto.Bool(synthesize)}}
	preview, err := driver.service.control.PreviewAgentRunRecovery(expected)
	if err != nil {
		t.Fatal(err)
	}
	result, err := driver.service.control.CommitAgentRunRecovery(store.CommitAgentRunRecoveryRequest{AgentRunRecoveryExpected: expected,
		OperationID: "driver-rerun", Actor: agentDriverActor, ContextSHA256: preview.ContextSHA256,
		Context: agent.ArtifactRef{SHA256: strings.Repeat("e", 64), Length: 500}, Request: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 140},
		EventBodies: []agent.ArtifactRef{{SHA256: strings.Repeat("1", 64), Length: 30}, {SHA256: strings.Repeat("2", 64), Length: 30}}})
	if err != nil {
		t.Fatal(err)
	}
	wrapped := &driverProgressFixture{driverFixtureWorker: fixture}
	driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		return &agentExecutionSignerSession{client: wrapped, binding: fixture.binding}, nil
	}
	return driver, wrapped, result.Run
}

func TestDriverRerunTerminatesOnlyItsSelectionThenCommitsProgressBeforeOptionalSynthesis(t *testing.T) {
	for _, role := range []string{"critic", "researcher"} {
		for _, synthesize := range []bool{true, false} {
			t.Run(role+map[bool]string{true: "-synthesize", false: "-keep-final"}[synthesize], func(t *testing.T) {
				driver, fixture, run := driverProgressFixtureRun(t, role, synthesize)
				originalFinal := run.FinalAfter
				if err := driver.drive(context.Background(), run); err != nil {
					t.Fatal(err)
				}
				run = driverFixtureRun(t, driver, run.RunID)
				if fixture.progressCalls != 0 || run.RecoveryProgress != nil || run.FinalAfter != originalFinal || len(fixture.calls) != 5 || fixture.calls[4] != role {
					t.Fatal("task execution cleared the final answer or reran another role")
				}
				actions, err := driver.service.control.ListAuthoritativeRecords("action")
				if err != nil {
					t.Fatal(err)
				}
				count := len(actions)
				before := run.NextIndex
				if err := driver.drive(context.Background(), run); err != nil {
					t.Fatal(err)
				}
				run = driverFixtureRun(t, driver, run.RunID)
				actions, err = driver.service.control.ListAuthoritativeRecords("action")
				if err != nil || len(actions) != count || len(fixture.calls) != 5 || fixture.progressCalls != 1 || run.NextIndex != before+2 || run.SchemaVersion != 4 || run.RecoveryProgress == nil {
					t.Fatal("progress admitted an effect or failed its atomic cursor")
				}
				events, err := driver.service.control.AgentRunEventsAfter(run.RunID, before-1, 10)
				if err != nil || len(events) != 2 || events[0].Type != "agent_note" || events[0].Phase != role {
					t.Fatal("rerun note was lost or reordered", err)
				}
				if synthesize {
					if events[1].Type != "final_reset" || run.Status != "running" || run.FinalAfter != before+1 {
						t.Fatal("late final reset did not follow the selected task")
					}
					if err := driver.drive(context.Background(), run); err != nil {
						t.Fatal(err)
					}
					run = driverFixtureRun(t, driver, run.RunID)
					if run.Status != "done" || len(fixture.calls) != 6 || fixture.calls[5] != "synthesizer" {
						t.Fatal("separately admitted synthesis did not finish")
					}
				} else if events[1].Type != "run_status" || events[1].Status != "done" || run.Status != "done" || run.FinalAfter != originalFinal || driverRunnable(run) {
					t.Fatal("no-synthesis rerun changed the old final or stayed runnable")
				}
			})
		}
	}
}

func TestDriverRerunCannotReplaceMissingOrInvalidProgressCustodyWithANewAction(t *testing.T) {
	driver, fixture, run := driverProgressFixtureRun(t, "critic", true)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	actions, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	count := len(actions)
	before := run.NextIndex
	for _, fault := range []string{"transport", "nil", "stale", "unknown", "signature", "missing-method"} {
		fixture.fault = fault
		if fault == "missing-method" {
			driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				return &agentExecutionSignerSession{client: fixture.driverFixtureWorker, binding: fixture.binding}, nil
			}
		}
		if err := driver.drive(context.Background(), run); err == nil {
			t.Fatal("invalid progress custody admitted", fault)
		}
		after := driverFixtureRun(t, driver, run.RunID)
		records, err := driver.service.control.ListAuthoritativeRecords("action")
		if err != nil || after.NextIndex != before || after.FinalAfter != run.FinalAfter || len(records) != count || len(fixture.calls) != 5 {
			t.Fatal("progress refusal changed final answer or granted synthesis")
		}
	}
	fixture.fault = ""
	driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		return &agentExecutionSignerSession{client: fixture, binding: fixture.binding}, nil
	}
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal("refusal poisoned a later valid progress receipt", err)
	}
}

func TestDriverResumesTypedTaskCompletionBeforeAttemptingRerunProgress(t *testing.T) {
	driver, fixture, run := driverProgressFixtureRun(t, "critic", false)
	ctx := context.Background()
	claim, err := driver.admission(ctx, run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, nil)
	if err != nil {
		t.Fatal(err)
	}
	claim, grant, err := driver.grant(ctx, claim)
	if err != nil {
		t.Fatal(err)
	}
	nodes := &agentNodeService{run: driver.service}
	effect, err := driver.node(ctx, fixture, nodes, claim, grant, "critic")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := (proto.MarshalOptions{Deterministic: true}).Marshal(effect.OutcomeReceipt)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	_, err = nodes.SettleTask(ctx, &agentv1.SettleAgentTaskRequest{SchemaVersion: 1, OperationId: driverOperation("settle-task:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", "critic"), Actor: agentDriverActor,
		Fence: claim.Fence, RunId: run.RunID, NodeId: "critic", Owner: claim.Owner, ClaimToken: claim.ClaimToken, ExpectedIndex: run.NextIndex, SourceReceiptSha256: hex.EncodeToString(digest[:])})
	if err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if !driverTasksComplete(run) {
		t.Fatal("task output was not independently published")
	}
	phase, existing, err := driver.phase(run)
	if err != nil || phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS || existing == nil {
		t.Fatal("unclosed task phase was erased", err)
	}
	if err := driver.drive(ctx, run); err != nil {
		t.Fatal("restart tried progress before typed task termination", err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if fixture.progressCalls != 0 || len(fixture.calls) != 5 {
		t.Fatal("restart redispatched or skipped terminal task settlement")
	}
	if err := driver.drive(ctx, run); err != nil {
		t.Fatal(err)
	}
	if fixture.progressCalls != 1 || driverFixtureRun(t, driver, run.RunID).Status != "done" || len(fixture.calls) != 5 {
		t.Fatal("completed task did not progress without another provider call")
	}
}
