package store

import (
	"errors"
	"testing"
)

// A failure settlement must refuse when the action it names is absent, belongs
// to another run, or is not in a phase that can fail. Those branches were
// uncovered, and a wrong acceptance would settle a failure the controller never
// proved.

func TestFailureSettlementRefusesAnAbsentOrForeignAction(t *testing.T) {
	t.Run("absent-action", func(t *testing.T) {
		control, input, _ := failureSettlementFixture(t, "plan")
		foreign := input
		foreign.ActionID = "action_that_does_not_exist"
		foreign.OperationID = "failure-settle-absent"
		_, err := control.SettleAgentFailure(foreign)
		if !errors.Is(err, ErrCorruptRecord) && !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("an absent action settled a failure: %v", err)
		}
	})

	t.Run("foreign-run", func(t *testing.T) {
		control, input, _ := failureSettlementFixture(t, "plan")
		foreign := input
		foreign.RunID = "run_that_does_not_own_this_action"
		foreign.OperationID = "failure-settle-foreign-run"
		_, err := control.SettleAgentFailure(foreign)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a foreign run settled this failure: %v", err)
		}
	})

	t.Run("stale-epoch", func(t *testing.T) {
		control, input, _ := failureSettlementFixture(t, "plan")
		stale := input
		stale.Epoch = input.Epoch + 7
		stale.OperationID = "failure-settle-stale-epoch"
		_, err := control.SettleAgentFailure(stale)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a stale epoch settled this failure: %v", err)
		}
	})
}

func TestFailureSettlementRefusesAMalformedRequest(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	for name, mutate := range map[string]func(*AgentFailureSettlementRequest){
		"empty-operation": func(in *AgentFailureSettlementRequest) { in.OperationID = "" },
		"empty-action":    func(in *AgentFailureSettlementRequest) { in.ActionID = "" },
		"empty-run":       func(in *AgentFailureSettlementRequest) { in.RunID = "" },
		"empty-owner":     func(in *AgentFailureSettlementRequest) { in.Owner = "" },
		"empty-token":     func(in *AgentFailureSettlementRequest) { in.ClaimToken = "" },
		"zero-epoch":      func(in *AgentFailureSettlementRequest) { in.Epoch = 0 },
		"nil-projection":  func(in *AgentFailureSettlementRequest) { in.Projection = nil },
	} {
		t.Run(name, func(t *testing.T) {
			changed := input
			mutate(&changed)
			if _, err := control.SettleAgentFailure(changed); err == nil {
				t.Fatalf("%s settled a failure", name)
			}
		})
	}
}
