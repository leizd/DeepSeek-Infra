package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strconv"
	"testing"
)

func targetDeletePromotedStore(t *testing.T, empty bool) *Control {
	t.Helper()
	control, checkpoint, dual, imported := importedHealthStore(t, empty)
	t.Cleanup(func() { _ = control.Close() })
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
		t.Fatal(err)
	}
	return control
}

func reopenTargetDeleteStore(t *testing.T, path string, now func() int64) *Control {
	t.Helper()
	control, err := OpenControl(OpenOptions{Path: path, Owner: "target-delete-recovered", Now: now, AuthorizeCutover: true,
		PromotionSignerPublicKey: promotionTestPublic, FleetID: "fleet-a", Environment: "production"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	return control
}

func TestTargetDeleteContractBindsDurableOperationAcrossReopenAndReplay(t *testing.T) {
	control := targetDeletePromotedStore(t, false)
	path := control.path
	cutover, err := control.GetCutover("target")
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.DeleteBackupTarget("target-delete-operation", "target-delete-action", "operator:test", "t-1")
	if err != nil || result.Status != OperatorMutationApplied || result.ExecutionEpoch != uint64(cutover.Epoch) ||
		result.CutoverRevision != cutover.Revision || result.FencingToken != cutover.FencingToken || result.ActionID != "target-delete-action" {
		t.Fatalf("deletion lost its live authority binding: %+v %v", result, err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	replay, err := recovered.DeleteBackupTarget("target-delete-operation", "target-delete-action", "operator:test", "t-1")
	if err != nil || replay.Status != OperatorMutationAlreadyApplied {
		t.Fatalf("lost response could not be reconciled after restart: %+v %v", replay, err)
	}
	replay.Status = result.Status
	if !reflect.DeepEqual(result, replay) {
		t.Fatal("replay rebound the original mutation to the new writer")
	}
	for _, changed := range [][3]string{
		{"target-delete-action", "operator:test", "different-target"},
		{"different-action", "operator:test", "t-1"},
		{"target-delete-action", "different-actor", "t-1"},
	} {
		if _, err := recovered.DeleteBackupTarget("target-delete-operation", changed[0], changed[1], changed[2]); !errors.Is(err, ErrMutationRequestReplayConflict) {
			t.Fatalf("operation replay changed its scope: %v", err)
		}
	}
	if _, err := recovered.DeleteBackupTarget("new-target-delete-operation", "new-target-delete-action", "operator:test", "t-1"); !errors.Is(err, ErrBackupTargetNotFound) {
		t.Fatalf("a new deletion of an absent target changed original 404 semantics: %v", err)
	}
	if count, err := recovered.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatalf("recovery appended duplicate or conflicting mutations: %d %v", count, err)
	}
	record, exists, err := recovered.Get("target", "t-1")
	if err != nil || !exists || record.State != TombstoneState || record.Revision != result.Revision {
		t.Fatalf("restart lost deletion history: %+v %v %v", record, exists, err)
	}
}

func TestTargetDeleteContractDeletesRetainedStatesWithoutChangingHealth(t *testing.T) {
	for _, states := range [][]string{nil, {"DISABLED"}, {"DRAINING"}, {"DRAINING", "DRAINED"}} {
		t.Run(fmt.Sprint(states), func(t *testing.T) {
			control := targetDeletePromotedStore(t, false)
			_, health, err := control.ListAuthoritativeTargets()
			if err != nil {
				t.Fatal(err)
			}
			for index, state := range states {
				record, _, err := control.Get("target", "t-1")
				if err != nil {
					t.Fatal(err)
				}
				var body map[string]json.RawMessage
				if err := json.Unmarshal(record.Payload, &body); err != nil {
					t.Fatal(err)
				}
				record.Revision++
				record.State = state
				body["topologyGeneration"] = json.RawMessage(strconv.FormatInt(record.Revision, 10))
				record.Payload, err = json.Marshal(body)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := control.ApplyOperatorMutation(OperatorMutation{
					OperationID: fmt.Sprintf("target-state-%d", index), ActionID: "target-state-action", Actor: "operator:test", Record: record,
				}); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.DeleteBackupTarget("delete-retained-target", "delete-retained-action", "operator:test", "t-1"); err != nil {
				t.Fatalf("registered target in retained state could not be deleted: %v", err)
			}
			records, afterHealth, err := control.ListAuthoritativeTargets()
			if err != nil || len(records) != 0 || !reflect.DeepEqual(health, afterHealth) {
				t.Fatalf("deletion lost health or retained a visible registration: %v", err)
			}
		})
	}
}

func TestTargetDeleteContractRollsBackWhenWriterExpiresBeforeCommit(t *testing.T) {
	initial := targetDeletePromotedStore(t, false)
	path := initial.path
	if err := initial.Close(); err != nil {
		t.Fatal(err)
	}
	expire, calls := false, 0
	control := reopenTargetDeleteStore(t, path, func() int64 {
		calls++
		if expire && calls >= 2 {
			return 1_000_000
		}
		return 1000
	})
	expire, calls = true, 0
	if _, err := control.DeleteBackupTarget("expired-delete", "expired-delete-action", "operator:test", "t-1"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expired writer committed a target deletion: %v", err)
	}
	expire = false
	record, exists, err := control.Get("target", "t-1")
	if err != nil || !exists || record.State != "ACTIVE" || record.Revision != 1 {
		t.Fatalf("refused deletion left a partial record change: %+v %v %v", record, exists, err)
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
		t.Fatalf("refused deletion left a partial operator receipt: %d %v", count, err)
	}
}

func TestTargetDeleteContractDatabaseInterruptionsCannotPartiallyCommit(t *testing.T) {
	for _, test := range []struct {
		name, targetID string
		fault          inventoryReadFault
	}{
		{"receipt read", "t-1", inventoryReadFault{query: "FROM control_operator_mutations WHERE operation_id = ?"}},
		{"target read", "t-1", inventoryReadFault{query: "FROM targets WHERE id = ?"}},
		{"target history", "t-1", inventoryReadFault{query: "record_digest, writer_fencing_token, recorded_at, event_id"}},
		{"missing target history", "not-present", inventoryReadFault{query: "SELECT COUNT(*) FROM control_events WHERE domain = ? AND record_id = ?"}},
		{"inventory CAS", "t-1", inventoryReadFault{query: "SELECT COUNT(*) FROM control_inventory_imports WHERE domain=?"}},
		{"target update", "t-1", inventoryReadFault{query: "UPDATE targets", exec: true}},
		{"receipt insert", "t-1", inventoryReadFault{query: "INSERT INTO control_operator_mutations", exec: true}},
		{"commit", "t-1", inventoryReadFault{commit: true}},
	} {
		t.Run(test.name, func(t *testing.T) {
			control := targetDeletePromotedStore(t, false)
			before, health, err := control.ListAuthoritativeTargets()
			if err != nil || len(before) != 1 {
				t.Fatal("signed target fixture unavailable", err)
			}
			events := countControlEvents(t, control)
			fault := test.fault
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, err := control.DeleteBackupTarget("interrupted-target-delete", "interrupted-target-action", "operator:test", test.targetID); !errors.Is(err, errInventoryReadInterrupted) || !fault.injected {
				t.Fatalf("database interruption was hidden: injected=%v err=%v", fault.injected, err)
			}
			after, afterHealth, err := control.ListAuthoritativeTargets()
			if err != nil || !reflect.DeepEqual(before, after) || !reflect.DeepEqual(health, afterHealth) {
				t.Fatalf("interrupted deletion retained partial registry or health changes: %v", err)
			}
			if count := countControlEvents(t, control); count != events {
				t.Fatalf("interrupted deletion retained a partial immutable event: %d -> %d", events, count)
			}
			if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
				t.Fatalf("interrupted deletion retained a partial receipt: %d %v", count, err)
			}
		})
	}
}

func TestTargetDeleteContractInvalidScopeAndCorruptPayloadFailClosed(t *testing.T) {
	t.Run("invalid scope", func(t *testing.T) {
		control := targetDeletePromotedStore(t, false)
		for _, input := range [][4]string{
			{"valid-operation", "valid-action", "operator:test", ""},
			{"", "valid-action", "operator:test", "t-1"},
			{"valid-operation", "", "operator:test", "t-1"},
			{"valid-operation", "valid-action", "", "t-1"},
		} {
			if _, err := control.DeleteBackupTarget(input[0], input[1], input[2], input[3]); err == nil {
				t.Fatal("incomplete operation scope was admitted")
			}
		}
		if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
			t.Fatal("invalid scope changed the operator journal", count, err)
		}
	})
	t.Run("mismatched public identity", func(t *testing.T) {
		control := targetDeletePromotedStore(t, false)
		record, _, err := control.Get("target", "t-1")
		if err != nil {
			t.Fatal(err)
		}
		var document map[string]json.RawMessage
		if err := json.Unmarshal(record.Payload, &document); err != nil {
			t.Fatal(err)
		}
		record.Revision++
		record.State = "DISABLED"
		document["targetId"] = json.RawMessage(`"different-public-identity"`)
		document["topologyGeneration"] = json.RawMessage(strconv.FormatInt(record.Revision, 10))
		record.Payload, err = json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "mismatched-identity-fixture",
			ActionID: "mismatched-identity-action", Actor: "operator:test", Record: record}); err != nil {
			t.Fatal(err)
		}
		before, exists, err := control.Get("target", "t-1")
		if err != nil || !exists {
			t.Fatal("mismatched identity fixture lost its retained record", err)
		}
		if _, err := control.DeleteBackupTarget("reject-mismatched-target", "reject-mismatched-action", "operator:test", "t-1"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("a target with mismatched public identity was deleted: %v", err)
		}
		retained, _, err := control.Get("target", "t-1")
		if err != nil || !reflect.DeepEqual(retained, before) {
			t.Fatal("refusing a mismatched identity changed its record", err)
		}
		if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
			t.Fatal("refusing a mismatched identity appended a receipt", count, err)
		}
	})
	t.Run("replay still requires a live writer", func(t *testing.T) {
		control := targetDeletePromotedStore(t, false)
		if _, err := control.DeleteBackupTarget("durable-delete", "durable-delete-action", "operator:test", "t-1"); err != nil {
			t.Fatal(err)
		}
		calls := 0
		control.now = func() int64 {
			calls++
			if calls >= 2 {
				return 1_000_000
			}
			return 1000
		}
		if _, err := control.DeleteBackupTarget("durable-delete", "durable-delete-action", "operator:test", "t-1"); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("journal replay bypassed the expiring writer fence: %v", err)
		}
		control.now = func() int64 { return 1000 }
		if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
			t.Fatal("refused replay changed the durable receipt", count, err)
		}
	})
}
