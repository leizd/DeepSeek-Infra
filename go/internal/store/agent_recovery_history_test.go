package store

import (
	"errors"
	"testing"
)

func TestRecoveryHistoryRequiresTheOriginalTypedCancellationSettlement(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	defer control.Close()
	before := countControlEvents(t, control)
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	_, err = control.agentRecoveryHistoryTx(tx, run, 0)
	_ = tx.Rollback()
	if !errors.Is(err, ErrAgentRunRecoveryUnsettled) || countControlEvents(t, control) != before {
		t.Fatal("unsettled cancelled effects authorized recovery", err)
	}
	if _, err := control.SettleAgentCancellation(input); err != nil {
		t.Fatal(err)
	}
	before = countControlEvents(t, control)
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	history, err := control.agentRecoveryHistoryTx(tx, run, 0)
	_ = tx.Rollback()
	if err != nil || len(history.effects) != 1 || len(history.outputs) != 0 || len(history.synthesis) != 0 {
		t.Fatal("confirmed cancellation was lost or invented public success", history, err)
	}
	if history.effects[0].Dispatch.RunId != run.RunID || history.effects[0].SettlementMetadataIndex != 0 ||
		history.effects[0].Outcome == nil || history.effects[0].Outcome.Evidence == nil {
		t.Fatal("original Rust outcome provenance was not retained")
	}
	if countControlEvents(t, control) != before {
		t.Fatal("history inspection wrote control state")
	}
}

func TestRecoveryHistoryRefusesExpiredRunReservations(t *testing.T) {
	control, input := runtimeUpdateFixture(t, true)
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	before := countControlEvents(t, control)
	now := control.now()
	control.now = func() int64 { return now + 10_000 }
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.agentRecoveryHistoryTx(tx, run, 0); !errors.Is(err, ErrAgentRunRecoveryUnsettled) {
		t.Fatal("lease expiry substituted for verified settlement", err)
	}
	_ = tx.Rollback()
	if countControlEvents(t, control) != before {
		t.Fatal("refusal changed control history")
	}
}
