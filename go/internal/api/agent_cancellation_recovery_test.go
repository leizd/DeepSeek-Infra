package api

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// Offline signed fixtures prove orchestration and refusal only. Actual Rust
// custody and restart recovery remain separate mandatory cloud process gates.
func cancelledDriverRecoveryFixture(t *testing.T, phase string, seconds uint32) (*agentRunDriver, *driverFixtureWorker, agent.RunMetadata, string) {
	t.Helper()
	driver, fixture, run := newDriverFixture(t, false)
	if phase != "plan" {
		if err := driver.drive(context.Background(), run); err != nil {
			t.Fatal(err)
		}
		run = driverFixtureRun(t, driver, run.RunID)
	}
	if phase == "synthesis" {
		if err := driver.drive(context.Background(), run); err != nil {
			t.Fatal(err)
		}
		run = driverFixtureRun(t, driver, run.RunID)
	}
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	request := executionRPCInput(authority.MetadataEpoch)
	request.RunId, request.MetadataFence.ActionId = run.RunID, run.RunID
	request.Owner, request.LeaseSeconds, request.ExpectedIndex = driver.owner, seconds, run.NextIndex
	request.OperationId = "cancel-recovery-claim-" + phase
	node := "coder"
	if phase == "plan" {
		request.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
		node = "leader"
	}
	if phase == "synthesis" {
		request.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
		node = "synthesizer"
	}
	admitted, err := driver.service.ClaimExecution(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fixture.execute(context.Background(), admitted.Claim, "cancel-recovery-effect-"+phase, node); err != nil {
		t.Fatal(err)
	}
	cancel := agentRPCAppend(run.NextIndex, authority.MetadataEpoch)
	cancel.Create, cancel.OperationId = nil, "cancel-recovery-public-"+phase
	cancel.Event = &agentv1.AgentRunEventMetadata{Type: "run_status", Status: "cancelled", Body: driverFixtureReference("cancelled-recovery")}
	if _, err := driver.service.AppendEvent(context.Background(), cancel); err != nil {
		t.Fatal(err)
	}
	return driver, fixture, driverFixtureRun(t, driver, run.RunID), admitted.Claim.Fence.ActionId
}

func assertCancellationRecoveryRetainsCustody(t *testing.T, driver *agentRunDriver, fixture *driverFixtureWorker, run agent.RunMetadata, action string, calls int) {
	t.Helper()
	resources, err := driver.service.control.GetResourceLeases(action)
	if err != nil || len(resources) != 1 {
		t.Fatal("refusal released unresolved custody", err)
	}
	actual := driverFixtureRun(t, driver, run.RunID)
	if !actual.Cancelled || actual.Status != "cancelled" || actual.NextIndex != run.NextIndex || len(fixture.calls) != calls {
		t.Fatal("refusal changed cancellation or redispatched", actual, fixture.calls)
	}
}

type cancellationQueryWorker struct {
	*driverFixtureWorker
	query func(context.Context, *agentv1.QueryAgentNodeEffectRequest) (*agentv1.AgentNodeEffectMetadata, error)
}

func (worker *cancellationQueryWorker) QueryAgentNodeEffect(ctx context.Context, input *agentv1.QueryAgentNodeEffectRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	return worker.query(ctx, input)
}

type cancellationAuthorizerOnly struct{ agentExecutionAuthorizer }

func TestCancellationRecoveryUnavailableCustodyCannotDispatchOrRelease(t *testing.T) {
	for _, fault := range []string{"factory", "nil-session", "nil-client", "authorizer-only", "default", "query-error", "nil-effect", "unknown-effect", "missing-receipt", "forged-proof", "cancelled-query"} {
		t.Run(fault, func(t *testing.T) {
			driver, fixture, run, action := cancelledDriverRecoveryFixture(t, "tasks", 60)
			calls := len(fixture.calls)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			original := driver.service.executionSigner
			driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				switch fault {
				case "factory":
					return nil, context.DeadlineExceeded
				case "nil-session":
					return nil, nil
				case "nil-client":
					return &agentExecutionSignerSession{}, nil
				case "authorizer-only":
					return &agentExecutionSignerSession{client: cancellationAuthorizerOnly{fixture}}, nil
				}
				worker := &cancellationQueryWorker{driverFixtureWorker: fixture, query: func(ctx context.Context, input *agentv1.QueryAgentNodeEffectRequest) (*agentv1.AgentNodeEffectMetadata, error) {
					if fault == "query-error" {
						return nil, context.DeadlineExceeded
					}
					if fault == "nil-effect" {
						return nil, nil
					}
					effect, err := fixture.QueryAgentNodeEffect(ctx, input)
					if err != nil {
						return nil, err
					}
					switch fault {
					case "unknown-effect":
						effect.State = "EFFECT_UNKNOWN"
					case "missing-receipt":
						effect.OutcomeReceipt = nil
					case "forged-proof":
						effect.OutcomeReceipt.Signature[0] ^= 1
					case "cancelled-query":
						cancel()
					}
					return effect, nil
				}}
				return &agentExecutionSignerSession{client: worker, binding: fixture.binding, close: func() error { fixture.closes++; return nil }}, nil
			}
			if fault == "default" {
				driver.service.executionSigner = nil
				t.Setenv("DEEPSEEK_WORKER_TARGET", "")
			}
			if err := driver.recoverCancellation(ctx, run); err == nil {
				t.Fatal("unavailable custody settled cancellation", fault)
			}
			assertCancellationRecoveryRetainsCustody(t, driver, fixture, run, action, calls)
			driver.service.executionSigner = original
			if err := driver.recoverCancellation(context.Background(), run); err != nil {
				t.Fatal("refusal poisoned retained-custody retry", err)
			}
		})
	}
}

func TestCancellationRecoveryRequiresCancelledAuthoritativeProcess(t *testing.T) {
	for _, fault := range []string{"not-cancelled", "wrong-status", "shadow", "no-control", "cancelled-context", "plan-phase", "changed-request", "changed-run", "closed-store", "stale-cursor", "invalid-node", "missing-dispatched-node"} {
		t.Run(fault, func(t *testing.T) {
			phase := "tasks"
			if fault == "plan-phase" {
				phase = "plan"
			}
			driver, fixture, run, action := cancelledDriverRecoveryFixture(t, phase, 60)
			original, calls := run, len(fixture.calls)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			control, health := driver.service.control, driver.service.health
			switch fault {
			case "not-cancelled":
				run.Cancelled = false
			case "wrong-status":
				run.Status = "done"
			case "shadow":
				driver.service.health = HealthFor(config.Config{Mode: config.ModeShadow})
			case "no-control":
				driver.service.control = nil
			case "cancelled-context":
				cancel()
			case "changed-request":
				run.Request.SHA256 = strings.Repeat("f", 64)
			case "changed-run":
				run.RunID += "-different"
			case "closed-store":
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
			case "stale-cursor":
				run.NextIndex--
			case "invalid-node":
				run.Plan[0].ID = "../invalid-node"
			case "missing-dispatched-node":
				run.Plan = run.Plan[1:]
			}
			err := driver.recoverCancellation(ctx, run)
			if fault == "changed-run" {
				if err != nil {
					t.Fatal("unrelated run action was not ignored", err)
				}
			} else if err == nil {
				t.Fatal("invalid cancellation process accepted", fault)
			}
			driver.service.control, driver.service.health = control, health
			if fault != "closed-store" {
				assertCancellationRecoveryRetainsCustody(t, driver, fixture, original, action, calls)
			}
		})
	}
}

func TestCancellationSuccessorWaitsForLiveLeaseAndReconcilesOriginalReceipt(t *testing.T) {
	driver, fixture, run, action := cancelledDriverRecoveryFixture(t, "tasks", 3)
	calls := len(fixture.calls)
	lease, found, err := driver.service.control.GetActionLease(action)
	if err != nil || !found {
		t.Fatal(err)
	}
	driver.owner = "cancel-successor-controller"
	if err := driver.recoverCancellation(context.Background(), run); err == nil {
		t.Fatal("successor took live original custody")
	}
	assertCancellationRecoveryRetainsCustody(t, driver, fixture, run, action, calls)
	for time.Now().Unix() <= lease.LeaseUntil {
		time.Sleep(20 * time.Millisecond)
	}
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal("successor could not settle original effect", err)
	}
	record, found, err := driver.service.control.Get("action", action)
	if err != nil || !found || record.State != "CANCELLED_CONFIRMED" || record.ExecutionEpoch != uint64(lease.Epoch+1) {
		t.Fatal("takeover failed to fence original effect", record, err)
	}
	resources, err := driver.service.control.GetResourceLeases(action)
	actual := driverFixtureRun(t, driver, run.RunID)
	if err != nil || len(resources) != 0 || actual.NextIndex != run.NextIndex || actual.Nodes()["coder"].State == "succeeded" || len(fixture.calls) != calls {
		t.Fatal("successor redispatched or fabricated completion", err)
	}
}

func TestCancellationSameOwnerExpiredClaimOnlyReconcilesRetainedCustody(t *testing.T) {
	driver, fixture, run, action := cancelledDriverRecoveryFixture(t, "tasks", 3)
	calls := len(fixture.calls)
	lease, found, err := driver.service.control.GetActionLease(action)
	if err != nil || !found {
		t.Fatal(err)
	}
	// Custody is recorded before expiry; the commit must still require a live lease.
	for _, effect := range fixture.effects {
		if effect.NodeId == "coder" {
			if _, err := fixture.nodes.RecordOutcome(context.Background(), &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1, OperationId: "cancel-retain-before-expiry", Actor: agentDriverActor, Fence: effect.Fence, RunId: run.RunID, NodeId: "coder", OutcomeReceipt: proto.Clone(effect.OutcomeReceipt).(*agentv1.AgentNodeOutcomeReceipt)}); err != nil {
				t.Fatal(err)
			}
		}
	}
	for time.Now().Unix() <= lease.LeaseUntil {
		time.Sleep(20 * time.Millisecond)
	}
	if err := driver.recoverCancellation(context.Background(), run); !errors.Is(err, errAgentDriverUnknown) {
		t.Fatal("expired same-owner claim was treated as terminal", err)
	}
	assertCancellationRecoveryRetainsCustody(t, driver, fixture, run, action, calls)
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal("renewed reconciliation did not settle", err)
	}
	if len(fixture.calls) != calls {
		t.Fatal("expired same-owner effect redispatched")
	}
}

func TestCancellationDuringSynthesisRetainsCancelledPublicCursor(t *testing.T) {
	driver, fixture, run, action := cancelledDriverRecoveryFixture(t, "synthesis", 60)
	calls := len(fixture.calls)
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	actual := driverFixtureRun(t, driver, run.RunID)
	record, found, err := driver.service.control.Get("action", action)
	if err != nil || !found || record.State != "CANCELLED_CONFIRMED" || actual.Status != "cancelled" || actual.NextIndex != run.NextIndex || len(fixture.calls) != calls {
		t.Fatal("late synthesis overwrote public cancellation", actual, err)
	}
}
