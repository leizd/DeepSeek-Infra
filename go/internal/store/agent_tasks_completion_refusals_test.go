package store

import (
	"errors"
	"testing"
)

// CompleteAgentTasks must refuse unless every execution task carries its own
// verified settlement marker. Those two guards were uncovered: without them a
// partially settled task set could be reported as complete.

func TestCompleteAgentTasksRefusesWithoutEverySettlementMarker(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	// Drop the per-task markers from the action payload so the count no longer
	// matches the execution tasks. The immutability trigger has to be removed for
	// the tamper and restored afterwards, or the store refuses every later write.
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	_, tamperErr := control.db.Exec(
		`UPDATE control_events SET payload_json = json_remove(payload_json, '$.nativeAgentTaskSettlements')
		 WHERE domain='action' AND record_id=?`,
		input.ActionID,
	)
	if _, restoreErr := control.db.Exec(controlEventImmutabilityTriggers[0]); restoreErr != nil {
		t.Fatalf("restore immutability trigger: %v", restoreErr)
	}
	if tamperErr != nil {
		t.Skipf("cannot drop the settlement markers: %v", tamperErr)
	}
	if _, err := control.CompleteAgentTasks(input); !errors.Is(err, ErrAgentNodeOutcomeRequired) &&
		!errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("an unsettled task set was completed: %v", err)
	}
}

func TestCompleteAgentTasksRefusesAForeignOrStaleEnvelope(t *testing.T) {
	t.Run("foreign-run", func(t *testing.T) {
		control, input := tasksCompletionFixture(t, false)
		foreign := input
		foreign.RunID = "run_that_does_not_own_this_action"
		if _, err := control.CompleteAgentTasks(foreign); err == nil {
			t.Fatal("a foreign run completed these tasks")
		}
	})

	t.Run("stale-epoch", func(t *testing.T) {
		control, input := tasksCompletionFixture(t, false)
		stale := input
		stale.Epoch = input.Epoch + 7
		if _, err := control.CompleteAgentTasks(stale); err == nil {
			t.Fatal("a stale epoch completed these tasks")
		}
	})

	t.Run("empty-operation", func(t *testing.T) {
		control, input := tasksCompletionFixture(t, false)
		empty := input
		empty.OperationID = ""
		if _, err := control.CompleteAgentTasks(empty); err == nil {
			t.Fatal("an empty operation id completed these tasks")
		}
	})
}
