package store

import (
	"errors"
	"math"
	"strings"
	"testing"
)

func TestStorageDispatchRejectsInvalidMetadataBeforeClaim(t *testing.T) {
	for name, corrupt := range map[string]func(*StorageDispatchIntent){
		"schema":               func(i *StorageDispatchIntent) { i.Schema = "unknown" },
		"action":               func(i *StorageDispatchIntent) { i.ActionID = "other" },
		"epoch":                func(i *StorageDispatchIntent) { i.ExecutionEpoch = 2 },
		"zero epoch":           func(i *StorageDispatchIntent) { i.ExecutionEpoch = 0 },
		"overflow epoch":       func(i *StorageDispatchIntent) { i.ExecutionEpoch = math.MaxUint64 },
		"blank operation":      func(i *StorageDispatchIntent) { i.OperationID = " \t\u2003" },
		"long operation":       func(i *StorageDispatchIntent) { i.OperationID = strings.Repeat("é", 513) },
		"nul operation":        func(i *StorageDispatchIntent) { i.OperationID = "op\x00" },
		"invalid utf8":         func(i *StorageDispatchIntent) { i.ObjectKey = string([]byte{0xff}) },
		"provider":             func(i *StorageDispatchIntent) { i.Provider = "filesystem" },
		"mutation":             func(i *StorageDispatchIntent) { i.MutationType = "DELETE" },
		"placement":            func(i *StorageDispatchIntent) { i.TargetIdentity = "" },
		"bucket":               func(i *StorageDispatchIntent) { i.Bucket = "" },
		"key":                  func(i *StorageDispatchIntent) { i.ObjectKey = "" },
		"digest":               func(i *StorageDispatchIntent) { i.PayloadDigest = strings.Repeat("G", 64) },
		"authorization digest": func(i *StorageDispatchIntent) { i.AuthorizationDigest = "" },
		"length":               func(i *StorageDispatchIntent) { i.ExpectedLength = 8*1024*1024 + 1 },
		"condition":            func(i *StorageDispatchIntent) { i.Condition = "UNCONDITIONAL" },
		"create etag":          func(i *StorageDispatchIntent) { i.ExpectedETag = "\"etag\"" },
		"match missing etag":   func(i *StorageDispatchIntent) { i.Condition = "IF_MATCH" },
		"weak etag":            func(i *StorageDispatchIntent) { i.Condition = "IF_MATCH"; i.ExpectedETag = "W/\"weak\"" },
		"etag newline":         func(i *StorageDispatchIntent) { i.Condition = "IF_MATCH"; i.ExpectedETag = "\"a\r\nb\"" },
	} {
		t.Run(name, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			record := claimedAction(t, control)
			intent := dispatchIntent()
			corrupt(&intent)
			if err := control.ClaimStorageDispatch(record, intent); !errors.Is(err, ErrInvalidStorageIntent) {
				t.Fatalf("invalid metadata: %v", err)
			}
			assertNoDispatchClaim(t, control)
		})
	}
}

func TestStorageDispatchImmutableGuardsAndExactConditionalMetadata(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	intent := dispatchIntent()
	intent.OperationID = " operation-A "
	intent.Condition = "IF_MATCH"
	intent.ExpectedETag = "\"opaque-etag\""
	intent.ExpectedLength = 0
	intent.RequestID = "request-id"
	intent.Nonce = "nonce"
	intent.RequestSchemaVersion = 1
	if err := control.ClaimStorageDispatch(claimedAction(t, control), intent); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"UPDATE storage_dispatches SET operation_id='different'",
		"DELETE FROM storage_dispatches",
		"INSERT OR REPLACE INTO storage_dispatches SELECT action_id,execution_epoch,'different',intent_json,intent_digest,claim_revision,writer_fencing_token,recorded_at FROM storage_dispatches",
		"INSERT OR REPLACE INTO storage_dispatches SELECT action_id,execution_epoch,operation_id,intent_json,intent_digest,claim_revision,writer_fencing_token,recorded_at FROM storage_dispatches",
		"INSERT OR REPLACE INTO storage_dispatches(rowid,action_id,execution_epoch,operation_id,intent_json,intent_digest,claim_revision,writer_fencing_token,recorded_at) SELECT rowid,'other',execution_epoch,operation_id,intent_json,intent_digest,claim_revision,writer_fencing_token,recorded_at FROM storage_dispatches",
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatalf("mutation allowed: %s", statement)
		}
	}
	got, ok, err := control.GetStorageDispatch("a", 1)
	if err != nil || !ok || got.Intent != intent {
		t.Fatalf("exact intent=%+v ok=%v err=%v", got, ok, err)
	}
}

func TestStorageDispatchReadBoundsAndInactiveStore(t *testing.T) {
	control := openControlAt(t, 1000)
	for _, epoch := range []uint64{0, math.MaxUint64} {
		if _, _, err := control.GetStorageDispatch("a", epoch); !errors.Is(err, ErrInvalidStorageIntent) {
			t.Fatal(err)
		}
	}
	if _, _, err := control.GetStorageDispatch("../a", 1); !errors.Is(err, ErrInvalidStorageIntent) {
		t.Fatal(err)
	}
	if _, ok, err := control.GetStorageDispatch("missing", 1); err != nil || ok {
		t.Fatalf("missing: %v %v", ok, err)
	}
	if err := control.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.GetStorageDispatch("a", 1); !errors.Is(err, ErrSchemaInactive) {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.GetStorageDispatch("a", 1); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal(err)
	}
}
