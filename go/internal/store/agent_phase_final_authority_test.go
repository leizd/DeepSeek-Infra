package store

import (
	"errors"
	"reflect"
	"testing"
)

// Exercise the actual public store methods after their earlier deadline check has
// passed. Crossing the original action deadline in the shared finish must undo
// all durable writes and reservations, and permit a valid retry.
func TestAgentLivePhaseFinalActionDeadlineRollsBackEveryDurableWrite(t *testing.T) {
	for _, phase := range []string{"claim", "dispatch", "plan", "task", "tasks"} {
		t.Run(phase, func(t *testing.T) {
			var control *Control
			var apply func() error
			var actionID string
			finalCall := 3
			var deadline int64
			switch phase {
			case "claim":
				control = agentExecutionControl(t, true)
				input := agentExecutionRequest(t, control)
				input.LeaseSeconds = 5
				deadline = control.now() + input.LeaseSeconds
				finalCall = 4 // Initial admission also checks the clock.
				apply = func() error { _, err := control.ClaimAgentRunExecution(input); return err }
			case "dispatch":
				var input AgentNodeDispatchRequest
				control, input = agentNodeDispatchFixture(t)
				actionID = input.ActionID
				apply = func() error { _, err := control.BeginAgentNodeDispatch(input); return err }
			case "plan":
				var input AgentPlanSettlementRequest
				control, input, _ = planSettlementFixture(t, false)
				actionID = input.ActionID
				apply = func() error { _, err := control.SettleAgentPlan(input); return err }
			case "task":
				var input AgentTaskSettlementRequest
				control, input, _ = taskSettlementFixture(t, false)
				actionID = input.ActionID
				apply = func() error { _, err := control.SettleAgentTask(input); return err }
			case "tasks":
				var input CompleteAgentTasksRequest
				control, input = tasksCompletionFixture(t, false)
				actionID = input.ActionID
				apply = func() error { _, err := control.CompleteAgentTasks(input); return err }
			}
			if actionID != "" {
				lease, found, err := control.GetActionLease(actionID)
				if err != nil || !found {
					t.Fatalf("missing original phase lease: %v", err)
				}
				deadline = lease.LeaseUntil
			}
			original := control.now
			start := original()
			// Keep the writer valid to isolate the original action deadline.
			control.leaseSeconds = deadline - start + 120
			before := synthesisSnapshot(t, control)
			calls := 0
			control.now = func() int64 {
				calls++
				if calls >= finalCall {
					return deadline
				}
				return start
			}
			err := apply()
			control.now = original
			if calls != finalCall || !errors.Is(err, ErrActionLeaseExpired) || synthesisSnapshot(t, control) != before {
				t.Fatalf("final %s deadline retained partial durable state: calls=%d err=%v", phase, calls, err)
			}
			if err := apply(); err != nil {
				t.Fatalf("rollback poisoned valid %s retry: %v", phase, err)
			}
		})
	}
}

// The writer can renew while a transaction is in progress; the returned live
// deadline must be the one actually committed, without renewing the action lease.
func TestAgentExecutionReturnsTheFinallyCommittedWriterDeadline(t *testing.T) {
	control := agentExecutionControl(t, true)
	input := agentExecutionRequest(t, control)
	original := control.now
	start, calls := original(), int64(0)
	control.now = func() int64 { calls++; return start + calls }
	first, err := control.ClaimAgentRunExecution(input)
	if err != nil {
		t.Fatal(err)
	}
	assertDeadline := func(result AgentRunExecutionResult) {
		t.Helper()
		var durable int64
		if err := control.db.QueryRow("SELECT lease_until FROM control_writer WHERE singleton=1").Scan(&durable); err != nil {
			t.Fatal(err)
		}
		if result.WriterLeaseUntil != durable || control.leaseUntil != durable || durable != start+calls+control.leaseSeconds {
			t.Fatalf("advertised writer deadline differs from committed authority: result=%d durable=%d cached=%d", result.WriterLeaseUntil, durable, control.leaseUntil)
		}
	}
	assertDeadline(first)
	events := countControlEvents(t, control)
	replay, err := control.ClaimAgentRunExecution(input)
	if err != nil {
		t.Fatal(err)
	}
	assertDeadline(replay)
	if !reflect.DeepEqual(replay.Admission.Lease, first.Admission.Lease) || countControlEvents(t, control) != events {
		t.Fatal("writer renewal changed original action authority or duplicated control history")
	}
	control.now = original
}
