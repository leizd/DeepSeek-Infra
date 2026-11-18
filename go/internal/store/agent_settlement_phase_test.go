package store

import (
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func TestSettlementEntryPointsRefuseAMismatchedPhase(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	actionOf := func(phase string) string {
		t.Helper()
		var id string
		if err := control.db.QueryRow(`SELECT id FROM action_journal WHERE json_extract(payload_json,'$.nativeAgentExecution.runId')=?
            AND json_extract(payload_json,'$.nativeAgentExecution.phase')=? LIMIT 1`, runID, phase).Scan(&id); err != nil {
			t.Skipf("no %s action in the fixture: %v", phase, err)
		}
		return id
	}
	tasks := actionOf("tasks")
	plan := actionOf("plan")
	if _, err := control.SettleAgentTask(AgentTaskSettlementRequest{OperationID: "phase-task-on-plan", Actor: "operator:boundary-test",
		ActionID: plan, RunID: runID, NodeID: "coder", Epoch: 1, ExpectedIndex: 1, SourceReceiptSHA256: strings.Repeat("a", 64)}); err == nil {
		t.Fatal("a task settlement accepted a plan action")
	}
	if _, err := control.SettleAgentPlan(AgentPlanSettlementRequest{OperationID: "phase-plan-on-tasks", Actor: "operator:boundary-test",
		ActionID: tasks, RunID: runID, Epoch: 1, Projection: &agentv1.AgentPlanProjectionReceipt{}}); err == nil {
		t.Fatal("a plan settlement accepted a tasks action")
	}
	if _, err := control.SettleAgentSynthesis(AgentSynthesisSettlementRequest{OperationID: "phase-synthesis-on-tasks", Actor: "operator:boundary-test",
		ActionID: tasks, RunID: runID, Epoch: 1, Projection: &agentv1.AgentSynthesisProjectionReceipt{}}); err == nil {
		t.Fatal("a synthesis settlement accepted a tasks action")
	}
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
