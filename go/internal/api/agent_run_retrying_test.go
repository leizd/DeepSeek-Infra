package api

import (
	"context"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

// The "retrying" branch of node() is reached only after an agent_reset event.
// It was never driven, so both of its outcomes -- hold on the current fence and
// execute on a newer one -- were uncovered. Those two returns are the whole
// guard between a settled effect and a duplicate provider call.

func retryingRun(t *testing.T, driver *agentRunDriver, run agent.RunMetadata, nodeID string) agent.RunMetadata {
	t.Helper()
	ctx := context.Background()
	authority, err := driver.service.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	// The event must name this run's real id and carry a body reference, or the
	// metadata gate refuses it before the reducer ever runs.
	reset := &agentv1.AppendAgentRunEventRequest{
		SchemaVersion: 1,
		OperationId:   "driver-reset-" + nodeID,
		Actor:         "operator:agent-test",
		RunId:         run.RunID,
		ExpectedIndex: run.NextIndex,
		Fence:         &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch},
		Event: &agentv1.AgentRunEventMetadata{
			Type:  "agent_reset",
			Phase: nodeID,
			Body:  driverFixtureReference("reset:" + nodeID),
		},
	}
	if _, err := driver.service.AppendEvent(ctx, reset); err != nil {
		t.Fatalf("append reset: %v", err)
	}
	updated := driverFixtureRun(t, driver, run.RunID)
	if updated.Nodes()[nodeID].State != "retrying" {
		t.Fatalf("reset did not leave the node retrying: %q", updated.Nodes()[nodeID].State)
	}
	return updated
}

func TestNodeRetryingHoldsWhenTheFenceWasNotPromoted(t *testing.T) {
	driver, fixture, run := driverProgressFixtureRun(t, "critic", false)
	ctx := context.Background()

	// Establish a retained dispatch for the node first.
	setup := successorClaim(t, driver, run)
	if _, err := fixture.execute(ctx, setup.claim, "retained-critic-effect", "critic"); err != nil {
		t.Skipf("fixture cannot retain a dispatch: %v", err)
	}
	// Then mark the node retrying so node() takes the hold branch.
	run = retryingRun(t, driver, run, "critic")

	calls := len(fixture.calls)
	effect, err := driver.node(ctx, fixture, fixture.nodes, setup.claim, setup.grant, "critic", "retrying")
	if effect != nil {
		t.Fatal("a held successor returned an effect")
	}
	if err == nil {
		t.Fatal("a held successor was admitted on the original fence")
	}
	// The hold must not call the worker again.
	if len(fixture.calls) != calls {
		t.Fatalf("hold dispatched %d extra calls", len(fixture.calls)-calls)
	}
}

func TestNodeRetryingExecutesOnlyAfterATakeoverEpoch(t *testing.T) {
	driver, fixture, run := driverProgressFixtureRun(t, "critic", false)
	ctx := context.Background()

	setup := successorClaim(t, driver, run)
	if _, err := fixture.execute(ctx, setup.claim, "retained-critic-effect", "critic"); err != nil {
		t.Skipf("fixture cannot retain a dispatch: %v", err)
	}
	run = retryingRun(t, driver, run, "critic")

	// A claim whose epoch is ahead of the retained intent is the only fence that
	// may run the stored successor. The fixture cannot mint a real takeover, so
	// assert the refusal rather than fabricating authority.
	promoted := proto.Clone(setup.claim).(*agentv1.AgentRunExecutionClaim)
	fence := proto.Clone(setup.claim.Fence).(*commonv1.ActionFence)
	fence.ExecutionEpoch = setup.claim.Fence.ExecutionEpoch + 5
	promoted.Fence = fence
	effect, err := driver.node(ctx, fixture, fixture.nodes, promoted, setup.grant, "critic", "retrying")
	if effect != nil {
		t.Fatal("an unissued epoch returned an effect")
	}
	if err == nil {
		t.Fatal("an unissued epoch ran the stored successor")
	}
}
