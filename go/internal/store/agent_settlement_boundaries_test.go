package store

import (
	"errors"
	"strings"
	"testing"
)

func TestSettlementEntryPointsRefuseUnknownActions(t *testing.T) {
	control, source, _ := agentOutcomeFixture(t)
	_, err := control.SettleAgentTask(AgentTaskSettlementRequest{OperationID: "settle-unknown-task", Actor: "operator:boundary-test",
		ActionID: "action_missing_boundary", RunID: source.RunID, NodeID: "coder", Epoch: 1, ExpectedIndex: 1,
		SourceReceiptSHA256: strings.Repeat("a", 64)})
	if !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("an unknown action was settled: %v", err)
	}
}
