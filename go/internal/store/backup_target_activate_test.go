package store

import (
	"encoding/json"
	"errors"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

func targetActivationStoreFixture(t *testing.T) *Control {
	t.Helper()
	control := targetDeletePromotedStore(t, false)
	record, _, err := control.Get("target", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]any
	if err := decodeSingleJSON(record.Payload, &document); err != nil {
		t.Fatal(err)
	}
	record.Revision++
	record.State = "DRAINING"
	document["topologyGeneration"] = json.Number(strconv.FormatInt(record.Revision, 10))
	document["drainState"] = "draining"
	document["drainReason"] = "retained original activation fixture"
	document["drainingAt"] = "2026-11-11T00:00:00Z"
	document["drainedAt"] = "2026-11-11T00:00:01Z"
	document["retainedLargeInteger"] = json.Number("9007199254740993")
	record.Payload, err = json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "store-activation-fixture",
		ActionID: "store-activation-fixture-action", Actor: "operator:test", Record: record}); err != nil {
		t.Fatal(err)
	}
	return control
}

func TestTargetActivateContractJournalsOriginalDocumentAcrossReopenAndLaterMutations(t *testing.T) {
	control := targetActivationStoreFixture(t)
	path := control.path
	cutover, err := control.GetCutover("target")
	if err != nil {
		t.Fatal(err)
	}
	activated, receipt, err := control.ActivateBackupTarget("original-activation", "original-activation-action", "operator:test", "t-1")
	if err != nil || receipt.Status != OperatorMutationApplied || receipt.ExecutionEpoch != uint64(cutover.Epoch) ||
		receipt.CutoverRevision != cutover.Revision || receipt.FencingToken != cutover.FencingToken || receipt.ActionID != "original-activation-action" {
		t.Fatalf("activation lost its live action and authority binding: %+v %v", receipt, err)
	}
	var body map[string]any
	if err := decodeSingleJSON(activated.Payload, &body); err != nil || body["drainState"] != "active" ||
		body["retainedLargeInteger"] != json.Number("9007199254740993") {
		t.Fatal("activation lost retained document semantics", err)
	}
	for _, field := range []string{"drainReason", "drainingAt", "drainedAt"} {
		if _, exists := body[field]; exists {
			t.Fatal("activation retained drain metadata", field)
		}
	}
	if _, _, err := control.ActivateBackupTarget("later-activation", "later-activation-action", "operator:test", "t-1"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.DeleteBackupTarget("later-delete", "later-delete-action", "operator:test", "t-1"); err != nil {
		t.Fatal(err)
	}
	latest, _, err := control.Get("target", "t-1")
	if err != nil || latest.State != TombstoneState {
		t.Fatal("later deletion fixture failed", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	replayed, replayReceipt, err := recovered.ActivateBackupTarget("original-activation", "original-activation-action", "operator:test", "t-1")
	if err != nil || replayReceipt.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(replayed, activated) {
		t.Fatalf("replay substituted the later target state for its original committed result: %+v %v", replayReceipt, err)
	}
	replayReceipt.Status = receipt.Status
	if !reflect.DeepEqual(replayReceipt, receipt) {
		t.Fatal("replay rebound the original operation to the recovered writer")
	}
	if _, err := recovered.DeleteBackupTarget("original-activation", "original-activation-action", "operator:test", "t-1"); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("activation receipt was reused as a deletion: %v", err)
	}
	after, _, err := recovered.Get("target", "t-1")
	if err != nil || !reflect.DeepEqual(latest, after) {
		t.Fatal("historical replay revived or rewrote the current target", err)
	}
	if count, err := recovered.OperatorJournalSize(); err != nil || count != 4 {
		t.Fatal("historical replay appended another mutation", count, err)
	}
}

func TestTargetActivateContractDatabaseInterruptionsRollbackAndReplayStayClosed(t *testing.T) {
	for _, test := range []struct {
		name  string
		fault inventoryReadFault
	}{
		{"receipt read", inventoryReadFault{query: "FROM control_operator_mutations WHERE operation_id = ?"}},
		{"target read", inventoryReadFault{query: "FROM targets WHERE id = ?"}},
		{"target history", inventoryReadFault{query: "record_digest, writer_fencing_token, recorded_at, event_id"}},
		{"inventory CAS", inventoryReadFault{query: "SELECT COUNT(*) FROM control_inventory_imports WHERE domain=?"}},
		{"target update", inventoryReadFault{query: "UPDATE targets", exec: true}},
		{"receipt insert", inventoryReadFault{query: "INSERT INTO control_operator_mutations", exec: true}},
		{"commit", inventoryReadFault{commit: true}},
	} {
		t.Run(test.name, func(t *testing.T) {
			control := targetActivationStoreFixture(t)
			before, health, err := control.ListAuthoritativeTargets()
			if err != nil {
				t.Fatal(err)
			}
			events, receipts := countControlEvents(t, control), countOperatorMutations(t, control)
			fault := test.fault
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, _, err := control.ActivateBackupTarget("interrupted-activation", "interrupted-activation-action", "operator:test", "t-1"); !errors.Is(err, errInventoryReadInterrupted) || !fault.injected {
				t.Fatalf("interrupted activation claimed success: injected=%v err=%v", fault.injected, err)
			}
			after, afterHealth, err := control.ListAuthoritativeTargets()
			if err != nil || !reflect.DeepEqual(before, after) || !reflect.DeepEqual(health, afterHealth) ||
				countControlEvents(t, control) != events || countOperatorMutations(t, control) != receipts {
				t.Fatal("interrupted activation retained a partial registry, event, receipt or health change", err)
			}
		})
	}
	for _, fault := range []inventoryReadFault{
		{query: "FROM targets WHERE id = ?"},
		{query: "FROM control_events WHERE domain = ? AND record_id = ? AND revision = ?"},
		{query: "FROM control_events WHERE domain = ? AND record_id = ? AND revision = ?", malformed: true},
	} {
		t.Run("replay "+fault.query+strconv.FormatBool(fault.malformed), func(t *testing.T) {
			control := targetActivationStoreFixture(t)
			original, _, err := control.ActivateBackupTarget("recover-activation", "recover-activation-action", "operator:test", "t-1")
			if err != nil {
				t.Fatal(err)
			}
			beforeEvents, beforeReceipts := countControlEvents(t, control), countOperatorMutations(t, control)
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, _, err := control.ActivateBackupTarget("recover-activation", "recover-activation-action", "operator:test", "t-1"); err == nil || !fault.injected {
				t.Fatal("replay returned an unverified historical document", err)
			}
			replayed, receipt, err := control.ActivateBackupTarget("recover-activation", "recover-activation-action", "operator:test", "t-1")
			if err != nil || receipt.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(original, replayed) ||
				countControlEvents(t, control) != beforeEvents || countOperatorMutations(t, control) != beforeReceipts {
				t.Fatal("recovering a replay repeated or lost the committed activation", err)
			}
		})
	}
}

func TestTargetActivateContractRejectsAbsentCorruptAndMisboundReceipts(t *testing.T) {
	control := targetDeletePromotedStore(t, false)
	for _, identifier := range []string{"", "not-present"} {
		if _, _, err := control.ActivateBackupTarget("absent-activation", "absent-activation-action", "operator:test", identifier); !errors.Is(err, ErrBackupTargetNotFound) {
			t.Fatal("absent activation did not preserve original absence", err)
		}
	}
	if _, _, err := control.mutateBackupTarget("invalid-command", "invalid-command-action", "operator:test", "t-1", targetMetadataCommand("unrecognized")); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatal("unknown target command was admitted", err)
	}
	_, receipt, err := control.ActivateBackupTarget("bound-activation", "bound-activation-action", "operator:test", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*OperatorMutationResult){
		func(r *OperatorMutationResult) { r.Domain = "policy" },
		func(r *OperatorMutationResult) { r.RecordID = "not-present" },
		func(r *OperatorMutationResult) { r.Revision += 1000 },
		func(r *OperatorMutationResult) { r.State = TombstoneState },
		func(r *OperatorMutationResult) { r.PayloadDigest = strings.Repeat("0", 64) },
	} {
		altered := receipt
		change(&altered)
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		_, err = readTargetMutationResultTx(tx, altered)
		_ = tx.Rollback()
		if !errors.Is(err, ErrCorruptRecord) {
			t.Fatal("historical result accepted an incorrectly bound receipt", err)
		}
	}
	record, _, err := control.Get("target", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]any
	if err := decodeSingleJSON(record.Payload, &document); err != nil {
		t.Fatal(err)
	}
	record.Revision++
	record.State = "DISABLED"
	document["targetId"] = "different-public-identity"
	document["topologyGeneration"] = json.Number(strconv.FormatInt(record.Revision, 10))
	record.Payload, err = json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "activation-invalid-identity-fixture",
		ActionID: "activation-invalid-identity-action", Actor: "operator:test", Record: record}); err != nil {
		t.Fatal(err)
	}
	beforeEvents, beforeReceipts := countControlEvents(t, control), countOperatorMutations(t, control)
	if _, _, err := control.ActivateBackupTarget("invalid-identity-activation", "invalid-identity-activation-action", "operator:test", "t-1"); !errors.Is(err, ErrCorruptRecord) || countControlEvents(t, control) != beforeEvents || countOperatorMutations(t, control) != beforeReceipts {
		t.Fatal("invalid public target identity was rewritten by activation", err)
	}
}

func TestTargetActivateContractWriterExpiryCannotCommitOrRebindReplay(t *testing.T) {
	control := targetActivationStoreFixture(t)
	before, _, err := control.Get("target", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	beforeReceipts := countOperatorMutations(t, control)
	expire := func() {
		calls := 0
		control.now = func() int64 {
			calls++
			if calls >= 2 {
				return 1_000_000
			}
			return 1000
		}
	}
	expire()
	if _, _, err := control.ActivateBackupTarget("expired-activation", "expired-activation-action", "operator:test", "t-1"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal("activation committed after the writer expired", err)
	}
	control.now = func() int64 { return 1000 }
	after, _, err := control.Get("target", "t-1")
	if err != nil || !reflect.DeepEqual(before, after) || countOperatorMutations(t, control) != beforeReceipts {
		t.Fatal("expired activation left a partial result", err)
	}
	if _, _, err := control.ActivateBackupTarget("expired-activation", "expired-activation-action", "operator:test", "t-1"); err != nil {
		t.Fatal("rolled-back operation could not be safely admitted later", err)
	}
	expire()
	if _, _, err := control.ActivateBackupTarget("expired-activation", "expired-activation-action", "operator:test", "t-1"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal("replay bypassed its expiring writer fence", err)
	}
	control.now = func() int64 { return 1000 }
	if countOperatorMutations(t, control) != beforeReceipts+1 {
		t.Fatal("expired replay appended another activation")
	}
}
