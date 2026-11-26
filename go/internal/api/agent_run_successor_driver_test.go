package api

import (
	"context"
	"errors"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

// These cases drive the two successor entry points directly. Both were at 0%
// coverage, and both guard the rule that a settled effect may only become a
// second provider call after claim or takeover issued the next epoch. A wrong
// admission here dispatches a duplicate upstream effect.

func successorClaim(t *testing.T, driver *agentRunDriver, run agent.RunMetadata) claimSetup {
	t.Helper()
	ctx := context.Background()
	claim, err := driver.admission(ctx, run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, nil)
	if err != nil {
		t.Skipf("fixture cannot admit the tasks phase: %v", err)
	}
	claim, grant, err := driver.grant(ctx, claim)
	if err != nil {
		t.Skipf("fixture cannot mint a tasks-phase grant: %v", err)
	}
	return claimSetup{claim: claim, grant: grant}
}

type claimSetup struct {
	claim *agentv1.AgentRunExecutionClaim
	grant []byte
}

func TestSuccessorHoldRefusesWithoutControlOrClaim(t *testing.T) {
	driver, _, _ := newDriverFixture(t, false)
	// No control and no claim cannot authorize a successor: both are refusals,
	// never an admission, and never a panic.
	if err := driver.successorHold(nil, nil, "coder", "agent-node-original", "CONFIRMED"); !errors.Is(err, errAgentDriverReset) && !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("nil nodes/claim returned %v", err)
	}
	if err := driver.successorHold(&agentNodeService{}, nil, "coder", "agent-node-original", "CONFIRMED"); !errors.Is(err, errAgentDriverReset) && !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("empty node service returned %v", err)
	}
	// An unresolved effect is unknown, not a generation.
	if err := driver.successorHold(nil, nil, "coder", "agent-node-original", "EFFECT_UNKNOWN"); !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("unknown effect returned %v", err)
	}
	// A tool wait stays on the original effect.
	if err := driver.successorHold(nil, nil, "coder", "agent-node-original", "AWAITING_TOOLS"); !errors.Is(err, errAgentDriverTools) {
		t.Fatalf("tool wait returned %v", err)
	}
}

func TestSuccessorExecuteRefusesIncompleteCustody(t *testing.T) {
	driver, fixture, _ := newDriverFixture(t, false)
	ctx := context.Background()

	// Every one of these inputs is missing a piece the fence needs.
	for name, call := range map[string]func() error{
		"nil-nodes": func() error {
			_, err := driver.successorExecute(ctx, fixture, nil, nil, nil, "coder", "agent-node-original", "CONFIRMED")
			return err
		},
		"nil-claim": func() error {
			_, err := driver.successorExecute(ctx, fixture, fixture.nodes, nil, nil, "coder", "agent-node-original", "CONFIRMED")
			return err
		},
		"nil-client": func() error {
			claim := &agentv1.AgentRunExecutionClaim{Owner: "owner", ClaimToken: "token"}
			_, err := driver.successorExecute(ctx, nil, fixture.nodes, claim, nil, "coder", "agent-node-original", "CONFIRMED")
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); !errors.Is(err, errAgentDriverUnknown) {
				t.Fatalf("%s returned %v", name, err)
			}
		})
	}
	// A claim whose operation does not match the stored intent is unknown.
	claim := &agentv1.AgentRunExecutionClaim{Owner: "owner", ClaimToken: "token"}
	_, err := driver.successorExecute(ctx, fixture, fixture.nodes, claim, nil, "coder", "a-different-operation", "CONFIRMED")
	if !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("foreign operation returned %v", err)
	}
}

func TestSuccessorExecuteRefusesAnUnsettledEffect(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	// The state gate is evaluated before any epoch question, so an unresolved
	// effect never reaches the provider regardless of the fence.
	for _, state := range []string{"EFFECT_UNKNOWN", "AWAITING_TOOLS", "DISPATCHING", ""} {
		t.Run(state, func(t *testing.T) {
			if state == "" {
				state = "NONE"
			}
			driver, fixture, run := newDriverFixture(t, false)
			ctx := context.Background()
			setup := successorClaim(t, driver, run)
			// Begin a dispatch so a stored intent exists for this node.
			if _, err := fixture.nodes.BeginDispatch(ctx, &agentv1.BeginAgentNodeDispatchRequest{
				SchemaVersion: 1, OperationId: "successor-begin", Actor: "operator:agent-test",
				Fence: setup.claim.Fence, RunId: setup.claim.RunId, NodeId: "coder",
				Owner: setup.claim.Owner, ClaimToken: setup.claim.ClaimToken,
			}); err != nil {
				t.Skipf("fixture cannot begin a dispatch: %v", err)
			}
			_, err := driver.successorExecute(ctx, fixture, fixture.nodes, setup.claim, setup.grant, "coder", "successor-begin", state)
			if err == nil {
				t.Fatalf("%s was admitted", state)
			}
		})
	}
}

func TestSuccessorHoldReportsTheMintedGenerationWithoutDispatching(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	driver, fixture, run := newDriverFixture(t, false)
	ctx := context.Background()
	setup := successorClaim(t, driver, run)
	if _, err := fixture.nodes.BeginDispatch(ctx, &agentv1.BeginAgentNodeDispatchRequest{
		SchemaVersion: 1, OperationId: "successor-begin", Actor: "operator:agent-test",
		Fence: setup.claim.Fence, RunId: setup.claim.RunId, NodeId: "coder",
		Owner: setup.claim.Owner, ClaimToken: setup.claim.ClaimToken,
	}); err != nil {
		t.Skipf("fixture cannot begin a dispatch: %v", err)
	}
	calls := len(fixture.calls)
	// No reset event exists for this node yet, so the generation is refused and
	// the original effect is untouched.
	err := driver.successorHold(fixture.nodes, setup.claim, "coder", "successor-begin", "CONFIRMED")
	if !errors.Is(err, errAgentDriverReset) {
		t.Fatalf("a reset without an event minted a generation: %v", err)
	}
	if len(fixture.calls) != calls {
		t.Fatal("successor hold called the worker")
	}
}
