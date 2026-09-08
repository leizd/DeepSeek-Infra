package store

import (
	"errors"
	"strings"
	"testing"
)

func TestStorageDispatchSchemaIsAdditive(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if got := control.SchemaVersion(); got != 4 {
		t.Fatalf("dispatch intent requires schema 4; got %d", got)
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM storage_dispatches").Scan(&count); err != nil || count != 0 {
		t.Fatalf("new dispatch journal count=%d err=%v", count, err)
	}
	if err := verifySchemaTxForTest(control); err != nil {
		t.Fatal(err)
	}
}

func verifySchemaTxForTest(control *Control) error {
	tx, err := control.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	return verifySchemaTx(tx, control.schema)
}

func TestStorageDispatchHistoryCannotBeRolledBack(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.ClaimStorageDispatch(claimedAction(t, control), dispatchIntent()); err != nil {
		t.Fatal(err)
	}
	before := control.Writer()
	if err := control.Rollback(0); !errors.Is(err, ErrDispatchHistoryRetained) {
		t.Fatalf("rollback must retain intent history: %v", err)
	}
	if got := control.Writer(); got != before {
		t.Fatalf("failed rollback changed writer: %+v", got)
	}
	if control.SchemaVersion() != CurrentSchema {
		t.Fatal("failed rollback changed schema")
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_events").Scan(&count); err != nil || count != 3 {
		t.Fatalf("history count=%d err=%v", count, err)
	}
}

func dispatchIntent() StorageDispatchIntent {
	return StorageDispatchIntent{
		Schema: "storage-dispatch-intent-v1", ActionID: "a", ExecutionEpoch: 1, OperationID: "operation-a",
		MutationType: "PUT_CHUNK", Provider: "s3", TargetIdentity: strings.Repeat("a", 64),
		Bucket: "test-bucket", Prefix: "test", ObjectKey: "object", PayloadDigest: strings.Repeat("b", 64),
		ExpectedLength: 16, Condition: "CREATE_ONLY", AuthorizationDigest: strings.Repeat("c", 64),
	}
}

func claimedAction(t *testing.T, control *Control) Record {
	t.Helper()
	record := Record{Domain: "action", ID: "a", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}
	if err := control.Put(record); err != nil {
		t.Fatal(err)
	}
	record.Revision = 2
	record.State = "CLAIMED"
	if err := control.Put(record); err != nil {
		t.Fatal(err)
	}
	record.Revision = 3
	record.State = "EXECUTING"
	return record
}

func TestStorageDispatchClaimPersistsExactIntentAcrossGoWriterTakeover(t *testing.T) {
	control := openControlAt(t, 1000)
	path := control.path
	record := claimedAction(t, control)
	intent := dispatchIntent()
	if err := control.ClaimStorageDispatch(record, intent); err != nil {
		t.Fatal(err)
	}
	got, exists, err := control.GetStorageDispatch("a", 1)
	if err != nil || !exists || got.Intent != intent || got.ClaimRevision != 3 || got.WriterFencingToken != 1 {
		t.Fatalf("intent=%+v exists=%v err=%v", got, exists, err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "successor", Now: func() int64 { return 1000 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	recovered, exists, err := reopened.GetStorageDispatch("a", 1)
	if err != nil || !exists || recovered != got {
		t.Fatalf("recovered=%+v exists=%v err=%v", recovered, exists, err)
	}
	if reopened.Writer().FencingToken != 2 {
		t.Fatal("expected successor writer fence")
	}
	current, exists, err := reopened.Get("action", "a")
	if err != nil || !exists || current.State != "EXECUTING" {
		t.Fatalf("action=%+v exists=%v err=%v", current, exists, err)
	}
}
