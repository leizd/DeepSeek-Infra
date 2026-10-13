package api

import (
	"context"
	"errors"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func TestAgentDriverClosedStoreCannotAdmitOrQueryAnEffect(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	phase, _, err := driver.phase(run)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := driver.admission(context.Background(), run, phase, nil)
	if err != nil {
		t.Fatal(err)
	}
	record, found, err := driver.service.control.Get("action", claim.Fence.ActionId)
	if err != nil || !found {
		t.Fatal(err)
	}
	if err := driver.service.control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := driver.phase(run); err == nil {
		t.Fatal("closed store fabricated a phase")
	}
	if _, err := driver.admission(context.Background(), run, phase, &record); err == nil {
		t.Fatal("closed store renewed admission")
	}
	if _, err := driver.node(context.Background(), fixture, fixture.nodes, claim, nil, "leader"); err == nil {
		t.Fatal("closed store bypassed dispatch custody")
	}
	if err := driver.drive(context.Background(), run); err == nil || len(fixture.calls) != 0 {
		t.Fatal("closed store dispatched provider work", err)
	}
}

func TestAgentDriverStaleCallerMetadataCannotAdmitOrDispatch(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	stale := run
	stale.NextIndex++
	if _, err := driver.admission(context.Background(), stale, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, nil); err == nil {
		t.Fatal("stale caller admitted")
	}
	if err := driver.drive(context.Background(), stale); err == nil {
		t.Fatal("stale caller dispatched")
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 0 || len(fixture.calls) != 0 {
		t.Fatal("stale caller retained an admission or effect", err)
	}
	if driverTasksComplete(agent.RunMetadata{}) {
		t.Fatal("empty plan was treated as completed")
	}
	if driverErrorCode(context.Canceled) != "AGENT_DRIVER_CANCELLED" || driverErrorCode(errors.Join(context.Canceled, errors.New("offline-private-body"))) != "AGENT_DRIVER_CANCELLED" {
		t.Fatal("cancelled driver error exposed caller details")
	}
}

func TestAgentDriverIgnoresOtherRunAndFindsOriginalPendingSynthesis(t *testing.T) {
	driver, fixture, run, action := cancelledDriverRecoveryFixture(t, "synthesis", 60)
	phase, existing, err := driver.phase(run)
	if err != nil || phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS || existing == nil || existing.ID != action {
		t.Fatal("pending synthesis was replaced by fresh task admission", err)
	}
	unrelated := run
	unrelated.RunID += "-other"
	if _, existing, err := driver.phase(unrelated); err != nil || existing != nil {
		t.Fatal("another run reused original effect", err)
	}
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	if len(fixture.calls) != 4 {
		t.Fatal("pending synthesis query repeated provider work", fixture.calls)
	}
}

func TestCancellationEmptyNodeSelectorCannotDiscardOriginalDispatch(t *testing.T) {
	driver, fixture, run, action := cancelledDriverRecoveryFixture(t, "tasks", 60)
	calls := len(fixture.calls)
	changed := run
	changed.Plan = append([]agent.RunPlanNode(nil), run.Plan...)
	changed.Plan[0].ID = ""
	if err := driver.recoverCancellation(context.Background(), changed); err == nil {
		t.Fatal("empty node selector bypassed original custody")
	}
	assertCancellationRecoveryRetainsCustody(t, driver, fixture, run, action, calls)
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal("invalid selector poisoned original custody", err)
	}
}
