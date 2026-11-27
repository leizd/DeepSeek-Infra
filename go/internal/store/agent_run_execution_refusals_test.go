package store

import (
	"testing"
)

// ClaimAgentRunExecution and its neighbours must refuse an action that is absent,
// carries a foreign run, or names a stale epoch. Those branches were uncovered,
// and a wrong acceptance would hand out an execution claim over another run.

func TestRunExecutionRefusesAForeignOrStaleEnvelope(t *testing.T) {
	t.Run("foreign-run", func(t *testing.T) {
		control := agentExecutionControl(t, true)
		request := agentExecutionRequest(t, control)
		request.RunID = "run_that_does_not_own_this_action"
		if _, err := control.ClaimAgentRunExecution(request); err == nil {
			t.Fatal("a foreign run was claimed")
		}
	})

	t.Run("stale-epoch", func(t *testing.T) {
		control := agentExecutionControl(t, true)
		request := agentExecutionRequest(t, control)
		request.Fence.ExecutionEpoch = request.Fence.ExecutionEpoch + 7
		if _, err := control.ClaimAgentRunExecution(request); err == nil {
			t.Fatal("a stale epoch was claimed")
		}
	})

	t.Run("empty-operation", func(t *testing.T) {
		control := agentExecutionControl(t, true)
		request := agentExecutionRequest(t, control)
		request.OperationID = ""
		if _, err := control.ClaimAgentRunExecution(request); err == nil {
			t.Fatal("an empty operation id was claimed")
		}
	})

	t.Run("empty-owner", func(t *testing.T) {
		control := agentExecutionControl(t, true)
		request := agentExecutionRequest(t, control)
		request.Owner = ""
		if _, err := control.ClaimAgentRunExecution(request); err == nil {
			t.Fatal("an empty owner was claimed")
		}
	})
}
