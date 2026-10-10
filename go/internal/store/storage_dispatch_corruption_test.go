package store

import (
	"errors"
	"strings"
	"testing"
)

func TestStorageDispatchReadRejectsCorruptBindings(t *testing.T) {
	for name, update := range map[string]string{
		"wrong JSON shape":       `intent_json='[]'`,
		"unknown field":          `intent_json=json_set(intent_json,'$.futureField',true)`,
		"invalid intent":         `intent_json=json_set(intent_json,'$.provider','filesystem')`,
		"noncanonical JSON":      `intent_json=intent_json||' '`,
		"digest mismatch":        `intent_digest='` + strings.Repeat("d", 64) + `'`,
		"operation substitution": `operation_id='substituted'`,
		"missing parent":         `claim_revision=99`,
		"wrong parent revision":  `claim_revision=2`,
		"wrong parent writer":    `writer_fencing_token=99`,
		"wrong parent time":      `recorded_at=1001`,
	} {
		t.Run(name, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			if err := control.ClaimStorageDispatch(claimedAction(t, control), dispatchIntent()); err != nil {
				t.Fatal(err)
			}
			// Deliberate local corruption, not a way to seed successful execution.
			if _, err := control.db.Exec("DROP TRIGGER storage_dispatches_no_update"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("UPDATE storage_dispatches SET " + update); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec(storageDispatchSchemaObjects["storage_dispatches_no_update"]); err != nil {
				t.Fatal(err)
			}
			if _, _, err := control.GetStorageDispatch("a", 1); !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("corrupt binding accepted: %v", err)
			}
		})
	}
}

func TestStorageDispatchReadRejectsMissingActionAndOversizeRecord(t *testing.T) {
	for _, oversize := range []bool{false, true} {
		control := openControlAt(t, 1000)
		defer control.Close()
		if err := control.ClaimStorageDispatch(claimedAction(t, control), dispatchIntent()); err != nil {
			t.Fatal(err)
		}
		if oversize {
			if _, err := control.db.Exec("DROP TRIGGER storage_dispatches_no_update"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("UPDATE storage_dispatches SET intent_json=?", `{"oversize":"`+strings.Repeat("x", maximumPayloadBytes)+`"}`); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec(storageDispatchSchemaObjects["storage_dispatches_no_update"]); err != nil {
				t.Fatal(err)
			}
		} else if _, err := control.db.Exec("DELETE FROM action_journal"); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.GetStorageDispatch("a", 1); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt parent/bounds accepted: %v", err)
		}
	}
}

func TestStorageDispatchReadRejectsChangedGuardAndClosedDatabase(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if _, err := control.db.Exec("DROP TRIGGER storage_dispatches_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("CREATE TRIGGER storage_dispatches_no_update BEFORE UPDATE ON storage_dispatches BEGIN SELECT 1; END"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.GetStorageDispatch("a", 1); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("weakened guard accepted: %v", err)
	}
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.GetStorageDispatch("a", 1); err == nil {
		t.Fatal("closed SQL connection was treated as missing intent")
	}
}
