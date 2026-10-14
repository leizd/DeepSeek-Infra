package store

import (
	"database/sql"
	"encoding/json"
	"strings"
	"testing"
)

// Disposable historical fixtures only. No downgrade API accepts live cancellation
// history, and the legacy CHECK constraint still rejects the new terminal state.
func materializeLegacyActionLeasesFixture(t *testing.T, tx *sql.Tx) {
	t.Helper()
	for _, statement := range []string{
		`CREATE TEMP TABLE legacy_action_leases_fixture AS SELECT * FROM action_leases`,
		`DROP TABLE action_leases`, actionAdmissionSchemaObjects["action_leases"],
		`INSERT INTO action_leases SELECT * FROM legacy_action_leases_fixture`,
		`DROP TABLE legacy_action_leases_fixture`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
}
func materializeCancellationV15Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	materializeLegacyActionLeasesFixture(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_meta_v15_fixture(singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 15),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_meta_v15_fixture SELECT singleton,runtime,mode,15,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_meta_v15_fixture RENAME TO control_store_meta`,
		`DELETE FROM schema_migrations WHERE version=16`, `PRAGMA user_version=15`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV15); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV15
}
func cancellationMigrationRows(t *testing.T, control *Control) string {
	t.Helper()
	var values []json.RawMessage
	for _, table := range []string{"action_leases", "action_resource_leases", "action_lease_events", "control_events"} {
		rows, err := control.db.Query("SELECT * FROM " + table + " ORDER BY rowid")
		if err != nil {
			t.Fatal(err)
		}
		cols, err := rows.Columns()
		if err != nil {
			t.Fatal(err)
		}
		var tableRows [][]any
		for rows.Next() {
			v := make([]any, len(cols))
			ptrs := make([]any, len(cols))
			for i := range v {
				ptrs[i] = &v[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				t.Fatal(err)
			}
			tableRows = append(tableRows, v)
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		_ = rows.Close()
		encoded, err := json.Marshal(tableRows)
		if err != nil {
			t.Fatal(err)
		}
		values = append(values, encoded)
	}
	raw, err := json.Marshal(values)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}
func TestV16CancellationMigrationPreservesActiveLeaseAndImmutableHistory(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	materializeCancellationV15Fixture(t, control)
	before := cancellationMigrationRows(t, control)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := control.migrateToV16Tx(tx); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if after := cancellationMigrationRows(t, control); after != before {
		t.Fatal("migration changed original action/lease/resource/history bytes")
	}
	if control.SchemaVersion() != SchemaV16 {
		t.Fatal("wrong migrated schema")
	}
	if _, err := control.SettleAgentCancellation(input); err != nil {
		t.Fatal("migrated lease could not settle original confirmed custody", err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 0 {
		t.Fatal(err)
	}
}
func TestV16CancellationMigrationFailureRollsBackAllTablesAndVersion(t *testing.T) {
	control, _ := cancellationSettlementFixture(t, true)
	materializeCancellationV15Fixture(t, control)
	before := cancellationMigrationRows(t, control)
	if _, err := control.db.Exec(`CREATE TEMP TRIGGER migration_v16_fault BEFORE INSERT ON schema_migrations WHEN NEW.version=16 BEGIN SELECT RAISE(ABORT,'migration-v16-fixture-fault'); END`); err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := control.migrateToV16Tx(tx); err == nil || !strings.Contains(err.Error(), "migration-v16-fixture-fault") {
		t.Fatal("late fault was not preserved", err)
	}
	_ = tx.Rollback()
	if control.SchemaVersion() != SchemaV15 || cancellationMigrationRows(t, control) != before {
		t.Fatal("failed migration partially changed source")
	}
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, SchemaV15); err != nil {
		t.Fatal("legacy schema changed after rollback", err)
	}
}
