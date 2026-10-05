package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// countingNow returns a clock that reports fixed values in order, so a caller
// can make time pass inside one operation without sleeping.
func countingNow(values ...int64) func() int64 {
	index := 0
	return func() int64 {
		value := values[len(values)-1]
		if index < len(values) {
			value = values[index]
		}
		index++
		return value
	}
}

func TestInventoryHandbackRefusesAStoreWithoutALiveWriter(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	original := control.now
	control.now = func() int64 { return -1 }
	if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("handback without a live writer lease: %v", err)
	}
	control.now = original
	if _, err := control.GetInventoryHandback("policy", result.TransferID); !errors.Is(err, ErrInventoryHandbackNotFound) {
		t.Fatalf("refused handback still wrote a journal row: %v", err)
	}
}

func TestInventoryHandbackRefusesALeaseThatExpiresBeforeCommit(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	control.now = countingNow(1000, 1<<40)
	if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("lease expiry before commit: %v", err)
	}
	// The failing commit left the imported copy and the journal untouched.
	var records, events, handbacks int
	for query, destination := range map[string]*int{
		"SELECT COUNT(*) FROM policies":                    &records,
		"SELECT COUNT(*) FROM control_events":              &events,
		"SELECT COUNT(*) FROM control_inventory_handbacks": &handbacks,
	} {
		if err := control.db.QueryRow(query).Scan(destination); err != nil {
			t.Fatal(err)
		}
	}
	if records != 1 || events != 1 || handbacks != 0 {
		t.Fatalf("failed handback changed durable state: %d %d %d", records, events, handbacks)
	}
}

func TestInventoryHandbackRefusesADemotedDomainWithPromotionHistory(t *testing.T) {
	control, checkpoint, result := handbackFixture(t, "policy")
	dual, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, result)); err != nil {
		t.Fatal(err)
	}
	// A promoted domain returns to dual evaluation with its authorization and
	// artifact history retained, so it is never a handback candidate again.
	for _, state := range []CutoverState{CutoverShadow, CutoverDualEvaluate} {
		current, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(CutoverTransition{
			Domain: "policy", To: state, TransferID: "return-to-" + string(state),
			ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
		}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrInventoryHandbackAuthoritative) {
		t.Fatalf("domain with promotion history handed back: %v", err)
	}
}

func TestInventoryHandbackGetterRefusesClosedInactiveAndTamperedStores(t *testing.T) {
	t.Run("closed", func(t *testing.T) {
		control, _, result := handbackFixture(t, "policy")
		if err := control.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("closed store handed back: %v", err)
		}
		if _, err := control.GetInventoryHandback("policy", result.TransferID); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("closed store read a handback: %v", err)
		}
	})
	t.Run("unknown-domain", func(t *testing.T) {
		control, _, _ := handbackFixture(t, "policy")
		for _, domain := range []string{"action", "policy"} {
			transfer := "fixture-policy"
			if domain == "policy" {
				transfer = ""
			}
			if _, err := control.GetInventoryHandback(domain, transfer); !errors.Is(err, ErrUnknownDomain) {
				t.Fatalf("getter accepted %q %q: %v", domain, transfer, err)
			}
		}
	})
	t.Run("inactive-schema", func(t *testing.T) {
		control, _, result := handbackFixture(t, "policy")
		schema := control.schema
		control.schema = 0
		if _, err := control.GetInventoryHandback("policy", result.TransferID); !errors.Is(err, ErrSchemaInactive) {
			t.Fatalf("inactive schema read a handback: %v", err)
		}
		control.schema = schema
	})
	t.Run("tampered-digest", func(t *testing.T) {
		control, _, result := handbackFixture(t, "policy")
		if _, err := control.db.Exec(`INSERT INTO control_inventory_handbacks(
			domain,transfer_id,manifest_digest,source_digest,authority_generation,authority_digest,
			rolled_back_records,rolled_back_events,cutover_revision,cutover_epoch,handback_digest,
			writer_fencing_token,recorded_at) VALUES('policy',?,?,?,1,?,1,1,2,2,?,1,1000)`,
			result.TransferID, result.ManifestDigest, result.SourceDigest, strings.Repeat("b", 64),
			strings.Repeat("a", 64),
		); err != nil {
			t.Fatal(err)
		}
		if _, err := control.GetInventoryHandback("policy", result.TransferID); !errors.Is(err, ErrInventoryHandbackInvalid) {
			t.Fatalf("forged handback digest read back: %v", err)
		}
	})
}

func TestInventoryHandbackRefusesATamperedJournalSchema(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	if _, err := control.db.Exec("DROP TRIGGER control_inventory_handbacks_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("handback accepted a missing journal trigger: %v", err)
	}
	if _, err := control.GetInventoryHandback("policy", result.TransferID); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("getter accepted a missing journal trigger: %v", err)
	}
}

func TestInventoryHandbackIsAtomicUnderDurableWriteFailures(t *testing.T) {
	cases := map[string]struct {
		trigger string
		message string
	}{
		"event-delete": {
			trigger: `CREATE TRIGGER refuse_handback_event_delete BEFORE DELETE ON control_events
				BEGIN SELECT RAISE(ABORT,'refuse event delete'); END`,
		},
		"record-delete": {
			trigger: `CREATE TRIGGER refuse_handback_record_delete BEFORE DELETE ON policies
				BEGIN SELECT RAISE(ABORT,'refuse record delete'); END`,
		},
		"import-delete": {
			trigger: `CREATE TRIGGER refuse_handback_import_delete BEFORE DELETE ON control_inventory_imports
				BEGIN SELECT RAISE(ABORT,'refuse import delete'); END`,
		},
		"journal-insert": {
			trigger: `CREATE TRIGGER refuse_handback_journal BEFORE INSERT ON control_inventory_handbacks
				BEGIN SELECT RAISE(ABORT,'refuse handback journal'); END`,
		},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			control, _, result := handbackFixture(t, "policy")
			if _, err := control.db.Exec(testCase.trigger); err != nil {
				t.Fatal(err)
			}
			if _, err := control.RollbackPythonInventory("policy", result.TransferID); err == nil {
				t.Fatal("failing durable write was accepted")
			}
			var records, events, handbacks int
			for query, destination := range map[string]*int{
				"SELECT COUNT(*) FROM policies":                    &records,
				"SELECT COUNT(*) FROM control_events":              &events,
				"SELECT COUNT(*) FROM control_inventory_handbacks": &handbacks,
			} {
				if err := control.db.QueryRow(query).Scan(destination); err != nil {
					t.Fatal(err)
				}
			}
			if records != 1 || events != 1 || handbacks != 0 {
				t.Fatalf("failed handback changed durable state: %d %d %d", records, events, handbacks)
			}
			// The lifted immutability triggers are restored by the rollback, so
			// the store still verifies and the transfer is still readable.
			if _, err := control.GetCutover("policy"); err != nil {
				t.Fatalf("failed handback damaged the schema: %v", err)
			}
			if record, exists, err := control.Get("policy", "p-1"); err != nil || !exists || record.Revision != 2 {
				t.Fatalf("imported record lost on a failed handback: %+v %v %v", record, exists, err)
			}
		})
	}
}

func TestWithLiftedTriggersRefusesMismatchedAndMissingTriggers(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	failing := errors.New("operation failed")
	for name, run := range map[string]func(tx *sql.Tx) error{
		"mismatched": func(tx *sql.Tx) error {
			return withLiftedTriggers(tx, []string{"control_events_no_delete"}, nil, func() error { return nil })
		},
		"missing-trigger": func(tx *sql.Tx) error {
			return withLiftedTriggers(tx, []string{"absent_trigger"}, []string{"CREATE TRIGGER absent_trigger AFTER INSERT ON control_events BEGIN SELECT 1; END"}, func() error { return nil })
		},
		"failing-operation": func(tx *sql.Tx) error {
			return withLiftedTriggers(tx, []string{"control_events_no_delete"},
				[]string{controlEventImmutabilityTriggers[1]}, func() error { return failing })
		},
		"invalid-definition": func(tx *sql.Tx) error {
			return withLiftedTriggers(tx, []string{"control_events_no_delete"}, []string{"CREATE TRIGGER"}, func() error { return nil })
		},
	} {
		t.Run(name, func(t *testing.T) {
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if err := run(tx); err == nil {
				t.Fatal("lifted-trigger helper accepted an unrecoverable request")
			}
		})
	}
}

func TestImportedRevisionRequiresTheJournalRowForItsWriter(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.ImportPythonInventory(raw); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_inventory_imports_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_inventory_imports WHERE domain='policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(inventoryImportSchemaObjects["control_inventory_imports_no_delete"]); err != nil {
		t.Fatal(err)
	}
	if _, exists, err := control.Get("policy", "p-1"); !errors.Is(err, ErrCorruptRecord) || exists {
		t.Fatalf("imported baseline without its journal row was accepted: %v %v", exists, err)
	}
	if _, err := control.RollbackPythonInventory("policy", "isolated-transfer"); !errors.Is(err, ErrInventoryHandbackNotFound) {
		t.Fatalf("handback without import provenance: %v", err)
	}
}

func TestHandbackDocumentRenderingRefusesUnboundValues(t *testing.T) {
	control, _, result := handbackFixture(t, "target")
	handback, err := control.RollbackPythonInventory("target", result.TransferID)
	if err != nil {
		t.Fatal(err)
	}
	document, err := CanonicalInventoryHandback(handback)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(document, &decoded); err != nil || len(decoded) != 14 {
		t.Fatalf("rendered document: %v %v", decoded, err)
	}
	handback.RecordedAt = 0
	if _, err := CanonicalInventoryHandback(handback); !errors.Is(err, ErrInventoryHandbackInvalid) {
		t.Fatalf("altered document rendered without a matching digest: %v", err)
	}
}
