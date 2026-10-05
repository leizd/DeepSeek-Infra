package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func handbackFixture(t *testing.T, domain string) (*Control, *AuthorityCheckpoint, PythonInventoryImportResult) {
	t.Helper()
	path, raw := copiedPythonSourceFixture(t, domain)
	control := openAuthority(t)
	t.Cleanup(func() { _ = control.Close() })
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim Python checkpoint: %v %v", advanced, err)
	}
	dualEvaluate(t, control, domain)
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.ImportAttestedPythonInventory(raw, attestation)
	if err != nil || result.Imported != 1 {
		t.Fatalf("import: %+v %v", result, err)
	}
	return control, checkpoint, result
}

func TestInventoryHandbackRemovesTheImportedCopyAndJournalsTheTransfer(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			control, _, result := handbackFixture(t, domain)
			handback, err := control.RollbackPythonInventory(domain, result.TransferID)
			if err != nil {
				t.Fatal(err)
			}
			if handback.Schema != InventoryHandbackSchema || handback.Domain != domain ||
				handback.TransferID != result.TransferID ||
				handback.ManifestDigest != result.ManifestDigest || handback.SourceDigest != result.SourceDigest ||
				handback.RolledBackRecords != 1 || handback.RolledBackEvents != 1 ||
				handback.CutoverRevision < 1 || handback.CutoverEpoch < 1 ||
				len(handback.HandbackDigest) != 64 || handback.RecordedAt <= 0 {
				t.Fatalf("unbound handback document: %+v", handback)
			}
			document, err := CanonicalInventoryHandback(handback)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.HasSuffix(document, []byte("\n")) || !bytes.Contains(document, []byte(`"handbackDigest"`)) {
				t.Fatalf("handback document is not canonical: %s", document)
			}
			var reparsed map[string]any
			if err := decodeSingleJSON(document, &reparsed); err != nil || len(reparsed) != 14 {
				t.Fatalf("handback document fields: %v %v", reparsed, err)
			}
			stored, err := control.GetInventoryHandback(domain, result.TransferID)
			if err != nil || stored != handback {
				t.Fatalf("stored handback differs: %+v %v", stored, err)
			}
			table, _ := tableForDomain(domain)
			for _, query := range []string{
				"SELECT COUNT(*) FROM " + table,
				"SELECT COUNT(*) FROM control_events WHERE domain='" + domain + "'",
				"SELECT COUNT(*) FROM control_inventory_imports WHERE domain='" + domain + "'",
			} {
				var count int
				if err := control.db.QueryRow(query).Scan(&count); err != nil || count != 0 {
					t.Fatalf("%s left %d rows: %v", query, count, err)
				}
			}
			if _, exists, err := control.Get(domain, map[string]string{"policy": "p-1", "target": "t-1"}[domain]); err != nil || exists {
				t.Fatalf("imported record survived the handback: %v %v", exists, err)
			}
			// The abandoned transfer is fully attested: the journal row keeps the
			// digests of the source it came from and cannot be rewritten.
			for _, statement := range []string{
				"UPDATE control_inventory_handbacks SET source_digest='" + strings.Repeat("a", 64) + "'",
				"DELETE FROM control_inventory_handbacks",
			} {
				if _, err := control.db.Exec(statement); err == nil {
					t.Fatalf("handback journal accepted %s", statement)
				}
			}
			if _, err := control.RollbackPythonInventory(domain, result.TransferID); !errors.Is(err, ErrInventoryHandbackConflict) {
				t.Fatalf("repeat handback accepted: %v", err)
			}
			if _, err := control.RollbackPythonInventory(domain, "other-transfer"); !errors.Is(err, ErrInventoryHandbackNotFound) {
				t.Fatalf("unmatched transfer accepted: %v", err)
			}
			// The domain is usable again: a fresh transfer can be imported after
			// the abandoned one, which is what makes the rollback reversible.
			_, raw := copiedPythonSourceFixture(t, domain)
			reimported, err := control.ImportPythonInventory(raw)
			if err != nil || reimported.Imported != 1 {
				t.Fatalf("re-import after handback: %+v %v", reimported, err)
			}
			if _, err := control.RollbackPythonInventory(domain, result.TransferID); !errors.Is(err, ErrInventoryHandbackConflict) {
				t.Fatalf("handback of a retired transfer accepted: %v", err)
			}
		})
	}
}

func TestInventoryHandbackRefusesAuthoritativeMutatedAndUnmatchedDomains(t *testing.T) {
	t.Run("promoted", func(t *testing.T) {
		control, checkpoint, result := handbackFixture(t, "policy")
		dual, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, result)); err != nil {
			t.Fatal(err)
		}
		if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrInventoryHandbackAuthoritative) {
			t.Fatalf("promoted domain handed back: %v", err)
		}
	})
	t.Run("go-write", func(t *testing.T) {
		control, _, result := handbackFixture(t, "policy")
		payload := json.RawMessage(`{"enabled":true,"policyId":"p-1","policyRevision":3}`)
		if err := control.Put(Record{Domain: "policy", ID: "p-1", Revision: 3, State: "DISABLED", Payload: payload}); err != nil {
			t.Fatal(err)
		}
		if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrInventoryHandbackConflict) {
			t.Fatalf("mutated domain handed back: %v", err)
		}
	})
	t.Run("de-promoted", func(t *testing.T) {
		control, _, result := handbackFixture(t, "policy")
		dual, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(CutoverTransition{
			Domain: "policy", To: CutoverShadow, TransferID: "back-to-shadow",
			ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrInventoryHandbackAuthoritative) {
			t.Fatalf("non-evaluating domain handed back: %v", err)
		}
	})
	t.Run("no-import", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, advanced, err := control.ClaimControlAuthority(realPythonInventoryCheckpoint(t)); err != nil || !advanced {
			t.Fatalf("claim: %v %v", advanced, err)
		}
		dualEvaluate(t, control, "policy")
		if _, err := control.RollbackPythonInventory("policy", "fixture-policy"); !errors.Is(err, ErrInventoryHandbackNotFound) {
			t.Fatalf("handback without an import accepted: %v", err)
		}
	})
	t.Run("unknown-domain", func(t *testing.T) {
		control, _, _ := handbackFixture(t, "policy")
		if _, err := control.RollbackPythonInventory("action", "fixture-policy"); !errors.Is(err, ErrUnknownDomain) {
			t.Fatalf("unrelated domain handed back: %v", err)
		}
		if _, err := control.RollbackPythonInventory("policy", ""); !errors.Is(err, ErrInventoryHandbackInvalid) {
			t.Fatalf("empty transfer accepted: %v", err)
		}
	})
}

func TestInventoryHandbackWithoutTheCutoverCapabilityIsRefused(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reader, err := OpenControl(OpenOptions{Path: path, Owner: "no-capability", Now: func() int64 { return 1000 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	if _, err := reader.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("handback without the deployment capability: %v", err)
	}
	var handbacks int
	if err := reader.db.QueryRow("SELECT COUNT(*) FROM control_inventory_handbacks").Scan(&handbacks); err != nil || handbacks != 0 {
		t.Fatalf("refused handback still wrote a journal row: %d %v", handbacks, err)
	}
}

func TestInventoryHandbackSurvivesRestartAndStillBlocksPromotion(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	handback, err := control.RollbackPythonInventory("policy", result.TransferID)
	if err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{
		Path: path, Owner: "handback-successor", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
		FleetID: "fleet-a", Environment: "production",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("reopened at schema %d", reopened.SchemaVersion())
	}
	stored, err := reopened.GetInventoryHandback("policy", result.TransferID)
	if err != nil || stored != handback {
		t.Fatalf("handback lost across restart: %+v %v", stored, err)
	}
	dual, err := reopened.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	// The retired transfer cannot be resurrected as an authoritative state: its
	// import provenance is gone, so the signed promotion is unprovable.
	checkpoint := realPythonInventoryCheckpoint(t)
	req := CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: result.TransferID, Authority: checkpoint,
	}
	if _, err := signedTransition(t, reopened, req); !errors.Is(err, ErrInventoryPromotionUnproven) {
		t.Fatalf("retired transfer promoted: %v", err)
	}
	if err := reopened.Rollback(0); !errors.Is(err, ErrInventoryHandbackHistoryRetained) {
		t.Fatalf("schema rollback discarded handback history: %v", err)
	}
}

func TestV11StoreMigratesToTheHandbackSchemaWithoutInventingHistory(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	path := control.path
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	downgradeInventoryImportTableToV11Tx(t, tx)
	for _, statement := range []string{
		"DROP TABLE IF EXISTS control_operator_mutations",
		"DROP TABLE IF EXISTS backup_target_health",
		"DROP TABLE IF EXISTS control_target_health_imports",
		"DROP TABLE control_inventory_handbacks",
		`CREATE TABLE control_meta_v11_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 11),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v11_fixture SELECT singleton,runtime,mode,11,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v11_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=12",
		"PRAGMA user_version=11",
	} {
		if _, err := tx.Exec(statement); err != nil {
			_ = tx.Rollback()
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{
		Path: path, Owner: "v12-successor", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
		FleetID: "fleet-a", Environment: "production",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("expected schema %d, got %d", CurrentSchema, reopened.SchemaVersion())
	}
	// The imported policy is retained and was not given invented handback history.
	if record, exists, err := reopened.Get("policy", "p-1"); err != nil || !exists || record.Revision != 2 {
		t.Fatalf("imported record lost in migration: %+v %v %v", record, exists, err)
	}
	var handbacks int
	if err := reopened.db.QueryRow("SELECT COUNT(*) FROM control_inventory_handbacks").Scan(&handbacks); err != nil || handbacks != 0 {
		t.Fatalf("migration invented handback history: %d %v", handbacks, err)
	}
	// The migrated store can hand the still-active transfer back, which proves
	// the new schema is reachable rather than merely present.
	handback, err := reopened.RollbackPythonInventory("policy", result.TransferID)
	if err != nil || handback.RolledBackRecords != 1 || handback.ManifestDigest != result.ManifestDigest {
		t.Fatalf("handback after migration: %+v %v", handback, err)
	}
}

func TestHandbackMigrationLateFailureRollsBackSchema(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	downgradeInventoryImportTableToV11Tx(t, tx)
	for _, statement := range []string{
		"DROP TABLE IF EXISTS backup_target_health",
		"DROP TABLE IF EXISTS control_target_health_imports",
		"DROP TABLE control_inventory_handbacks",
		`CREATE TABLE control_meta_v11_failure_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 11),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v11_failure_fixture SELECT singleton,runtime,mode,11,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v11_failure_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=12",
		"PRAGMA user_version=11",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	originalNow := control.now
	control.schema = SchemaV11
	control.now = func() int64 { return -1 }
	if err := control.migrateToV12Tx(tx); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("late migration failure: %v", err)
	}
	control.now = originalNow
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	control.schema = CurrentSchema
	if _, err := control.GetCutover("policy"); err != nil {
		t.Fatalf("failed migration damaged the store: %v", err)
	}
}

func TestCanonicalHandbackRejectsAnUnboundDocument(t *testing.T) {
	control, _, result := handbackFixture(t, "target")
	handback, err := control.RollbackPythonInventory("target", result.TransferID)
	if err != nil {
		t.Fatal(err)
	}
	handback.HandbackDigest = strings.Repeat("b", 64)
	if _, err := CanonicalInventoryHandback(handback); !errors.Is(err, ErrInventoryHandbackInvalid) {
		t.Fatalf("forged handback digest accepted: %v", err)
	}
	handback.Schema = "control-inventory-handback-v0"
	if _, err := CanonicalInventoryHandback(handback); !errors.Is(err, ErrInventoryHandbackInvalid) {
		t.Fatalf("unknown handback schema accepted: %v", err)
	}
	if _, err := control.GetInventoryHandback("target", "missing-transfer"); !errors.Is(err, ErrInventoryHandbackNotFound) {
		t.Fatalf("missing handback accepted: %v", err)
	}
}
