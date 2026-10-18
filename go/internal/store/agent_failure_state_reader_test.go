package store

import (
	"errors"
	"reflect"
	"testing"
)

func TestConclusiveFailureReaderAddsNoGenericTransition(t *testing.T) {
	if !knownState("action", "FAILED") {
		t.Fatal("typed terminal failure is unreadable after the writer accepted it")
	}
	for _, state := range []string{"", "PENDING", "CLAIMED", "EXECUTING", "EFFECT_UNKNOWN", "RECONCILING", "VERIFYING", "FAILED"} {
		if LegalTransition("action", state, "FAILED") {
			t.Fatal("reader recognition granted generic failure settlement", state)
		}
	}
	control, input, _ := failureSettlementFixture(t, "plan")
	before := synthesisSnapshot(t, control)
	if _, err := control.transitionLeasedAction(input.ActionID, input.Epoch, input.ClaimToken, "FAILED", nil); err == nil {
		t.Fatal("generic action writer settled a typed provider failure")
	}
	if synthesisSnapshot(t, control) != before {
		t.Fatal("unproved terminal transition changed history or custody")
	}
	if _, active, err := control.GetActionLease(input.ActionID); err != nil || !active {
		t.Fatal("generic failure released the original unresolved lease", err)
	}
}

func TestConclusiveFailureCanBeReadOnlyWithItsVerifiedHistory(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, phase)
			result, err := control.SettleAgentFailure(input)
			if err != nil {
				t.Fatal("typed writer could not read back its own terminal checkpoint", err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			record, found, err := readAgentExecutionActionTx(tx, input.ActionID)
			if err != nil || !found || record.State != "FAILED" || !reflect.DeepEqual(record, result.Action) {
				t.Fatal("read lost the original typed terminal checkpoint", err)
			}
			if err := validateControlHistory(tx, record); err != nil {
				t.Fatal("typed terminal history is unreadable", err)
			}
			if err := control.validateAgentFailureSettlementTx(tx, record); err != nil {
				t.Fatal("typed terminal lost independent signed failure verification", err)
			}
			// Recognition of FAILED alone does not authenticate a phase. A
			// conclusive lease and the original action state must still agree.
			record.State = "SUCCEEDED"
			if err := control.validateAgentFailureSettlementTx(tx, record); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("static diagnostic lost typed corruption identity", err)
			}
		})
	}
}
