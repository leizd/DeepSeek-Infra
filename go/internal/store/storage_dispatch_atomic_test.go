package store

import (
	"errors"
	"strings"
	"sync"
	"testing"
)

func TestStorageDispatchFinalInsertFailureRollsBackClaimEventAndLease(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	record := claimedAction(t, control)
	before := control.Writer()
	if _, err := control.db.Exec(`CREATE TRIGGER injected_dispatch_insert BEFORE INSERT ON storage_dispatches BEGIN SELECT RAISE(ABORT,'injected final insert'); END`); err != nil {
		t.Fatal(err)
	}
	control.now = func() int64 { return 1001 }
	if err := control.ClaimStorageDispatch(record, dispatchIntent()); err == nil || !strings.Contains(err.Error(), "injected final insert") {
		t.Fatalf("must reach the injected final insert, not fail at an earlier guard: %v", err)
	}
	assertNoDispatchClaim(t, control)
	var until int64
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer").Scan(&until); err != nil || until != before.LeaseUntil {
		t.Fatalf("lease=%d err=%v", until, err)
	}
	if control.Writer() != before {
		t.Fatal("failed claim changed cached writer")
	}
	if _, err := control.db.Exec("DROP TRIGGER injected_dispatch_insert"); err != nil {
		t.Fatal(err)
	}
	if err := control.ClaimStorageDispatch(record, dispatchIntent()); err != nil {
		t.Fatal(err)
	}
}

func assertNoDispatchClaim(t *testing.T, control *Control) {
	t.Helper()
	record, ok, err := control.Get("action", "a")
	if err != nil || !ok || record.State != "CLAIMED" || record.Revision != 2 {
		t.Fatalf("claim=%+v ok=%v err=%v", record, ok, err)
	}
	for table, want := range map[string]int{"storage_dispatches": 0, "control_events": 2} {
		var count int
		if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != want {
			t.Fatalf("%s count=%d err=%v", table, count, err)
		}
	}
}

func TestStorageDispatchLeaseMustStillBeLiveAtCommit(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	record := claimedAction(t, control)
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1000
		}
		return 1030
	}
	if err := control.ClaimStorageDispatch(record, dispatchIntent()); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expired commit: %v", err)
	}
	assertNoDispatchClaim(t, control)
}

func TestStorageDispatchOnlyOneConcurrentClaimWins(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	record := claimedAction(t, control)
	var group sync.WaitGroup
	results := make(chan error, 2)
	for range 2 {
		group.Go(func() { results <- control.ClaimStorageDispatch(record, dispatchIntent()) })
	}
	group.Wait()
	close(results)
	wins := 0
	for err := range results {
		if err == nil {
			wins++
		} else if !errors.Is(err, ErrIllegalTransition) {
			t.Fatal(err)
		}
	}
	if wins != 1 {
		t.Fatalf("dispatch winners=%d", wins)
	}
}

func TestStorageDispatchReadRejectsBrokenActionHistory(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.ClaimStorageDispatch(claimedAction(t, control), dispatchIntent()); err != nil {
		t.Fatal(err)
	}
	// Corruption fixture, not an effect: leave the dispatch event intact but
	// break the earlier action chain. A matching last event alone is not enough.
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`UPDATE control_events SET payload_json='{"tampered":true}' WHERE revision=1`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.GetStorageDispatch("a", 1); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("broken action history accepted: %v", err)
	}
}
