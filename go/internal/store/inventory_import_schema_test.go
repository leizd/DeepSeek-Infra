package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
)

// Existing stores may still carry the v11/v12 import table. Build that exact
// historical table inside a test transaction before exercising an upgrade;
// changing only user_version would present an impossible mixed schema.
func downgradeInventoryImportTableToV11Tx(t *testing.T, tx *sql.Tx) {
	t.Helper()
	dropTargetHealthFixtureTables(t, tx)
	for _, statement := range []string{
		"DROP TRIGGER control_inventory_imports_no_update",
		"DROP TRIGGER control_inventory_imports_no_delete",
		"ALTER TABLE control_inventory_imports RENAME TO control_inventory_imports_v13_fixture",
		inventoryImportSchemaObjects["control_inventory_imports"],
		`INSERT INTO control_inventory_imports(
			domain,transfer_id,manifest_digest,source_digest,authority_generation,
			authority_digest,source_schema_version,source_boot_epoch,row_count,source_attested,
			writer_fencing_token,recorded_at)
		 SELECT domain,transfer_id,manifest_digest,source_digest,authority_generation,
			authority_digest,source_schema_version,source_boot_epoch,row_count,source_attested,
			writer_fencing_token,recorded_at FROM control_inventory_imports_v13_fixture`,
		"DROP TABLE control_inventory_imports_v13_fixture",
		inventoryImportSchemaObjects["control_inventory_imports_no_update"],
		inventoryImportSchemaObjects["control_inventory_imports_no_delete"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
}

func TestV10InventoryMigrationRetainsShadowHistoryWithoutForgingAttestation(t *testing.T) {
	control := openAuthority(t)
	path := control.path
	checkpoint := frozenCheckpoint(t, 0)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "policy")
	if err := control.Put(Record{Domain: "policy", ID: "legacy-shadow", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(`{"policyId":"legacy-shadow","policyRevision":1}`)}); err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"DROP TABLE IF EXISTS control_operator_mutations",
		"DROP TABLE IF EXISTS backup_target_health",
		"DROP TABLE IF EXISTS control_target_health_imports",
		"DROP TABLE control_inventory_handbacks",
		"DROP TABLE control_inventory_imports",
		`CREATE TABLE control_meta_v10_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 10),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v10_fixture SELECT singleton,runtime,mode,10,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v10_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=11",
		"PRAGMA user_version=10",
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
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "v11-successor", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic, FleetID: "fleet-a", Environment: "production"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("expected schema %d, got %d", CurrentSchema, reopened.SchemaVersion())
	}
	if record, exists, err := reopened.Get("policy", "legacy-shadow"); err != nil || !exists || record.Revision != 1 {
		t.Fatalf("old shadow record lost: %+v %v %v", record, exists, err)
	}
	var journalCount int
	if err := reopened.db.QueryRow("SELECT COUNT(*) FROM control_inventory_imports").Scan(&journalCount); err != nil || journalCount != 0 {
		t.Fatalf("v10 history acquired invented provenance: %d %v", journalCount, err)
	}
	var handbackCount int
	if err := reopened.db.QueryRow("SELECT COUNT(*) FROM control_inventory_handbacks").Scan(&handbackCount); err != nil || handbackCount != 0 {
		t.Fatalf("v10 history acquired invented handback provenance: %d %v", handbackCount, err)
	}
	req := CutoverTransition{Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "legacy-shadow-transfer", Authority: checkpoint}
	if _, err := signedTransition(t, reopened, req); !errors.Is(err, ErrInventoryPromotionUnproven) {
		t.Fatalf("unproven v10 shadow history promoted: %v", err)
	}
}

func TestV12ImportMigratesWithoutInventingLiveSourceBinding(t *testing.T) {
	control, checkpoint, imported := handbackFixture(t, "policy")
	storePath := control.path
	dual, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	downgradeInventoryImportTableToV11Tx(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_meta_v12_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 12),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v12_fixture SELECT singleton,runtime,mode,12,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v12_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=13",
		"PRAGMA user_version=12",
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
		Path: storePath, Owner: "v13-source-successor", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
		FleetID: "fleet-a", Environment: "production",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("v12 source not migrated: %d", reopened.SchemaVersion())
	}
	var path, projection sql.NullString
	var raw []byte
	if err := reopened.db.QueryRow(`SELECT source_path,projection_directory,manifest_bytes
		FROM control_inventory_imports WHERE domain='policy'`).Scan(&path, &projection, &raw); err != nil ||
		path.Valid || projection.Valid || raw != nil {
		t.Fatalf("old import gained invented source identity: %+v %+v %d %v", path, projection, len(raw), err)
	}
	if record, exists, err := reopened.Get("policy", "p-1"); err != nil || !exists || record.Revision != 2 {
		t.Fatalf("old import lost its policy: %+v %v %v", record, exists, err)
	}
	if _, err := reopened.TransitionCutover(signedInventoryPromotion(t, reopened, checkpoint, dual, imported)); !errors.Is(err, ErrInventoryPromotionUnproven) {
		t.Fatalf("unbound v12 import promoted: %v", err)
	}
	if handback, err := reopened.RollbackPythonInventory("policy", imported.TransferID); err != nil ||
		handback.RolledBackRecords != 1 {
		t.Fatalf("old import cannot be handed back for a fresh export: %+v %v", handback, err)
	}
}

func TestV12PromotedImportCannotUpgradeWithoutLiveSourceBinding(t *testing.T) {
	control, checkpoint, imported := handbackFixture(t, "policy")
	storePath := control.path
	databasePath := control.DatabasePath()
	dual, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if promoted, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil || promoted.State != CutoverGoAuthoritative {
		t.Fatalf("fixture promotion: %+v %v", promoted, err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	downgradeInventoryImportTableToV11Tx(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_meta_v12_promoted_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 12),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v12_promoted_fixture SELECT singleton,runtime,mode,12,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v12_promoted_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=13",
		"PRAGMA user_version=12",
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
		Path: storePath, Owner: "v13-promoted-successor", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
		FleetID: "fleet-a", Environment: "production",
	})
	if reopened != nil {
		_ = reopened.Close()
	}
	if !errors.Is(err, ErrInventoryPromotionUnproven) {
		t.Fatalf("promoted v12 import acquired invented source proof: %v", err)
	}
	connector, err := sqlitedb.NewConnector(readOnlyControlDatabaseDSN(databasePath))
	if err != nil {
		t.Fatal(err)
	}
	reader := sql.OpenDB(connector)
	defer reader.Close()
	var version, imports, promotions int
	if err := reader.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := reader.QueryRow("SELECT COUNT(*) FROM control_inventory_imports").Scan(&imports); err != nil {
		t.Fatal(err)
	}
	if err := reader.QueryRow("SELECT COUNT(*) FROM control_promotion_artifacts WHERE domain='policy'").Scan(&promotions); err != nil {
		t.Fatal(err)
	}
	if version != SchemaV12 || imports != 1 || promotions != 1 {
		t.Fatalf("failed upgrade changed historical store: schema=%d imports=%d promotions=%d", version, imports, promotions)
	}
}

func TestInventorySourceBindingMigrationLateFailureRollsBackSchema(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	downgradeInventoryImportTableToV11Tx(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_meta_v12_failure_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 12),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v12_failure_fixture SELECT singleton,runtime,mode,12,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v12_failure_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=13",
		"PRAGMA user_version=12",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	originalNow := control.now
	control.schema = SchemaV12
	control.now = func() int64 { return -1 }
	if err := control.migrateToV13Tx(tx); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("late source-binding migration failure: %v", err)
	}
	control.now = originalNow
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	control.schema = CurrentSchema
	if _, err := control.GetCutover("policy"); err != nil {
		t.Fatalf("failed source-binding migration damaged the schema: %v", err)
	}
}

func TestInventorySourceBindingMigrationRefusesMissingHistoryOrExistingObjects(t *testing.T) {
	for _, mode := range []string{"missing-promotion-history", "existing-v13-objects"} {
		t.Run(mode, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if mode == "missing-promotion-history" {
				if _, err := tx.Exec("DROP TABLE control_promotion_artifacts"); err != nil {
					t.Fatal(err)
				}
			} else {
				if _, err := tx.Exec("CREATE TABLE control_store_meta_v13(singleton INTEGER PRIMARY KEY)"); err != nil {
					t.Fatal(err)
				}
			}
			if err := control.migrateToV13Tx(tx); err == nil {
				t.Fatalf("%s was accepted as a v12 source-binding upgrade", mode)
			}
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			if _, err := control.GetCutover("policy"); err != nil {
				t.Fatalf("failed %s migration damaged store: %v", mode, err)
			}
		})
	}
}

func TestInventorySourceBindingMigrationRefusesDuplicateJournalVersion(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	downgradeInventoryImportTableToV11Tx(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_meta_v12_duplicate_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 12),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v12_duplicate_fixture SELECT singleton,runtime,mode,12,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v12_duplicate_fixture RENAME TO control_store_meta",
		"PRAGMA user_version=12",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := control.migrateToV13Tx(tx); err == nil {
		t.Fatal("duplicate v13 migration journal was overwritten")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); err != nil {
		t.Fatalf("duplicate-journal migration damaged store: %v", err)
	}
}

func TestInventorySchemaTamperAndRetainedHistoryAreRefused(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, raw := inventoryFixture(t, "policy")
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.ImportPythonInventory(raw); err != nil {
		t.Fatal(err)
	}
	if err := control.Rollback(0); !errors.Is(err, ErrInventoryHistoryRetained) {
		t.Fatalf("rollback discarded import provenance: %v", err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_inventory_imports_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing journal immutability trigger accepted: %v", err)
	}
}

func TestInventoryMigrationLateFailureRollsBackSchema(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	downgradeInventoryImportTableToV11Tx(t, tx)
	dropTargetHealthFixtureTables(t, tx)
	if _, err := tx.Exec("DROP TABLE control_inventory_handbacks"); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
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
		t.Fatalf("failed migration damaged the schema: %v", err)
	}
}

func TestInventoryMigrationRejectsExistingObjectsAndDuplicateJournalVersion(t *testing.T) {
	for _, mode := range []string{"existing-table", "duplicate-version"} {
		t.Run(mode, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if mode == "duplicate-version" {
				dropTargetHealthFixtureTables(t, tx)
				if _, err := tx.Exec("DROP TABLE control_inventory_handbacks"); err != nil {
					t.Fatal(err)
				}
			}
			if err := control.migrateToV12Tx(tx); err == nil {
				t.Fatalf("%s migration history was overwritten", mode)
			}
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			if _, err := control.GetCutover("policy"); err != nil {
				t.Fatalf("failed %s migration damaged store: %v", mode, err)
			}
		})
	}
}
