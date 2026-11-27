package store

import (
	"errors"
	"testing"
)

// Both settlement writers must refuse an action that is absent, belongs to
// another run, or carries the wrong phase. Those branches were uncovered in the
// plan and task paths, and a wrong acceptance would settle a phase the
// controller never proved.

func TestPlanSettlementRefusesAnAbsentOrForeignAction(t *testing.T) {
	t.Run("absent-action", func(t *testing.T) {
		control, input, _ := planSettlementFixture(t, false)
		foreign := input
		foreign.ActionID = "action_that_does_not_exist"
		foreign.OperationID = "plan-settle-absent"
		_, err := control.SettleAgentPlan(foreign)
		if !errors.Is(err, ErrCorruptRecord) && !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("an absent action settled a plan: %v", err)
		}
	})

	t.Run("foreign-run", func(t *testing.T) {
		control, input, _ := planSettlementFixture(t, false)
		foreign := input
		foreign.RunID = "run_that_does_not_own_this_action"
		foreign.OperationID = "plan-settle-foreign-run"
		_, err := control.SettleAgentPlan(foreign)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a foreign run settled this plan: %v", err)
		}
	})

	t.Run("stale-epoch", func(t *testing.T) {
		control, input, _ := planSettlementFixture(t, false)
		stale := input
		stale.Epoch = input.Epoch + 7
		stale.OperationID = "plan-settle-stale-epoch"
		_, err := control.SettleAgentPlan(stale)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a stale epoch settled this plan: %v", err)
		}
	})
}

func TestPlanSettlementRefusesAMalformedRequest(t *testing.T) {
	control, input, _ := planSettlementFixture(t, false)
	for name, mutate := range map[string]func(*AgentPlanSettlementRequest){
		"empty-operation": func(in *AgentPlanSettlementRequest) { in.OperationID = "" },
		"empty-action":    func(in *AgentPlanSettlementRequest) { in.ActionID = "" },
		"empty-run":       func(in *AgentPlanSettlementRequest) { in.RunID = "" },
		"empty-owner":     func(in *AgentPlanSettlementRequest) { in.Owner = "" },
		"empty-token":     func(in *AgentPlanSettlementRequest) { in.ClaimToken = "" },
		"zero-epoch":      func(in *AgentPlanSettlementRequest) { in.Epoch = 0 },
		"nil-projection":  func(in *AgentPlanSettlementRequest) { in.Projection = nil },
	} {
		t.Run(name, func(t *testing.T) {
			changed := input
			mutate(&changed)
			if _, err := control.SettleAgentPlan(changed); err == nil {
				t.Fatalf("%s settled a plan", name)
			}
		})
	}
}

func TestTaskSettlementRefusesAnAbsentOrForeignAction(t *testing.T) {
	t.Run("absent-action", func(t *testing.T) {
		control, input, _ := taskSettlementFixture(t, false)
		foreign := input
		foreign.ActionID = "action_that_does_not_exist"
		foreign.OperationID = "task-settle-absent"
		_, err := control.SettleAgentTask(foreign)
		if !errors.Is(err, ErrCorruptRecord) && !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("an absent action settled a task: %v", err)
		}
	})

	t.Run("foreign-run", func(t *testing.T) {
		control, input, _ := taskSettlementFixture(t, false)
		foreign := input
		foreign.RunID = "run_that_does_not_own_this_action"
		foreign.OperationID = "task-settle-foreign-run"
		_, err := control.SettleAgentTask(foreign)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a foreign run settled this task: %v", err)
		}
	})

	t.Run("stale-epoch", func(t *testing.T) {
		control, input, _ := taskSettlementFixture(t, false)
		stale := input
		stale.Epoch = input.Epoch + 7
		stale.OperationID = "task-settle-stale-epoch"
		_, err := control.SettleAgentTask(stale)
		if !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("a stale epoch settled this task: %v", err)
		}
	})
}

func TestTaskSettlementRefusesAMalformedRequest(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	for name, mutate := range map[string]func(*AgentTaskSettlementRequest){
		"empty-operation": func(in *AgentTaskSettlementRequest) { in.OperationID = "" },
		"empty-action":    func(in *AgentTaskSettlementRequest) { in.ActionID = "" },
		"empty-run":       func(in *AgentTaskSettlementRequest) { in.RunID = "" },
		"empty-node":      func(in *AgentTaskSettlementRequest) { in.NodeID = "" },
		"empty-owner":     func(in *AgentTaskSettlementRequest) { in.Owner = "" },
		"empty-token":     func(in *AgentTaskSettlementRequest) { in.ClaimToken = "" },
		"zero-epoch":      func(in *AgentTaskSettlementRequest) { in.Epoch = 0 },
	} {
		t.Run(name, func(t *testing.T) {
			changed := input
			mutate(&changed)
			if _, err := control.SettleAgentTask(changed); err == nil {
				t.Fatalf("%s settled a task", name)
			}
		})
	}
}
