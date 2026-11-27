package store

import (
	"errors"
	"testing"
)

// A synthesis settlement must refuse when the action it names is absent, belongs
// to another run, or is not in the synthesis phase. Each of those branches was
// uncovered, and a wrong acceptance would settle a phase the controller never
// proved.

func TestSynthesisSettlementRefusesAnAbsentOrForeignAction(t *testing.T) {
	t.Run("absent-action", func(t *testing.T) {
		control, input, _ := synthesisSettlementFixture(t)
		foreign := input
		foreign.ActionID = "action_that_does_not_exist"
		foreign.OperationID = "synthesis-settle-absent"
		_, err := control.SettleAgentSynthesis(foreign)
		if !errors.Is(err, ErrCorruptRecord) && !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("an absent action settled a synthesis: %v", err)
		}
	})

	t.Run("foreign-run", func(t *testing.T) {
		control, input, _ := synthesisSettlementFixture(t)
		foreign := input
		foreign.RunID = "run_that_does_not_own_this_action"
		foreign.OperationID = "synthesis-settle-foreign-run"
		_, err := control.SettleAgentSynthesis(foreign)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a foreign run settled this synthesis: %v", err)
		}
	})

	t.Run("stale-epoch", func(t *testing.T) {
		control, input, _ := synthesisSettlementFixture(t)
		stale := input
		stale.Epoch = input.Epoch + 7
		stale.OperationID = "synthesis-settle-stale-epoch"
		_, err := control.SettleAgentSynthesis(stale)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a stale epoch settled this synthesis: %v", err)
		}
	})
}

func TestSynthesisSettlementRefusesAMalformedRequest(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	for name, mutate := range map[string]func(*AgentSynthesisSettlementRequest){
		"empty-operation": func(in *AgentSynthesisSettlementRequest) { in.OperationID = "" },
		"empty-action":    func(in *AgentSynthesisSettlementRequest) { in.ActionID = "" },
		"empty-run":       func(in *AgentSynthesisSettlementRequest) { in.RunID = "" },
		"empty-owner":     func(in *AgentSynthesisSettlementRequest) { in.Owner = "" },
		"empty-token":     func(in *AgentSynthesisSettlementRequest) { in.ClaimToken = "" },
		"zero-epoch":      func(in *AgentSynthesisSettlementRequest) { in.Epoch = 0 },
		"nil-projection":  func(in *AgentSynthesisSettlementRequest) { in.Projection = nil },
	} {
		t.Run(name, func(t *testing.T) {
			changed := input
			mutate(&changed)
			if _, err := control.SettleAgentSynthesis(changed); err == nil {
				t.Fatalf("%s settled a synthesis", name)
			}
		})
	}
}
