package api

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// These signed offline fixtures exercise Go orchestration only. Actual Rust
// provider rejection, custody and public recovery require the process matrix.
func driverFailureFixture(t *testing.T, node string) (*agentRunDriver, *driverFixtureWorker, agent.RunMetadata) {
	t.Helper()
	driver, fixture, run := newDriverFixture(t, false)
	fixture.rejectNode = node
	if node == "synthesizer" {
		for i := 0; i < 2; i++ {
			if err := driver.drive(context.Background(), run); err != nil {
				t.Fatal(err)
			}
			run = driverFixtureRun(t, driver, run.RunID)
		}
	}
	return driver, fixture, run
}

func assertDriverFailureSettlement(t *testing.T, driver *agentRunDriver, fixture *driverFixtureWorker, id, node string) {
	t.Helper()
	run := driverFixtureRun(t, driver, id)
	if run.Status != "failed" || run.LastEvent.Type != "error" || run.LastEvent.Phase != node || driverRunnable(run) {
		t.Fatal("confirmed failure did not terminate exactly its phase", run.Status, run.LastEvent)
	}
	wantCalls, phase, wantActions := "leader", "plan", 1
	if node == "synthesizer" {
		wantCalls, phase, wantActions = "leader,coder,critic,synthesizer", "synthesis", 3
		if run.Nodes()["coder"].State != "succeeded" || run.Nodes()["critic"].State != "succeeded" {
			t.Fatal("failed synthesis discarded completed tasks")
		}
	}
	if strings.Join(fixture.calls, ",") != wantCalls {
		t.Fatal("failure repeated or skipped a dispatch", fixture.calls)
	}
	actions, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(actions) != wantActions {
		t.Fatal("failure changed phase admissions", err, len(actions))
	}
	for _, action := range actions {
		binding, err := store.AgentRunExecutionBindingFromRecord(action)
		if err != nil {
			t.Fatal(err)
		}
		if binding.Phase == phase {
			if action.State != "FAILED" || run.NextIndex != binding.MetadataIndex+1 {
				t.Fatal("failed phase forged done or appended extra events")
			}
			_, _, receipt, found, err := driver.service.control.AgentNodeOutcomeSnapshot(action.ID, node)
			if err != nil || !found || receipt == nil || !agentproof.ValidTerminalFailure(receipt.Evidence) {
				t.Fatal("terminal action lost its exact original signed outcome", err)
			}
		} else if action.State != "SUCCEEDED" {
			t.Fatal("failed phase altered a previous completed action")
		}
		resources, err := driver.service.control.GetResourceLeases(action.ID)
		if err != nil || len(resources) != 0 {
			t.Fatal("terminal action retained resources", err)
		}
	}
}

func TestAgentDriverSettlesOnlyProvedPlannerOrSynthesisFailure(t *testing.T) {
	for _, node := range []string{"leader", "synthesizer"} {
		t.Run(node, func(t *testing.T) {
			driver, fixture, run := driverFailureFixture(t, node)
			if err := driver.drive(context.Background(), run); err != nil {
				t.Fatal(err)
			}
			assertDriverFailureSettlement(t, driver, fixture, run.RunID, node)
		})
	}
}

func TestAgentDriverFailureProjectionRefusalPreservesCustodyAndRetryDoesNotDispatch(t *testing.T) {
	for _, node := range []string{"leader", "synthesizer"} {
		for _, failure := range []string{"failure-projection", "failure-proof"} {
			t.Run(node+"/"+failure, func(t *testing.T) {
				driver, fixture, run := driverFailureFixture(t, node)
				fixture.fail = failure
				if err := driver.drive(context.Background(), run); err == nil {
					t.Fatal("missing failure projection closed a phase")
				}
				run = driverFixtureRun(t, driver, run.RunID)
				if run.Status == "done" || run.Status == "failed" {
					t.Fatal("unverified failure became terminal")
				}
				fixture.fail = ""
				if err := driver.drive(context.Background(), run); err != nil {
					t.Fatal("known signed outcome could not reconcile", err)
				}
				assertDriverFailureSettlement(t, driver, fixture, run.RunID, node)
			})
		}
	}
}

func TestAgentDriverLostFailureReplyQueriesOriginalCustodyWithoutRedispatch(t *testing.T) {
	for _, node := range []string{"leader", "synthesizer"} {
		t.Run(node, func(t *testing.T) {
			driver, fixture, run := driverFailureFixture(t, node)
			fixture.failAfter = node
			if err := driver.drive(context.Background(), run); !errors.Is(err, context.DeadlineExceeded) {
				t.Fatal("lost reply fabricated an acknowledged failure", err)
			}
			run = driverFixtureRun(t, driver, run.RunID)
			if run.Status == "done" || run.Status == "failed" {
				t.Fatal("lost reply closed the run without retained proof")
			}
			if err := driver.drive(context.Background(), run); err != nil {
				t.Fatal("original failure receipt could not reconcile", err)
			}
			assertDriverFailureSettlement(t, driver, fixture, run.RunID, node)
		})
	}
}

func TestAgentFailureRPCRejectsShadowMalformedUnknownAndCancelledCalls(t *testing.T) {
	driver, fixture, _ := newDriverFixture(t, false)
	valid := &agentv1.SettleAgentFailureRequest{SchemaVersion: 1, Fence: &commonv1.ActionFence{ActionId: "failure-rpc", ExecutionEpoch: 1}, Projection: &agentv1.AgentFailureProjectionReceipt{SchemaVersion: 1}}
	shadow := &agentNodeService{run: &agentRunService{control: driver.service.control, health: ShadowHealth()}}
	if _, err := shadow.SettleFailure(context.Background(), valid); status.Code(err) != codes.PermissionDenied {
		t.Fatal("shadow runtime accepted failure mutation", err)
	}
	for _, mutation := range []string{"nil", "schema", "fence", "projection", "unknown", "unknown-fence"} {
		t.Run(mutation, func(t *testing.T) {
			x := proto.Clone(valid).(*agentv1.SettleAgentFailureRequest)
			switch mutation {
			case "nil":
				x = nil
			case "schema":
				x.SchemaVersion = 2
			case "fence":
				x.Fence = nil
			case "projection":
				x.Projection = nil
			case "unknown":
				x.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 0x01})
			case "unknown-fence":
				x.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 0x01})
			}
			if _, err := fixture.nodes.SettleFailure(context.Background(), x); status.Code(err) != codes.InvalidArgument {
				t.Fatal("malformed failure RPC accepted", err)
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := fixture.nodes.SettleFailure(ctx, valid); status.Code(err) != codes.Canceled {
		t.Fatal("cancelled failure RPC entered the store", err)
	}
	if got := nodeDispatchError(agentproof.ErrAgentFailureProjectionInvalid); status.Code(got) != codes.FailedPrecondition {
		t.Fatal("failure proof lost its public error boundary", got)
	}
	if actions, err := driver.service.control.ListAuthoritativeRecords("action"); err != nil || len(actions) != 0 {
		t.Fatal("refused RPC admitted a phase", err)
	}
}
