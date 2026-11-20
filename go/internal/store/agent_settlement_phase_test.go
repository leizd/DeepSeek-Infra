package store

import (
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

// TestSettlementEntryPointsRefuseAMismatchedPhase pins that each settlement
// entry point refuses an action whose recorded phase is not its own.
//
// The two directions need different fixtures: `planSettlementFixture` owns a
// run whose only action is a plan, and `tasksCompletionFixture` owns one whose
// action is tasks. Reading both actions out of a single fixture was not
// possible, and the earlier version skipped the whole test when the missing
// phase was not found, so none of the three assertions ran.
func TestSettlementEntryPointsRefuseAMismatchedPhase(t *testing.T) {
	t.Run("a task settlement refuses a plan action", func(t *testing.T) {
		control, plan, _ := planSettlementFixture(t, true)
		if _, err := control.SettleAgentTask(AgentTaskSettlementRequest{
			OperationID: "phase-task-on-plan", Actor: "operator:boundary-test",
			ActionID: plan.ActionID, RunID: plan.RunID, NodeID: "coder",
			Epoch: plan.Epoch, ExpectedIndex: 1, SourceReceiptSHA256: strings.Repeat("a", 64),
		}); err == nil {
			t.Fatal("a task settlement accepted a plan action")
		}
	})

	t.Run("a plan settlement refuses a tasks action", func(t *testing.T) {
		control, complete := tasksCompletionFixture(t, false)
		if _, err := control.SettleAgentPlan(AgentPlanSettlementRequest{
			OperationID: "phase-plan-on-tasks", Actor: "operator:boundary-test",
			ActionID: complete.ActionID, RunID: complete.RunID, Epoch: 1,
			Projection: &agentv1.AgentPlanProjectionReceipt{},
		}); err == nil {
			t.Fatal("a plan settlement accepted a tasks action")
		}
	})

	t.Run("a synthesis settlement refuses a tasks action", func(t *testing.T) {
		control, complete := tasksCompletionFixture(t, false)
		if _, err := control.SettleAgentSynthesis(AgentSynthesisSettlementRequest{
			OperationID: "phase-synthesis-on-tasks", Actor: "operator:boundary-test",
			ActionID: complete.ActionID, RunID: complete.RunID, Epoch: 1,
			Projection: &agentv1.AgentSynthesisProjectionReceipt{},
		}); err == nil {
			t.Fatal("a synthesis settlement accepted a tasks action")
		}
	})
}

func TestSettlementRefusesAForeignLeaseClaim(t *testing.T) {
	control, source, _ := agentOutcomeFixture(t)
	run, found, err := control.AgentRunMetadata(source.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	_, err = control.SettleAgentTask(AgentTaskSettlementRequest{OperationID: "foreign-lease-claim", Actor: "operator:boundary-test",
		ActionID: source.ActionID, RunID: source.RunID, NodeID: "coder", Epoch: source.Epoch, ExpectedIndex: run.NextIndex,
		SourceReceiptSHA256: strings.Repeat("a", 64), Owner: "intruder", ClaimToken: "bogus-claim"})
	if err == nil {
		t.Fatal("a foreign lease claim settled a task")
	}
}
