package store

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

func TestRenewWriterKeepsIdleLeaseWithoutChangingDomainState(t *testing.T) {
	now := int64(1000)
	control, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	record := Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}
	if err := control.Put(record); err != nil {
		t.Fatal(err)
	}
	initial := control.Writer()
	now = 1009
	if err := control.RenewWriter(context.Background()); err != nil {
		t.Fatal(err)
	}
	if writer := control.Writer(); writer.FencingToken != initial.FencingToken || writer.OwnerInstanceID != initial.OwnerInstanceID || writer.LeaseUntil != 1019 {
		t.Fatalf("renewed writer: %+v", writer)
	}
	var persisted int64
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer WHERE singleton=1").Scan(&persisted); err != nil || persisted != 1019 {
		t.Fatalf("durable lease: %d %v", persisted, err)
	}
	got, ok, err := control.Get("policy", "p1")
	if err != nil || !ok || got.Revision != 1 || got.State != "ACTIVE" {
		t.Fatalf("changed domain: %+v %v", got, err)
	}
	var events int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_events").Scan(&events); err != nil || events != 1 {
		t.Fatalf("changed history: %d %v", events, err)
	}
	now = 1019
	if err := control.RenewWriter(context.Background()); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expired lease resurrected: %v", err)
	}
}

func TestRenewWriterCannotReviveOldOwnerAfterTakeover(t *testing.T) {
	now := int64(1000)
	path := t.TempDir()
	first, err := OpenControl(OpenOptions{Path: path, Owner: "old", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	now = 1011
	second, err := OpenControl(OpenOptions{Path: path, Owner: "new", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if err := first.RenewWriter(context.Background()); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("stale renewal: %v", err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	if err := second.RenewWriter(context.Background()); err != nil {
		t.Fatalf("stale close released successor: %v", err)
	}
}

func TestRenewWriterCancellationAndCorruptionRollBack(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	initial := control.Writer()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := control.RenewWriter(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled renewal: %v", err)
	}
	if _, err := control.db.Exec("PRAGMA user_version=999"); err != nil {
		t.Fatal(err)
	}
	if err := control.RenewWriter(context.Background()); err == nil {
		t.Fatal("corrupt schema renewed")
	}
	var persisted int64
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer WHERE singleton=1").Scan(&persisted); err != nil || persisted != initial.LeaseUntil {
		t.Fatalf("failed renewal leaked: %d %v", persisted, err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if err := control.RenewWriter(context.Background()); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed renewal: %v", err)
	}
}

func TestRenewWriterRejectsInactiveSchemaAndDeadlineBeforeCommit(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if err := control.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if err := control.RenewWriter(context.Background()); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("inactive renewal: %v", err)
	}

	now := int64(1000)
	advance := false
	clock := func() int64 {
		result := now
		if advance {
			now += 11
		}
		return result
	}
	other, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "slow", Now: clock, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	initial := other.Writer()
	now, advance = 1009, true
	if err := other.RenewWriter(context.Background()); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("renewal finished after its deadline: %v", err)
	}
	var persisted int64
	if err := other.db.QueryRow("SELECT lease_until FROM control_writer WHERE singleton=1").Scan(&persisted); err != nil || persisted != initial.LeaseUntil {
		t.Fatalf("expired renewal committed: %d %v", persisted, err)
	}
}

func TestRenewWriterCancellationBeforeCommitPreservesPriorLease(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	now, calls := int64(1000), 0
	cancelOnCommit := false
	clock := func() int64 {
		if cancelOnCommit {
			calls++
			if calls == 2 {
				cancel()
			}
		}
		return now
	}
	control, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "cancelled", Now: clock})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	initial := control.Writer()
	now, cancelOnCommit = 1009, true
	if err := control.RenewWriter(ctx); err == nil {
		t.Fatal("cancelled transaction committed")
	}
	var persisted int64
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer WHERE singleton=1").Scan(&persisted); err != nil || persisted != initial.LeaseUntil {
		t.Fatalf("partial renewal persisted: %d %v", persisted, err)
	}
	if control.Writer().LeaseUntil != initial.LeaseUntil {
		t.Fatal("uncommitted lease published in memory")
	}
}
