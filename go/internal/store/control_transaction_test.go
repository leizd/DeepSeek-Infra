package store

import (
	"errors"
	"strings"
	"testing"
)

func TestControlWriteRejectsWriterExpiryAtCommit(t *testing.T) {
	for _, nextState := range []string{"CLAIMED", "FAILED_BEFORE_EFFECT"} {
		t.Run(nextState, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			record := Record{Domain: "action", ID: "atomic-action", Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: []byte(`{}`)}
			if err := control.Put(record); err != nil {
				t.Fatal(err)
			}
			before := control.Writer()
			calls := 0
			control.now = func() int64 {
				calls++
				if calls == 1 {
					return 1001
				}
				return 1031 // The lease tentatively renewed in this transaction is now expired.
			}
			next := record
			next.Revision++
			next.State = nextState
			if err := control.Put(next); !errors.Is(err, ErrWriterFenceHeld) {
				t.Fatalf("write committed after writer expiry: %v", err)
			}
			got, exists, err := control.Get("action", record.ID)
			if err != nil || !exists || !sameControlRecord(got, record) {
				t.Fatalf("failed write changed action: %+v exists=%v err=%v", got, exists, err)
			}
			assertControlTransactionUnchanged(t, control, before, 1)
		})
	}
}

func assertControlTransactionUnchanged(t *testing.T, control *Control, writer WriterLease, events int) {
	t.Helper()
	var persistedLease int64
	var eventCount int
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer").Scan(&persistedLease); err != nil || persistedLease != writer.LeaseUntil {
		t.Fatalf("persisted lease=%d err=%v", persistedLease, err)
	}
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_events").Scan(&eventCount); err != nil || eventCount != events {
		t.Fatalf("event count=%d err=%v", eventCount, err)
	}
	if control.Writer() != writer {
		t.Fatal("failed write changed cached writer")
	}
}

func TestControlRecordTransactionLeavesCommitAndWriterToCaller(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	before := control.Writer()
	write, err := prepareControlRecordWrite(Record{Domain: "policy", ID: "transaction-policy", Revision: 1, State: "ACTIVE"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback() // Run before Close even if an assertion fails.
	if err := control.putControlRecordTx(tx, write, 1001); err != nil {
		t.Fatal(err)
	}
	var count int
	var lease int64
	if err := tx.QueryRow("SELECT COUNT(*) FROM policies").Scan(&count); err != nil || count != 1 {
		t.Fatalf("transaction must remain open with its write: count=%d err=%v", count, err)
	}
	if err := tx.QueryRow("SELECT lease_until FROM control_writer").Scan(&lease); err != nil || lease != before.LeaseUntil {
		t.Fatalf("helper must not renew writer itself: lease=%d err=%v", lease, err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, exists, err := control.Get("policy", "transaction-policy"); err != nil || exists {
		t.Fatalf("helper committed outside caller transaction: exists=%v err=%v", exists, err)
	}
	assertControlTransactionUnchanged(t, control, before, 0)
}

func TestControlRecordTransactionRollsBackDispatchAndCallerWriterRenewal(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	record := claimedAction(t, control)
	before := control.Writer()
	intent := dispatchIntent()
	write, err := prepareControlRecordWrite(record, &intent)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.assertWriterTx(tx, 1001); err != nil {
		t.Fatal(err)
	}
	if err := control.putControlRecordTx(tx, write, 1001); err != nil {
		t.Fatal(err)
	}
	for table, want := range map[string]int{"storage_dispatches": 1, "control_events": 3} {
		var count int
		if err := tx.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != want {
			t.Fatalf("pending %s=%d err=%v", table, count, err)
		}
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	assertNoDispatchClaim(t, control)
	assertControlTransactionUnchanged(t, control, before, 2)
}

func TestPreparedControlRecordWriteOwnsCanonicalMetadata(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	record := claimedAction(t, control)
	record.Payload = []byte(`{"intent":"original"}`)
	intent := dispatchIntent()
	write, err := prepareControlRecordWrite(record, &intent)
	if err != nil {
		t.Fatal(err)
	}
	// Caller mutations after preparation must not rebind the durable write.
	copy(record.Payload, strings.Repeat("x", len(record.Payload)))
	intent.OperationID = "substituted-operation"
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.putControlRecordTx(tx, write, 1000); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	got, exists, err := control.GetStorageDispatch("a", 1)
	if err != nil || !exists || got.Intent.OperationID != dispatchIntent().OperationID {
		t.Fatalf("prepared dispatch changed: %+v exists=%v err=%v", got, exists, err)
	}
	gotRecord, exists, err := control.Get("action", "a")
	if err != nil || !exists || string(gotRecord.Payload) != `{"intent":"original"}` {
		t.Fatalf("prepared payload changed: %+v exists=%v err=%v", gotRecord, exists, err)
	}
}

func TestControlRecordTransactionErrorDoesNotEndCallerTransaction(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	before := control.Writer()
	first, err := prepareControlRecordWrite(Record{Domain: "policy", ID: "first", Revision: 1, State: "ACTIVE"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	invalidTransition, err := prepareControlRecordWrite(Record{Domain: "action", ID: "second", Revision: 1, ExecutionEpoch: 1, State: "SUCCEEDED"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.putControlRecordTx(tx, first, 1000); err != nil {
		t.Fatal(err)
	}
	if err := control.putControlRecordTx(tx, invalidTransition, 1000); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("invalid transition: %v", err)
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM policies").Scan(&count); err != nil || count != 1 {
		t.Fatalf("helper ended caller's multi-write transaction: count=%d err=%v", count, err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, exists, err := control.Get("policy", "first"); err != nil || exists {
		t.Fatalf("caller rollback lost ownership: exists=%v err=%v", exists, err)
	}
	assertControlTransactionUnchanged(t, control, before, 0)
}
